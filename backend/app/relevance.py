"""Single source of truth for relevance thresholds and labels, used by every
feature that ranks papers/sections by embedding distance (analyze, contribute,
whole-library chat). Keeping this in one place prevents the Python and
JavaScript sides from drifting out of sync with different magic numbers.

Thresholds use COSINE distance (0 = identical, 2 = opposite). IMPORTANT:
these values were empirically calibrated against this app's actual
embedding model (all-MiniLM-L6-v2), NOT assumed from theory. Testing showed
a genuinely unrelated query ("i want to make a boat") scored 0.86-0.94
against real library papers — well below the naive "1.0 = orthogonal"
assumption. This is a known property of sentence-transformer embedding
spaces (anisotropy): unrelated text pairs cluster closer together than a
uniform cosine distribution would suggest. If the embedding model is ever
changed, these thresholds should be re-measured, not assumed.
"""

HIGH_RELEVANCE_THRESHOLD = 0.45  # below this: strong topical match
RELEVANT_THRESHOLD = 0.75        # below this: meaningful but partial match
                                  # at/above this: essentially unrelated
NO_MATCH_THRESHOLD = 0.80        # used by contribute.py to reject ideas with
                                  # no genuinely related paper in the library


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