import os
import sqlite3
from pathlib import Path

from flask import Flask, g, redirect, render_template, request, session, url_for

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


def clamp(text, max_length):
    return text.strip()[:max_length]


class _TursoCursor:
    """Adapts a libsql_client ResultSet to the sqlite3 cursor shape (.description/.fetchall)."""

    def __init__(self, result_set):
        self._rows = [tuple(row) for row in result_set.rows]
        self.description = [(c,) for c in result_set.columns] if result_set.columns else None

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
    ]:
        try:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")
        except Exception:
            pass  # column already exists
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
        if not name:
            return render_template("login.html", error="Wpisz imię.")
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

    games = query_all(db, "SELECT * FROM games ORDER BY created_at DESC")

    interest_names = {}  # game_id -> list of names
    my_interests = set()
    for row in query_all(db, "SELECT user_name, game_id FROM interests"):
        interest_names.setdefault(row["game_id"], []).append(row["user_name"])
        if row["user_name"] == name:
            my_interests.add(row["game_id"])

    wishes = group_wishes(query_all(db, "SELECT * FROM wishes ORDER BY created_at DESC"))

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
    if request.method == "POST":
        new_name = clamp(request.form.get("name", ""), NAME_MAX_LENGTH)
        if not new_name:
            error = "Wpisz imię."
        else:
            session["name"] = new_name
            name = new_name

    db = get_db()
    my_games = query_all(
        db, "SELECT * FROM games WHERE owner_name = ? ORDER BY created_at DESC", (name,)
    )
    my_wishes = query_all(
        db, "SELECT * FROM wishes WHERE requester_name = ? ORDER BY created_at DESC", (name,)
    )

    return render_template(
        "account.html",
        user=name,
        error=error,
        my_games=my_games,
        my_wishes=my_wishes,
    )


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
        requesters = ",".join(row["requester_name"] for row in same_name)
        db.execute(
            "INSERT INTO games (name, notes, owner_name, image_url, origin_wish_requester) VALUES (?, ?, ?, ?, ?)",
            (wish["name"], wish["notes"], current_name(), wish["image_url"], requesters),
        )
        db.execute("DELETE FROM wishes WHERE name = ? COLLATE NOCASE", (wish["name"],))
        db.commit()
    return redirect(url_for("games_view"))


init_db()


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
