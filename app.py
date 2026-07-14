import os
import sqlite3
from pathlib import Path

from flask import Flask, g, redirect, render_template, request, session, url_for

BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "boardgames.db"
SCHEMA_PATH = BASE_DIR / "schema.sql"

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me")


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.executescript(SCHEMA_PATH.read_text())
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
        except sqlite3.OperationalError:
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
        name = request.form.get("name", "").strip()
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

    games = db.execute(
        "SELECT * FROM games ORDER BY created_at DESC"
    ).fetchall()

    interest_names = {}  # game_id -> list of names
    my_interests = set()
    for row in db.execute("SELECT user_name, game_id FROM interests"):
        interest_names.setdefault(row["game_id"], []).append(row["user_name"])
        if row["user_name"] == name:
            my_interests.add(row["game_id"])

    wishes = db.execute(
        "SELECT * FROM wishes ORDER BY created_at DESC"
    ).fetchall()

    return render_template(
        "games.html",
        user=name,
        games=games,
        wishes=wishes,
        interest_names=interest_names,
        my_interests=my_interests,
    )


@app.route("/moje")
def my_view():
    db = get_db()
    name = current_name()

    my_games = db.execute(
        "SELECT * FROM games WHERE owner_name = ? ORDER BY created_at DESC",
        (name,),
    ).fetchall()
    my_wishes = db.execute(
        "SELECT * FROM wishes WHERE requester_name = ? ORDER BY created_at DESC",
        (name,),
    ).fetchall()

    return render_template(
        "my_games.html",
        user=name,
        my_games=my_games,
        my_wishes=my_wishes,
    )


@app.route("/konto", methods=["GET", "POST"])
def account_view():
    name = current_name()
    error = None
    if request.method == "POST":
        new_name = request.form.get("name", "").strip()
        if not new_name:
            error = "Wpisz imię."
        else:
            session["name"] = new_name
            name = new_name
    return render_template("account.html", user=name, error=error)


@app.route("/games/add", methods=["POST"])
def add_game():
    name = request.form.get("name", "").strip()
    notes = request.form.get("notes", "").strip()
    image_url = request.form.get("image_url", "").strip() or None
    if name:
        db = get_db()
        db.execute(
            "INSERT INTO games (name, notes, owner_name, image_url) VALUES (?, ?, ?, ?)",
            (name, notes, current_name(), image_url),
        )
        db.commit()
    return redirect(url_for("my_view"))


@app.route("/games/<int:game_id>/delete", methods=["POST"])
def delete_game(game_id):
    db = get_db()
    game = db.execute(
        "SELECT * FROM games WHERE id = ? AND owner_name = ?",
        (game_id, current_name()),
    ).fetchone()
    if game:
        if game["origin_wish_requester"]:
            db.execute(
                "INSERT INTO wishes (name, notes, requester_name, image_url) VALUES (?, ?, ?, ?)",
                (game["name"], game["notes"], game["origin_wish_requester"], game["image_url"]),
            )
        db.execute("DELETE FROM games WHERE id = ?", (game_id,))
        db.commit()
    return redirect(url_for("my_view"))


@app.route("/games/<int:game_id>/interest", methods=["POST"])
def toggle_interest(game_id):
    db = get_db()
    name = current_name()
    existing = db.execute(
        "SELECT 1 FROM interests WHERE user_name = ? AND game_id = ?",
        (name, game_id),
    ).fetchone()
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
    name = request.form.get("name", "").strip()
    notes = request.form.get("notes", "").strip()
    image_url = request.form.get("image_url", "").strip() or None
    if name:
        db = get_db()
        db.execute(
            "INSERT INTO wishes (name, notes, requester_name, image_url) VALUES (?, ?, ?, ?)",
            (name, notes, current_name(), image_url),
        )
        db.commit()
    return redirect(url_for("my_view"))


@app.route("/wishes/<int:wish_id>/delete", methods=["POST"])
def delete_wish(wish_id):
    db = get_db()
    db.execute(
        "DELETE FROM wishes WHERE id = ? AND requester_name = ?",
        (wish_id, current_name()),
    )
    db.commit()
    return redirect(url_for("my_view"))


@app.route("/wishes/<int:wish_id>/bring", methods=["POST"])
def bring_wish(wish_id):
    db = get_db()
    wish = db.execute("SELECT * FROM wishes WHERE id = ?", (wish_id,)).fetchone()
    if wish:
        db.execute(
            "INSERT INTO games (name, notes, owner_name, image_url, origin_wish_requester) VALUES (?, ?, ?, ?, ?)",
            (wish["name"], wish["notes"], current_name(), wish["image_url"], wish["requester_name"]),
        )
        db.execute("DELETE FROM wishes WHERE id = ?", (wish_id,))
        db.commit()
    return redirect(url_for("games_view"))


init_db()


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
