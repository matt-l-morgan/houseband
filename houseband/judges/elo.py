"""Elo ratings for the pairwise tournament.

Standard Elo, with one deliberate departure: the human reference is **pinned**.
Its rating never updates, no matter how many comparisons it wins or loses.

That matters because Elo is a zero-sum system with no absolute scale. Let every
competitor float and the whole pool drifts: a round where every team improves
produces almost no rating movement, and a round where every team regresses looks
identical to one where they all held steady. Pinning one competitor at a known
value turns the pool's ratings into measurements against a fixed yardstick, so
1350 means the same thing in round one and round five, and "how far below the
reference" becomes a number worth reporting.

The cost is that ratings no longer sum to a constant. That is the correct trade:
we want a scale, not a closed economy.
"""

from __future__ import annotations

from typing import Iterable

# Where an unrated competitor starts. The absolute value is arbitrary; the gap
# to REFERENCE_RATING is what carries meaning.
DEFAULT_RATING = 1200.0

# The pinned reference. 400 points above the default is one full Elo "class":
# a competitor at DEFAULT_RATING is expected to beat it about 9 percent of the
# time, which is roughly where machine-composed music starts against a human
# arrangement and leaves plenty of room to climb.
REFERENCE_RATING = 1600.0

DEFAULT_K = 32.0


def expected(rating_a: float, rating_b: float) -> float:
    """Probability that A beats B under the logistic Elo model."""
    return 1.0 / (1.0 + 10.0 ** ((rating_b - rating_a) / 400.0))


def update(
    ratings: dict[str, float],
    a: str,
    b: str,
    outcome: float,
    k: float = DEFAULT_K,
    pinned: set[str] | None = None,
) -> None:
    """Apply one result in place.

    ``outcome`` is from A's point of view: 1.0 A wins, 0.0 B wins, 0.5 draw.

    Both sides are updated from the ratings as they stood *before* this result,
    so the order of the two assignments cannot affect the answer. Pinned
    competitors are skipped entirely: the opponent still moves by the full
    amount, which is exactly what "measured against a fixed yardstick" means.
    """
    pinned = pinned or set()
    ratings.setdefault(a, REFERENCE_RATING if a in pinned else DEFAULT_RATING)
    ratings.setdefault(b, REFERENCE_RATING if b in pinned else DEFAULT_RATING)

    before_a, before_b = ratings[a], ratings[b]
    expected_a = expected(before_a, before_b)

    if a not in pinned:
        ratings[a] = before_a + k * (outcome - expected_a)
    if b not in pinned:
        ratings[b] = before_b + k * ((1.0 - outcome) - (1.0 - expected_a))


def run_ratings(
    pair_results: Iterable[tuple[str, str, float]],
    pinned: set[str] | None = None,
    initial: dict[str, float] | None = None,
    k: float = DEFAULT_K,
) -> dict[str, float]:
    """Rate a whole tournament from its results.

    ``pair_results`` is an iterable of ``(a_id, b_id, outcome)`` triples, where
    ``outcome`` is from A's point of view as in :func:`update`.

    Elo is order-dependent, and the comparisons that produce these results run
    concurrently, so the results are sorted by ``(a_id, b_id)`` before being
    applied. Python's sort is stable, so repeated comparisons of the same pair
    keep their original order. Without this, rerunning a round on the same
    verdicts could produce different ratings purely from thread scheduling,
    which would make round-over-round movement unreadable.
    """
    pinned = pinned or set()
    ratings: dict[str, float] = dict(initial or {})
    for name in pinned:
        ratings.setdefault(name, REFERENCE_RATING)

    for a, b, outcome in sorted(pair_results, key=lambda r: (r[0], r[1])):
        update(ratings, a, b, outcome, k=k, pinned=pinned)
    return ratings
