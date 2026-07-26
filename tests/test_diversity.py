"""Tests for diversity-based selection.

Everything here is arithmetic over real MIDI written by ``houseband.house.Score``,
with no client and no key: the whole point of computing descriptors from the score
rather than asking a judge is that the numbers are reproducible, so the tests can
assert on them directly.

The properties under test are the ones that make the selection trustworthy:

* the same score always produces the same vector, and length does not change it
* two obviously different clips are far apart and two near-identical ones are not
* selection returns k ids, refuses candidates below the quality floor, and picks
  the outlier over a third copy of the same idea
* a broken MIDI degrades to a zero vector instead of taking the round down
"""

from __future__ import annotations

import pytest

from houseband.house import Score
from houseband.judges import diversity
from houseband.types import Candidate, CandidateVerdict, ScoredDimension

# ---------------------------------------------------------------------------
# Fixtures: two clips that could not be confused for each other
# ---------------------------------------------------------------------------


def _ambient(path, bars: int = 16, velocity: int = 48):
    """A slow, sparse, dead-straight pad-and-bass loop."""
    score = Score(bpm=68, key="Am")
    score.mark_section("loop", start_bar=0, bars=bars)
    pad = score.track("pad", patch="warm_pad", pan=-0.2)
    bass = score.track("bass", patch="fingered_bass")
    for bar in range(bars):
        pad.chord(bar=bar, beat=1, symbol="Am9", dur=4.0, vel=velocity, octave=3)
        bass.note(bar=bar, beat=1, pitch="A1", dur=4.0, vel=velocity + 4)
    score.write(str(path))
    return path


def _breakbeat(path, bars: int = 16):
    """A fast, dense, heavily syncopated drums-bass-stab loop."""
    score = Score(bpm=172, key="F#m")
    score.mark_section("loop", start_bar=0, bars=bars)
    drums = score.drum_track("drums")
    bass = score.track("bass", patch="picked_bass")
    stab = score.track("stab", patch="clean_guitar", pan=0.3)
    for bar in range(bars):
        for i in range(16):
            drums.hit(bar, 1 + i * 0.25, "hat", vel=58 + (i % 4) * 10)
        drums.hit(bar, 1, "kick", vel=122)
        drums.hit(bar, 2.75, "kick", vel=112)
        drums.hit(bar, 2, "snare", vel=118)
        drums.hit(bar, 4.5, "snare", vel=101)
        for i, beat in enumerate((1, 1.75, 2.5, 3.25, 4.25)):
            bass.note(bar, beat, 42 + (i * 3) % 8, 0.4, vel=95 + i)
        stab.chord(bar, 3.5, symbol="F#m7", dur=0.4, vel=105, octave=4)
    score.write(str(path))
    return path


def _candidate(candidate_id: str, path) -> Candidate:
    return Candidate(candidate_id=candidate_id, team="crate", midi_path=path)


@pytest.fixture
def ambient(tmp_path) -> Candidate:
    return _candidate("amb", _ambient(tmp_path / "ambient.mid"))


@pytest.fixture
def breakbeat(tmp_path) -> Candidate:
    return _candidate("brk", _breakbeat(tmp_path / "breakbeat.mid"))


def _verdict(candidate_id: str, total: int) -> CandidateVerdict:
    """A starter verdict whose weighted total is exactly ``total``.

    Every dimension at the same score, so the weights cannot move the mean and
    the fixture says what it means.
    """
    from houseband.types import STARTER_DIMENSIONS

    return CandidateVerdict(
        candidate_id=candidate_id,
        team="crate",
        mode="starter",
        dimensions=[
            ScoredDimension(dimension=d, score=total, rationale="scripted")
            for d in STARTER_DIMENSIONS
        ],
    )


# ---------------------------------------------------------------------------
# Descriptors
# ---------------------------------------------------------------------------


def test_every_descriptor_is_present_and_normalised(ambient, breakbeat):
    for candidate in (ambient, breakbeat):
        vector = diversity.descriptors(candidate)
        assert tuple(vector) == diversity.DESCRIPTOR_KEYS
        assert all(0.0 <= v <= 1.0 for v in vector.values()), vector


def test_descriptors_are_deterministic(tmp_path, ambient):
    """Twice on the same file, and once more on an identical file written apart."""
    twin = _candidate("twin", _ambient(tmp_path / "twin.mid"))

    assert diversity.descriptors(ambient) == diversity.descriptors(ambient)
    assert diversity.descriptors(ambient) == diversity.descriptors(twin)
    assert diversity.distance(
        diversity.descriptors(ambient), diversity.descriptors(twin)
    ) == pytest.approx(0.0)


def test_descriptors_are_invariant_to_loop_length(tmp_path):
    """The same eight bars pasted to fill thirty-two is the same idea.

    If descriptors scaled with length, distance between candidates would be
    dominated by how long each one happens to be, and selection would return the
    shortest and the longest clip rather than the two most different ones.
    """
    short = _candidate("s", _ambient(tmp_path / "short.mid", bars=8))
    long = _candidate("l", _ambient(tmp_path / "long.mid", bars=32))

    assert diversity.descriptors(short) == diversity.descriptors(long)
    assert diversity.niche_of(short) == diversity.niche_of(long)


def test_descriptors_do_change_with_the_music(ambient, breakbeat):
    slow, fast = diversity.descriptors(ambient), diversity.descriptors(breakbeat)

    assert fast["tempo"] > slow["tempo"]
    assert fast["density"] > slow["density"]
    assert fast["syncopation"] > slow["syncopation"]
    assert fast["velocity_mean"] > slow["velocity_mean"]
    assert fast["velocity_spread"] > slow["velocity_spread"]
    assert fast["track_count"] > slow["track_count"]
    # The pad-and-bass loop puts every onset on the downbeat, so it is entirely
    # on the beat and not syncopated at all.
    assert slow["grid_beat"] == pytest.approx(1.0)
    assert slow["syncopation"] == pytest.approx(0.0)


def test_triplets_are_not_mistaken_for_sixteenths(tmp_path):
    """The tolerance that separates a swung feel from a straight one.

    A triplet eighth sits 0.083 beats from the nearest sixteenth, which is close
    enough that a loose grid tolerance would file every triplet as a straight
    sixteenth and make the subdivision profile blind to the one rhythmic
    distinction a producer would name first.
    """
    score = Score(bpm=120, key="C")
    keys = score.track("keys", patch="electric_piano")
    for bar in range(4):
        for beat in (1.0, 1 + 1 / 3, 1 + 2 / 3, 2.0, 2 + 1 / 3, 2 + 2 / 3):
            keys.note(bar=bar, beat=beat, pitch="C4", dur=0.3, vel=80)
    score.write(str(tmp_path / "triplets.mid"))

    vector = diversity.descriptors(_candidate("trip", tmp_path / "triplets.mid"))

    assert vector["grid_sixteenth"] == pytest.approx(0.0)
    # Two of every three onsets are off the straight grid.
    assert vector["grid_off"] == pytest.approx(2 / 3, abs=0.01)
    assert vector["grid_beat"] == pytest.approx(1 / 3, abs=0.01)


def test_humanised_timing_still_counts_as_the_grid_it_intends(tmp_path):
    """Nudging a part by a few hundredths of a beat is feel, not a new subdivision."""
    score = Score(bpm=120, key="C")
    keys = score.track("keys", patch="electric_piano")
    for bar in range(4):
        for beat in (1.02, 2.03, 2.97, 4.01):
            keys.note(bar=bar, beat=beat, pitch="E4", dur=0.4, vel=80)
    score.write(str(tmp_path / "human.mid"))

    vector = diversity.descriptors(_candidate("hum", tmp_path / "human.mid"))

    assert vector["grid_beat"] == pytest.approx(1.0)
    assert vector["grid_off"] == pytest.approx(0.0)


def test_transposition_leaves_pitch_entropy_alone(tmp_path):
    """Entropy is about how much of the chromatic set is used, not which notes."""

    def build(path, offset: int):
        score = Score(bpm=100, key="C")
        lead = score.track("lead", patch="square_lead")
        for bar in range(4):
            for i, step in enumerate((0, 2, 4, 7)):
                lead.note(bar=bar, beat=1 + i, pitch=60 + offset + step, dur=0.9, vel=80)
        score.write(str(path))
        return path

    home = _candidate("home", build(tmp_path / "home.mid", 0))
    up = _candidate("up", build(tmp_path / "up.mid", 5))

    assert diversity.descriptors(home)["pitch_entropy"] == pytest.approx(
        diversity.descriptors(up)["pitch_entropy"]
    )


def test_an_unparseable_score_is_a_zero_vector_not_an_exception(tmp_path):
    """Selection must survive one broken candidate; the gate already reports it."""
    broken = tmp_path / "broken.mid"
    broken.write_text("this is not a MIDI file")

    vector = diversity.descriptors(_candidate("bad", broken))

    assert vector == {key: 0.0 for key in diversity.DESCRIPTOR_KEYS}


# ---------------------------------------------------------------------------
# Distance
# ---------------------------------------------------------------------------


def test_obviously_different_clips_are_far_apart(ambient, breakbeat):
    far = diversity.distance(
        diversity.descriptors(ambient), diversity.descriptors(breakbeat)
    )
    assert far > 0.3


def test_near_identical_clips_are_close(tmp_path, ambient):
    """One velocity point apart is the same idea and must read as one."""
    nudged = _candidate("nudge", _ambient(tmp_path / "nudged.mid", velocity=49))

    close = diversity.distance(
        diversity.descriptors(ambient), diversity.descriptors(nudged)
    )
    far = diversity.distance(
        diversity.descriptors(ambient), diversity.descriptors(_breakbeat(tmp_path / "b.mid"))
    )

    assert close < 0.02
    assert close < far / 10


def test_distance_is_symmetric_and_zero_on_itself(ambient, breakbeat):
    a, b = diversity.descriptors(ambient), diversity.descriptors(breakbeat)

    assert diversity.distance(a, b) == pytest.approx(diversity.distance(b, a))
    assert diversity.distance(a, a) == pytest.approx(0.0)


def test_diversity_matrix_holds_both_orders_and_no_self_pairs(tmp_path, ambient, breakbeat):
    middle = _candidate("mid", _ambient(tmp_path / "mid.mid", velocity=90))
    candidates = [ambient, breakbeat, middle]

    matrix = diversity.diversity_matrix(candidates)

    assert len(matrix) == 6  # three unordered pairs, stored both ways
    assert ("amb", "amb") not in matrix
    for left, right in (("amb", "brk"), ("amb", "mid"), ("brk", "mid")):
        assert matrix[(left, right)] == pytest.approx(matrix[(right, left)])
    assert diversity.mean_distance(candidates) == pytest.approx(
        sum(matrix.values()) / len(matrix)
    )


def test_mean_distance_of_a_single_candidate_is_zero(ambient):
    assert diversity.mean_distance([ambient]) == 0.0


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------


@pytest.fixture
def three_clones_and_an_outlier(tmp_path) -> list[Candidate]:
    """The case Elo gets wrong: a crowded idea plus one different one."""
    return [
        _candidate("d1", _ambient(tmp_path / "d1.mid", velocity=48)),
        _candidate("d2", _ambient(tmp_path / "d2.mid", velocity=49)),
        _candidate("d3", _ambient(tmp_path / "d3.mid", velocity=50)),
        _candidate("out", _breakbeat(tmp_path / "out.mid")),
    ]


def test_select_varied_prefers_the_outlier_over_a_third_copy(three_clones_and_an_outlier):
    """Two picks from three near-duplicates and an outlier must include the outlier.

    The duplicates score higher, so a ranking would return two of them and the
    producer would be handed the same idea twice.
    """
    verdicts = {
        "d1": _verdict("d1", 7),
        "d2": _verdict("d2", 7),
        "d3": _verdict("d3", 7),
        "out": _verdict("out", 6),
    }

    chosen = diversity.select_varied(three_clones_and_an_outlier, verdicts, k=2)

    assert len(chosen) == 2
    assert "out" in chosen
    assert len([c for c in chosen if c.startswith("d")]) == 1
    # Seeded with the best-scoring candidate, then the farthest from it.
    assert chosen == ["d1", "out"]


def test_select_varied_returns_k_ids_and_is_deterministic(three_clones_and_an_outlier):
    verdicts = {c.candidate_id: _verdict(c.candidate_id, 7) for c in three_clones_and_an_outlier}

    chosen = diversity.select_varied(three_clones_and_an_outlier, verdicts, k=3)
    again = diversity.select_varied(
        list(reversed(three_clones_and_an_outlier)), verdicts, k=3
    )

    assert len(chosen) == 3
    assert len(set(chosen)) == 3
    assert set(chosen) <= {"d1", "d2", "d3", "out"}
    # The outlier is picked second, before the pool is exhausted of duplicates.
    assert chosen[1] == "out"
    # Input order must not change the answer, or a round could not be reproduced.
    assert chosen == again


def test_select_varied_respects_the_quality_floor(three_clones_and_an_outlier):
    """A clip nobody could use is not worth handing over for being unusual."""
    verdicts = {
        "d1": _verdict("d1", 7),
        "d2": _verdict("d2", 6),
        "d3": _verdict("d3", 5),
        "out": _verdict("out", 3),  # below the floor, and the only different idea
    }

    chosen = diversity.select_varied(
        three_clones_and_an_outlier, verdicts, k=3, min_quality=4.0
    )

    assert "out" not in chosen
    assert set(chosen) == {"d1", "d2", "d3"}

    # Lower the bar and it comes back.
    assert "out" in diversity.select_varied(
        three_clones_and_an_outlier, verdicts, k=2, min_quality=2.0
    )


def test_select_varied_ignores_unjudged_candidates(three_clones_and_an_outlier):
    verdicts = {"d1": _verdict("d1", 7), "d2": _verdict("d2", 7)}

    chosen = diversity.select_varied(three_clones_and_an_outlier, verdicts, k=4)

    assert set(chosen) == {"d1", "d2"}


def test_select_varied_returns_what_it_has_when_k_exceeds_the_pool(ambient, breakbeat):
    verdicts = {"amb": _verdict("amb", 6), "brk": _verdict("brk", 6)}

    assert set(diversity.select_varied([ambient, breakbeat], verdicts, k=6)) == {
        "amb",
        "brk",
    }


def test_select_varied_degenerate_inputs(ambient):
    verdicts = {"amb": _verdict("amb", 6)}

    assert diversity.select_varied([ambient], verdicts, k=0) == []
    assert diversity.select_varied([], verdicts, k=3) == []
    assert diversity.select_varied([ambient], verdicts, k=3, min_quality=9.5) == []


def test_select_varied_uses_the_verdicts_own_mode_for_quality(ambient):
    """Quality is read through weighted_total, so a starter is scored as one.

    A verdict carrying starter weights and one carrying long-form weights over
    the same dimensions do not produce the same total, and the floor has to be
    applied to whichever the candidate was actually judged as.
    """
    lopsided = {
        "prompt_adherence": 5,
        "melody": 2,
        "harmony_voice_leading": 5,
        "rhythm_groove": 9,
        "loop_usability": 8,
        "orchestration_register": 5,
        "headroom": 6,
        "production": 5,
        "originality": 5,
    }
    verdict = CandidateVerdict(
        candidate_id="amb",
        team="crate",
        mode="starter",
        dimensions=[
            ScoredDimension(dimension=d, score=s, rationale="scripted")
            for d, s in lopsided.items()
        ],
    )

    assert verdict.weighted_total > 5.0
    assert diversity.select_varied([ambient], {"amb": verdict}, k=1) == ["amb"]
    assert diversity.select_varied(
        [ambient], {"amb": verdict}, k=1, min_quality=verdict.weighted_total + 0.1
    ) == []


# ---------------------------------------------------------------------------
# Niches
# ---------------------------------------------------------------------------


def test_niches_separate_the_two_extremes(ambient, breakbeat):
    assert diversity.niche_of(ambient) == ("energy:low", "density:sparse", "sync:straight")
    assert diversity.niche_of(breakbeat) == (
        "energy:high",
        "density:dense",
        "sync:syncopated",
    )


def test_near_identical_clips_share_a_niche(tmp_path, ambient):
    twin = _candidate("twin", _ambient(tmp_path / "twin.mid", velocity=50))

    assert diversity.niche_of(twin) == diversity.niche_of(ambient)


def test_niche_labels_are_absolute_not_relative_to_the_batch(tmp_path, ambient, breakbeat):
    """The property that lets two sessions be compared at all.

    A niche computed from one candidate must not depend on which other candidates
    happened to be in the round, or "nobody has explored sparse high-energy yet"
    is not a question that can be asked across runs.
    """
    alone = diversity.niche_of(ambient)
    crowd = [ambient, breakbeat, _candidate("x", _ambient(tmp_path / "x.mid", velocity=120))]

    assert diversity.niche_of(ambient) == alone
    coverage = diversity.niche_coverage(crowd)
    assert coverage[alone] == ["amb"]
    assert len(coverage) == 3  # three candidates, three distinct niches


def test_niche_coverage_groups_duplicates(three_clones_and_an_outlier):
    """Several candidates in one cell is a round that explored less than it looks."""
    coverage = diversity.niche_coverage(three_clones_and_an_outlier)

    assert len(coverage) == 2
    crowded = max(coverage.values(), key=len)
    assert crowded == ["d1", "d2", "d3"]
