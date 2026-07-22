CREATE TABLE IF NOT EXISTS games (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    notes TEXT,
    owner_name TEXT NOT NULL,
    bgg_id INTEGER,
    image_url TEXT,
    origin_wish_requester TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS game_expansions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    bgg_id INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS interests (
    user_name TEXT NOT NULL,
    game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    PRIMARY KEY (user_name, game_id)
);

CREATE TABLE IF NOT EXISTS wishes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    notes TEXT,
    requester_name TEXT NOT NULL,
    bgg_id INTEGER,
    image_url TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS players (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    color TEXT,
    pin_code TEXT,
    track_unread INTEGER DEFAULT 1,
    is_admin INTEGER DEFAULT 0,
    email TEXT,
    password_hash TEXT,
    email_verified INTEGER DEFAULT 0,
    email_verify_token TEXT,
    email_verify_expires TEXT,
    password_reset_token TEXT,
    password_reset_expires TEXT
);

CREATE TABLE IF NOT EXISTS comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    author_name TEXT NOT NULL,
    text TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS comment_reads (
    user_name TEXT NOT NULL,
    game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    last_seen_at TEXT NOT NULL,
    PRIMARY KEY (user_name, game_id)
);

CREATE TABLE IF NOT EXISTS wish_comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wish_id INTEGER NOT NULL REFERENCES wishes(id) ON DELETE CASCADE,
    author_name TEXT NOT NULL,
    text TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS wish_comment_reads (
    user_name TEXT NOT NULL,
    wish_id INTEGER NOT NULL REFERENCES wishes(id) ON DELETE CASCADE,
    last_seen_at TEXT NOT NULL,
    PRIMARY KEY (user_name, wish_id)
);

CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    game_name TEXT NOT NULL,
    session_time TEXT NOT NULL,
    notes TEXT,
    organizer_name TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS session_joins (
    user_name TEXT NOT NULL,
    session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    PRIMARY KEY (user_name, session_id)
);

CREATE TABLE IF NOT EXISTS session_comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    author_name TEXT NOT NULL,
    text TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS collection_games (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    notes TEXT,
    owner_name TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS collection_requests (
    user_name TEXT NOT NULL,
    collection_game_id INTEGER NOT NULL REFERENCES collection_games(id) ON DELETE CASCADE,
    PRIMARY KEY (user_name, collection_game_id)
);

CREATE TABLE IF NOT EXISTS collection_comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    collection_game_id INTEGER NOT NULL REFERENCES collection_games(id) ON DELETE CASCADE,
    author_name TEXT NOT NULL,
    text TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS activity_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_name TEXT NOT NULL,
    action TEXT NOT NULL,
    details TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
