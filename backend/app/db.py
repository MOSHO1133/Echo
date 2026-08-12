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
        CREATE TABLE IF NOT EXISTS papers (
            id TEXT PRIMARY KEY,
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
            target_type TEXT,
            target_id TEXT,
            vote TEXT,
            created_at TEXT
        );
        """
    )
    conn.commit()
    conn.close()