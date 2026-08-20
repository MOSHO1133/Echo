import json
import re

from . import db, embeddings, rag

SUMMARY_PROMPT = """You are analyzing a research paper using only the source excerpts below.
Answer all four fields, each in 1-2 sentences, using ONLY information from the sources.
If something isn't stated in the sources, say so plainly instead of guessing.

{context}

Respond with ONLY a JSON object (no markdown fences, no extra text) in exactly this shape:
{{
  "methodology": "What method, model, or approach does this paper use?",
  "findings": "What are the key results and findings of this paper?",
  "research_gap": "Based only on this paper's own stated limitations and future work, what is left untested, unresolved, or missing?",
  "future_work": "What future work or next steps does this paper's own text suggest?"
}}
"""


def _parse_json_response(text):
    """Gemini sometimes wraps JSON in ```json fences despite instructions — strip those first."""
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.MULTILINE)
    return json.loads(cleaned)


def _extract_field_fallback(text, field):
    """If the JSON as a whole doesn't parse, try to pull just this one field out
    via regex instead of dumping the same broken blob into every field."""
    pattern = rf'"{field}"\s*:\s*"((?:[^"\\]|\\.)*)"'
    match = re.search(pattern, text, re.DOTALL)
    if match:
        return match.group(1).replace('\\"', '"').replace("\\n", " ")
    return "Could not parse model response for this field."


def summarize_paper(paper_id, user_id):
    chunks = embeddings.query_chunks(
        "methodology findings limitations future work research gap",
        k=6,
        paper_ids=[paper_id],
        user_id=user_id,
    )

    if not chunks:
        result = {k: "Could not generate: no indexed content found for this paper yet." for k in
                   ("methodology", "findings", "research_gap", "future_work")}
    else:
        titles = rag.get_paper_titles([paper_id])
        context = rag.build_context(chunks, titles)
        prompt = SUMMARY_PROMPT.format(context=context)
        raw = None
        try:
            raw = rag.call_llm(prompt, max_tokens=2048)
            parsed = _parse_json_response(raw)
            result = {
                "methodology": parsed.get("methodology", "Not found in sources."),
                "findings": parsed.get("findings", "Not found in sources."),
                "research_gap": parsed.get("research_gap", "Not found in sources."),
                "future_work": parsed.get("future_work", "Not found in sources."),
            }
        except json.JSONDecodeError:
            result = {k: _extract_field_fallback(raw or "", k) for k in
                       ("methodology", "findings", "research_gap", "future_work")}
        except Exception as e:
            error_msg = f"Could not generate: {e}"
            result = {k: error_msg for k in ("methodology", "findings", "research_gap", "future_work")}

    conn = db.get_conn()
    conn.execute(
        "UPDATE papers SET methodology=?, findings=?, research_gap=?, future_work=? WHERE id=?",
        (result["methodology"], result["findings"], result["research_gap"], result["future_work"], paper_id),
    )
    conn.commit()
    conn.close()
    return result