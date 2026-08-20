from . import db, embeddings, rag

# Beyond this distance, the "closest" paper isn't actually related to the idea —
# it's just the least-far option among unrelated papers. Embedding similarity
# search always returns *something*, so without this cutoff, completely
# unrelated ideas (e.g. "I want to make a bottle of water") get confidently
# matched and given fabricated-sounding guidance instead of an honest
# "nothing relevant found" response.
NO_MATCH_THRESHOLD = 1.3


def match_idea(idea_text, library_ids, user_id, k=6):
    """Real embedding-similarity matching (not keyword overlap) against the
    user's library, then a second RAG call for concrete guidance."""
    if not library_ids:
        return None

    chunks = embeddings.query_chunks(idea_text, k=k, paper_ids=library_ids, user_id=user_id)
    if not chunks:
        return None

    scores = {}
    for c in chunks:
        scores.setdefault(c["paper_id"], []).append(c["distance"])
    best_id = min(scores, key=lambda pid: sum(scores[pid]) / len(scores[pid]))
    avg_distance = sum(scores[best_id]) / len(scores[best_id])

    if avg_distance > NO_MATCH_THRESHOLD:
        return {
            "error": (
                "No sufficiently related paper found in your library for this idea. "
                "Try rephrasing it, being more specific, or adding papers on this topic first."
            )
        }

    if avg_distance < 0.6:
        novelty = "Low novelty — closely overlaps with existing work in your library"
    elif avg_distance < 1.0:
        novelty = "Medium novelty — partial overlap found"
    else:
        novelty = "High novelty — related, but little direct overlap found in your library"

    guidance_question = (
        f'A researcher proposes this idea: "{idea_text}". '
        "Based on this paper's research gap and limitations, give exactly 3 concise, "
        "concrete, numbered suggestions for how they could build on or extend it."
    )
    guidance = rag.ask(guidance_question, paper_ids=[best_id], user_id=user_id, k=4)

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