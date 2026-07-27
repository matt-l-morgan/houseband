"""Tests for the judged dimension set and its weighting.

This replaces an earlier mode-aware test file. The tool used to support two
deliverables, a long-form piece and a short clip, with separate dimension sets
and weights threaded through every layer. That turned out to be complexity in
service of a job nobody was asking for, so there is now exactly one deliverable
and one dimension set, and these tests pin the properties that matter about it.

The properties worth pinning are not "the numbers are these numbers", which would
just restate the source. They are the *relationships* that encode the product
decision: the groove outranks the melody, form is deliberately absent, and every
dimension we claim to judge has a rubric on disk to judge it against.
"""

from __future__ import annotations

import pytest

from houseband.judges import rubric
from houseband.types import (
    DIMENSION_TITLES,
    DIMENSION_WEIGHTS,
    DIMENSIONS,
    CandidateVerdict,
    ScoredDimension,
)


class TestDimensionSet:
    def test_every_dimension_has_a_rubric_on_disk(self):
        """The panel cannot judge a dimension it has no anchored scale for."""
        assert rubric.missing_rubrics() == []

    def test_every_dimension_has_a_title_and_a_weight(self):
        for dimension in DIMENSIONS:
            assert dimension in DIMENSION_TITLES, dimension
            assert dimension in DIMENSION_WEIGHTS, dimension

    def test_no_weight_or_title_is_orphaned(self):
        """A weight for a dimension nobody judges is dead config that will mislead."""
        assert set(DIMENSION_WEIGHTS) == set(DIMENSIONS)
        assert set(DIMENSION_TITLES) == set(DIMENSIONS)

    def test_the_clip_specific_dimensions_are_present(self):
        assert "loop_usability" in DIMENSIONS
        assert "headroom" in DIMENSIONS

    def test_form_arrangement_is_deliberately_absent(self):
        """A 16-bar loop has one section by definition.

        The form rubric rewards a climax in the final third and sections whose
        material contrasts, so a clip would score 2 on it no matter how good it
        was, and a composer reading that finding would be coached into ruining
        the thing that made it useful.
        """
        assert "form_arrangement" not in DIMENSIONS
        assert "form_arrangement" not in DIMENSION_WEIGHTS

    def test_no_rubric_file_is_left_behind_unused(self):
        """A stale rubric on disk is a trap for whoever reads the directory next."""
        on_disk = {path.stem for path in rubric.RUBRIC_DIR.glob("*.md")}
        assert on_disk == set(DIMENSIONS), on_disk.symmetric_difference(DIMENSIONS)


class TestWeighting:
    def test_groove_and_loop_usability_carry_the_most(self):
        """The groove is the product.

        A producer decides on a clip by dropping it on a timeline and nodding or
        not, within about two bars. A great progression over a stiff groove gets
        deleted; an ordinary progression over a groove that moves gets kept.
        """
        top = max(DIMENSION_WEIGHTS.values())
        assert DIMENSION_WEIGHTS["rhythm_groove"] == top
        assert DIMENSION_WEIGHTS["loop_usability"] == top

    def test_melody_is_weighted_below_the_groove(self):
        """The one weight that looks wrong and is the most deliberate.

        The producer supplies the topline. A fully-formed melody competes for the
        register and attention a vocal needs, and is the first thing deleted.
        """
        assert DIMENSION_WEIGHTS["melody"] < DIMENSION_WEIGHTS["rhythm_groove"]
        assert DIMENSION_WEIGHTS["melody"] < DIMENSION_WEIGHTS["harmony_voice_leading"]

    def test_headroom_does_not_outrank_the_groove(self):
        """Space is necessary and cheap to fake.

        Weighting it above the groove would reward a composer for handing in less.
        """
        assert DIMENSION_WEIGHTS["headroom"] < DIMENSION_WEIGHTS["rhythm_groove"]

    def test_prompt_adherence_stays_high(self):
        """Asked for dub techno at 132 and given lo-fi hip hop is not a near miss."""
        assert DIMENSION_WEIGHTS["prompt_adherence"] >= 1.5


class TestWeightedTotal:
    def _verdict(self, scores: dict[str, int]) -> CandidateVerdict:
        return CandidateVerdict(
            candidate_id="c1",
            team="crate",
            dimensions=[
                ScoredDimension(dimension=d, score=s, rationale="stub")
                for d, s in scores.items()
            ],
        )

    def test_stays_on_the_ten_point_scale(self):
        assert self._verdict({d: 7 for d in DIMENSIONS}).weighted_total == pytest.approx(7.0)
        assert self._verdict({d: 1 for d in DIMENSIONS}).weighted_total == pytest.approx(1.0)
        assert self._verdict({d: 10 for d in DIMENSIONS}).weighted_total == pytest.approx(10.0)

    def test_a_missing_dimension_does_not_drag_the_average_down(self):
        """A judge failure should not read as a low score.

        Only judged dimensions are averaged, so a dropped call leaves the total
        computed over what actually came back rather than scoring the gap as zero.
        """
        partial = self._verdict({"rhythm_groove": 8, "melody": 8})
        assert partial.weighted_total == pytest.approx(8.0)

    def test_groove_moves_the_total_more_than_melody(self):
        """The weighting has to actually bite, not just exist in a table."""
        base = {d: 5 for d in DIMENSIONS}

        groove_up = dict(base, rhythm_groove=10)
        melody_up = dict(base, melody=10)
        assert (
            self._verdict(groove_up).weighted_total
            > self._verdict(melody_up).weighted_total
        )

    def test_empty_verdict_is_zero_not_an_error(self):
        assert self._verdict({}).weighted_total == 0.0


class TestRubricContent:
    """The rubrics are prose loaded at runtime, so a few load-bearing claims about
    them are worth asserting rather than trusting."""

    @pytest.mark.parametrize("dimension", DIMENSIONS)
    def test_each_rubric_has_anchored_descriptors(self, dimension):
        text = rubric.load_rubric(dimension)
        # An anchored scale is the whole point: without explicit descriptors the
        # judge is inventing a number rather than matching a description.
        for anchor in ("2", "4", "6", "8", "10"):
            assert anchor in text, f"{dimension} rubric has no anchor for {anchor}"

    @pytest.mark.parametrize("dimension", DIMENSIONS)
    def test_each_rubric_demands_evidence(self, dimension):
        text = rubric.load_rubric(dimension).lower()
        assert "bar" in text, f"{dimension} rubric never mentions bar anchoring"

    def test_loop_usability_permits_repetition(self):
        """High repetition is correct in a loop and would be a failure in a long
        piece, so the rubric has to say so or the judge imports the wrong instinct."""
        text = rubric.load_rubric("loop_usability").lower()
        assert "repet" in text or "repeat" in text

    def test_headroom_rewards_leaving_space(self):
        text = rubric.load_rubric("headroom").lower()
        assert "space" in text or "room" in text
