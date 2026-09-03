import os
import datetime

import psycopg2
import psycopg2.extras
import psycopg2.pool
from pgvector.psycopg2 import register_vector

DATABASE_URL = os.environ["DATABASE_URL"]

EMBED_DIM = 384  # all-MiniLM-L6-v2 output size, used by the chunks table below

# A pooled connection is reused across requests instead of opening a fresh
# TCP+SSL handshake to Supabase on every single query — with the DB in a
# different region from the app server, that handshake alone can cost
# 100-300ms, and functions like analyze_library() run many queries per
# request. minconn=1 keeps at least one warm connection ready; maxconn=10
# is comfortably under Supabase's free-tier connection limit.
_pool = None


def _get_pool():
    global _pool
    if _pool is None:
        _pool = psycopg2.pool.ThreadedConnectionPool(1, 10, DATABASE_URL)
    return _pool


class ConnWrapper:
    """Thin compatibility layer so the rest of the app can keep using the
    same sqlite3-style API it was written against — conn.execute(sql, params)
    with '?' placeholders, returning a cursor with .fetchall()/.fetchone(),
    rows behaving like dicts — after the underlying database moved from
    local SQLite to hosted Postgres (Supabase)."""

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
        # Returns the connection to the pool instead of actually closing the
        # socket, so the next get_conn() call can reuse it warm.
        _get_pool().putconn(self._conn)


def get_conn():
    pg_conn = _get_pool().getconn()
    # Registers pgvector's 'vector' type on this connection so numpy arrays
    # passed as query params get adapted correctly — needed by embeddings.py.
    # Cheap to call even on an already-registered pooled connection.
    register_vector(pg_conn)
    return ConnWrapper(pg_conn)


def init_db():
    conn = get_conn()
    conn.executescript(
        f"""
        CREATE EXTENSION IF NOT EXISTS vector;

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