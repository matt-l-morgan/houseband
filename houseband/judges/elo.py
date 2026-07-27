"""Elo ratings for the pairwise tournament.

Standard, zero-sum Elo. It answers one question well: within a round, which of
these takes did the judges prefer.

It deliberately does **not** answer "are the teams getting better". Elo has no
absolute scale, so in a closed pool a round where every team improves produces
almost no rating movement and looks identical to a round where they all held
steady. This module used to pin a human reference at a fixed rating to supply
that yardstick, which worked, but required every run to carry a transcription of
a commercial recording -- and in practice supplied the wrong yardstick, because
the reference was a full song being compared against 16-bar clips.

The absolute measure is the weighted rubric total instead. Those rubrics are
anchored at 2/4/6/8/10 against written descriptors, so a 7 in round one means the
same thing as a 7 in round five without needing anything pinned. Read progress
there and ranking here.
"""

from __future__ import annotations

from typing import Iterable

# Where an unrated competitor starts. Arbitrary, and only differences matter.
DEFAULT_RATING = 1200.0

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
) -> None:
    """Apply one result in place.

    ``outcome`` is from A's point of view: 1.0 A wins, 0.0 B wins, 0.5 draw.

    Both sides are updated from the ratings as they stood *before* this result,
    so the order of the two assignments cannot affect the answer.
    """
    ratings.setdefault(a, DEFAULT_RATING)
    ratings.setdefault(b, DEFAULT_RATING)

    before_a, before_b = ratings[a], ratings[b]
    expected_a = expected(before_a, before_b)

    ratings[a] = before_a + k * (outcome - expected_a)
    ratings[b] = before_b + k * ((1.0 - outcome) - (1.0 - expected_a))


def run_ratings(
    pair_results: Iterable[tuple[str, str, float]],
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
    ratings: dict[str, float] = dict(initial or {})
    for a, b, outcome in sorted(pair_results, key=lambda r: (r[0], r[1])):
        update(ratings, a, b, outcome, k=k)
    return ratings
