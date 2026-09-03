import threading

import numpy as np
from fastembed import TextEmbedding

from . import db

_model = None
_model_lock = threading.Lock()


def get_model():
    global _model
    # Adding several papers in quick succession fires several background
    # tasks nearly simultaneously, each landing on its own thread. Without
    # this lock, multiple threads could all see _model as None at once and
    # each try to initialize (and download) the model concurrently — which
    # can corrupt the download or leave one thread waiting forever on a lock
    # file another thread also holds, hanging that paper's processing
    # indefinitely with no error ever surfacing. The double-checked lock
    # keeps this fast for the common case (model already loaded) while
    # guaranteeing only one thread ever does the actual initialization.
    if _model is None:
        with _model_lock:
            if _model is None:
                # fastembed runs on onnxruntime instead of torch, cutting
                # memory use dramatically compared to sentence-transformers/
                # torch — that stack was causing out-of-memory crashes on
                # Render's free tier. Same underlying model
                # (all-MiniLM-L6-v2, 384-dim output) as before.
                _model = TextEmbedding(model_name="sentence-transformers/all-MiniLM-L6-v2")
    return _model


def embed_texts(texts):
    """Raw embedding vectors as plain Python lists of floats. Used both for
    indexing chunks below and by analysis.py's library-diversity scoring,
    which normalizes them manually rather than relying on any particular
    embedding backend's internal normalization behavior."""
    return [v.tolist() for v in get_model().embed(texts)]


def index_paper_chunks(paper_id, chunks, user_id):
    if not chunks:
        return
    texts = [c["text"] for c in chunks]
    vectors = embed_texts(texts)
    conn = db.get_conn()
    for i, (c, vec) in enumerate(zip(chunks, vectors)):
        conn.execute(
            "INSERT INTO chunks (id, paper_id, user_id, section, text, embedding) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (id) DO UPDATE SET "
            "section=excluded.section, text=excluded.text, embedding=excluded.embedding",
            (f"{paper_id}__{i}", paper_id, user_id, c["section"], c["text"], np.array(vec, dtype=np.float32)),
        )
    conn.commit()
    conn.close()


def delete_paper_chunks(paper_id):
    conn = db.get_conn()
    conn.execute("DELETE FROM chunks WHERE paper_id=?", (paper_id,))
    conn.commit()
    conn.close()


def encode_query(query):
    """Embeds a single query string, returning a flat vector (list of
    floats) ready to pass straight into a pgvector similarity query."""
    return embed_texts([query])[0]


def _build_where(paper_ids=None, user_id=None):
    clauses = []
    params = []
    if user_id is not None:
        clauses.append("user_id = ?")
        params.append(user_id)
    if paper_ids:
        clauses.append("paper_id = ANY(?)")
        params.append(list(paper_ids))
    where_sql = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return where_sql, params


def query_chunks_by_vector(q_emb, k=5, paper_ids=None, user_id=None):
    """Same retrieval as query_chunks, but takes an already-computed
    embedding vector instead of re-encoding text — used when the same
    question needs to be queried multiple times with different filters
    (e.g. per-paper)."""
    where_sql, where_params = _build_where(paper_ids=paper_ids, user_id=user_id)
    vec = np.array(q_emb, dtype=np.float32)
    sql = (
        "SELECT paper_id, section, text, embedding <=> ? AS distance "
        f"FROM chunks{where_sql} "
        "ORDER BY embedding <=> ? LIMIT ?"
    )
    # Param order must match '?' occurrence order in the SQL string above:
    # 1) the SELECT's embedding <=> ?, 2) WHERE clause params (in the order
    # _build_where added them), 3) the ORDER BY's embedding <=> ?, 4) LIMIT.
    params = [vec, *where_params, vec, k]
    conn = db.get_conn()
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return [
        {"text": r["text"], "paper_id": r["paper_id"], "section": r["section"], "distance": float(r["distance"])}
        for r in rows
    ]


def query_chunks(query, k=5, paper_ids=None, user_id=None):
    """user_id is required for any user-facing query — it's the hard
    boundary that prevents one user's question from retrieving another
    user's chunks, even if paper_ids were somehow guessed or reused."""
    return query_chunks_by_vector(encode_query(query), k=k, paper_ids=paper_ids, user_id=user_id)