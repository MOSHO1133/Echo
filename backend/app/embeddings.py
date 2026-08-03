import os

import chromadb
from sentence_transformers import SentenceTransformer

_model = None
_client = None

CHROMA_PATH = os.path.join(os.path.dirname(__file__), "..", "chroma_db")


def get_model():
    global _model
    if _model is None:
        # Downloads weights from huggingface.co on first run — needs normal internet access.
        _model = SentenceTransformer("all-MiniLM-L6-v2")
    return _model


def get_collection():
    global _client
    if _client is None:
        _client = chromadb.PersistentClient(path=CHROMA_PATH)
    return _client.get_or_create_collection("chunks")


def index_paper_chunks(paper_id, chunks):
    if not chunks:
        return
    model = get_model()
    coll = get_collection()
    texts = [c["text"] for c in chunks]
    vectors = model.encode(texts).tolist()
    ids = [f"{paper_id}__{i}" for i in range(len(chunks))]
    metadatas = [{"paper_id": paper_id, "section": c["section"]} for c in chunks]
    coll.add(ids=ids, embeddings=vectors, documents=texts, metadatas=metadatas)


def delete_paper_chunks(paper_id):
    coll = get_collection()
    coll.delete(where={"paper_id": paper_id})


def query_chunks(query, k=5, paper_ids=None):
    model = get_model()
    coll = get_collection()
    q_emb = model.encode([query]).tolist()
    where = {"paper_id": {"$in": paper_ids}} if paper_ids else None
    res = coll.query(query_embeddings=q_emb, n_results=k, where=where)
    out = []
    docs = res.get("documents", [[]])[0]
    metas = res.get("metadatas", [[]])[0]
    dists = res.get("distances", [[]])[0]
    for doc, meta, dist in zip(docs, metas, dists):
        out.append({"text": doc, "paper_id": meta["paper_id"], "section": meta["section"], "distance": dist})
    return out
