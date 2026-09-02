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

# Chunks pulled per paper when building the section matrix. This used to be
# 10, which was far too small: with up to 8 possible section categories, the
# top-10 globally-ranked chunks for a paper frequently didn't include ANY
# chunk from several sections — making those sections render as "no
# matching content" when really they just weren't sampled at all. This is
# now raised substantially so essentially all of a typical paper's chunks
# get considered, which eliminates almost all *artificial* blanks. A blank
# cell after this change is a real signal: that section genuinely has
# little content related to the question. (If a paper has more indexed
# chunks than this number, some undersampling can still theoretically
# happen — for a fully airtight fix, this should be replaced with a
# per-section query, or a k that adapts to each paper's actual chunk count.)
PER_PAPER_K = 60


def canonical_category(raw_section):
    return CATEGORY_MAP.get(raw_section, raw_section.title() if raw_section else "Other")


def _labeled(distance):
    text, css_class = relevance.distance_to_label(distance)
    return {
        "distance": distance,
        "score": relevance.distance_to_score(distance),
        "label": text,
        "css_class": css_class,
    }


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
            "subtopics": [], "coverage": {}, "fit_summary": None,
        }

    q_emb = embeddings.encode_query(question)

    paper_section_scores = {}  # paper_id -> {category: {distance, score, label, css_class}}
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
                    pid: {
                        "distance": best_per_paper.get(pid),
                        "covered": relevance.is_covered(best_per_paper.get(pid)),
                        **relevance.label_pair(best_per_paper.get(pid)),
                    }
                    for pid in owned_ids
                }
            except Exception:
                coverage[st] = {
                    pid: {"distance": None, "covered": False, **relevance.label_pair(None)}
                    for pid in owned_ids
                }

        # Sort so the biggest gaps (fewest papers covering that sub-topic)
        # surface first — otherwise a well-covered sub-topic could bury a
        # genuine gap further down an unsorted list.
        subtopics.sort(key=lambda st: sum(1 for cell in coverage[st].values() if cell["covered"]))

    titles_map = {p["id"]: p["title"] for p in owned_papers}
    fit_summary = _build_fit_summary(ranked_overall, subtopics, coverage, titles_map)

    return {
        "ranked_overall": ranked_overall,
        "paper_section_scores": paper_section_scores,
        "section_leaders": section_leaders,
        "subtopics": subtopics,
        "coverage": coverage,
        "fit_summary": fit_summary,
    }


def _build_fit_summary(ranked_overall, subtopics, coverage, titles_map):
    """One synthesized, plain-English readout of how well the library
    actually answers THIS question. Every phrase is derived from the same
    css_class/label values shown as badges elsewhere on the page, and now
    explicitly accounts for ALL papers (high + relevant + loosely relevant),
    not just the top two tiers — so the sentence never implies papers are
    unaccounted for."""
    if not ranked_overall:
        return None

    high_count = sum(1 for r in ranked_overall if r["css_class"] == "reviewed")
    relevant_count = sum(1 for r in ranked_overall if r["css_class"] == "preprint")
    total = len(ranked_overall)
    low_count = total - high_count - relevant_count

    coverage_pct = None
    if subtopics:
        covered_subtopics = sum(
            1 for st in subtopics if any(cell["covered"] for cell in coverage[st].values())
        )
        coverage_pct = round(covered_subtopics / len(subtopics) * 100)

    weakest = None
    if len(ranked_overall) >= 2:
        weakest_entry = ranked_overall[-1]
        if weakest_entry["css_class"] == "low":  # only flag it if genuinely weak, not just "last but still fine"
            weakest = titles_map.get(weakest_entry["paper_id"], weakest_entry["paper_id"])

    return {
        "high_count": high_count,
        "relevant_count": relevant_count,
        "low_count": low_count,
        "total": total,
        "coverage_pct": coverage_pct,
        "weakest_title": weakest,
        # Server-owned English labels for the tiers referenced in the
        # summary sentence, so the frontend never writes its own synonym
        # that could drift from the badge text.
        "relevant_label": relevance.distance_to_label(0.5)[0],   # "Relevant"
        "high_label": relevance.distance_to_label(0.0)[0],       # "Highly relevant"
        "low_label": relevance.distance_to_label(0.9)[0],        # "Loosely relevant"
    }


def _normalize(vector):
    """Manual L2 normalization — used instead of relying on any particular
    embedding backend's built-in normalization option (the old
    sentence-transformers call used normalize_embeddings=True, which
    fastembed's API doesn't have an equivalent parameter for)."""
    norm = sum(x * x for x in vector) ** 0.5
    return [x / norm for x in vector] if norm else vector


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

    texts = [
        f"{p.get('title', '')}. {p.get('methodology') or ''} {p.get('findings') or ''}".strip()
        for p in owned_papers
    ]
    vectors = [_normalize(v) for v in embeddings.embed_texts(texts)]

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