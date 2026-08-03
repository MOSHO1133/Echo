import anthropic

from . import db, embeddings

client = anthropic.Anthropic()
MODEL = "claude-sonnet-4-6"


def get_paper_titles(paper_ids):
    if not paper_ids:
        return {}
    conn = db.get_conn()
    placeholders = ",".join("?" for _ in paper_ids)
    rows = conn.execute(f"SELECT id, title FROM papers WHERE id IN ({placeholders})", paper_ids).fetchall()
    conn.close()
    return {r["id"]: r["title"] for r in rows}


def build_context(chunks, titles):
    parts = []
    for i, c in enumerate(chunks, start=1):
        title = titles.get(c["paper_id"], c["paper_id"])
        parts.append(f"[Source {i} — {title} ({c['section']})]\n{c['text']}")
    return "\n\n".join(parts)


def ask(question, paper_ids=None, k=6):
    """The one retrieve-and-generate function every AI feature in Echo calls."""
    chunks = embeddings.query_chunks(question, k=k, paper_ids=paper_ids)
    if not chunks:
        return {"answer": "No indexed content found for this scope yet.", "sources": []}

    titles = get_paper_titles(list({c["paper_id"] for c in chunks}))
    context = build_context(chunks, titles)
    prompt = f"""Answer the question using ONLY the sources below. Cite sources like [Source 1].
If the sources don't contain the answer, say so plainly instead of guessing.

{context}

Question: {question}
Answer:"""

    resp = client.messages.create(model=MODEL, max_tokens=600, messages=[{"role": "user", "content": prompt}])
    return {"answer": resp.content[0].text, "sources": chunks}
