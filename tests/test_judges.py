"""Tests for the judge panel.

No API key exists in this environment and none should be needed: every judge
entry point takes an injected ``client``, so the tests drive the real prompt
construction, sampling, reconciliation, and error paths against a stub that
returns canned ``parsed_output``. The properties under test are the ones that
would be expensive to discover from a live run:

* median-of-3 reduces to the median and keeps the spread
* a pair whose two presentation orders disagree is recorded as a draw
* Elo is conserved, so a round's ranking is readable
* an unreachable judge produces a neutral verdict instead of killing the round
* the judge is never shown the team name
"""

from __future__ import annotations

import pretty_midi
import pytest

from houseband import config as cfg
from houseband.events import EventLog
from houseband.judges import elo, pairwise, rubric
from houseband.types import (
    DIMENSION_TITLES,
    DIMENSIONS,
    Brief,
    Candidate,
    CandidateVerdict,
    DimensionVerdict,
    Finding,
    PairwiseVerdict,
    ScoredDimension,
)

# One judged candidate costs one call per dimension, except the median-sampled
# ones which cost cfg.MEDIAN_SAMPLES each. Derived rather than hardcoded so that
# adding a dimension or changing the sampling does not silently invalidate the
# test that is meant to pin the call count.
CALLS_PER_CANDIDATE = len(DIMENSIONS) + len(cfg.MEDIAN_SAMPLED_DIMENSIONS) * (
    cfg.MEDIAN_SAMPLES - 1
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
    """Records every call and replays a scripted sequence of parsed outputs."""

    def __init__(self, outputs, error: Exception | None = None):
        self.outputs = list(outputs)
        self.error = error
        self.calls: list[dict] = []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        if not self.outputs:
            raise AssertionError("stub client ran out of scripted outputs")
        return StubResponse(self.outputs.pop(0))


class StubClient:
    def __init__(self, outputs=(), error: Exception | None = None):
        self.messages = StubMessages(outputs, error=error)

    @property
    def calls(self) -> list[dict]:
        return self.messages.calls


class StubConfig:
    """Stands in for houseband.config.Config without touching the filesystem."""

    model = "claude-opus-5"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


BRIEF = Brief(
    prompt="A short, wistful piano piece.",
    genre="ambient",
    mood="wistful",
    tempo_hint="80 BPM",
    instrumentation=["piano"],
)

CRITERIA = "Sections must be distinguishable by orchestration, not only density."


def _verdict(score: int, rationale: str = "") -> DimensionVerdict:
    return DimensionVerdict(
        score=score,
        rationale=rationale or f"Scored {score} against the anchors.",
        findings=[
            Finding(
                claim=f"Placeholder finding for score {score}.",
                bar_start=8,
                bar_end=15,
                track="lead",
                severity="moderate",
                suggested_revision="Raise the repeated figure to start on the fifth.",
                attributed_role="songwriter",
            )
        ],
    )


def _midi(path, note_count: int = 8):
    midi = pretty_midi.PrettyMIDI(initial_tempo=120.0)
    lead = pretty_midi.Instrument(program=0, name="lead")
    for i in range(note_count):
        lead.notes.append(
            pretty_midi.Note(
                velocity=90, pitch=60 + (i % 5), start=i * 0.5, end=i * 0.5 + 0.4
            )
        )
    midi.instruments.append(lead)
    midi.write(str(path))
    return path


@pytest.fixture
def candidate(tmp_path) -> Candidate:
    return Candidate(
        candidate_id="c1",
        team="the_luddites",
        midi_path=_midi(tmp_path / "c1.mid"),
        score_text="KEY C major   TIME 4/4   BPM 80   BARS 32   LENGTH 1:36",
        piano_roll=None,
    )


# ---------------------------------------------------------------------------
# Sampling
# ---------------------------------------------------------------------------


def test_median_of_three_picks_the_median_and_records_all_samples(candidate):
    # 4, 9, 6 -> median 6. The 9 is the wild sample a mean would let through.
    client = StubClient([_verdict(4), _verdict(9, "the middle read"), _verdict(6)])

    scored = rubric.judge_dimension(
        candidate,
        "melody",  # in cfg.MEDIAN_SAMPLED_DIMENSIONS, so sampling is automatic
        BRIEF,
        CRITERIA,
        client=client,
        config=StubConfig(),
    )

    assert "melody" in cfg.MEDIAN_SAMPLED_DIMENSIONS
    assert len(client.calls) == cfg.MEDIAN_SAMPLES == 3
    assert scored.samples == [4, 9, 6]
    assert scored.score == 6
    assert scored.spread == 5
    # Rationale and findings come from the sample that produced the reported
    # score, not from the first or last call.
    assert scored.rationale == "Scored 6 against the anchors."
    assert scored.findings[0].claim == "Placeholder finding for score 6."


def test_unsampled_dimension_makes_one_call(candidate):
    client = StubClient([_verdict(7)])

    scored = rubric.judge_dimension(
        candidate, "production", BRIEF, CRITERIA, client=client, config=StubConfig()
    )

    assert "production" not in cfg.MEDIAN_SAMPLED_DIMENSIONS
    assert len(client.calls) == 1
    assert scored.score == 7
    assert scored.samples == [7]
    assert scored.spread == 0


def test_partial_sample_failure_still_returns_a_real_score(candidate, tmp_path):
    """One dead sample out of three should not throw the dimension away."""

    class FlakyMessages(StubMessages):
        def parse(self, **kwargs):
            self.calls.append(kwargs)
            if len(self.calls) == 2:
                raise RuntimeError("overloaded_error")
            return StubResponse(_verdict(5 if len(self.calls) == 1 else 7))

    client = StubClient()
    client.messages = FlakyMessages([])
    log = EventLog(tmp_path / "events.jsonl")

    scored = rubric.judge_dimension(
        candidate,
        "melody",
        BRIEF,
        CRITERIA,
        client=client,
        config=StubConfig(),
        log=log,
    )

    assert scored.samples == [5, 7]
    assert scored.score in (5, 7)
    assert "2 of 3 samples succeeded" in scored.rationale


# ---------------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------------


def test_raising_client_yields_a_neutral_verdict(candidate, tmp_path):
    client = StubClient(error=RuntimeError("connection reset"))
    log = EventLog(tmp_path / "events.jsonl")

    scored = rubric.judge_dimension(
        candidate,
        "harmony_voice_leading",
        BRIEF,
        CRITERIA,
        client=client,
        config=StubConfig(),
        log=log,
    )

    assert scored.score == 5
    assert scored.samples == []
    assert "Not judged" in scored.rationale
    assert "connection reset" in scored.rationale

    kinds = [event.kind for event in _read(log)]
    assert "judge.failed" in kinds
    assert "judge.verdict" not in kinds


def test_whole_panel_survives_a_dead_client(candidate):
    """A dead judge degrades the round to neutral scores rather than raising."""
    client = StubClient(error=RuntimeError("api down"))

    verdict = rubric.judge_candidate(
        candidate, BRIEF, CRITERIA, client=client, config=StubConfig()
    )

    assert len(verdict.dimensions) == len(DIMENSIONS)
    assert {d.score for d in verdict.dimensions} == {5}
    assert verdict.weighted_total == pytest.approx(5.0)


def test_missing_piano_roll_is_not_an_error(candidate, tmp_path):
    candidate.piano_roll = tmp_path / "does_not_exist.png"
    client = StubClient([_verdict(6)])

    scored = rubric.judge_dimension(
        candidate, "production", BRIEF, CRITERIA, client=client, config=StubConfig()
    )

    assert scored.score == 6
    content = client.calls[0]["messages"][0]["content"]
    assert all(block["type"] == "text" for block in content)


def test_present_piano_roll_becomes_a_base64_image_block(candidate, tmp_path):
    roll = tmp_path / "roll.png"
    roll.write_bytes(b"\x89PNG\r\n\x1a\n fake but non-empty")
    candidate.piano_roll = roll
    client = StubClient([_verdict(6)])

    rubric.judge_dimension(
        candidate,
        "loop_usability",
        BRIEF,
        CRITERIA,
        client=client,
        config=StubConfig(),
        samples=1,
    )

    content = client.calls[0]["messages"][0]["content"]
    images = [b for b in content if b["type"] == "image"]
    assert len(images) == 1
    assert images[0]["source"]["media_type"] == "image/png"
    assert images[0]["source"]["type"] == "base64"


# ---------------------------------------------------------------------------
# Prompt shape
# ---------------------------------------------------------------------------


def test_judge_never_sees_the_team_or_the_reference_flag(tmp_path):
    """Blindness is the property that makes the calibration check meaningful."""
    reference = Candidate(
        candidate_id="c0",
        team="reference",
        midi_path=_midi(tmp_path / "ref.mid"),
        score_text="KEY D minor   TIME 4/4   BPM 80   BARS 48",
    )
    client = StubClient([_verdict(8)])

    rubric.judge_dimension(
        reference, "melody", BRIEF, CRITERIA, client=client, config=StubConfig(), samples=1
    )

    call = client.calls[0]
    rendered = repr(call["system"]) + repr(call["messages"])
    assert "c0" in rendered
    assert "reference" not in rendered.lower()


def test_system_prompt_caches_the_stable_prefix(candidate):
    blocks = rubric.build_system_prompt("melody", BRIEF, CRITERIA)

    assert blocks[-1].get("cache_control") == {"type": "ephemeral"}
    assert not any("cache_control" in b for b in blocks[:-1])
    # Stability order: global instructions, then run-level brief and criteria,
    # then the dimension rubric that the breakpoint terminates.
    assert "blind panel" in blocks[0]["text"]
    assert BRIEF.prompt in blocks[1]["text"]
    assert CRITERIA in blocks[2]["text"]
    assert "Anchored scale" in blocks[-1]["text"]


def test_request_uses_the_judge_token_ceiling_and_no_effort(candidate):
    client = StubClient([_verdict(6)])

    rubric.judge_dimension(
        candidate, "production", BRIEF, CRITERIA, client=client, config=StubConfig()
    )

    call = client.calls[0]
    assert call["max_tokens"] == cfg.JUDGE_MAX_TOKENS
    assert call["model"] == "claude-opus-5"
    assert call["output_format"] is DimensionVerdict
    # The API default effort is already 'high', and combining output_format with
    # output_config risks a conflict, so neither may appear.
    assert "effort" not in call
    assert "output_config" not in call


def test_every_dimension_has_a_rubric_with_all_five_anchors():
    assert rubric.missing_rubrics() == []
    for dimension in cfg.MEDIAN_SAMPLED_DIMENSIONS + ("originality", "production"):
        text = rubric.load_rubric(dimension)
        for anchor in ("**2 =", "**4 =", "**6 =", "**8 =", "**10 ="):
            assert anchor in text, f"{dimension} is missing the {anchor} anchor"
        assert "bar_start" in text or "bar range" in text


# ---------------------------------------------------------------------------
# Panel
# ---------------------------------------------------------------------------


def test_run_panel_keys_by_candidate_id_and_preserves_dimension_order(tmp_path):
    candidates = [
        Candidate(
            candidate_id=f"c{i}",
            team=f"team{i}",
            midi_path=_midi(tmp_path / f"c{i}.mid"),
            score_text="KEY C major   BARS 32",
        )
        for i in range(2)
    ]
    # Derived, not hardcoded: the dimension set and the sampled subset have both
    # changed under this test before, and a literal here fails as a stub that runs
    # dry rather than as the mismatch it actually is.
    client = StubClient(
        [_verdict(6) for _ in range(2 * CALLS_PER_CANDIDATE)]
    )
    log = EventLog(tmp_path / "events.jsonl")

    verdicts = rubric.run_panel(
        candidates, BRIEF, CRITERIA, client=client, config=StubConfig(), log=log
    )

    assert set(verdicts) == {"c0", "c1"}
    assert [d.dimension for d in verdicts["c0"].dimensions] == list(DIMENSIONS)
    assert len(client.calls) == 2 * CALLS_PER_CANDIDATE

    events = _read(log)
    assert sum(1 for e in events if e.kind == "judge.started") == 2 * len(DIMENSIONS)
    assert sum(1 for e in events if e.kind == "judge.verdict") == 2 * CALLS_PER_CANDIDATE
    # Usage is attached so the cost readout and the budget guard both work.
    assert all(e.usage is not None for e in events if e.kind == "judge.verdict")


# ---------------------------------------------------------------------------
# Pairwise
# ---------------------------------------------------------------------------


class OrderedPairwiseClient:
    """Returns one scripted PairwiseVerdict per call, in order."""

    def __init__(self, winners: list[str]):
        self.messages = StubMessages(
            [
                PairwiseVerdict(winner=w, reason=f"scripted {w}")
                for w in winners
            ]
        )

    @property
    def calls(self) -> list[dict]:
        return self.messages.calls


@pytest.fixture
def pair(tmp_path) -> tuple[Candidate, Candidate]:
    a = Candidate(
        candidate_id="c1", team="alpha", midi_path=_midi(tmp_path / "a.mid", 8)
    )
    b = Candidate(
        candidate_id="c2", team="beta", midi_path=_midi(tmp_path / "b.mid", 12)
    )
    return a, b


def test_both_orders_are_judged(pair):
    a, b = pair
    client = OrderedPairwiseClient(["A", "B"])

    pairwise.compare(a, b, BRIEF, CRITERIA, client=client, config=StubConfig())

    assert len(client.calls) == 2
    first_labels = [
        block["text"]
        for call in client.calls
        for block in call["messages"][0]["content"]
        if block["type"] == "text" and block["text"].startswith("=== CANDIDATE A")
    ]
    # The candidate in slot A differs between the two calls: that is the point.
    assert first_labels == ["=== CANDIDATE A (id c1) ===", "=== CANDIDATE A (id c2) ==="]


def test_disagreement_between_orders_is_a_draw(pair, tmp_path):
    a, b = pair
    # Order 1 (c1, c2) says slot A wins -> c1. Order 2 (c2, c1) says slot A
    # wins -> c2. Slot preference, not a preference between the pieces.
    client = OrderedPairwiseClient(["A", "A"])
    log = EventLog(tmp_path / "events.jsonl")

    verdict = pairwise.compare(
        a, b, BRIEF, CRITERIA, client=client, config=StubConfig(), log=log
    )

    assert verdict.winner == "tie"
    assert "disagreed" in verdict.reason
    reconciled = [
        e for e in _read(log) if e.kind == "pairwise.verdict" and e.data["order"] == "both"
    ]
    assert len(reconciled) == 1
    assert reconciled[0].data["agreed"] is False


def test_agreement_across_orders_produces_a_winner(pair):
    a, b = pair
    # Order 1 (c1, c2): B wins -> c2. Order 2 (c2, c1): A wins -> c2. Agreed.
    client = OrderedPairwiseClient(["B", "A"])

    verdict = pairwise.compare(a, b, BRIEF, CRITERIA, client=client, config=StubConfig())

    assert verdict.winner == "B"
    assert "preferred the same piece" in verdict.reason


def test_both_orders_tying_is_a_tie(pair):
    a, b = pair
    client = OrderedPairwiseClient(["tie", "tie"])

    verdict = pairwise.compare(a, b, BRIEF, CRITERIA, client=client, config=StubConfig())

    assert verdict.winner == "tie"
    assert "called it a tie" in verdict.reason


def test_a_failed_order_is_a_draw_not_a_half_verdict(pair, tmp_path):
    """One surviving order is exactly the biased signal we refuse to trust."""
    a, b = pair

    class HalfDeadMessages(StubMessages):
        def parse(self, **kwargs):
            self.calls.append(kwargs)
            if len(self.calls) == 1:
                return StubResponse(PairwiseVerdict(winner="A", reason="only read"))
            raise RuntimeError("overloaded_error")

    client = OrderedPairwiseClient([])
    client.messages = HalfDeadMessages([])
    log = EventLog(tmp_path / "events.jsonl")

    verdict = pairwise.compare(
        a, b, BRIEF, CRITERIA, client=client, config=StubConfig(), log=log
    )

    assert verdict.winner == "tie"
    assert "not every presentation order" in verdict.reason
    assert any(e.kind == "judge.failed" for e in _read(log))


class KeyedPairwiseClient:
    """Answers by (slot A id, slot B id).

    The tournament runs pairs concurrently, so a stub that replays a fixed
    sequence would hand answers to whichever thread arrived first. Keying on the
    presentation order makes the test deterministic and lets it script position
    bias explicitly.
    """

    def __init__(self, answers: dict[tuple[str, str], str]):
        self.answers = answers
        self.calls: list[tuple[str, str]] = []
        outer = self

        class Messages:
            def parse(self, **kwargs):
                slots: dict[str, str] = {}
                for block in kwargs["messages"][0]["content"]:
                    if block["type"] != "text":
                        continue
                    text = block["text"]
                    if text.startswith("=== CANDIDATE "):
                        slot = text.split()[2]
                        slots[slot] = text.split("(id ")[1].split(")")[0]
                key = (slots["A"], slots["B"])
                outer.calls.append(key)
                return StubResponse(
                    PairwiseVerdict(winner=outer.answers[key], reason=f"scripted {key}")
                )

        self.messages = Messages()


def test_tournament_rates_everyone_and_reconciles_position_bias(tmp_path):
    strong = Candidate(
        candidate_id="c0", team="gamma", midi_path=_midi(tmp_path / "c.mid", 24)
    )
    agent_a = Candidate(
        candidate_id="c1", team="alpha", midi_path=_midi(tmp_path / "a.mid", 8)
    )
    agent_b = Candidate(
        candidate_id="c2", team="beta", midi_path=_midi(tmp_path / "b.mid", 12)
    )

    # Three pairs, two orders each. c0 wins both of its pairs in both orders, so
    # that is a real preference; c1 and c2 each get a slot-A preference in both
    # orders, which is position bias and must reconcile to a draw.
    client = KeyedPairwiseClient(
        {
            ("c0", "c1"): "A",
            ("c1", "c0"): "B",
            ("c0", "c2"): "A",
            ("c2", "c0"): "B",
            ("c1", "c2"): "A",
            ("c2", "c1"): "A",
        }
    )
    log = EventLog(tmp_path / "events.jsonl")

    ratings = pairwise.tournament(
        [agent_b, strong, agent_a],
        BRIEF,
        CRITERIA,
        client=client,
        config=StubConfig(),
        log=log,
    )

    assert len(client.calls) == 6
    assert ratings["c0"] > elo.DEFAULT_RATING  # won both pairs, both orders
    # Both lost to c0 and drew with each other, so both sit below the starting
    # rating and neither is separated from the other.
    assert ratings["c1"] < elo.DEFAULT_RATING
    assert ratings["c2"] < elo.DEFAULT_RATING
    assert ratings["c1"] == pytest.approx(ratings["c2"], abs=1.5)
    assert any(e.kind == "elo.updated" for e in _read(log))


# ---------------------------------------------------------------------------
# Elo
# ---------------------------------------------------------------------------

# These tests used to cover a pinned reference whose rating never moved, which
# gave the pool an absolute yardstick. References were removed from the tool, so
# Elo is plain zero-sum again: it ranks within a round and says little across
# them. The absolute measure is the weighted rubric total, whose anchors are
# written descriptors, and that is covered in test_snippet_judging.py.


def test_a_win_moves_both_sides_by_the_same_amount():
    """Zero-sum is the property that replaced pinning, so it is worth asserting
    rather than assuming: the points the winner gains are the points the loser
    drops, and the pool total is conserved."""
    ratings = {"a": elo.DEFAULT_RATING, "b": elo.DEFAULT_RATING}
    before = sum(ratings.values())

    elo.update(ratings, "a", "b", 1.0)

    assert ratings["a"] > elo.DEFAULT_RATING
    assert ratings["b"] < elo.DEFAULT_RATING
    assert sum(ratings.values()) == pytest.approx(before)


def test_beating_a_stronger_opponent_is_worth_more():
    underdog = {"weak": 1000.0, "strong": 1400.0}
    evens = {"weak": 1200.0, "strong": 1200.0}

    elo.update(underdog, "weak", "strong", 1.0, k=32.0)
    elo.update(evens, "weak", "strong", 1.0, k=32.0)

    assert underdog["weak"] - 1000.0 > evens["weak"] - 1200.0


def test_a_draw_between_equals_moves_nothing():
    ratings = {"a": elo.DEFAULT_RATING, "b": elo.DEFAULT_RATING}
    elo.update(ratings, "a", "b", 0.5)
    assert ratings["a"] == pytest.approx(elo.DEFAULT_RATING)
    assert ratings["b"] == pytest.approx(elo.DEFAULT_RATING)


def test_run_ratings_is_deterministic_regardless_of_result_order():
    """The comparisons that produce these results run concurrently, so without
    sorting, rerunning a round on the same verdicts could produce different
    ratings purely from thread scheduling."""
    results = [
        ("c1", "c2", 1.0),
        ("c0", "c1", 1.0),
        ("c0", "c2", 0.5),
    ]
    first = elo.run_ratings(results)
    second = elo.run_ratings(list(reversed(results)))

    assert first == second
    assert first["c1"] > first["c2"]


def test_unrated_competitors_start_at_the_default():
    assert elo.run_ratings([]) == {}

    ratings = elo.run_ratings([("a", "b", 0.5)])
    assert ratings["a"] == pytest.approx(elo.DEFAULT_RATING)
    assert ratings["b"] == pytest.approx(elo.DEFAULT_RATING)


def test_initial_ratings_are_carried_in():
    """How a team's rating survives across rounds: the id it is given is
    reshuffled every round, so the loop passes last round's rating back in."""
    ratings = elo.run_ratings([("a", "b", 1.0)], initial={"a": 1300.0, "b": 1100.0})
    assert ratings["a"] > 1300.0
    assert ratings["b"] < 1100.0


def test_expected_score_is_symmetric_and_favours_the_higher_rating():
    assert elo.expected(1200.0, 1200.0) == pytest.approx(0.5)
    assert elo.expected(1600.0, 1200.0) == pytest.approx(
        1.0 - elo.expected(1200.0, 1600.0)
    )
    assert elo.expected(1600.0, 1200.0) > 0.9


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read(log: EventLog):
    from houseband.events import read_events

    return read_events(log.path)
