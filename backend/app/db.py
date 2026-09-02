import os
import datetime
import psycopg2
import psycopg2.extras
from pgvector.psycopg2 import register_vector

DATABASE_URL = os.environ["DATABASE_URL"]
EMBED_DIM = 384  # all-MiniLM-L6-v2 output size, used by the chunks table below


class ConnWrapper:
    """Thin compatibility layer so the rest of the app can keep using the
    same sqlite3-style API it was written against — conn.execute(sql, params)
    with '?' placeholders, returning a cursor with .fetchall()/.fetchone(),
    rows behaving like dicts — after the underlying database moved from
    local SQLite to hosted Postgres (Supabase). This means main.py, rag.py,
    contribute.py, and summarize.py needed no changes beyond one raw-SQL
    fix (INSERT OR REPLACE isn't valid Postgres syntax)."""
    def __init__(self, pg_conn):
        self._conn = pg_conn

    def execute(self, query, params=()):
        cur = self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(query.replace("?", "%s"), params)
        return cur

    def executescript(self, script):
        cur = self._conn.cursor()
        cur.execute(script)
        return cur

    def commit(self):
        self._conn.commit()

    def close(self):
        self._conn.close()


def get_conn():
    pg_conn = psycopg2.connect(DATABASE_URL)
    # Registers pgvector's 'vector' type on this connection so numpy arrays
    # passed as query params get adapted correctly — needed by embeddings.py.
    # Safe to call here because by the time anything but init_db() runs,
    # the extension is guaranteed to already exist (see init_db below).
    register_vector(pg_conn)
    return ConnWrapper(pg_conn)


def init_db():
    # Step 1: create the extension on a PLAIN connection — register_vector
    # must NOT be called yet, since on a brand-new database the 'vector'
    # type doesn't exist until this CREATE EXTENSION runs. Calling
    # register_vector before this would throw on a fresh Supabase DB.
    pg_conn = psycopg2.connect(DATABASE_URL)
    cur = pg_conn.cursor()
    cur.execute("CREATE EXTENSION IF NOT EXISTS vector;")
    pg_conn.commit()
    pg_conn.close()

    # Step 2: now it's safe to use get_conn() — the extension exists, so
    # register_vector succeeds, and we can create the rest of the schema.
    conn = get_conn()
    conn.executescript(
        f"""
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
            id SERIAL PRIMARY KEY,
            user_id TEXT,
            target_type TEXT,
            target_id TEXT,
            vote TEXT,
            created_at TEXT
        );
        -- Replaces the old local Chroma vector store. One row per indexed
        -- chunk; embedding is a pgvector column so similarity search runs
        -- as a normal SQL query via the <=> (cosine distance) operator.
        CREATE TABLE IF NOT EXISTS chunks (
            id TEXT PRIMARY KEY,
            paper_id TEXT,
            user_id TEXT,
            section TEXT,
            text TEXT,
            embedding vector({EMBED_DIM})
        );
        ALTER TABLE papers ADD COLUMN IF NOT EXISTS user_id TEXT;
        ALTER TABLE feedback ADD COLUMN IF NOT EXISTS user_id TEXT;
        """
    )
    conn.commit()
    conn.close()


def upsert_user(user_id, email, name, picture):
    """Creates the user on first sign-in, refreshes profile info on every
    subsequent sign-in (name/photo can change on Google's side)."""
    conn = get_conn()
    conn.execute(
        """INSERT INTO users (id, email, name, picture, created_at)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT (id) DO UPDATE SET email=excluded.email, name=excluded.name, picture=excluded.picture""",
        (user_id, email, name, picture, datetime.datetime.now(datetime.UTC).isoformat()),
    )
    conn.commit()
    conn.close()