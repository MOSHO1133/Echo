from . import db, rag

FIELD_QUERIES = {
    "methodology": "What method, model, or approach does this paper use? Answer in 1-2 sentences.",
    "findings": "What are the key results and findings of this paper? Answer in 1-2 sentences.",
    "research_gap": (
        "Based only on this paper's own stated limitations and future work, what is left "
        "untested, unresolved, or missing? Phrase it as a single research gap in 1-2 sentences."
    ),
    "future_work": "What future work or next steps does this paper's own text suggest? Answer in 1-2 sentences.",
}


def summarize_paper(paper_id):
    result = {}
    for field, question in FIELD_QUERIES.items():
        try:
            out = rag.ask(question, paper_ids=[paper_id], k=4)
            result[field] = out["answer"]
        except Exception as e:
            result[field] = f"Could not generate: {e}"

    conn = db.get_conn()
    conn.execute(
        "UPDATE papers SET methodology=?, findings=?, research_gap=?, future_work=? WHERE id=?",
        (result["methodology"], result["findings"], result["research_gap"], result["future_work"], paper_id),
    )
    conn.commit()
    conn.close()
    return result