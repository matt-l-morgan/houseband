"""Tests for starter-mode judging.

The properties here are the ones that would otherwise only show up as a bad
score in a paid run:

* every dimension either mode judges has a rubric file on disk
* a starter is never judged on form and arrangement, whose anchors a loop cannot
  reach, and is judged on loop usability and headroom instead
* long-form judging is byte-for-byte what it was before modes existed
* the weighted total is computed against the weights of the mode the verdict was
  actually scored under, including after a round-trip through the run log

No API key exists in this environment and none is needed: the panel takes an
injected client, so these drive the real prompt construction and dimension
routing against the same stub pattern as ``tests/test_judges.py``.
"""

from __future__ import annotations

import pretty_midi
import pytest

from houseband import config as cfg
from houseband.judges import rubric
from houseband.types import (
    DIMENSION_TITLES,
    DIMENSION_WEIGHTS,
    DIMENSIONS,
    DIMENSIONS_FOR_MODE,
    LONGFORM_DIMENSIONS,
    MODES,
    STARTER_DIMENSIONS,
    STARTER_WEIGHTS,
    WEIGHTS_FOR_MODE,
    Brief,
    Candidate,
    CandidateVerdict,
    DimensionVerdict,
    ScoredDimension,
)

# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


class StubUsage:
    def __init__(self) -> None:
        self.input_tokens = 1200
        self.output_tokens = 300
        self.cache_creation_input_tokens = 0
        self.cache_read_input_tokens = 1100


class StubResponse:
    def __init__(self, parsed):
        self.parsed_output = parsed
        self.usage = StubUsage()


class StubMessages:
    """Answers every call with the same verdict, recording what it was asked."""

    def __init__(self, score: int = 6):
        self.score = score
        self.calls: list[dict] = []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        return StubResponse(
            DimensionVerdict(score=self.score, rationale="scripted", findings=[])
        )


class StubClient:
    def __init__(self, score: int = 6):
        self.messages = StubMessages(score)

    @property
    def calls(self) -> list[dict]:
        return self.messages.calls


class StubConfig:
    model = "claude-opus-5"


BRIEF = Brief(
    prompt="A 16-bar dub techno starter at 124 BPM.",
    genre="dub techno",
    mood="hypnotic",
    tempo_hint="124 BPM",
    instrumentation=["drums", "bass", "chord stab"],
)

CRITERIA = "The clip must loop cleanly and leave the vocal register open."


def _midi(path):
    midi = pretty_midi.PrettyMIDI(initial_tempo=124.0)
    bass = pretty_midi.Instrument(program=33, name="bass")
    for i in range(16):
        bass.notes.append(
            pretty_midi.Note(velocity=88, pitch=40 + (i % 3), start=i * 0.5, end=i * 0.5 + 0.4)
        )
    midi.instruments.append(bass)
    midi.write(str(path))
    return path


@pytest.fixture
def candidate(tmp_path) -> Candidate:
    return Candidate(
        candidate_id="c1",
        team="crate",
        midi_path=_midi(tmp_path / "c1.mid"),
        score_text="KEY A minor   TIME 4/4   BPM 124   BARS 16   LENGTH 0:31",
    )


# ---------------------------------------------------------------------------
# Rubric coverage
# ---------------------------------------------------------------------------


def test_every_dimension_of_every_mode_has_a_rubric_on_disk():
    """The check that would otherwise fail partway into a paid run."""
    for mode in MODES:
        assert rubric.missing_rubrics_for_mode(mode) == [], mode
    # And the underlying primitive still covers the union, so a dimension added
    # to one mode and not the other cannot slip through.
    union = tuple(sorted(set(STARTER_DIMENSIONS) | set(LONGFORM_DIMENSIONS)))
    assert rubric.missing_rubrics(union) == []


@pytest.mark.parametrize("dimension", ("loop_usability", "headroom"))
def test_new_rubrics_match_the_house_format(dimension):
    text = rubric.load_rubric(dimension)
    for anchor in ("**2 =", "**4 =", "**6 =", "**8 =", "**10 ="):
        assert anchor in text, f"{dimension} is missing the {anchor} anchor"
    assert "## Anchored scale" in text
    assert "## Between the anchors" in text
    assert "## Reading the evidence" in text
    assert "## What a finding needs" in text
    assert "bar_start" in text
    # Anchors are worthless if the judge cannot tie them to the score text it is
    # given, so each rubric must name the blocks it reads.
    assert "NOTES" in text
    assert "TRACKS" in text
    # House style, enforced because it is easy to violate by accident.
    assert "—" not in text, "em dash in rubric prose"


def test_loop_rubric_disarms_the_long_form_repetition_instinct():
    """A loop that repeats is doing its job, and the judge has to be told so.

    Without this the loop_usability judge imports the form rubric's argument (a
    piece whose bars repeat verbatim is capped at 4) and marks every competent
    clip down for the one property that makes it a clip.
    """
    text = rubric.load_rubric("loop_usability")
    assert "REPETITION" in text
    assert "High repetition is correct here" in text
    assert "not** a penalty on this dimension" in text
    assert "form and arrangement" in text


def test_headroom_rubric_says_finished_is_worse_than_spacious():
    text = rubric.load_rubric("headroom")
    assert "fully arranged and fully mixed scores worse" in text
    # Both failure directions, so the judge does not read "leave space" as
    # "hand in less" and reward an empty clip.
    assert "Empty is not the same as spacious" in text
    assert "delete" in text  # separability, the part producers actually exercise


# ---------------------------------------------------------------------------
# Dimension sets
# ---------------------------------------------------------------------------


def test_starter_drops_form_and_adds_the_two_starter_dimensions():
    assert "form_arrangement" not in STARTER_DIMENSIONS
    assert "loop_usability" in STARTER_DIMENSIONS
    assert "headroom" in STARTER_DIMENSIONS
    # Everything else long-form judges is still judged.
    kept = set(DIMENSIONS) - {"form_arrangement"}
    assert kept <= set(STARTER_DIMENSIONS)
    assert set(STARTER_DIMENSIONS) - kept == {"loop_usability", "headroom"}


def test_longform_is_exactly_what_it_was():
    assert LONGFORM_DIMENSIONS == DIMENSIONS
    assert DIMENSIONS_FOR_MODE["longform"] == DIMENSIONS
    assert WEIGHTS_FOR_MODE["longform"] is DIMENSION_WEIGHTS
    assert "loop_usability" not in DIMENSIONS
    assert "headroom" not in DIMENSIONS


def test_every_mode_is_fully_titled_and_fully_weighted():
    for mode in MODES:
        dimensions = DIMENSIONS_FOR_MODE[mode]
        weights = WEIGHTS_FOR_MODE[mode]
        assert set(dimensions) <= set(DIMENSION_TITLES), mode
        # No dimension may fall back to the implicit weight of 1.0: a silent
        # default is how a dimension ends up mattering more or less than anyone
        # intended.
        assert set(dimensions) == set(weights), mode
        assert all(w > 0 for w in weights.values()), mode
        assert len(set(dimensions)) == len(dimensions), mode


def test_groove_and_loop_usability_carry_the_most_weight_in_a_starter():
    """For a producer starter the groove is the product, so it outranks melody."""
    heaviest = max(STARTER_WEIGHTS.values())
    assert STARTER_WEIGHTS["rhythm_groove"] == heaviest
    assert STARTER_WEIGHTS["loop_usability"] == heaviest
    assert STARTER_WEIGHTS["rhythm_groove"] > STARTER_WEIGHTS["melody"]
    assert STARTER_WEIGHTS["rhythm_groove"] > DIMENSION_WEIGHTS["rhythm_groove"]
    # The producer writes the topline, so a starter's melody weighs less than a
    # long-form piece's.
    assert STARTER_WEIGHTS["melody"] < DIMENSION_WEIGHTS["melody"]


# ---------------------------------------------------------------------------
# The panel
# ---------------------------------------------------------------------------


def _expected_calls(dimensions: tuple[str, ...]) -> int:
    return sum(
        cfg.MEDIAN_SAMPLES if d in cfg.MEDIAN_SAMPLED_DIMENSIONS else 1
        for d in dimensions
    )


def test_run_panel_in_starter_mode_judges_exactly_the_starter_dimensions(candidate):
    client = StubClient()

    verdicts = rubric.run_panel(
        [candidate],
        BRIEF,
        CRITERIA,
        client=client,
        config=StubConfig(),
        mode="starter",
    )

    verdict = verdicts["c1"]
    assert [d.dimension for d in verdict.dimensions] == list(STARTER_DIMENSIONS)
    assert verdict.mode == "starter"
    assert verdict.score("form_arrangement") is None
    assert verdict.score("loop_usability") == 6
    assert verdict.score("headroom") == 6
    assert len(client.calls) == _expected_calls(STARTER_DIMENSIONS)

    # No call may mention form: the dimension is not merely dropped from the
    # output, it is never asked about, so it costs nothing and cannot leak the
    # long-form anchors into a starter's findings.
    rendered = repr(client.calls)
    assert "form_arrangement" not in rendered
    assert "Form and arrangement" not in rendered


def test_run_panel_defaults_to_longform(candidate):
    client = StubClient()

    verdicts = rubric.run_panel(
        [candidate], BRIEF, CRITERIA, client=client, config=StubConfig()
    )

    verdict = verdicts["c1"]
    assert [d.dimension for d in verdict.dimensions] == list(DIMENSIONS)
    assert verdict.mode == "longform"
    assert verdict.score("form_arrangement") == 6
    assert len(client.calls) == _expected_calls(DIMENSIONS)


def test_explicit_dimensions_still_win_over_the_mode(candidate):
    """A caller isolating one dimension is not overridden by the mode default."""
    client = StubClient()

    verdict = rubric.judge_candidate(
        candidate,
        BRIEF,
        CRITERIA,
        dimensions=("loop_usability",),
        client=client,
        config=StubConfig(),
        mode="starter",
    )

    assert [d.dimension for d in verdict.dimensions] == ["loop_usability"]
    assert len(client.calls) == 1


def test_the_starter_rubric_reaches_the_prompt_and_the_cache_prefix():
    blocks = rubric.build_system_prompt("loop_usability", BRIEF, CRITERIA)

    assert blocks[-1].get("cache_control") == {"type": "ephemeral"}
    assert not any("cache_control" in b for b in blocks[:-1])
    assert "Loop usability" in blocks[-1]["text"]
    assert "Anchored scale" in blocks[-1]["text"]


# ---------------------------------------------------------------------------
# Mode-aware totals
# ---------------------------------------------------------------------------

# Deliberately lopsided: a strong groove and a weak melody, which is exactly the
# shape the two modes are supposed to disagree about.
SHARED_SCORES = {
    "prompt_adherence": 5,
    "melody": 3,
    "harmony_voice_leading": 5,
    "rhythm_groove": 9,
    "orchestration_register": 5,
    "production": 5,
    "originality": 5,
}


def _verdict(mode: str, scores: dict[str, int]) -> CandidateVerdict:
    return CandidateVerdict(
        candidate_id="c1",
        team="crate",
        mode=mode,
        dimensions=[
            ScoredDimension(dimension=d, score=s, rationale="scripted")
            for d, s in scores.items()
        ],
    )


def test_weighted_total_reweights_the_same_scores_by_mode():
    starter = _verdict("starter", SHARED_SCORES)
    longform = _verdict("longform", SHARED_SCORES)

    # Hand-computed so a weight change has to be acknowledged here rather than
    # silently rewriting the expectation.
    assert longform.weighted_total == pytest.approx(39.0 / 7.5)
    assert starter.weighted_total == pytest.approx(45.5 / 8.0)
    # A great groove under a thin melody is a good starter and a mediocre piece.
    assert starter.weighted_total > longform.weighted_total


def test_the_two_modes_rank_the_same_two_candidates_differently():
    """The point of mode-aware weights, stated as the disagreement they cause.

    Same scores, opposite emphases: a clip that grooves and barely sings against
    one that sings and barely grooves. Whichever wins depends on what is being
    delivered, and that is the whole reason the weights are not shared.
    """
    grooves = dict(SHARED_SCORES)  # melody 3, rhythm_groove 9
    sings = {**SHARED_SCORES, "melody": 9, "rhythm_groove": 3}

    assert (
        _verdict("starter", grooves).weighted_total
        > _verdict("starter", sings).weighted_total
    )
    assert (
        _verdict("longform", sings).weighted_total
        > _verdict("longform", grooves).weighted_total
    )


def test_weights_property_names_the_table_in_use():
    assert _verdict("starter", SHARED_SCORES).weights is STARTER_WEIGHTS
    assert _verdict("longform", SHARED_SCORES).weights is DIMENSION_WEIGHTS


def test_an_unlabelled_verdict_is_scored_as_longform():
    """Verdicts written before modes existed must deserialise unchanged."""
    verdict = CandidateVerdict.model_validate(
        {
            "candidate_id": "c1",
            "team": "crate",
            "dimensions": [
                {"dimension": d, "score": s, "rationale": "scripted"}
                for d, s in SHARED_SCORES.items()
            ],
        }
    )

    assert verdict.mode == "longform"
    assert verdict.weighted_total == pytest.approx(39.0 / 7.5)


def test_mode_survives_the_run_log_round_trip():
    """The reporter and the coach read verdicts back from disk, not from memory."""
    original = _verdict("starter", SHARED_SCORES)

    restored = CandidateVerdict.model_validate_json(original.model_dump_json())

    assert restored.mode == "starter"
    assert restored.weighted_total == pytest.approx(original.weighted_total)


def test_a_starter_is_not_penalised_for_having_no_form_dimension():
    """Weighting over present dimensions only, so a missing form is not a zero."""
    starter = _verdict("starter", {d: 6 for d in STARTER_DIMENSIONS})
    longform = _verdict("longform", {d: 6 for d in DIMENSIONS})

    assert starter.weighted_total == pytest.approx(6.0)
    assert longform.weighted_total == pytest.approx(6.0)
