"""Selection that rewards variety instead of crowning a winner.

Elo answers "which one is best". For a producer browsing starters, that is the
wrong question, and pursuing it actively destroys the thing they came for.

A rating system converges. Pairwise comparison plus Elo takes six takes and
returns an ordering, the coach learns from the top of that ordering, and next
round every composer writes something closer to whatever won. Two rounds of that
and the six takes are six variations on one take. The ranking is still correct;
it is just measuring a pool that no longer has anything in it. That is not a
tuning problem, it is what a scalar objective does to a population, and it is why
this module exists alongside :mod:`houseband.judges.elo` rather than replacing
it. Ranking remains the right tool for "is the panel calibrated" and "did the
agents improve". It is the wrong tool for "what do we hand the producer".

So selection here optimises coverage. Six clips that are all decent and pull in
six different directions beat one clip that a judge ranked first, because the
producer's own taste is the ranking function and we do not have access to it. The
best we can do is span the space and let them choose.

Two design decisions carry the module.

**Descriptors are computed from the score, deterministically, with no LLM.** An
LLM asked how similar two pieces are will answer plausibly and unrepeatably, and
this number has to be stable enough to compare across sessions. Tempo, density,
subdivision profile, pitch-class entropy, register, track count, velocity and
syncopation are all arithmetic over the notes, so the same MIDI always yields the
same vector and a distance of 0.31 means the same thing in June as in December.

**Normalisation is against fixed ranges, not against the batch.** Min-max
normalising over the candidates in hand would be more sensitive within a round
and would destroy comparability between rounds: a tempo of 90 would read as 0.0
in one batch and 0.7 in another, every existing niche label would move when a new
candidate arrived, and :func:`niche_of` could not be used to ask whether this
week's run explored anywhere last week's did not. Fixed ranges cost some
resolution and buy a coordinate system.
"""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Iterable, Mapping, Sequence

from houseband.score_text import load_view
from houseband.types import Candidate, CandidateVerdict

# ---------------------------------------------------------------------------
# Descriptor scales
# ---------------------------------------------------------------------------

# Fixed ranges for the descriptors that are not already proportions. Each maps a
# musical quantity onto 0-1 by clipping, chosen to put ordinary music in the
# middle of the range rather than bunched at an end.
TEMPO_RANGE = (40.0, 200.0)          # downtempo ambient to drum and bass
DENSITY_RANGE = (0.0, 24.0)          # notes per bar summed over all tracks
REGISTER_SPAN_RANGE = (0.0, 60.0)    # semitones, so five octaves reads as 1.0
REGISTER_CENTROID_RANGE = (24.0, 96.0)  # C1 to C7 covers anything usable
TRACK_COUNT_RANGE = (1.0, 8.0)       # a solo part to a full arrangement
VELOCITY_SPREAD_RANGE = (0.0, 32.0)  # standard deviation in velocity units

# How far off an exact sixteenth-note position an onset may sit and still count
# as intending that position, in beats. Squeezed from both sides. Humanised parts
# are displaced by two or three hundredths of a beat on purpose, and treating
# that as a different subdivision would make the profile a measure of
# humanisation rather than of the grid, so the tolerance has to be at least
# ~0.03. A triplet eighth sits at 0.333, which is 0.083 from the nearest
# sixteenth at 0.25, so anything above 0.042 starts calling triplets sixteenths
# and loses the straight-versus-swung distinction that matters most. 0.04 is the
# only place both constraints hold.
GRID_TOLERANCE = 0.04

# The descriptor space, and how much each axis counts towards distance.
#
# The four ``grid_*`` keys are one descriptor split four ways: they are
# proportions of the same total and always sum to 1, so giving each a full weight
# would let the subdivision profile outvote everything else. A quarter each means
# the profile as a whole carries the weight of one axis.
#
# ``syncopation`` is by construction the complement of ``grid_beat``, and it
# keeps its own full weight anyway. The two are not redundant in the way the
# arithmetic suggests: the profile says *how* a piece is off the beat (eighths,
# sixteenths, or loose) and syncopation says *how much*, and how much is the axis
# a producer hears first and the axis :func:`niche_of` buckets on.
DESCRIPTOR_WEIGHTS: dict[str, float] = {
    "tempo": 1.0,
    "density": 1.0,
    "grid_beat": 0.25,
    "grid_eighth": 0.25,
    "grid_sixteenth": 0.25,
    "grid_off": 0.25,
    "syncopation": 1.0,
    "pitch_entropy": 1.0,
    "register_span": 1.0,
    "register_centroid": 1.0,
    "track_count": 1.0,
    "velocity_mean": 1.0,
    "velocity_spread": 1.0,
}

DESCRIPTOR_KEYS: tuple[str, ...] = tuple(DESCRIPTOR_WEIGHTS)


def _scale(value: float, bounds: tuple[float, float]) -> float:
    low, high = bounds
    if high <= low:
        return 0.0
    return min(1.0, max(0.0, (value - low) / (high - low)))


# ---------------------------------------------------------------------------
# Descriptors
# ---------------------------------------------------------------------------


def descriptors(candidate: Candidate) -> dict[str, float]:
    """Normalised descriptor vector for one candidate, straight from its MIDI.

    Every value lands in 0-1 and every key in :data:`DESCRIPTOR_KEYS` is always
    present, so callers never have to defend against a missing axis.

    Deliberately per-bar and proportional throughout rather than absolute, which
    makes the vector invariant to length: the same eight bars pasted to fill
    thirty-two describes the same music and must land in the same place, or the
    distance between two candidates would be dominated by how long they happen
    to be. Two clips at the same tempo with the same parts are near-identical
    here even if one is twice as long, which is the correct answer for ideation.

    Drums are excluded from the pitch descriptors, because a kit maps pitch to
    timbre and its "register" is an artefact of the GM key map rather than a
    musical choice. They are included everywhere else: a kit is most of the
    rhythm, most of the density and most of the velocity story.

    A candidate whose MIDI will not parse returns an all-zero vector rather than
    raising. Selection running on five candidates because the sixth is broken is
    a better outcome than a round dying at the selection step, and the broken one
    is already reported by the deterministic gate.
    """
    try:
        view = load_view(candidate.midi_path, candidate.sidecar_path)
    except Exception:  # pretty_midi raises a variety of parse errors
        return {key: 0.0 for key in DESCRIPTOR_KEYS}

    sounding_bars: set[int] = set()
    sounding_tracks: set[str] = set()
    onset_offsets: list[float] = []      # position within the beat, 0-1
    velocities: list[int] = []
    pitches: list[int] = []
    pitch_classes: dict[int, int] = defaultdict(int)
    note_total = 0

    for name, per_bar in view.bars.items():
        is_drum = view.track_meta[name]["is_drum"]
        for bar, notes in per_bar.items():
            if not notes:
                continue
            sounding_bars.add(bar)
            # Counted from the notes rather than from the track list, because a
            # declared track that never plays is not part of the texture.
            sounding_tracks.add(name)
            for note in notes:
                note_total += 1
                velocities.append(note.velocity)
                # ``beat`` is 1-indexed the way musicians count, so beat 1 is
                # the downbeat and the fractional part is the offset into the
                # beat that the subdivision profile is about.
                onset_offsets.append((note.beat - 1.0) % 1.0)
                if not is_drum:
                    pitches.append(note.pitch)
                    pitch_classes[note.pitch % 12] += 1

    if note_total == 0:
        return {key: 0.0 for key in DESCRIPTOR_KEYS}

    # Tempo averaged over the bars that actually sound, so a clip with a coda at
    # half speed reads as somewhere between the two rather than as its opening.
    tempos = [view.tempo.bpm_at(bar) for bar in sorted(sounding_bars)]
    tempo = sum(tempos) / len(tempos)

    density = note_total / len(sounding_bars)

    profile = _subdivision_profile(onset_offsets)

    values: dict[str, float] = {
        "tempo": _scale(tempo, TEMPO_RANGE),
        "density": _scale(density, DENSITY_RANGE),
        **profile,
        # Off the beat by any amount, which is what a listener registers as push.
        "syncopation": 1.0 - profile["grid_beat"],
        "pitch_entropy": _pitch_class_entropy(pitch_classes),
        "register_span": _scale(
            (max(pitches) - min(pitches)) if pitches else 0.0, REGISTER_SPAN_RANGE
        ),
        "register_centroid": _scale(
            (sum(pitches) / len(pitches)) if pitches else 0.0, REGISTER_CENTROID_RANGE
        ),
        "track_count": _scale(float(len(sounding_tracks)), TRACK_COUNT_RANGE),
        "velocity_mean": _scale(sum(velocities) / len(velocities), (0.0, 127.0)),
        "velocity_spread": _scale(_stdev(velocities), VELOCITY_SPREAD_RANGE),
    }
    return {key: values[key] for key in DESCRIPTOR_KEYS}


def _subdivision_profile(offsets: Sequence[float]) -> dict[str, float]:
    """Proportion of onsets on the beat, the eighth, the sixteenth, or off grid.

    ``offsets`` are positions within a beat, in 0-1. Each is snapped to the
    nearest sixteenth; anything further than :data:`GRID_TOLERANCE` from every
    sixteenth is ``grid_off``, which is where triplets, swung eighths and heavily
    displaced playing land. Lumping those three together is coarse on purpose:
    for telling one clip apart from another, "not on the straight grid" is the
    distinction that carries, and separating a triplet from a swing feel would
    need a confidence model that buys nothing here.
    """
    counts = {"grid_beat": 0, "grid_eighth": 0, "grid_sixteenth": 0, "grid_off": 0}
    for offset in offsets:
        # Work in sixteenths so the wrap is free: an onset at 0.97 rounds to the
        # fourth sixteenth, which is the *next* beat, and 4 % 4 is 0.
        sixteenths = offset * 4.0
        nearest = round(sixteenths)
        if abs(sixteenths - nearest) / 4.0 > GRID_TOLERANCE:
            counts["grid_off"] += 1
            continue
        step = int(nearest) % 4
        if step == 0:
            counts["grid_beat"] += 1
        elif step == 2:
            counts["grid_eighth"] += 1
        else:
            counts["grid_sixteenth"] += 1
    total = len(offsets) or 1
    return {key: value / total for key, value in counts.items()}


def _pitch_class_entropy(counts: Mapping[int, int]) -> float:
    """Shannon entropy of the pitch-class histogram, scaled to 0-1.

    Divided by log2(12) so a fully chromatic piece reads 1.0 and a one-note drone
    reads 0.0. This separates a modal two-chord loop from something chromatic
    without caring which key either is in, and transposition leaves it untouched.
    """
    total = sum(counts.values())
    if total <= 0:
        return 0.0
    entropy = 0.0
    for count in counts.values():
        if count <= 0:
            continue
        p = count / total
        entropy -= p * math.log2(p)
    return min(1.0, entropy / math.log2(12))


def _stdev(values: Sequence[int]) -> float:
    """Population standard deviation, tolerant of a single value.

    Population rather than sample because these are not a sample of anything:
    the notes in the clip are the whole population, and ``statistics.stdev``
    raising on a one-note track would be a crash in a descriptor.
    """
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    return math.sqrt(sum((v - mean) ** 2 for v in values) / len(values))


# ---------------------------------------------------------------------------
# Distance
# ---------------------------------------------------------------------------

# Largest distance the metric can return, used to normalise into 0-1 so that
# thresholds like min_quality's cousin "these two are basically the same clip"
# can be stated as readable numbers.
_MAX_DISTANCE = math.sqrt(sum(DESCRIPTOR_WEIGHTS.values()))


def distance(a: Mapping[str, float], b: Mapping[str, float]) -> float:
    """Weighted Euclidean distance between two descriptor vectors, in 0-1.

    Euclidean rather than cosine because the descriptors are absolute positions
    on fixed scales, not a direction: a slow sparse clip and a fast dense one
    differ in magnitude on every axis, and cosine would call them similar for
    pointing the same way. Scaled by the largest achievable distance so 0 is
    identical and 1 is opposite on every axis, which makes the numbers legible
    in a report.
    """
    total = 0.0
    for key, weight in DESCRIPTOR_WEIGHTS.items():
        delta = a.get(key, 0.0) - b.get(key, 0.0)
        total += weight * delta * delta
    return math.sqrt(total) / _MAX_DISTANCE


def diversity_matrix(candidates: list[Candidate]) -> dict[tuple[str, str], float]:
    """Pairwise distances, keyed by ``(candidate_id, candidate_id)``.

    Symmetric and stored both ways round, so a caller never has to remember which
    order the key was built in. Self-pairs are omitted rather than stored as
    zero: a distance from a candidate to itself is not a fact about the round,
    and leaving it out means iterating the matrix gives exactly the pairs worth
    looking at. Descriptors are computed once per candidate, not once per pair.
    """
    vectors = {c.candidate_id: descriptors(c) for c in candidates}
    ids = list(vectors)
    matrix: dict[tuple[str, str], float] = {}
    for i, left in enumerate(ids):
        for right in ids[i + 1 :]:
            value = distance(vectors[left], vectors[right])
            matrix[(left, right)] = value
            matrix[(right, left)] = value
    return matrix


def mean_distance(candidates: list[Candidate]) -> float:
    """Average pairwise distance across a round. The round's spread, in one number.

    Worth logging every round: it is the number that shows the pool collapsing,
    and collapse is invisible in the scores (a converged pool scores well) and
    invisible in the Elo ratings (a converged pool ranks cleanly).
    """
    matrix = diversity_matrix(candidates)
    if not matrix:
        return 0.0
    # Every unordered pair appears twice, which does not affect a mean.
    return sum(matrix.values()) / len(matrix)


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------


def select_varied(
    candidates: list[Candidate],
    verdicts: Mapping[str, CandidateVerdict],
    k: int,
    min_quality: float = 4.0,
) -> list[str]:
    """Pick up to ``k`` candidate ids that are each decent and mutually distant.

    Quality is a floor, not the objective. ``min_quality`` is checked against
    ``CandidateVerdict.weighted_total`` (which is mode-aware, so a starter is
    measured on starter weights) and everything at or above it is treated as
    equally eligible from then on. That is the whole point: past "a producer
    could use this", we have no basis for preferring a 7.4 to a 6.9 over the
    producer's own taste, and pretending we do is how the pool collapses.

    Greedy farthest-point selection: seed with the best-scoring eligible
    candidate, then repeatedly add whichever eligible candidate is farthest from
    everything already chosen. The greedy answer is not the optimal k-subset, and
    the optimal one is not worth computing here. This is a 2-approximation of the
    max-min-distance objective, k is six and the pool is six, and every extra
    unit of cleverness would be spent on a metric that is itself an
    approximation of taste.

    A candidate with no verdict is excluded. Its quality is unknown, and shipping
    an unjudged clip to a producer on the grounds that it is unusual is the one
    failure mode this function must not have.

    Returns ids in selection order, so a caller that wants fewer than it asked
    for can truncate and keep the most-different ones.
    """
    if k <= 0:
        return []

    eligible = [
        c
        for c in candidates
        if c.candidate_id in verdicts
        and verdicts[c.candidate_id].weighted_total >= min_quality
    ]
    if not eligible:
        return []

    vectors = {c.candidate_id: descriptors(c) for c in eligible}

    def quality(candidate_id: str) -> float:
        return verdicts[candidate_id].weighted_total

    # Ordered best-first with the id as the tie-break, so a rerun on the same
    # round returns the same set in the same order. Without it, two clips with
    # identical scores and identical descriptors would be separated by whatever
    # order the dict happened to be in.
    remaining = sorted(
        (c.candidate_id for c in eligible), key=lambda cid: (-quality(cid), cid)
    )
    selected = [remaining.pop(0)]

    while remaining and len(selected) < k:
        best_id = remaining[0]
        best_key = (-1.0, 0.0)
        for candidate_id in remaining:
            spread = min(
                distance(vectors[candidate_id], vectors[chosen]) for chosen in selected
            )
            key = (spread, quality(candidate_id))
            # Strictly greater, so a tie goes to whichever came first in
            # ``remaining`` -- that is, the higher score and then the lower id.
            if key > best_key:
                best_key = key
                best_id = candidate_id
        remaining.remove(best_id)
        selected.append(best_id)

    return selected


# ---------------------------------------------------------------------------
# Niches
# ---------------------------------------------------------------------------

# Coarse buckets over three axes a producer would actually name. Thresholds are
# absolute, so a niche label means the same thing in every run and two sessions'
# coverage can be compared directly: "three rounds and nobody has produced a
# sparse high-energy clip" is a question you can only ask if the label is stable.
#
# This is MAP-Elites in embryo. The full idea keeps one best-performing
# individual per behavioural niche and lets each niche improve independently,
# which is what stops a population from collapsing onto one behaviour. Here we
# only label the niches; keeping an archive per niche across rounds is the
# obvious next step and needs somewhere durable to put it.
# Thresholds are on the normalised descriptors, so the musical readings are:
# density sparse below about 6.5 notes per bar summed over every track (whole-note
# pads and a bass note) and dense above about 13 (sixteenth hats and a busy
# arrangement); syncopation straight when at least 70 percent of onsets land on a
# beat, and syncopated when fewer than 40 percent do.
ENERGY_BUCKETS = ((0.34, "low"), (0.62, "mid"), (1.01, "high"))
DENSITY_BUCKETS = ((0.27, "sparse"), (0.55, "medium"), (1.01, "dense"))
SYNCOPATION_BUCKETS = ((0.30, "straight"), (0.60, "loose"), (1.01, "syncopated"))


def _bucket(value: float, buckets: tuple[tuple[float, str], ...]) -> str:
    for ceiling, label in buckets:
        if value < ceiling:
            return label
    return buckets[-1][1]


def niche_of(candidate: Candidate) -> tuple[str, ...]:
    """Coarse behavioural bucket: energy, density, syncopation.

    Three axes and three levels each gives 27 cells, which is the right order of
    magnitude for rounds of six candidates: fine enough that two clips in one
    cell really are interchangeable ideas, coarse enough that a run has a
    realistic chance of revisiting a cell and improving on it.

    Energy combines tempo and mean velocity because neither alone is what a
    producer means by energy: a fast quiet clip and a slow hard-hitting one are
    both mid-energy, and both are distinct from a fast loud one.
    """
    vector = descriptors(candidate)
    energy = (vector["tempo"] + vector["velocity_mean"]) / 2.0
    return (
        f"energy:{_bucket(energy, ENERGY_BUCKETS)}",
        f"density:{_bucket(vector['density'], DENSITY_BUCKETS)}",
        f"sync:{_bucket(vector['syncopation'], SYNCOPATION_BUCKETS)}",
    )


def niche_coverage(candidates: Iterable[Candidate]) -> dict[tuple[str, ...], list[str]]:
    """Which niches this round landed in, and which candidates landed there.

    Several candidates sharing a niche is the signal that a round explored less
    than its candidate count suggests, and it is worth reading before the scores.
    """
    coverage: dict[tuple[str, ...], list[str]] = {}
    for candidate in candidates:
        coverage.setdefault(niche_of(candidate), []).append(candidate.candidate_id)
    return coverage
