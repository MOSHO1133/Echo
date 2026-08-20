import json
import re

from . import embeddings, rag, relevance

# Canonicalizes processing.py's raw SECTION_HEADS (and its "body" fallback)
# into clean display categories. Kept in sync with app/processing.py's
# SECTION_HEADS list — if that list changes, update this mapping too.
CATEGORY_MAP = {
    "abstract": "Abstract",
    "introduction": "Introduction",
    "related work": "Related Work",
    "background": "Background",
    "methodology": "Methodology", "method": "Methodology", "methods": "Methodology",
    "dataset": "Dataset", "data": "Dataset",
    "results": "Findings", "experiments": "Findings", "evaluation": "Findings",
    "discussion": "Discussion",
    "limitations": "Limitations",
    "conclusion": "Conclusion", "conclusions": "Conclusion",
    "future work": "Future Work",
    "references": "References",
    "body": "Other",
}

PER_PAPER_K = 10  # chunks pulled per paper when building the section matrix


def canonical_category(raw_section):
    return CATEGORY_MAP.get(raw_section, raw_section.title() if raw_section else "Other")


def _labeled(distance):
    text, css_class = relevance.distance_to_label(distance)
    return {"distance": distance, "label": text, "css_class": css_class}


def _decompose_question(question):
    """One LLM call: breaks the question into short sub-topics. Returns []
    on any failure (malformed JSON, API error) rather than raising, so a
    decomposition failure never breaks the rest of the analysis."""
    prompt = (
        'Break this research question into 3-5 short, distinct sub-topics '
        '(a few words each) that a paper would need to address to fully answer it.\n\n'
        f'Question: "{question}"\n\n'
        'Respond with ONLY a JSON array of strings, no markdown, no extra text. '
        'Example: ["topic one", "topic two", "topic three"]'
    )
    try:
        raw = rag.call_llm(prompt, max_tokens=300)
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.MULTILINE)
        parsed = json.loads(cleaned)
        if isinstance(parsed, list):
            return [str(x).strip() for x in parsed if str(x).strip()][:5]
    except Exception:
        pass
    return []


def analyze_library(question, owned_papers, user_id):
    """owned_papers: list of dicts with at least 'id' and 'title', already
    verified as belonging to user_id by the caller (main.py).

    Returns per-paper overall ranking (with server-computed labels), a
    per-section leaderboard, and sub-topic coverage — all threshold
    decisions made here via relevance.py, never re-derived in the frontend.
    """
    if not owned_papers:
        return {
            "ranked_overall": [], "paper_section_scores": {}, "section_leaders": {},
            "subtopics": [], "coverage": {},
        }

    q_emb = embeddings.encode_query(question)

    paper_section_scores = {}  # paper_id -> {category: {distance, label, css_class}}
    overall_paper_scores = {}  # paper_id -> best (lowest) distance across all its sections

    for p in owned_papers:
        chunks = embeddings.query_chunks_by_vector(q_emb, k=PER_PAPER_K, paper_ids=[p["id"]], user_id=user_id)
        cat_min_dist = {}
        for c in chunks:
            cat = canonical_category(c["section"])
            if cat not in cat_min_dist or c["distance"] < cat_min_dist[cat]:
                cat_min_dist[cat] = c["distance"]
        paper_section_scores[p["id"]] = {cat: _labeled(dist) for cat, dist in cat_min_dist.items()}
        overall_paper_scores[p["id"]] = min(cat_min_dist.values()) if cat_min_dist else None

    ranked_overall = [
        {"paper_id": pid, **_labeled(dist)}
        for pid, dist in sorted(
            [(pid, dist) for pid, dist in overall_paper_scores.items() if dist is not None],
            key=lambda x: x[1],
        )
    ]

    all_categories = sorted({cat for scores in paper_section_scores.values() for cat in scores})
    section_leaders = {}
    for cat in all_categories:
        candidates = [
            {"paper_id": pid, **paper_section_scores[pid][cat]}
            for pid in paper_section_scores if cat in paper_section_scores[pid]
        ]
        candidates.sort(key=lambda x: x["distance"])
        section_leaders[cat] = candidates

    subtopics = _decompose_question(question)
    coverage = {}
    if subtopics:
        owned_ids = [p["id"] for p in owned_papers]
        for st in subtopics:
            try:
                st_emb = embeddings.encode_query(st)
                st_chunks = embeddings.query_chunks_by_vector(
                    st_emb, k=max(6, len(owned_ids) * 3), paper_ids=owned_ids, user_id=user_id
                )
                best_per_paper = {}
                for c in st_chunks:
                    pid = c["paper_id"]
                    if pid not in best_per_paper or c["distance"] < best_per_paper[pid]:
                        best_per_paper[pid] = c["distance"]
                coverage[st] = {
                    pid: {"distance": best_per_paper.get(pid), "covered": relevance.is_covered(best_per_paper.get(pid))}
                    for pid in owned_ids
                }
            except Exception:
                coverage[st] = {pid: {"distance": None, "covered": False} for pid in owned_ids}

    return {
        "ranked_overall": ranked_overall,
        "paper_section_scores": paper_section_scores,
        "section_leaders": section_leaders,
        "subtopics": subtopics,
        "coverage": coverage,
    }


def library_diversity(owned_papers):
    """Heuristic 0-100 diversity score from the average pairwise embedding
    distance between papers' title+methodology+findings text. Higher = less
    redundant / more topically varied library.

    This is an approximate signal for guiding your own judgment, not a
    precise or validated scientific metric — treat the label, not the exact
    number, as the useful part.
    """
    if len(owned_papers) < 2:
        return {"score": None, "label": "Add at least 2 papers to measure diversity", "pair_count": 0}

    model = embeddings.get_model()
    texts = [
        f"{p.get('title', '')}. {p.get('methodology') or ''} {p.get('findings') or ''}".strip()
        for p in owned_papers
    ]
    vectors = model.encode(texts, normalize_embeddings=True)

    n = len(vectors)
    dists = []
    for i in range(n):
        for j in range(i + 1, n):
            a, b = vectors[i], vectors[j]
            cos_sim = float(sum(x * y for x, y in zip(a, b)))  # vectors are already unit-normalized
            dists.append(1 - cos_sim)

    avg_dist = sum(dists) / len(dists) if dists else 0
    score = max(0, min(100, round(avg_dist / 1.2 * 100)))

    if score >= 65:
        label = "High diversity — broad coverage, low redundancy"
    elif score >= 35:
        label = "Moderate diversity — some topical overlap"
    else:
        label = "Low diversity — your papers are very similar to each other"

    return {"score": score, "label": label, "pair_count": len(dists)}