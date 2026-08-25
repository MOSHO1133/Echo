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

The frontend NEVER hardcodes these numbers — it fetches them once from
GET /config/relevance and reads them off the response. This file is the
only place a threshold value is allowed to be written literally.
"""

HIGH_RELEVANCE_THRESHOLD = 0.45  # below this: strong topical match
RELEVANT_THRESHOLD = 0.75        # below this: meaningful but partial match
                                  # at/above this: essentially unrelated
NO_MATCH_THRESHOLD = 0.80        # used by contribute.py to reject ideas with
                                  # no genuinely related paper in the library

# Anchor points mapping cosine distance -> an intuitive 0-100 "relevance
# score" for display, calibrated so the score's own tier boundaries land
# exactly on HIGH_RELEVANCE_THRESHOLD / RELEVANT_THRESHOLD. This converts
# "lower distance = better" (confusing to read at a glance) into "higher
# score = better" everywhere in the UI, while staying mathematically tied
# to the same thresholds that drive the color tiers — a score and a color
# can never disagree, by construction.
#   score >= 75  ->  always "Highly relevant" (teal)
#   40 <= score < 75  ->  always "Relevant" (amber)
#   score < 40  ->  always "Loosely relevant" (red)
_SCORE_ANCHORS = [
    (0.0, 100.0),
    (HIGH_RELEVANCE_THRESHOLD, 75.0),
    (RELEVANT_THRESHOLD, 40.0),
    (1.0, 0.0),
]


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


def distance_to_score(distance):
    """0-100 display score, higher = better match, piecewise-linear between
    _SCORE_ANCHORS. Returns None (not 0) when distance is None, so the
    frontend can distinguish "no evidence at all" from "found evidence but
    it scored 0"."""
    if distance is None:
        return None
    d = max(0.0, min(distance, 1.0))
    for (d0, s0), (d1, s1) in zip(_SCORE_ANCHORS, _SCORE_ANCHORS[1:]):
        if d0 <= d <= d1:
            frac = (d - d0) / (d1 - d0) if d1 != d0 else 0.0
            return round(s0 + frac * (s1 - s0))
    return 0


def label_pair(distance):
    """label + css_class + score as a dict, for easy ** unpacking into API
    response objects (coverage cells, heatmap cells, etc.). Every derived
    number in this dict traces back to the same distance value, so they
    can never contradict each other on the frontend."""
    label, css_class = distance_to_label(distance)
    return {"label": label, "css_class": css_class, "score": distance_to_score(distance)}


def is_covered(distance, threshold=RELEVANT_THRESHOLD):
    return distance is not None and distance < threshold


def config_payload():
    """Serialized for GET /config/relevance so the frontend can render
    correct legends/captions without ever hardcoding a threshold value."""
    return {
        "high_relevance_threshold": HIGH_RELEVANCE_THRESHOLD,
        "relevant_threshold": RELEVANT_THRESHOLD,
        "no_match_threshold": NO_MATCH_THRESHOLD,
        "labels": {
            "reviewed": "Highly relevant",
            "preprint": "Relevant",
            "low": "Loosely relevant / No evidence found",
        },
        "distance_metric": "cosine",
        "lower_is_closer": True,
        "score_note": "Display score is 0-100, higher is better. >=75 highly relevant, 40-74 relevant, <40 loosely relevant.",
    }