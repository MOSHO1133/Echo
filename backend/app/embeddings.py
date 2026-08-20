import os

import chromadb
from sentence_transformers import SentenceTransformer

_model = None
_client = None

CHROMA_PATH = os.path.join(os.path.dirname(__file__), "..", "chroma_db")


def get_model():
    global _model
    if _model is None:
        _model = SentenceTransformer("all-MiniLM-L6-v2")
    return _model


def get_collection():
    global _client
    if _client is None:
        _client = chromadb.PersistentClient(path=CHROMA_PATH)
    return _client.get_or_create_collection("chunks")


def index_paper_chunks(paper_id, chunks, user_id):
    if not chunks:
        return
    model = get_model()
    coll = get_collection()
    texts = [c["text"] for c in chunks]
    vectors = model.encode(texts).tolist()
    ids = [f"{paper_id}__{i}" for i in range(len(chunks))]
    metadatas = [{"paper_id": paper_id, "section": c["section"], "user_id": user_id} for c in chunks]
    coll.add(ids=ids, embeddings=vectors, documents=texts, metadatas=metadatas)


def delete_paper_chunks(paper_id):
    coll = get_collection()
    coll.delete(where={"paper_id": paper_id})


def query_chunks(query, k=5, paper_ids=None, user_id=None):
    """user_id is required for any user-facing query — it's the hard boundary
    that prevents one user's question from retrieving another user's chunks,
    even if paper_ids were somehow guessed or reused."""
    model = get_model()
    coll = get_collection()
    q_emb = model.encode([query]).tolist()

    conditions = []
    if user_id is not None:
        conditions.append({"user_id": user_id})
    if paper_ids:
        conditions.append({"paper_id": {"$in": paper_ids}})

    if len(conditions) == 0:
        where = None
    elif len(conditions) == 1:
        where = conditions[0]
    else:
        where = {"$and": conditions}

    res = coll.query(query_embeddings=q_emb, n_results=k, where=where)
    out = []
    docs = res.get("documents", [[]])[0]
    metas = res.get("metadatas", [[]])[0]
    dists = res.get("distances", [[]])[0]
    for doc, meta, dist in zip(docs, metas, dists):
        out.append({"text": doc, "paper_id": meta["paper_id"], "section": meta["section"], "distance": dist})
    return out