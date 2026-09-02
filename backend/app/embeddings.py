import os

import chromadb
from fastembed import TextEmbedding

_model = None
_client = None

CHROMA_PATH = os.path.join(os.path.dirname(__file__), "..", "chroma_db")


def get_model():
    global _model
    if _model is None:
        # fastembed runs on onnxruntime instead of torch, which cuts memory
        # usage dramatically compared to the old sentence-transformers/torch
        # stack — that stack was causing out-of-memory crashes on Render's
        # 512MB free tier as soon as a paper got processed. Same underlying
        # model (all-MiniLM-L6-v2, 384-dim output) as before, converted to
        # ONNX, so retrieval quality is effectively unchanged.
        _model = TextEmbedding(model_name="sentence-transformers/all-MiniLM-L6-v2")
    return _model


def get_collection():
    global _client
    if _client is None:
        _client = chromadb.PersistentClient(path=CHROMA_PATH)
    # Explicit cosine distance — Chroma defaults to raw L2 (Euclidean) if
    # unspecified, which has an unpredictable range on un-normalized
    # embeddings and breaks every relevance threshold in the app (they're
    # all calibrated for cosine distance's predictable 0-2 range). This
    # metadata only takes effect when the collection is FIRST created —
    # if "chunks" already exists from before this fix, it must be deleted
    # (see README/setup notes) so it gets recreated with this setting.
    return _client.get_or_create_collection("chunks", metadata={"hnsw:space": "cosine"})


def index_paper_chunks(paper_id, chunks, user_id):
    if not chunks:
        return
    model = get_model()
    coll = get_collection()
    texts = [c["text"] for c in chunks]
    # model.embed() returns a generator of numpy arrays, one per input text —
    # materialize to a list of plain lists for Chroma's API.
    vectors = [v.tolist() for v in model.embed(texts)]
    ids = [f"{paper_id}__{i}" for i in range(len(chunks))]
    metadatas = [{"paper_id": paper_id, "section": c["section"], "user_id": user_id} for c in chunks]
    coll.add(ids=ids, embeddings=vectors, documents=texts, metadatas=metadatas)


def delete_paper_chunks(paper_id):
    coll = get_collection()
    coll.delete(where={"paper_id": paper_id})


def encode_query(query):
    """Embeds a single query string. Exposed separately so callers that need
    to run the SAME question against several different filters (e.g. one
    query per paper) can encode once and reuse the vector, instead of paying
    the encoding cost repeatedly."""
    return [v.tolist() for v in get_model().embed([query])]


def _build_where(paper_ids=None, user_id=None):
    conditions = []
    if user_id is not None:
        conditions.append({"user_id": user_id})
    if paper_ids:
        conditions.append({"paper_id": {"$in": paper_ids}})
    if len(conditions) == 0:
        return None
    if len(conditions) == 1:
        return conditions[0]
    return {"$and": conditions}


def query_chunks_by_vector(q_emb, k=5, paper_ids=None, user_id=None):
    """Same retrieval as query_chunks, but takes an already-computed embedding
    vector instead of re-encoding text — used when the same question needs to
    be queried multiple times with different filters (e.g. per-paper)."""
    coll = get_collection()
    where = _build_where(paper_ids=paper_ids, user_id=user_id)
    res = coll.query(query_embeddings=q_emb, n_results=k, where=where)
    out = []
    docs = res.get("documents", [[]])[0]
    metas = res.get("metadatas", [[]])[0]
    dists = res.get("distances", [[]])[0]
    for doc, meta, dist in zip(docs, metas, dists):
        out.append({"text": doc, "paper_id": meta["paper_id"], "section": meta["section"], "distance": dist})
    return out


def query_chunks(query, k=5, paper_ids=None, user_id=None):
    """user_id is required for any user-facing query — it's the hard boundary
    that prevents one user's question from retrieving another user's chunks,
    even if paper_ids were somehow guessed or reused."""
    return query_chunks_by_vector(encode_query(query), k=k, paper_ids=paper_ids, user_id=user_id)