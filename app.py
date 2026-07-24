import hashlib
import io
import os
import secrets
import smtplib
import sqlite3
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import urlparse

import requests
from flask import Flask, flash, g, jsonify, redirect, render_template, request, send_file, session, url_for
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
BGG_COLLECTION_URL = "https://boardgamegeek.com/xmlapi2/collection"

SMTP_HOST = os.environ.get("SMTP_HOST")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USERNAME = os.environ.get("SMTP_USERNAME")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD")
SMTP_FROM = os.environ.get("SMTP_FROM", SMTP_USERNAME)
APP_BASE_URL = os.environ.get("APP_BASE_URL", "http://localhost:5000")

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me")
app.config["MAX_CONTENT_LENGTH"] = 6 * 1024 * 1024  # 6MB - headroom above SESSION_FILE_MAX_SIZE for multipart overhead
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
# Secure requires HTTPS - only enforce it when actually deployed (Turso configured),
# so the cookie still works for local dev over plain http://localhost.
app.config["SESSION_COOKIE_SECURE"] = bool(TURSO_URL)

NAME_MAX_LENGTH = 50
GAME_NAME_MAX_LENGTH = 100
NOTES_MAX_LENGTH = 300
COMMENT_MAX_LENGTH = 255
SESSION_TIME_MAX_LENGTH = 32
LANGUAGE_MAX_LENGTH = 50
EMAIL_MAX_LENGTH = 120
PASSWORD_MIN_LENGTH = 8
FILENAME_MAX_LENGTH = 150
SESSION_FILE_MAX_SIZE = 5 * 1024 * 1024  # 5 MB - a rulebook photo, PDF scoring sheet, scanned map
INLINE_SAFE_CONTENT_TYPES = {
    "application/pdf",
    "image/png", "image/jpeg", "image/gif", "image/webp", "image/bmp",
    "text/plain",
}

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


def get_hidden_days(db):
    return {row["day_date"] for row in query_all(db, "SELECT day_date FROM hidden_days")}

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


def bgg_search(query, search_type="boardgame"):
    """Board games (or, with search_type="boardgameexpansion", expansions) on
    BGG matching `query`, exact-name matches first. BGG's type filter treats
    the two as disjoint - a plain "boardgame" search never returns expansions,
    even ones whose name matches exactly."""
    root = bgg_get(BGG_SEARCH_URL, {"query": query, "type": search_type})
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


def bgg_collection_owned(username):
    """A BGG user's owned board games (expansions excluded). BGG generates
    collection exports asynchronously - a first-time request answers 202
    while it queues the export, so this polls briefly. Returns None if it
    never finished in time (caller should ask the user to retry shortly),
    or a list of {bgg_id, name, image_url} dicts - empty if the account
    exists but owns nothing, matching plain bgg_get()'s exception behavior
    for a genuinely bad username/network failure."""
    headers = {"Authorization": f"Bearer {BGG_TOKEN}"} if BGG_TOKEN else {}
    params = {"username": username, "own": "1", "excludesubtype": "boardgameexpansion"}
    for attempt in range(5):
        response = requests.get(BGG_COLLECTION_URL, params=params, headers=headers, timeout=10)
        if response.status_code == 202:
            time.sleep(2)
            continue
        response.raise_for_status()
        root = ET.fromstring(response.content)
        items = []
        for item in root.findall("item"):
            name_el = item.find("name")
            image_el = item.find("image")
            items.append({
                "bgg_id": item.get("objectid"),
                "name": name_el.text if name_el is not None else None,
                "image_url": image_el.text if image_el is not None else None,
            })
        return items
    return None


def send_email(to_addr, subject, body):
    """Best-effort transactional email over plain SMTP. Silently skips (logging
    to stderr) when SMTP isn't configured, e.g. in local dev, rather than
    raising - a missing mail provider shouldn't break registration/reset."""
    if not (SMTP_HOST and SMTP_USERNAME and SMTP_PASSWORD):
        print(f"[email skipped - SMTP not configured] to={to_addr} subject={subject!r}")
        return
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = SMTP_FROM
    msg["To"] = to_addr
    msg.set_content(body)
    if SMTP_PORT == 465:
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=10) as server:
            server.login(SMTP_USERNAME, SMTP_PASSWORD)
            server.send_message(msg)
    else:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as server:
            server.starttls()
            server.login(SMTP_USERNAME, SMTP_PASSWORD)
            server.send_message(msg)


def new_token():
    return secrets.token_urlsafe(32)


def send_verify_email(to_addr, name, token):
    link = f"{APP_BASE_URL}{url_for('verify_email', token=token)}"
    send_email(
        to_addr,
        "Potwierdź adres e-mail – Zgrani 2026",
        f"Cześć {name},\n\n"
        f"Potwierdź swój adres e-mail, klikając w poniższy link (ważny 24h):\n{link}\n\n"
        "Jeśli to nie Ty zakładałeś/aś konto, zignoruj tę wiadomość.",
    )


def send_reset_email(to_addr, name, token):
    link = f"{APP_BASE_URL}{url_for('reset_password', token=token)}"
    send_email(
        to_addr,
        "Reset hasła – Zgrani 2026",
        f"Cześć {name},\n\n"
        f"Kliknij w poniższy link, aby ustawić nowe hasło (ważny 1h):\n{link}\n\n"
        "Jeśli to nie Ty prosiłeś/aś o reset hasła, zignoruj tę wiadomość.",
    )


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


def log_action(db, action, details="", actor=None):
    """Record a change for the admin-only activity log. Callers pass an
    already-rendered detail string (game name, etc.) - queried right before
    the row is deleted, since it won't exist to look up afterwards. `actor`
    overrides current_name() for actions that happen before a session exists
    (registering, verifying an email, resetting a password)."""
    db.execute(
        "INSERT INTO activity_log (user_name, action, details) VALUES (?, ?, ?)",
        (actor or current_name(), action, details),
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
        ("players", "email", "TEXT"),
        ("players", "password_hash", "TEXT"),
        ("players", "email_verified", "INTEGER DEFAULT 0"),
        ("players", "email_verify_token", "TEXT"),
        ("players", "email_verify_expires", "TEXT"),
        ("players", "password_reset_token", "TEXT"),
        ("players", "password_reset_expires", "TEXT"),
        ("players", "bgg_username", "TEXT"),
        ("collection_games", "bgg_id", "INTEGER"),
        ("collection_games", "image_url", "TEXT"),
        ("sessions", "promoted", "INTEGER DEFAULT 0"),
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
    if request.endpoint in (
        "login", "static", "register", "verify_email", "forgot_password", "reset_password",
    ):
        return None
    if current_name() is None:
        return redirect(url_for("login"))


@app.errorhandler(413)
def handle_request_too_large(e):
    flash("Przesłane dane są za duże.")
    return safe_redirect_back("sessions_view")


def find_player_by_identifier(db, identifier):
    """Look up a player by name first, then by email if the identifier looks
    like one - lets the same login field take either. Python's .lower() is
    used instead of SQL COLLATE NOCASE because SQLite's builtin NOCASE only
    folds ASCII a-z, not Polish diacritics (Ł/ł, Ż/ż, ...)."""
    players = query_all(db, "SELECT * FROM players")
    player = next((p for p in players if p["name"].lower() == identifier.lower()), None)
    if not player and "@" in identifier:
        player = next(
            (p for p in players if p["email"] and p["email"].lower() == identifier.lower()), None
        )
    return player


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        identifier = clamp(request.form.get("name", ""), max(NAME_MAX_LENGTH, EMAIL_MAX_LENGTH))
        code = request.form.get("code", "").strip()
        password = request.form.get("password", "")
        if not identifier:
            return render_template("login.html", error="Wpisz imię lub e-mail.")

        db = get_db()
        player = find_player_by_identifier(db, identifier)
        name = player["name"] if player else identifier  # reuse recorded spelling

        # A real password (from registering or securing an existing account)
        # always wins over the legacy 4-digit PIN lock, so setting one can't
        # accidentally be bypassed by the older, weaker check.
        if player and player["password_hash"]:
            if not password:
                return render_template("login.html", name=name, need_password=True)
            if not check_password_hash(player["password_hash"], password):
                return render_template(
                    "login.html", name=name, need_password=True, error="Nieprawidłowe hasło."
                )
        elif player and player["pin_code"]:
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


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        name = clamp(request.form.get("name", ""), NAME_MAX_LENGTH)
        email = clamp(request.form.get("email", ""), EMAIL_MAX_LENGTH)
        password = request.form.get("password", "")
        confirm = request.form.get("confirm", "")

        error = None
        if not name or not email or not password:
            error = "Wypełnij wszystkie pola."
        elif "@" not in email:
            error = "Podaj prawidłowy adres e-mail."
        elif len(password) < PASSWORD_MIN_LENGTH:
            error = f"Hasło musi mieć co najmniej {PASSWORD_MIN_LENGTH} znaków."
        elif password != confirm:
            error = "Hasła nie są takie same."

        db = get_db()
        if not error:
            players = query_all(db, "SELECT * FROM players")
            if any(p["name"].lower() == name.lower() for p in players):
                error = "Ta nazwa jest już zajęta. Jeśli to Twoje konto, zaloguj się i dodaj hasło w Koncie."
            elif any(p["email"] and p["email"].lower() == email.lower() for p in players):
                error = "Ten adres e-mail jest już zarejestrowany."

        if error:
            return render_template("register.html", error=error, name=name, email=email)

        token = new_token()
        db.execute(
            "INSERT INTO players (name, email, password_hash, email_verify_token, email_verify_expires) "
            "VALUES (?, ?, ?, ?, datetime('now', '+1 day'))",
            (name, email, generate_password_hash(password), token),
        )
        log_action(db, "register", f"Zarejestrowano konto: {name}", actor=name)
        db.commit()
        send_verify_email(email, name, token)

        session["name"] = name
        flash("Konto utworzone! Sprawdź e-mail, aby potwierdzić adres.")
        return redirect(url_for("games_view"))
    return render_template("register.html")


@app.route("/verify-email/<token>")
def verify_email(token):
    db = get_db()
    player = query_one(
        db,
        "SELECT * FROM players WHERE email_verify_token = ? AND email_verify_expires > datetime('now')",
        (token,),
    )
    if not player:
        flash("Link weryfikacyjny jest nieprawidłowy lub wygasł.")
        return redirect(url_for("games_view") if current_name() else url_for("login"))

    db.execute(
        "UPDATE players SET email_verified = 1, email_verify_token = NULL, email_verify_expires = NULL "
        "WHERE id = ?",
        (player["id"],),
    )
    log_action(db, "verify_email", f"Potwierdzono e-mail: {player['name']}", actor=player["name"])
    db.commit()
    flash("Adres e-mail potwierdzony!")
    return redirect(url_for("games_view") if current_name() else url_for("login"))


@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "POST":
        email = clamp(request.form.get("email", ""), EMAIL_MAX_LENGTH)
        db = get_db()
        player = next(
            (
                p for p in query_all(db, "SELECT * FROM players")
                if p["email"] and p["password_hash"] and p["email"].lower() == email.lower()
            ),
            None,
        )
        if player:
            token = new_token()
            db.execute(
                "UPDATE players SET password_reset_token = ?, "
                "password_reset_expires = datetime('now', '+1 hour') WHERE id = ?",
                (token, player["id"]),
            )
            db.commit()
            send_reset_email(player["email"], player["name"], token)
        # Same response either way - otherwise this becomes a way to probe
        # which email addresses have an account.
        return render_template("forgot_password.html", sent=True)
    return render_template("forgot_password.html")


@app.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    db = get_db()
    player = query_one(
        db,
        "SELECT * FROM players WHERE password_reset_token = ? AND password_reset_expires > datetime('now')",
        (token,),
    )
    if not player:
        flash("Link do resetu hasła jest nieprawidłowy lub wygasł.")
        return redirect(url_for("forgot_password"))

    if request.method == "POST":
        password = request.form.get("password", "")
        confirm = request.form.get("confirm", "")
        if len(password) < PASSWORD_MIN_LENGTH:
            return render_template(
                "reset_password.html", token=token,
                error=f"Hasło musi mieć co najmniej {PASSWORD_MIN_LENGTH} znaków.",
            )
        if password != confirm:
            return render_template("reset_password.html", token=token, error="Hasła nie są takie same.")

        db.execute(
            "UPDATE players SET password_hash = ?, password_reset_token = NULL, "
            "password_reset_expires = NULL WHERE id = ?",
            (generate_password_hash(password), player["id"]),
        )
        log_action(db, "reset_password", f"Zresetowano hasło: {player['name']}", actor=player["name"])
        db.commit()
        session["name"] = player["name"]
        flash("Hasło zostało zmienione.")
        return redirect(url_for("games_view"))
    return render_template("reset_password.html", token=token)


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


@app.route("/konto/secure", methods=["POST"])
def secure_account():
    """Adds email+password to an already-authenticated legacy (name-only)
    account - the "optional upgrade" path. Never touches other players'
    rows, so there's no way to hijack someone else's name this way."""
    email = clamp(request.form.get("email", ""), EMAIL_MAX_LENGTH)
    password = request.form.get("password", "")
    confirm = request.form.get("confirm", "")
    name = current_name()

    db = get_db()
    player = query_one(db, "SELECT * FROM players WHERE name = ?", (name,))

    error = None
    if player and player["password_hash"]:
        error = "To konto jest już zabezpieczone hasłem - zmiana hasła nie jest jeszcze obsługiwana tutaj."
    elif not email or not password:
        error = "Wypełnij wszystkie pola."
    elif "@" not in email:
        error = "Podaj prawidłowy adres e-mail."
    elif len(password) < PASSWORD_MIN_LENGTH:
        error = f"Hasło musi mieć co najmniej {PASSWORD_MIN_LENGTH} znaków."
    elif password != confirm:
        error = "Hasła nie są takie same."

    if not error:
        others = query_all(db, "SELECT * FROM players WHERE name != ?", (name,))
        if any(p["email"] and p["email"].lower() == email.lower() for p in others):
            error = "Ten adres e-mail jest już używany przez inne konto."

    if error:
        flash(error)
        return redirect(url_for("account_view"))

    token = new_token()
    db.execute(
        "UPDATE players SET email = ?, password_hash = ?, email_verified = 0, "
        "email_verify_token = ?, email_verify_expires = datetime('now', '+1 day') WHERE name = ?",
        (email, generate_password_hash(password), token, name),
    )
    log_action(db, "secure_account", f"Dodano e-mail/hasło do konta: {name}")
    db.commit()
    send_verify_email(email, name, token)
    flash("Zabezpieczono konto! Sprawdź e-mail, aby potwierdzić adres.")
    return redirect(url_for("account_view"))


@app.route("/konto/resend-verification", methods=["POST"])
def resend_verification():
    db = get_db()
    name = current_name()
    player = query_one(db, "SELECT * FROM players WHERE name = ?", (name,))
    if player and player["email"] and not player["email_verified"]:
        token = new_token()
        db.execute(
            "UPDATE players SET email_verify_token = ?, "
            "email_verify_expires = datetime('now', '+1 day') WHERE name = ?",
            (token, name),
        )
        db.commit()
        send_verify_email(player["email"], name, token)
        flash("Wysłano ponownie e-mail weryfikacyjny.")
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
    search_type = "boardgameexpansion" if request.args.get("type") == "expansion" else "boardgame"
    if not query or not BGG_TOKEN:
        return jsonify([])
    try:
        return jsonify(bgg_search(query, search_type))
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

    files_by_session = {}
    for row in query_all(
        db,
        "SELECT id, session_id, filename, content_type, file_size, uploaded_by, created_at "
        "FROM session_files ORDER BY created_at",
    ):
        files_by_session.setdefault(row["session_id"], []).append(row)

    known_games = sorted(
        {row["name"] for row in query_all(db, "SELECT DISTINCT name FROM games")},
        key=polish_sort_key,
    )

    hidden_days = get_hidden_days(db)
    admin = current_is_admin(db)
    days = group_sessions_by_day(sessions)
    for day in days:
        day["hidden"] = day["date"] is not None and day["date"].isoformat() in hidden_days
    if not admin:
        days = [d for d in days if not d["hidden"]]

    return render_template(
        "sessions.html",
        user=name,
        days=days,
        joins_by_session=joins_by_session,
        my_joins=my_joins,
        comments_by_session=comments_by_session,
        files_by_session=files_by_session,
        known_games=known_games,
    )


@app.route("/rozgrywki/day/<day_date>/toggle-hidden", methods=["POST"])
def toggle_day_hidden(day_date):
    db = get_db()
    if not current_is_admin(db):
        return redirect(url_for("sessions_view"))
    try:
        date.fromisoformat(day_date)
    except ValueError:
        return redirect(url_for("sessions_view"))
    existing = query_one(db, "SELECT 1 FROM hidden_days WHERE day_date = ?", (day_date,))
    if existing:
        db.execute("DELETE FROM hidden_days WHERE day_date = ?", (day_date,))
        log_action(db, "unhide_day", f"Odsłonięto dzień: {day_date}")
    else:
        db.execute("INSERT INTO hidden_days (day_date) VALUES (?)", (day_date,))
        log_action(db, "hide_day", f"Ukryto dzień: {day_date}")
    db.commit()
    return redirect(url_for("sessions_view"))


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


@app.route("/rozgrywki/<int:session_id>/promote", methods=["POST"])
def toggle_session_promoted(session_id):
    db = get_db()
    if not current_is_admin(db):
        return redirect(url_for("sessions_view"))
    session_row = query_one(db, "SELECT game_name, promoted FROM sessions WHERE id = ?", (session_id,))
    if session_row:
        new_value = 0 if session_row["promoted"] else 1
        db.execute("UPDATE sessions SET promoted = ? WHERE id = ?", (new_value, session_id))
        action = "Wyróżniono" if new_value else "Cofnięto wyróżnienie"
        log_action(db, "toggle_session_promoted", f"{action} rozgrywkę: {session_row['game_name']}")
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


@app.route("/rozgrywki/<int:session_id>/files/add", methods=["POST"])
def upload_session_file(session_id):
    db = get_db()
    if not current_is_admin(db):
        return redirect(url_for("sessions_view"))
    session_row = query_one(db, "SELECT game_name FROM sessions WHERE id = ?", (session_id,))
    if not session_row:
        return redirect(url_for("sessions_view"))
    file = request.files.get("file")
    if file is None or not file.filename:
        flash("Wybierz plik do przesłania.")
        return redirect(url_for("sessions_view"))
    data = file.read()
    if not data:
        flash("Plik jest pusty.")
        return redirect(url_for("sessions_view"))
    if len(data) > SESSION_FILE_MAX_SIZE:
        flash("Plik jest za duży - maksymalnie 5 MB.")
        return redirect(url_for("sessions_view"))
    filename = clamp(os.path.basename(file.filename), FILENAME_MAX_LENGTH) or "plik"
    content_type = clamp(file.content_type or "", 100) or "application/octet-stream"
    db.execute(
        "INSERT INTO session_files (session_id, filename, content_type, file_size, data, uploaded_by) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (session_id, filename, content_type, len(data), data, current_name()),
    )
    log_action(db, "add_session_file", f"Dodano plik do rozgrywki {session_row['game_name']}: {filename}")
    db.commit()
    return redirect(url_for("sessions_view"))


@app.route("/rozgrywki/files/<int:file_id>/view")
def view_session_file(file_id):
    db = get_db()
    row = query_one(db, "SELECT filename, content_type, data FROM session_files WHERE id = ?", (file_id,))
    if not row:
        flash("Plik nie istnieje.")
        return redirect(url_for("sessions_view"))
    content_type = row["content_type"] if row["content_type"] in INLINE_SAFE_CONTENT_TYPES else "application/octet-stream"
    as_attachment = content_type == "application/octet-stream"
    response = send_file(
        io.BytesIO(row["data"]),
        mimetype=content_type,
        as_attachment=as_attachment,
        download_name=row["filename"],
        conditional=False,
        max_age=0,
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@app.route("/rozgrywki/files/<int:file_id>/delete", methods=["POST"])
def delete_session_file(file_id):
    db = get_db()
    if not current_is_admin(db):
        return redirect(url_for("sessions_view"))
    file_row = query_one(db, "SELECT session_id, filename FROM session_files WHERE id = ?", (file_id,))
    db.execute("DELETE FROM session_files WHERE id = ?", (file_id,))
    if file_row:
        session_row = query_one(db, "SELECT game_name FROM sessions WHERE id = ?", (file_row["session_id"],))
        label = session_row["game_name"] if session_row else file_row["session_id"]
        log_action(db, "delete_session_file", f"Usunięto plik z rozgrywki {label}: {file_row['filename']}")
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

    player = query_one(db, "SELECT bgg_username FROM players WHERE name = ?", (name,))

    return render_template(
        "szafa.html",
        user=name,
        my_collection=my_collection,
        others_collection=others_collection,
        requests_by_game=requests_by_game,
        my_requests=my_requests,
        comments_by_collection=comments_by_collection,
        bgg_username=player["bgg_username"] if player else None,
        bgg_enabled=bool(BGG_TOKEN),
    )


@app.route("/szafa/sync-bgg", methods=["POST"])
def sync_bgg_collection():
    if not BGG_TOKEN:
        flash("Synchronizacja z BGG nie jest skonfigurowana na tym serwerze.")
        return redirect(url_for("szafa_view"))

    name = current_name()
    db = get_db()
    username = clamp(request.form.get("bgg_username", ""), NAME_MAX_LENGTH)
    if not username:
        flash("Podaj swoją nazwę użytkownika BGG.")
        return redirect(url_for("szafa_view"))

    db.execute("UPDATE players SET bgg_username = ? WHERE name = ?", (username, name))
    db.commit()

    try:
        items = bgg_collection_owned(username)
    except (requests.RequestException, ET.ParseError):
        flash("Nie udało się połączyć z BGG. Spróbuj ponownie za chwilę.")
        return redirect(url_for("szafa_view"))

    if items is None:
        flash("BGG wciąż przygotowuje Twoją kolekcję - spróbuj ponownie za kilka sekund.")
        return redirect(url_for("szafa_view"))
    if not items:
        flash(f'Nie znaleziono posiadanych gier dla użytkownika "{username}" na BGG.')
        return redirect(url_for("szafa_view"))

    existing_bgg_ids = {
        row["bgg_id"] for row in query_all(
            db, "SELECT bgg_id FROM collection_games WHERE owner_name = ? AND bgg_id IS NOT NULL", (name,)
        )
    }
    added = 0
    for item in items:
        if not item["name"]:
            continue
        bgg_id = int(item["bgg_id"]) if item["bgg_id"] else None
        if bgg_id and bgg_id in existing_bgg_ids:
            continue
        db.execute(
            "INSERT INTO collection_games (name, owner_name, bgg_id, image_url) VALUES (?, ?, ?, ?)",
            (clamp(item["name"], GAME_NAME_MAX_LENGTH), name, bgg_id, item["image_url"]),
        )
        if bgg_id:
            existing_bgg_ids.add(bgg_id)
        added += 1

    log_action(db, "sync_bgg_collection", f"Zaimportowano {added} gier z BGG ({username})")
    db.commit()
    if added:
        flash(f"Zaimportowano {added} gier z Twojej kolekcji BGG.")
    else:
        flash("Wszystkie gry z Twojej kolekcji BGG są już dodane do szafy.")
    return redirect(url_for("szafa_view"))


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
