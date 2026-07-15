import hashlib
import os
import sqlite3
from pathlib import Path

from flask import Flask, flash, g, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "boardgames.db"
SCHEMA_PATH = BASE_DIR / "schema.sql"

TURSO_URL = os.environ.get("TURSO_DATABASE_URL")
TURSO_AUTH_TOKEN = os.environ.get("TURSO_AUTH_TOKEN")

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me")
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024  # 64KB - plenty for these small text forms

NAME_MAX_LENGTH = 50
GAME_NAME_MAX_LENGTH = 100
NOTES_MAX_LENGTH = 300

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


def group_games(rows):
    """Merge game rows that share a name and notes into one card with a list of owners."""
    groups = {}
    order = []
    for row in rows:
        key = (row["name"].strip().lower(), (row["notes"] or "").strip().lower())
        if key not in groups:
            groups[key] = {**row, "owners": []}
            order.append(key)
        groups[key]["owners"].append(row["owner_name"])
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
    ]:
        try:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")
        except Exception:
            pass  # column already exists
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
        player = query_one(db, "SELECT * FROM players WHERE name = ?", (name,))
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

    games = group_games(query_all(db, "SELECT * FROM games ORDER BY name COLLATE NOCASE"))

    interest_names = {}  # game_id -> list of names
    my_interests = set()
    for row in query_all(db, "SELECT user_name, game_id FROM interests"):
        interest_names.setdefault(row["game_id"], []).append(row["user_name"])
        if row["user_name"] == name:
            my_interests.add(row["game_id"])

    wishes = group_wishes(query_all(db, "SELECT * FROM wishes ORDER BY name COLLATE NOCASE"))

    return render_template(
        "games.html",
        user=name,
        games=games,
        wishes=wishes,
        interest_names=interest_names,
        my_interests=my_interests,
    )


@app.route("/konto", methods=["GET", "POST"])
def account_view():
    name = current_name()
    error = None
    db = get_db()
    if request.method == "POST":
        new_name = clamp(request.form.get("name", ""), NAME_MAX_LENGTH)
        existing = query_one(db, "SELECT * FROM players WHERE name = ?", (new_name,))
        if not new_name:
            error = "Wpisz imię."
        elif new_name != name and existing and existing["pin_code"]:
            error = "To imię jest zablokowane kodem - zaloguj się nim przez stronę logowania."
        else:
            record_player(db, new_name)
            session["name"] = new_name
            name = new_name

    my_games = query_all(
        db, "SELECT * FROM games WHERE owner_name = ? ORDER BY name COLLATE NOCASE", (name,)
    )
    my_wishes = query_all(
        db, "SELECT * FROM wishes WHERE requester_name = ? ORDER BY name COLLATE NOCASE", (name,)
    )
    player = query_one(db, "SELECT * FROM players WHERE name = ?", (name,))

    return render_template(
        "account.html",
        user=name,
        error=error,
        my_games=my_games,
        my_wishes=my_wishes,
        player=player,
        current_color=name_color(name),
        name_colors=NAME_COLORS,
    )


@app.route("/konto/color", methods=["POST"])
def set_color():
    color = request.form.get("color", "")
    if color in NAME_COLORS:
        db = get_db()
        db.execute(
            "UPDATE players SET color = ? WHERE name = ?", (color, current_name())
        )
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
        db.commit()
    return redirect(url_for("account_view"))


@app.route("/konto/unlock", methods=["POST"])
def unlock_account():
    db = get_db()
    db.execute("UPDATE players SET pin_code = NULL WHERE name = ?", (current_name(),))
    db.commit()
    return redirect(url_for("account_view"))


@app.route("/games/add", methods=["POST"])
def add_game():
    name = clamp(request.form.get("name", ""), GAME_NAME_MAX_LENGTH)
    notes = clamp(request.form.get("notes", ""), NOTES_MAX_LENGTH)
    image_url = request.form.get("image_url", "").strip() or None
    if name:
        db = get_db()
        db.execute(
            "INSERT INTO games (name, notes, owner_name, image_url) VALUES (?, ?, ?, ?)",
            (name, notes, current_name(), image_url),
        )
        db.commit()
    return redirect(url_for("games_view"))


@app.route("/games/<int:game_id>/delete", methods=["POST"])
def delete_game(game_id):
    db = get_db()
    game = query_one(
        db, "SELECT * FROM games WHERE id = ? AND owner_name = ?", (game_id, current_name())
    )
    if game:
        if game["origin_wish_requester"]:
            for requester in game["origin_wish_requester"].split(","):
                db.execute(
                    "INSERT INTO wishes (name, notes, requester_name, image_url) VALUES (?, ?, ?, ?)",
                    (game["name"], game["notes"], requester, game["image_url"]),
                )
        db.execute("DELETE FROM games WHERE id = ?", (game_id,))
        db.commit()
    return redirect(url_for("account_view"))


@app.route("/games/<int:game_id>/interest", methods=["POST"])
def toggle_interest(game_id):
    db = get_db()
    name = current_name()
    existing = query_one(
        db, "SELECT 1 FROM interests WHERE user_name = ? AND game_id = ?", (name, game_id)
    )
    if existing:
        db.execute(
            "DELETE FROM interests WHERE user_name = ? AND game_id = ?",
            (name, game_id),
        )
    else:
        db.execute(
            "INSERT INTO interests (user_name, game_id) VALUES (?, ?)",
            (name, game_id),
        )
    db.commit()
    return redirect(url_for("games_view"))


@app.route("/wishes/add", methods=["POST"])
def add_wish():
    name = clamp(request.form.get("name", ""), GAME_NAME_MAX_LENGTH)
    notes = clamp(request.form.get("notes", ""), NOTES_MAX_LENGTH)
    image_url = request.form.get("image_url", "").strip() or None
    if name:
        db = get_db()
        db.execute(
            "INSERT INTO wishes (name, notes, requester_name, image_url) VALUES (?, ?, ?, ?)",
            (name, notes, current_name(), image_url),
        )
        db.commit()
    return redirect(url_for("games_view"))


@app.route("/wishes/<int:wish_id>/delete", methods=["POST"])
def delete_wish(wish_id):
    db = get_db()
    db.execute(
        "DELETE FROM wishes WHERE id = ? AND requester_name = ?",
        (wish_id, current_name()),
    )
    db.commit()
    return redirect(url_for("account_view"))


@app.route("/wishes/<int:wish_id>/bring", methods=["POST"])
def bring_wish(wish_id):
    db = get_db()
    wish = query_one(db, "SELECT * FROM wishes WHERE id = ?", (wish_id,))
    if wish:
        # Everyone who wished for this same game name is fulfilled by one person bringing it.
        same_name = query_all(
            db, "SELECT * FROM wishes WHERE name = ? COLLATE NOCASE", (wish["name"],)
        )
        requester_names = [row["requester_name"] for row in same_name]
        requesters = ",".join(requester_names)
        cur = db.execute(
            "INSERT INTO games (name, notes, owner_name, image_url, origin_wish_requester) VALUES (?, ?, ?, ?, ?)",
            (wish["name"], wish["notes"], current_name(), wish["image_url"], requesters),
        )
        game_id = cur.lastrowid
        # The original wishers are automatically interested in the game that fulfills their wish.
        for requester in set(requester_names) - {current_name()}:
            db.execute(
                "INSERT INTO interests (user_name, game_id) VALUES (?, ?)",
                (requester, game_id),
            )
        db.execute("DELETE FROM wishes WHERE name = ? COLLATE NOCASE", (wish["name"],))
        db.commit()
    return redirect(url_for("games_view"))


init_db()


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
