import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "echo.db")


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,          -- Google 'sub' claim, stable unique user id
            email TEXT,
            name TEXT,
            picture TEXT,
            created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS papers (
            id TEXT PRIMARY KEY,
            user_id TEXT,
            title TEXT,
            authors TEXT,
            year TEXT,
            venue TEXT,
            source TEXT,
            doi TEXT,
            pdf_url TEXT,
            tags TEXT,
            credibility TEXT DEFAULT 'unscored',
            full_text TEXT,
            methodology TEXT,
            findings TEXT,
            research_gap TEXT,
            future_work TEXT,
            in_library INTEGER DEFAULT 0,
            created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT,
            target_type TEXT,
            target_id TEXT,
            vote TEXT,
            created_at TEXT
        );
        """
    )
    # Migration for databases created before user_id existed — adding the
    # column is safe/no-op if it's already present.
    for table, col in [("papers", "user_id"), ("feedback", "user_id")]:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} TEXT")
        except sqlite3.OperationalError:
            pass  # column already exists
    conn.commit()
    conn.close()


def upsert_user(user_id, email, name, picture):
    """Creates the user on first sign-in, refreshes profile info on every
    subsequent sign-in (name/photo can change on Google's side)."""
    import datetime
    conn = get_conn()
    conn.execute(
        """INSERT INTO users (id, email, name, picture, created_at)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(id) DO UPDATE SET email=excluded.email, name=excluded.name, picture=excluded.picture""",
        (user_id, email, name, picture, datetime.datetime.now(datetime.UTC).isoformat()),
    )
    conn.commit()
    conn.close()