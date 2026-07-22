import hashlib
import os
import sqlite3
import xml.etree.ElementTree as ET
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlparse

import requests
from flask import Flask, flash, g, jsonify, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "boardgames.db"
SCHEMA_PATH = BASE_DIR / "schema.sql"

TURSO_URL = os.environ.get("TURSO_DATABASE_URL")
TURSO_AUTH_TOKEN = os.environ.get("TURSO_AUTH_TOKEN")

# BoardGameGeek's classic XML API now rejects anonymous requests - this token
# was issued at https://boardgamegeek.com/application/7251/tokens and goes in
# an Authorization: Bearer header (confirmed by hand; BGG doesn't publicly
# document the new auth requirement anywhere else).
BGG_TOKEN = os.environ.get("BGG_ZGRANI")
BGG_SEARCH_URL = "https://boardgamegeek.com/xmlapi2/search"
BGG_THING_URL = "https://boardgamegeek.com/xmlapi2/thing"

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me")
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024  # 64KB - plenty for these small text forms

NAME_MAX_LENGTH = 50
GAME_NAME_MAX_LENGTH = 100
NOTES_MAX_LENGTH = 300
COMMENT_MAX_LENGTH = 255
SESSION_TIME_MAX_LENGTH = 32
LANGUAGE_MAX_LENGTH = 50

LANGUAGE_FLAGS = {"Angielski": "🇬🇧", "Polski": "🇵🇱", "Niemiecki": "🇩🇪"}

TRIP_START = date(2026, 7, 25)  # Saturday
TRIP_END = date(2026, 8, 2)  # Sunday
TRIP_DAYS = [TRIP_START + timedelta(days=i) for i in range((TRIP_END - TRIP_START).days + 1)]
POLISH_WEEKDAYS = ["Poniedziałek", "Wtorek", "Środa", "Czwartek", "Piątek", "Sobota", "Niedziela"]


def day_label(d):
    return f"{POLISH_WEEKDAYS[d.weekday()]} {d.day:02d}.{d.month:02d}"


def group_sessions_by_day(sessions):
    """Bucket sessions into the trip's day range; anything outside it lands in a trailing 'other' bucket."""
    days = [{"date": d, "label": day_label(d), "sessions": []} for d in TRIP_DAYS]
    by_date = {d["date"]: d for d in days}
    other = {"date": None, "label": "Poza terminem wyjazdu", "sessions": []}
    for s in sessions:
        try:
            session_date = date.fromisoformat(s["session_time"][:10])
        except ValueError:
            session_date = None
        by_date.get(session_date, other)["sessions"].append(s)
    if other["sessions"]:
        days.append(other)
    return days

NAME_COLORS = [
    "#c1552c",  # orange
    "#2f6f4e",  # green
    "#2f5d8c",  # blue
    "#8c3f8c",  # purple
    "#8c630f",  # gold
    "#3f7a7a",  # teal
    "#a13d5c",  # rose
    "#4f5f80",  # slate
    "#6b7a2f",  # olive
    "#7a4a2f",  # brown
    "#267a5f",  # jade
    "#8c2f4a",  # crimson
    "#324a8c",  # navy
    "#6b3f8c",  # violet
    "#8c6318",  # amber
    "#2f7a8c",  # cyan
]


def clamp(text, max_length):
    return text.strip()[:max_length]


def bgg_get(url, params):
    headers = {"Authorization": f"Bearer {BGG_TOKEN}"} if BGG_TOKEN else {}
    response = requests.get(url, params=params, headers=headers, timeout=5)
    response.raise_for_status()
    return ET.fromstring(response.content)


def bgg_search(query):
    """Board games on BGG matching `query`, exact-name matches first."""
    root = bgg_get(BGG_SEARCH_URL, {"query": query, "type": "boardgame"})
    results = []
    for item in root.findall("item"):
        name_el = item.find("name")
        if name_el is None or not name_el.get("value"):
            continue
        year_el = item.find("yearpublished")
        results.append({
            "id": item.get("id"),
            "name": name_el.get("value"),
            "year": year_el.get("value") if year_el is not None else None,
        })
    query_lower = query.strip().lower()
    results.sort(key=lambda r: r["name"].lower() != query_lower)
    return results[:8]


def bgg_thing(bgg_id):
    """Box art + canonical name for one BGG game id, or None if it doesn't exist.
    Also lists its expansions - BGG links an expansion back to its base game
    with a "boardgameexpansion" link marked inbound="true" on the expansion's
    own thing entry, and the base game's own entry links out to each of its
    expansions the same way but without that marker."""
    root = bgg_get(BGG_THING_URL, {"id": bgg_id})
    item = root.find("item")
    if item is None:
        return None
    image_el = item.find("image")
    primary_name = next(
        (n.get("value") for n in item.findall("name") if n.get("type") == "primary"), None
    )
    expansions = [
        {"id": link.get("id"), "name": link.get("value")}
        for link in item.findall("link")
        if link.get("type") == "boardgameexpansion" and link.get("inbound") != "true"
    ]
    return {
        "id": str(bgg_id),
        "name": primary_name,
        "image": image_el.text if image_el is not None else None,
        "expansions": expansions,
    }


class _TursoCursor:
    """Adapts a libsql_client ResultSet to the sqlite3 cursor shape (.description/.fetchall)."""

    def __init__(self, result_set):
        self._rows = [tuple(row) for row in result_set.rows]
        self.description = [(c,) for c in result_set.columns] if result_set.columns else None
        self.lastrowid = result_set.last_insert_rowid

    def fetchall(self):
        return self._rows


class TursoConn:
    """Adapts a libsql_client sync Client to the sqlite3 connection shape used below."""

    def __init__(self, client):
        self._client = client

    def execute(self, sql, params=()):
        return _TursoCursor(self._client.execute(sql, list(params)))

    def commit(self):
        pass  # each statement is already committed over HTTP

    def close(self):
        self._client.close()


def connect_db():
    if TURSO_URL:
        import libsql_client
        # libsql_client defaults libsql:// URLs to a WebSocket transport, which isn't
        # reliable on every network path. Force plain HTTPS, which works everywhere.
        url = TURSO_URL.replace("libsql://", "https://", 1)
        client = libsql_client.create_client_sync(url=url, auth_token=TURSO_AUTH_TOKEN)
        return TursoConn(client)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def get_db():
    if "db" not in g:
        g.db = connect_db()
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def query_all(db, sql, params=()):
    cur = db.execute(sql, params)
    cols = [d[0] for d in cur.description] if cur.description else []
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def query_one(db, sql, params=()):
    rows = query_all(db, sql, params)
    return rows[0] if rows else None


def log_action(db, action, details=""):
    """Record a change for the admin-only activity log. Callers pass an
    already-rendered detail string (game name, etc.) - queried right before
    the row is deleted, since it won't exist to look up afterwards."""
    db.execute(
        "INSERT INTO activity_log (user_name, action, details) VALUES (?, ?, ?)",
        (current_name(), action, details),
    )


def record_player(db, name):
    """Register a name's first appearance so it gets a stable color assignment."""
    db.execute("INSERT OR IGNORE INTO players (name) VALUES (?)", (name,))
    db.commit()


def get_player_color_map(db):
    if "player_colors" not in g:
        rows = query_all(db, "SELECT name, color FROM players ORDER BY id")
        g.player_colors = {
            row["name"]: row["color"] or NAME_COLORS[i % len(NAME_COLORS)]
            for i, row in enumerate(rows)
        }
    return g.player_colors


@app.template_filter("name_color")
def name_color(name):
    """Colors are assigned by order of first appearance, so a small group gets
    all-distinct colors instead of risking hash collisions."""
    colors = get_player_color_map(get_db())
    if name in colors:
        return colors[name]
    # Not registered yet (shouldn't normally happen) - fall back to a hash so it
    # still gets *some* color for this render.
    digest = hashlib.md5(name.strip().lower().encode()).hexdigest()
    return NAME_COLORS[int(digest, 16) % len(NAME_COLORS)]


@app.template_filter("language_flag")
def language_flag(value):
    return LANGUAGE_FLAGS.get(value, "")


def track_unread_enabled(player):
    return not player or player["track_unread"] != 0


def get_unread_game_ids(db, user_name):
    """Game ids with a comment posted after the user's last visit to that thread."""
    latest = {
        row["game_id"]: row["latest"]
        for row in query_all(
            db, "SELECT game_id, MAX(created_at) AS latest FROM comments GROUP BY game_id"
        )
    }
    seen = {
        row["game_id"]: row["last_seen_at"]
        for row in query_all(
            db, "SELECT game_id, last_seen_at FROM comment_reads WHERE user_name = ?", (user_name,)
        )
    }
    return {
        game_id
        for game_id, latest_at in latest.items()
        if game_id not in seen or latest_at > seen[game_id]
    }


def get_unread_wish_ids(db, user_name):
    """Wish ids with a comment posted after the user's last visit to that thread."""
    latest = {
        row["wish_id"]: row["latest"]
        for row in query_all(
            db, "SELECT wish_id, MAX(created_at) AS latest FROM wish_comments GROUP BY wish_id"
        )
    }
    seen = {
        row["wish_id"]: row["last_seen_at"]
        for row in query_all(
            db, "SELECT wish_id, last_seen_at FROM wish_comment_reads WHERE user_name = ?", (user_name,)
        )
    }
    return {
        wish_id
        for wish_id, latest_at in latest.items()
        if wish_id not in seen or latest_at > seen[wish_id]
    }


POLISH_ALPHABET = "aąbcćdeęfghijklłmnńoópqrsśtuvwxyzźż"
POLISH_ORDER = {ch: i for i, ch in enumerate(POLISH_ALPHABET)}


def polish_sort_key(text):
    """Sort key following Polish alphabetical order (e.g. l < ł < m)."""
    return [POLISH_ORDER.get(ch, len(POLISH_ALPHABET) + ord(ch)) for ch in text.strip().lower()]


def sort_by_name(rows):
    return sorted(rows, key=lambda row: polish_sort_key(row["name"]))


def group_wishes(rows):
    """Merge wish rows that share a name into one card with a list of requesters."""
    groups = {}
    order = []
    for row in rows:
        key = row["name"].strip().lower()
        if key not in groups:
            groups[key] = {**row, "requesters": []}
            order.append(key)
        groups[key]["requesters"].append(row["requester_name"])
    return [groups[key] for key in order]


def init_db():
    db = connect_db()
    for statement in SCHEMA_PATH.read_text().split(";"):
        statement = statement.strip()
        if statement:
            db.execute(statement)
    db.commit()
    # Additive migrations for databases created before a column existed.
    # Never drop/recreate tables here - that would destroy real trip data.
    for table, column, coltype in [
        ("games", "image_url", "TEXT"),
        ("wishes", "image_url", "TEXT"),
        ("games", "origin_wish_requester", "TEXT"),
        ("players", "color", "TEXT"),
        ("players", "pin_code", "TEXT"),
        ("players", "track_unread", "INTEGER DEFAULT 1"),
        ("players", "is_admin", "INTEGER DEFAULT 0"),
        ("games", "language", "TEXT"),
    ]:
        try:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")
        except Exception:
            pass  # column already exists
    db.commit()
    # Admin accounts are granted here rather than through the UI - the only
    # one right now is Radek. Upsert so this works whether he's logged in
    # before or not, without touching admin flags set by hand for anyone else.
    db.execute(
        "INSERT INTO players (name, is_admin) VALUES (?, 1) "
        "ON CONFLICT(name) DO UPDATE SET is_admin = 1",
        ("Radek",),
    )
    db.commit()
    # Backfill players from names already present in existing trip data, ordered
    # by earliest activity, so they keep stable, distinct colors.
    existing = {row["name"] for row in query_all(db, "SELECT name FROM players")}
    rows = query_all(
        db,
        "SELECT owner_name AS name, created_at FROM games "
        "UNION ALL "
        "SELECT requester_name AS name, created_at FROM wishes "
        "ORDER BY created_at",
    )
    for row in rows:
        if row["name"] not in existing:
            db.execute("INSERT OR IGNORE INTO players (name) VALUES (?)", (row["name"],))
            existing.add(row["name"])
    db.commit()
    db.close()


def current_name():
    return session.get("name")


def is_admin_name(db, name):
    if not name:
        return False
    row = query_one(db, "SELECT is_admin FROM players WHERE name = ?", (name,))
    return bool(row and row["is_admin"])


def current_is_admin(db):
    return is_admin_name(db, current_name())


@app.context_processor
def inject_is_admin():
    return {"is_admin": current_is_admin(get_db())}


def safe_redirect_back(fallback_endpoint):
    """Return to whatever page the delete was triggered from (games.html vs.
    account.html both use these routes), but never redirect off-site."""
    ref = request.referrer
    if ref and urlparse(ref).netloc == request.host:
        return redirect(ref)
    return redirect(url_for(fallback_endpoint))


@app.before_request
def require_login():
    if request.endpoint in ("login", "static"):
        return None
    if current_name() is None:
        return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        name = clamp(request.form.get("name", ""), NAME_MAX_LENGTH)
        code = request.form.get("code", "").strip()
        if not name:
            return render_template("login.html", error="Wpisz imię.")

        db = get_db()
        # Python's .lower() is used instead of SQL COLLATE NOCASE because SQLite's
        # builtin NOCASE only folds ASCII a-z, not Polish diacritics (Ł/ł, Ż/ż, ...).
        player = next(
            (p for p in query_all(db, "SELECT * FROM players") if p["name"].lower() == name.lower()),
            None,
        )
        if player:
            name = player["name"]  # reuse the spelling recorded at first login
        if player and player["pin_code"]:
            if not code:
                return render_template("login.html", name=name, need_code=True)
            if not check_password_hash(player["pin_code"], code):
                return render_template(
                    "login.html", name=name, need_code=True, error="Nieprawidłowy kod."
                )

        record_player(db, name)
        session["name"] = name
        return redirect(url_for("games_view"))
    return render_template("login.html")


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
def root():
    return redirect(url_for("games_view"))


@app.route("/gry")
def games_view():
    db = get_db()
    name = current_name()

    # Duplicates are allowed here - two owners each bringing "Catan" get two
    # separate cards; games and wishlist entries are otherwise unrelated.
    games = sort_by_name(query_all(db, "SELECT * FROM games"))
    for row in games:
        row["owners"] = [row["owner_name"]]

    interest_names = {}  # game_id -> list of names
    my_interests = set()
    for row in query_all(db, "SELECT user_name, game_id FROM interests"):
        interest_names.setdefault(row["game_id"], []).append(row["user_name"])
        if row["user_name"] == name:
            my_interests.add(row["game_id"])

    comments_by_game = {}  # game_id -> list of comment rows, chronological
    for row in query_all(db, "SELECT * FROM comments ORDER BY created_at"):
        comments_by_game.setdefault(row["game_id"], []).append(row)

    expansions_by_game = {}  # game_id -> list of expansion rows, in add order
    for row in query_all(db, "SELECT * FROM game_expansions ORDER BY id"):
        expansions_by_game.setdefault(row["game_id"], []).append(row)

    player = query_one(db, "SELECT * FROM players WHERE name = ?", (name,))
    unread_game_ids = (
        get_unread_game_ids(db, name) if track_unread_enabled(player) else set()
    )

    return render_template(
        "games.html",
        user=name,
        games=games,
        interest_names=interest_names,
        my_interests=my_interests,
        comments_by_game=comments_by_game,
        expansions_by_game=expansions_by_game,
        unread_game_ids=unread_game_ids,
    )


@app.route("/lista-zyczen")
def wishlist_view():
    db = get_db()
    name = current_name()

    wishes = group_wishes(sort_by_name(query_all(db, "SELECT * FROM wishes")))

    # group_wishes merges same-named rows into one card and only keeps the
    # first row's id/notes - editing needs the current user's own row within
    # that group, keyed the same way group_wishes keys its groups.
    my_wish_rows = {
        row["name"].strip().lower(): row
        for row in query_all(db, "SELECT * FROM wishes WHERE requester_name = ?", (name,))
    }

    comments_by_wish = {}  # wish_id -> list of comment rows, chronological
    for row in query_all(db, "SELECT * FROM wish_comments ORDER BY created_at"):
        comments_by_wish.setdefault(row["wish_id"], []).append(row)

    # Games and wishes are otherwise unrelated (no auto-merge), but a wish
    # card can still flag that someone's already bringing the same game.
    taken_game_names = {row["name"].strip().lower() for row in query_all(db, "SELECT name FROM games")}

    player = query_one(db, "SELECT * FROM players WHERE name = ?", (name,))
    unread_wish_ids = (
        get_unread_wish_ids(db, name) if track_unread_enabled(player) else set()
    )

    return render_template(
        "wishlist.html",
        user=name,
        wishes=wishes,
        my_wish_rows=my_wish_rows,
        comments_by_wish=comments_by_wish,
        taken_game_names=taken_game_names,
        unread_wish_ids=unread_wish_ids,
    )


@app.route("/konto")
def account_view():
    name = current_name()
    db = get_db()

    my_games = sort_by_name(
        query_all(db, "SELECT * FROM games WHERE owner_name = ?", (name,))
    )
    my_wishes = sort_by_name(
        query_all(db, "SELECT * FROM wishes WHERE requester_name = ?", (name,))
    )
    player = query_one(db, "SELECT * FROM players WHERE name = ?", (name,))

    return render_template(
        "account.html",
        user=name,
        my_games=my_games,
        my_wishes=my_wishes,
        player=player,
        current_color=name_color(name),
        name_colors=NAME_COLORS,
        track_unread=track_unread_enabled(player),
    )


@app.route("/konto/color", methods=["POST"])
def set_color():
    color = request.form.get("color", "")
    if color in NAME_COLORS:
        db = get_db()
        db.execute(
            "UPDATE players SET color = ? WHERE name = ?", (color, current_name())
        )
        log_action(db, "set_color", f"Zmiana koloru na {color}")
        db.commit()
    return redirect(url_for("account_view"))


@app.route("/konto/lock", methods=["POST"])
def set_lock():
    code = request.form.get("code", "").strip()
    if not (code.isdigit() and len(code) == 4):
        flash("Kod musi mieć dokładnie 4 cyfry.")
    else:
        db = get_db()
        db.execute(
            "UPDATE players SET pin_code = ? WHERE name = ?",
            (generate_password_hash(code), current_name()),
        )
        log_action(db, "set_lock", "Zablokowano konto kodem")
        db.commit()
    return redirect(url_for("account_view"))


@app.route("/konto/unlock", methods=["POST"])
def unlock_account():
    db = get_db()
    db.execute("UPDATE players SET pin_code = NULL WHERE name = ?", (current_name(),))
    log_action(db, "unlock_account", "Zdjęto blokadę konta")
    db.commit()
    return redirect(url_for("account_view"))


@app.route("/konto/unread-toggle", methods=["POST"])
def toggle_unread_tracking():
    db = get_db()
    player = query_one(db, "SELECT * FROM players WHERE name = ?", (current_name(),))
    new_value = 0 if track_unread_enabled(player) else 1
    db.execute(
        "UPDATE players SET track_unread = ? WHERE name = ?", (new_value, current_name())
    )
    log_action(
        db, "toggle_unread_tracking",
        "Włączono oznaczanie nieprzeczytanych" if new_value else "Wyłączono oznaczanie nieprzeczytanych",
    )
    db.commit()
    return redirect(url_for("account_view"))


@app.route("/bgg/search")
def bgg_search_route():
    query = request.args.get("q", "").strip()
    if not query or not BGG_TOKEN:
        return jsonify([])
    try:
        return jsonify(bgg_search(query))
    except (requests.RequestException, ET.ParseError):
        return jsonify([])


@app.route("/bgg/thing/<int:bgg_id>")
def bgg_thing_route(bgg_id):
    if not BGG_TOKEN:
        return jsonify(None)
    try:
        return jsonify(bgg_thing(bgg_id))
    except (requests.RequestException, ET.ParseError):
        return jsonify(None)


def save_game_expansions(db, game_id, replace=False):
    """Persists the repeatable "Dodatek" rows for a game. Rows are paired by
    index between the two same-length lists the browser submits, one entry
    per expansion row in the form."""
    names = request.form.getlist("expansion_name")
    bgg_ids = request.form.getlist("expansion_bgg_id")
    if replace:
        db.execute("DELETE FROM game_expansions WHERE game_id = ?", (game_id,))
    for i, raw_name in enumerate(names):
        name = clamp(raw_name, GAME_NAME_MAX_LENGTH)
        if not name:
            continue
        expansion_bgg_id = bgg_ids[i].strip() if i < len(bgg_ids) and bgg_ids[i].strip() else None
        db.execute(
            "INSERT INTO game_expansions (game_id, name, bgg_id) VALUES (?, ?, ?)",
            (game_id, name, expansion_bgg_id),
        )


@app.route("/games/add", methods=["POST"])
def add_game():
    name = clamp(request.form.get("name", ""), GAME_NAME_MAX_LENGTH)
    notes = clamp(request.form.get("notes", ""), NOTES_MAX_LENGTH)
    language = clamp(request.form.get("language", ""), LANGUAGE_MAX_LENGTH) or None
    image_url = request.form.get("image_url", "").strip() or None
    bgg_id = request.form.get("bgg_id", "").strip() or None
    if name:
        db = get_db()
        cur = db.execute(
            "INSERT INTO games (name, notes, owner_name, image_url, bgg_id, language) VALUES (?, ?, ?, ?, ?, ?)",
            (name, notes, current_name(), image_url, bgg_id, language),
        )
        save_game_expansions(db, cur.lastrowid)
        log_action(db, "add_game", f"Dodano grę: {name}")
        db.commit()
    return redirect(url_for("games_view"))


@app.route("/games/<int:game_id>/edit", methods=["POST"])
def edit_game(game_id):
    db = get_db()
    game = query_one(
        db, "SELECT * FROM games WHERE id = ? AND owner_name = ?", (game_id, current_name())
    )
    if game:
        name = clamp(request.form.get("name", ""), GAME_NAME_MAX_LENGTH)
        notes = clamp(request.form.get("notes", ""), NOTES_MAX_LENGTH)
        language = clamp(request.form.get("language", ""), LANGUAGE_MAX_LENGTH) or None
        image_url = request.form.get("image_url", "").strip() or None
        bgg_id = request.form.get("bgg_id", "").strip() or None
        if name:
            db.execute(
                "UPDATE games SET name = ?, notes = ?, image_url = ?, bgg_id = ?, language = ? WHERE id = ?",
                (name, notes, image_url, bgg_id, language, game_id),
            )
            save_game_expansions(db, game_id, replace=True)
            log_action(db, "edit_game", f"Edytowano grę: {game['name']} -> {name}")
            db.commit()
    return redirect(url_for("games_view"))


@app.route("/games/<int:game_id>/delete", methods=["POST"])
def delete_game(game_id):
    db = get_db()
    if current_is_admin(db):
        game = query_one(db, "SELECT * FROM games WHERE id = ?", (game_id,))
    else:
        game = query_one(
            db, "SELECT * FROM games WHERE id = ? AND owner_name = ?", (game_id, current_name())
        )
    if game:
        db.execute("DELETE FROM games WHERE id = ?", (game_id,))
        log_action(db, "delete_game", f"Usunięto grę: {game['name']} (właściciel: {game['owner_name']})")
        db.commit()
    return safe_redirect_back("account_view")


@app.route("/games/<int:game_id>/interest", methods=["POST"])
def toggle_interest(game_id):
    db = get_db()
    name = current_name()
    game = query_one(db, "SELECT name FROM games WHERE id = ?", (game_id,))
    existing = query_one(
        db, "SELECT 1 FROM interests WHERE user_name = ? AND game_id = ?", (name, game_id)
    )
    if existing:
        db.execute(
            "DELETE FROM interests WHERE user_name = ? AND game_id = ?",
            (name, game_id),
        )
        log_action(db, "toggle_interest", f"Zrezygnowano z zainteresowania grą: {game['name'] if game else game_id}")
    else:
        db.execute(
            "INSERT INTO interests (user_name, game_id) VALUES (?, ?)",
            (name, game_id),
        )
        log_action(db, "toggle_interest", f"Zainteresowanie grą: {game['name'] if game else game_id}")
    db.commit()
    return redirect(url_for("games_view"))


@app.route("/games/<int:game_id>/comments/add", methods=["POST"])
def add_comment(game_id):
    text = clamp(request.form.get("text", ""), COMMENT_MAX_LENGTH)
    if text:
        db = get_db()
        db.execute(
            "INSERT INTO comments (game_id, author_name, text) VALUES (?, ?, ?)",
            (game_id, current_name(), text),
        )
        game = query_one(db, "SELECT name FROM games WHERE id = ?", (game_id,))
        log_action(db, "add_comment", f"Komentarz do gry {game['name'] if game else game_id}: {text}")
        db.commit()
    return redirect(url_for("games_view"))


@app.route("/games/comments/<int:comment_id>/delete", methods=["POST"])
def delete_comment(comment_id):
    db = get_db()
    admin = current_is_admin(db)
    comment = query_one(db, "SELECT * FROM comments WHERE id = ?", (comment_id,))
    if admin:
        db.execute("DELETE FROM comments WHERE id = ?", (comment_id,))
    else:
        db.execute(
            "DELETE FROM comments WHERE id = ? AND author_name = ?",
            (comment_id, current_name()),
        )
    if comment and (admin or comment["author_name"] == current_name()):
        log_action(db, "delete_comment", f"Usunięto komentarz autora {comment['author_name']}: {comment['text']}")
    db.commit()
    return redirect(url_for("games_view"))


@app.route("/games/<int:game_id>/comments/seen", methods=["POST"])
def mark_comments_seen(game_id):
    db = get_db()
    db.execute(
        "INSERT INTO comment_reads (user_name, game_id, last_seen_at) "
        "VALUES (?, ?, datetime('now')) "
        "ON CONFLICT(user_name, game_id) DO UPDATE SET last_seen_at = excluded.last_seen_at",
        (current_name(), game_id),
    )
    db.commit()
    return ("", 204)


@app.route("/wishes/<int:wish_id>/comments/seen", methods=["POST"])
def mark_wish_comments_seen(wish_id):
    db = get_db()
    db.execute(
        "INSERT INTO wish_comment_reads (user_name, wish_id, last_seen_at) "
        "VALUES (?, ?, datetime('now')) "
        "ON CONFLICT(user_name, wish_id) DO UPDATE SET last_seen_at = excluded.last_seen_at",
        (current_name(), wish_id),
    )
    db.commit()
    return ("", 204)


@app.route("/wishes/<int:wish_id>/comments/add", methods=["POST"])
def add_wish_comment(wish_id):
    text = clamp(request.form.get("text", ""), COMMENT_MAX_LENGTH)
    if text:
        db = get_db()
        db.execute(
            "INSERT INTO wish_comments (wish_id, author_name, text) VALUES (?, ?, ?)",
            (wish_id, current_name(), text),
        )
        wish = query_one(db, "SELECT name FROM wishes WHERE id = ?", (wish_id,))
        log_action(db, "add_wish_comment", f"Komentarz do życzenia {wish['name'] if wish else wish_id}: {text}")
        db.commit()
    return redirect(url_for("wishlist_view"))


@app.route("/wishes/comments/<int:comment_id>/delete", methods=["POST"])
def delete_wish_comment(comment_id):
    db = get_db()
    admin = current_is_admin(db)
    comment = query_one(db, "SELECT * FROM wish_comments WHERE id = ?", (comment_id,))
    if admin:
        db.execute("DELETE FROM wish_comments WHERE id = ?", (comment_id,))
    else:
        db.execute(
            "DELETE FROM wish_comments WHERE id = ? AND author_name = ?",
            (comment_id, current_name()),
        )
    if comment and (admin or comment["author_name"] == current_name()):
        log_action(db, "delete_wish_comment", f"Usunięto komentarz autora {comment['author_name']}: {comment['text']}")
    db.commit()
    return redirect(url_for("wishlist_view"))


@app.route("/wishes/add", methods=["POST"])
def add_wish():
    name = clamp(request.form.get("name", ""), GAME_NAME_MAX_LENGTH)
    notes = clamp(request.form.get("notes", ""), NOTES_MAX_LENGTH)
    image_url = request.form.get("image_url", "").strip() or None
    bgg_id = request.form.get("bgg_id", "").strip() or None
    if name:
        db = get_db()
        db.execute(
            "INSERT INTO wishes (name, notes, requester_name, image_url, bgg_id) VALUES (?, ?, ?, ?, ?)",
            (name, notes, current_name(), image_url, bgg_id),
        )
        log_action(db, "add_wish", f"Dodano do listy życzeń: {name}")
        db.commit()
    return redirect(url_for("wishlist_view"))


@app.route("/wishes/<int:wish_id>/edit", methods=["POST"])
def edit_wish(wish_id):
    db = get_db()
    wish = query_one(
        db, "SELECT * FROM wishes WHERE id = ? AND requester_name = ?", (wish_id, current_name())
    )
    if wish:
        name = clamp(request.form.get("name", ""), GAME_NAME_MAX_LENGTH)
        notes = clamp(request.form.get("notes", ""), NOTES_MAX_LENGTH)
        image_url = request.form.get("image_url", "").strip() or None
        bgg_id = request.form.get("bgg_id", "").strip() or None
        if name:
            db.execute(
                "UPDATE wishes SET name = ?, notes = ?, image_url = ?, bgg_id = ? WHERE id = ?",
                (name, notes, image_url, bgg_id, wish_id),
            )
            log_action(db, "edit_wish", f"Edytowano życzenie: {wish['name']} -> {name}")
            db.commit()
    return redirect(url_for("wishlist_view"))


@app.route("/wishes/<int:wish_id>/delete", methods=["POST"])
def delete_wish(wish_id):
    db = get_db()
    if current_is_admin(db):
        # A wish "card" is really one row per requester sharing a name -
        # admin delete removes the whole card.
        wish = query_one(db, "SELECT * FROM wishes WHERE id = ?", (wish_id,))
        if wish:
            db.execute("DELETE FROM wishes WHERE name = ? COLLATE NOCASE", (wish["name"],))
            log_action(db, "delete_wish", f"Usunięto życzenie: {wish['name']} (zgłaszający: {wish['requester_name']})")
    else:
        wish = query_one(
            db, "SELECT * FROM wishes WHERE id = ? AND requester_name = ?", (wish_id, current_name())
        )
        db.execute(
            "DELETE FROM wishes WHERE id = ? AND requester_name = ?",
            (wish_id, current_name()),
        )
        if wish:
            log_action(db, "delete_wish", f"Usunięto życzenie: {wish['name']}")
    db.commit()
    return safe_redirect_back("account_view")


@app.route("/wishes/<int:wish_id>/join", methods=["POST"])
def join_wish(wish_id):
    db = get_db()
    name = current_name()
    wish = query_one(db, "SELECT * FROM wishes WHERE id = ?", (wish_id,))
    if wish:
        already = query_one(
            db,
            "SELECT 1 FROM wishes WHERE name = ? COLLATE NOCASE AND requester_name = ?",
            (wish["name"], name),
        )
        if not already:
            db.execute(
                "INSERT INTO wishes (name, notes, requester_name, image_url, bgg_id) VALUES (?, ?, ?, ?, ?)",
                (wish["name"], wish["notes"], name, wish["image_url"], wish["bgg_id"]),
            )
            log_action(db, "join_wish", f"Dołączono do życzenia: {wish['name']}")
            db.commit()
    return redirect(url_for("wishlist_view"))


@app.route("/rozgrywki")
def sessions_view():
    db = get_db()
    name = current_name()

    sessions = query_all(db, "SELECT * FROM sessions ORDER BY session_time")

    joins_by_session = {}
    my_joins = set()
    for row in query_all(db, "SELECT user_name, session_id FROM session_joins"):
        joins_by_session.setdefault(row["session_id"], []).append(row["user_name"])
        if row["user_name"] == name:
            my_joins.add(row["session_id"])

    comments_by_session = {}
    for row in query_all(db, "SELECT * FROM session_comments ORDER BY created_at"):
        comments_by_session.setdefault(row["session_id"], []).append(row)

    known_games = sorted(
        {row["name"] for row in query_all(db, "SELECT DISTINCT name FROM games")},
        key=polish_sort_key,
    )

    return render_template(
        "sessions.html",
        user=name,
        days=group_sessions_by_day(sessions),
        joins_by_session=joins_by_session,
        my_joins=my_joins,
        comments_by_session=comments_by_session,
        known_games=known_games,
    )


@app.route("/rozgrywki/add", methods=["POST"])
def add_session():
    game_name = clamp(request.form.get("game_name", ""), GAME_NAME_MAX_LENGTH)
    date_str = request.form.get("date", "").strip()
    time_str = request.form.get("time", "").strip()
    notes = clamp(request.form.get("notes", ""), NOTES_MAX_LENGTH)
    session_time = clamp(f"{date_str}T{time_str}", SESSION_TIME_MAX_LENGTH)
    if game_name and date_str and time_str:
        db = get_db()
        db.execute(
            "INSERT INTO sessions (game_name, session_time, notes, organizer_name) VALUES (?, ?, ?, ?)",
            (game_name, session_time, notes, current_name()),
        )
        log_action(db, "add_session", f"Zaproponowano rozgrywkę: {game_name} @ {session_time}")
        db.commit()
    return redirect(url_for("sessions_view"))


@app.route("/rozgrywki/<int:session_id>/join", methods=["POST"])
def toggle_session_join(session_id):
    db = get_db()
    name = current_name()
    session_row = query_one(db, "SELECT game_name FROM sessions WHERE id = ?", (session_id,))
    label = session_row["game_name"] if session_row else session_id
    existing = query_one(
        db,
        "SELECT 1 FROM session_joins WHERE user_name = ? AND session_id = ?",
        (name, session_id),
    )
    if existing:
        db.execute(
            "DELETE FROM session_joins WHERE user_name = ? AND session_id = ?",
            (name, session_id),
        )
        log_action(db, "toggle_session_join", f"Zrezygnowano z rozgrywki: {label}")
    else:
        db.execute(
            "INSERT INTO session_joins (user_name, session_id) VALUES (?, ?)",
            (name, session_id),
        )
        log_action(db, "toggle_session_join", f"Dołączono do rozgrywki: {label}")
    db.commit()
    return redirect(url_for("sessions_view"))


@app.route("/rozgrywki/<int:session_id>/delete", methods=["POST"])
def delete_session(session_id):
    db = get_db()
    if current_is_admin(db):
        session_row = query_one(db, "SELECT * FROM sessions WHERE id = ?", (session_id,))
        db.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
    else:
        session_row = query_one(
            db, "SELECT * FROM sessions WHERE id = ? AND organizer_name = ?", (session_id, current_name())
        )
        db.execute(
            "DELETE FROM sessions WHERE id = ? AND organizer_name = ?",
            (session_id, current_name()),
        )
    if session_row:
        log_action(db, "delete_session", f"Odwołano rozgrywkę: {session_row['game_name']} (organizator: {session_row['organizer_name']})")
    db.commit()
    return redirect(url_for("sessions_view"))


@app.route("/rozgrywki/<int:session_id>/comments/add", methods=["POST"])
def add_session_comment(session_id):
    text = clamp(request.form.get("text", ""), COMMENT_MAX_LENGTH)
    if text:
        db = get_db()
        db.execute(
            "INSERT INTO session_comments (session_id, author_name, text) VALUES (?, ?, ?)",
            (session_id, current_name(), text),
        )
        session_row = query_one(db, "SELECT game_name FROM sessions WHERE id = ?", (session_id,))
        label = session_row["game_name"] if session_row else session_id
        log_action(db, "add_session_comment", f"Komentarz do rozgrywki {label}: {text}")
        db.commit()
    return redirect(url_for("sessions_view"))


@app.route("/rozgrywki/comments/<int:comment_id>/delete", methods=["POST"])
def delete_session_comment(comment_id):
    db = get_db()
    admin = current_is_admin(db)
    comment = query_one(db, "SELECT * FROM session_comments WHERE id = ?", (comment_id,))
    if admin:
        db.execute("DELETE FROM session_comments WHERE id = ?", (comment_id,))
    else:
        db.execute(
            "DELETE FROM session_comments WHERE id = ? AND author_name = ?",
            (comment_id, current_name()),
        )
    if comment and (admin or comment["author_name"] == current_name()):
        log_action(db, "delete_session_comment", f"Usunięto komentarz autora {comment['author_name']}: {comment['text']}")
    db.commit()
    return redirect(url_for("sessions_view"))


@app.route("/szafa")
def szafa_view():
    db = get_db()
    name = current_name()

    all_games = query_all(db, "SELECT * FROM collection_games")

    requests_by_game = {}
    my_requests = set()
    for row in query_all(db, "SELECT user_name, collection_game_id FROM collection_requests"):
        requests_by_game.setdefault(row["collection_game_id"], []).append(row["user_name"])
        if row["user_name"] == name:
            my_requests.add(row["collection_game_id"])

    # Your own games: most-wanted first, then alphabetically.
    my_collection = [g for g in all_games if g["owner_name"] == name]
    my_collection.sort(
        key=lambda g: (-len(requests_by_game.get(g["id"], [])), polish_sort_key(g["name"]))
    )

    others_collection = sort_by_name([g for g in all_games if g["owner_name"] != name])

    comments_by_collection = {}
    for row in query_all(db, "SELECT * FROM collection_comments ORDER BY created_at"):
        comments_by_collection.setdefault(row["collection_game_id"], []).append(row)

    return render_template(
        "szafa.html",
        user=name,
        my_collection=my_collection,
        others_collection=others_collection,
        requests_by_game=requests_by_game,
        my_requests=my_requests,
        comments_by_collection=comments_by_collection,
    )


@app.route("/szafa/add", methods=["POST"])
def add_collection_game():
    name = clamp(request.form.get("name", ""), GAME_NAME_MAX_LENGTH)
    notes = clamp(request.form.get("notes", ""), NOTES_MAX_LENGTH)
    if name:
        db = get_db()
        db.execute(
            "INSERT INTO collection_games (name, notes, owner_name) VALUES (?, ?, ?)",
            (name, notes, current_name()),
        )
        log_action(db, "add_collection_game", f"Dodano do szafy: {name}")
        db.commit()
    return redirect(url_for("szafa_view"))


@app.route("/szafa/<int:collection_game_id>/request", methods=["POST"])
def toggle_collection_request(collection_game_id):
    db = get_db()
    name = current_name()
    game_row = query_one(db, "SELECT name FROM collection_games WHERE id = ?", (collection_game_id,))
    label = game_row["name"] if game_row else collection_game_id
    existing = query_one(
        db,
        "SELECT 1 FROM collection_requests WHERE user_name = ? AND collection_game_id = ?",
        (name, collection_game_id),
    )
    if existing:
        db.execute(
            "DELETE FROM collection_requests WHERE user_name = ? AND collection_game_id = ?",
            (name, collection_game_id),
        )
        log_action(db, "toggle_collection_request", f"Zrezygnowano z gry z szafy: {label}")
    else:
        db.execute(
            "INSERT INTO collection_requests (user_name, collection_game_id) VALUES (?, ?)",
            (name, collection_game_id),
        )
        log_action(db, "toggle_collection_request", f"Poproszono o grę z szafy: {label}")
    db.commit()
    return redirect(url_for("szafa_view"))


@app.route("/szafa/<int:collection_game_id>/delete", methods=["POST"])
def delete_collection_game(collection_game_id):
    db = get_db()
    if current_is_admin(db):
        game_row = query_one(db, "SELECT * FROM collection_games WHERE id = ?", (collection_game_id,))
        db.execute("DELETE FROM collection_games WHERE id = ?", (collection_game_id,))
    else:
        game_row = query_one(
            db, "SELECT * FROM collection_games WHERE id = ? AND owner_name = ?",
            (collection_game_id, current_name()),
        )
        db.execute(
            "DELETE FROM collection_games WHERE id = ? AND owner_name = ?",
            (collection_game_id, current_name()),
        )
    if game_row:
        log_action(db, "delete_collection_game", f"Usunięto z szafy: {game_row['name']} (właściciel: {game_row['owner_name']})")
    db.commit()
    return redirect(url_for("szafa_view"))


@app.route("/szafa/<int:collection_game_id>/comments/add", methods=["POST"])
def add_collection_comment(collection_game_id):
    text = clamp(request.form.get("text", ""), COMMENT_MAX_LENGTH)
    if text:
        db = get_db()
        db.execute(
            "INSERT INTO collection_comments (collection_game_id, author_name, text) VALUES (?, ?, ?)",
            (collection_game_id, current_name(), text),
        )
        game_row = query_one(db, "SELECT name FROM collection_games WHERE id = ?", (collection_game_id,))
        label = game_row["name"] if game_row else collection_game_id
        log_action(db, "add_collection_comment", f"Komentarz do gry z szafy {label}: {text}")
        db.commit()
    return redirect(url_for("szafa_view"))


@app.route("/szafa/comments/<int:comment_id>/delete", methods=["POST"])
def delete_collection_comment(comment_id):
    db = get_db()
    admin = current_is_admin(db)
    comment = query_one(db, "SELECT * FROM collection_comments WHERE id = ?", (comment_id,))
    if admin:
        db.execute("DELETE FROM collection_comments WHERE id = ?", (comment_id,))
    else:
        db.execute(
            "DELETE FROM collection_comments WHERE id = ? AND author_name = ?",
            (comment_id, current_name()),
        )
    if comment and (admin or comment["author_name"] == current_name()):
        log_action(db, "delete_collection_comment", f"Usunięto komentarz autora {comment['author_name']}: {comment['text']}")
    db.commit()
    return redirect(url_for("szafa_view"))


@app.route("/log")
def log_view():
    db = get_db()
    if not current_is_admin(db):
        return redirect(url_for("games_view"))
    entries = query_all(db, "SELECT * FROM activity_log ORDER BY id DESC LIMIT 500")
    return render_template("log.html", user=current_name(), entries=entries)


init_db()


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
