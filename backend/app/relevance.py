"""Single source of truth for relevance thresholds and labels, used by every
feature that ranks papers/sections by embedding distance (analyze, contribute,
whole-library chat). Keeping this in one place prevents the Python and
JavaScript sides from drifting out of sync with different magic numbers.

Thresholds assume COSINE distance (range 0-2, 1.0 = orthogonal/unrelated).
This only holds true if the ChromaDB collection is explicitly configured
with metadata={"hnsw:space": "cosine"} — see embeddings.py.
"""

HIGH_RELEVANCE_THRESHOLD = 0.6   # below this: strong topical match
RELEVANT_THRESHOLD = 1.0         # below this: meaningful but partial match
                                  # at/above this: essentially unrelated


def distance_to_label(distance):
    """Returns (label_text, badge_css_class) for a given distance, or for
    None (no matching content found at all)."""
    if distance is None:
        return "No evidence found", "low"
    if distance < HIGH_RELEVANCE_THRESHOLD:
        return "Highly relevant", "reviewed"
    if distance < RELEVANT_THRESHOLD:
        return "Relevant", "preprint"
    return "Loosely relevant", "low"


def is_covered(distance, threshold=RELEVANT_THRESHOLD):
    return distance is not None and distance < threshold