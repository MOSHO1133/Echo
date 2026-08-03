from . import db, embeddings, rag


def match_idea(idea_text, library_ids, k=6):
    """Real embedding-similarity matching (not keyword overlap) against the
    user's library, then a second RAG call for concrete guidance."""
    if not library_ids:
        return None

    chunks = embeddings.query_chunks(idea_text, k=k, paper_ids=library_ids)
    if not chunks:
        return None

    scores = {}
    for c in chunks:
        scores.setdefault(c["paper_id"], []).append(c["distance"])
    best_id = min(scores, key=lambda pid: sum(scores[pid]) / len(scores[pid]))
    avg_distance = sum(scores[best_id]) / len(scores[best_id])

    if avg_distance < 0.6:
        novelty = "Low novelty — closely overlaps with existing work in your library"
    elif avg_distance < 1.0:
        novelty = "Medium novelty — partial overlap found"
    else:
        novelty = "High novelty — little overlap found in your library"

    guidance_question = (
        f'A researcher proposes this idea: "{idea_text}". '
        "Based on this paper's research gap and limitations, give exactly 3 concise, "
        "concrete, numbered suggestions for how they could build on or extend it."
    )
    guidance = rag.ask(guidance_question, paper_ids=[best_id], k=4)

    conn = db.get_conn()
    row = conn.execute("SELECT title FROM papers WHERE id=?", (best_id,)).fetchone()
    conn.close()

    return {
        "paper_id": best_id,
        "title": row["title"] if row else best_id,
        "novelty": novelty,
        "avg_distance": avg_distance,
        "guidance": guidance["answer"],
    }
