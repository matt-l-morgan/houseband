"""End-to-end wiring test with a stubbed model.

This is the test that catches integration bugs the unit tests cannot: whether the
composer's tool loop, the deterministic gate, the judge panel, the pairwise
tournament, Elo, the coach, and the event log actually fit together over multiple
rounds.

It stubs the Anthropic client rather than calling the API, which means it runs in
CI with no credential and in under a second. What it deliberately does *not*
verify is judgment quality: a stub that always returns score 7 tells you nothing
about whether the rubrics work. That question needs a real credential and lives in
the calibration gate instead.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from houseband import config as cfg
from houseband.events import read_events
from houseband.types import (
    Brief,
    CandidateVerdict,
    DimensionVerdict,
    Finding,
    PairwiseVerdict,
)

# A minimal program the composer "writes". Deliberately valid and long enough to
# clear the gate's minimum-duration check.
GOOD_CODE = '''
from houseband.house import Score

s = Score(bpm=100, key="Am")
s.mark_section("intro", 0, 8)
s.mark_section("main", 8, 24)

keys = s.track("keys", patch="electric_piano", pan=-0.2)
bass = s.track("bass", patch="fingered_bass")
drums = s.drum_track("drums")

PROG = ["Am", "F", "C", "G"]
for bar in range(32):
    chord = PROG[bar % 4]
    keys.chord(bar, 1, symbol=chord, dur=3.5, vel=54 + (bar % 6) * 4)
    bass.note(bar, 1, ["A1", "F1", "C2", "G1"][bar % 4], 2.0, 70)
    if bar >= 8:
        drums.hit(bar, 1, "kick", 88)
        drums.hit(bar, 3, "snare", 76)
        for beat in (1, 2, 3, 4):
            drums.hit(bar, beat, "hat_closed", 44 + (beat % 2) * 8)

s.write("out.mid")
'''


class _Usage:
    input_tokens = 1200
    output_tokens = 800
    cache_creation_input_tokens = 0
    cache_read_input_tokens = 400


class _Block:
    def __init__(self, type_, **kw):
        self.type = type_
        for key, value in kw.items():
            setattr(self, key, value)


class _Message:
    def __init__(self, content, stop_reason="end_turn"):
        self.content = content
        self.stop_reason = stop_reason
        self.usage = _Usage()


class _Stream:
    def __init__(self, message):
        self._message = message

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return self._message


class _Parsed:
    def __init__(self, output):
        self.parsed_output = output
        self.usage = _Usage()
        self.stop_reason = "end_turn"
        self.content = [_Block("text", text="stub")]


class StubMessages:
    """Dispatches on the requested output type, which is how one stub can stand
    in for the composer, the judges, the coach and the brief at once."""

    def __init__(self, owner):
        self.owner = owner

    def stream(self, **kwargs):
        """Composer turn.

        Decided from the conversation passed in rather than from a call counter,
        because composers run concurrently in a thread pool and a shared counter
        would hand one team another team's turn.
        """
        self.owner.stream_calls += 1
        already_rendered = any(
            isinstance(m.get("content"), list)
            and any(
                isinstance(block, dict) and block.get("type") == "tool_result"
                for block in m["content"]
            )
            for m in kwargs.get("messages", [])
        )
        if already_rendered:
            return _Stream(
                _Message([_Block("text", text="Submitting: 32 bars, intro plus main.")])
            )
        return _Stream(
            _Message(
                [
                    _Block("text", text="Writing an AABA arrangement."),
                    _Block(
                        "tool_use",
                        id=f"toolu_{self.owner.stream_calls}",
                        name="render_midi",
                        input={"code": GOOD_CODE, "intent": "first draft"},
                    ),
                ],
                stop_reason="tool_use",
            )
        )

    def parse(self, **kwargs):
        output_format = kwargs.get("output_format")
        name = getattr(output_format, "__name__", "")
        self.owner.parse_calls.append(name)

        if name == "Brief":
            return _Parsed(Brief(prompt="stub", genre="rock", target_length="4 minutes"))
        if name == "DimensionVerdict":
            self.owner.dimension_calls += 1
            return _Parsed(
                DimensionVerdict(
                    score=6,
                    rationale="Stubbed verdict.",
                    findings=[
                        Finding(
                            claim="The bridge is the same density as the verses.",
                            bar_start=8,
                            bar_end=24,
                            track="drums",
                            severity="moderate",
                            suggested_revision="Drop the kit for four bars at bar 16.",
                            attributed_role="arranger",
                        )
                    ],
                )
            )
        if name == "PairwiseVerdict":
            # Alternate so the tournament sees both agreement and disagreement,
            # exercising the draw path.
            self.owner.pairwise_calls += 1
            winner = "A" if self.owner.pairwise_calls % 3 else "B"
            return _Parsed(PairwiseVerdict(winner=winner, reason="Stubbed comparison."))
        if name == "CoachOutput":
            from houseband.coach import CoachOutput
            from houseband.types import PlaybookRule

            self.owner.coach_calls += 1
            return _Parsed(
                CoachOutput(
                    rules=[
                        PlaybookRule(
                            role="arranger",
                            rule="Drop the drum kit for at least four bars before the final section.",
                            because="bars 8-24 hold one density throughout",
                        )
                    ]
                )
            )
        raise AssertionError(f"stub got an unexpected output_format: {name}")

    def create(self, **kwargs):
        # Analyst: plain text, no structured output.
        self.owner.create_calls += 1
        return _Message(
            [
                _Block(
                    "text",
                    text="# Criteria\n\n- Build through at least three tiers.\n"
                    "- Place the climax in the final third.\n\n"
                    "## Deliberately not specified\n\n- Key and tempo.\n",
                )
            ]
        )


class StubClient:
    def __init__(self):
        self.stream_calls = 0
        self.parse_calls: list[str] = []
        self.dimension_calls = 0
        self.pairwise_calls = 0
        self.coach_calls = 0
        self.create_calls = 0
        self.messages = StubMessages(self)


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    """A config pointed entirely at a temp directory, plus a fake credential.

    The credential is faked because loop.run refuses to start without one, which
    is correct behaviour worth preserving rather than special-casing.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-stub-not-a-real-key")
    config = cfg.Config(
        runs_dir=tmp_path / "runs",
        references_dir=tmp_path / "references",
        playbooks_dir=tmp_path / "playbooks",
    )
    config.references_dir.mkdir(parents=True, exist_ok=True)
    config.playbooks_dir.mkdir(parents=True, exist_ok=True)
    return config


def _make_reference(config, tmp_path):
    """A reference piece with different material from the candidates.

    Different on purpose: identical material would trip the originality gate and
    the test would be exercising rejection rather than the happy path.
    """
    from houseband.house import Score

    s = Score(bpm=132, key="E")
    s.mark_section("a", 0, 16)
    s.mark_section("b", 16, 16)
    lead = s.track("lead", patch="saw_lead")
    pad = s.track("pad", patch="warm_pad", pan=0.3)
    for bar in range(32):
        for i, beat in enumerate((1, 2.5, 4)):
            lead.note(bar, beat, 64 + ((bar * 5 + i * 7) % 19), 0.75, 70 + (i * 9))
        pad.chord(bar, 1, symbol="E" if bar % 2 else "C#m", dur=3.8, vel=48)
    path = config.references_dir / "ref.mid"
    s.write(str(path))
    return path


class TestFullLoop:
    def test_two_rounds_end_to_end(self, isolated, tmp_path):
        from houseband import loop

        _make_reference(isolated, tmp_path)
        client = StubClient()

        run_dir = loop.run(
            prompt="epic long-form rock that builds",
            teams=2,
            rounds=2,
            run_id="testrun",
            config=isolated,
            client=client,
            echo=False,
            max_turns=3,
        )

        events = read_events(run_dir / "events.jsonl")
        kinds = [e.kind for e in events]

        # The run completed rather than failing.
        assert "run.finished" in kinds, [e.message for e in events if e.kind == "run.failed"]
        assert "run.failed" not in kinds

        # Every stage fired.
        for expected in (
            "run.started",
            "analyst.finished",
            "round.started",
            "composer.started",
            "composer.tool_call",
            "composer.tool_result",
            "composer.finished",
            "gate.passed",
            "artifact.rendered",
            "judge.verdict",
            "pairwise.verdict",
            "elo.updated",
            "coach.rule_written",
            "round.finished",
        ):
            assert expected in kinds, f"never saw {expected}"

        # Two rounds, two teams.
        assert kinds.count("round.started") == 2
        assert len([e for e in events if e.kind == "composer.finished"]) == 4

    def test_judges_are_blind_to_team_identity(self, isolated, tmp_path):
        """The reference has to be indistinguishable in the pool, or the
        calibration check measures nothing."""
        from houseband import loop

        _make_reference(isolated, tmp_path)
        client = StubClient()
        run_dir = loop.run(
            prompt="test",
            teams=2,
            rounds=1,
            run_id="blind",
            config=isolated,
            client=client,
            echo=False,
            max_turns=3,
        )
        verdicts = json.loads((run_dir / "round1" / "verdicts.json").read_text())
        # Candidates are opaque ids, and the mapping back to teams is recorded
        # separately rather than being visible to the judge.
        assert set(verdicts["id_to_team"]) >= {"r1c1", "r1c2"}
        assert "r1ref" in verdicts["id_to_team"]
        assert verdicts["id_to_team"]["r1ref"] == "reference"

    def test_held_out_dimension_is_hidden_from_the_coach(self, isolated, tmp_path):
        """Agents optimise against whatever the coach is told, so one dimension
        stays out of the coaching path entirely."""
        from houseband import loop

        _make_reference(isolated, tmp_path)
        client = StubClient()
        run_dir = loop.run(
            prompt="test",
            teams=2,
            rounds=1,
            run_id="holdout",
            config=isolated,
            client=client,
            echo=False,
            max_turns=3,
        )
        meta = json.loads((run_dir / "meta.json").read_text())
        held_out = meta["held_out_dimension"]

        # It is still judged and recorded, just not coached on.
        verdicts = json.loads((run_dir / "round1" / "verdicts.json").read_text())
        first = next(iter(verdicts["verdicts"].values()))
        assert held_out in {d["dimension"] for d in first["dimensions"]}

    def test_playbooks_are_per_team_and_persist(self, isolated, tmp_path):
        """Teams compete, so a lesson learned by one must not leak to the other."""
        from houseband import loop

        _make_reference(isolated, tmp_path)
        client = StubClient()
        loop.run(
            prompt="test",
            teams=2,
            rounds=2,
            run_id="playbooks",
            config=isolated,
            client=client,
            echo=False,
            max_turns=3,
        )
        written = sorted(p.name for p in isolated.playbooks_dir.glob("*.md"))
        assert len(written) == 2, written
        for path in isolated.playbooks_dir.glob("*.ledger.json"):
            ledger = json.loads(path.read_text())
            assert ledger["entries"], f"{path.name} has no rules"

    def test_no_credential_fails_cleanly(self, isolated, monkeypatch):
        """A missing key should be a legible event, not a stack trace."""
        from houseband import loop

        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setattr(cfg, "credential_source", lambda: None)

        run_dir = loop.run(
            prompt="test", teams=1, rounds=1, run_id="nokey", config=isolated, echo=False
        )
        events = read_events(run_dir / "events.jsonl")
        assert events[-1].kind == "run.failed"
        assert "credential" in events[-1].message.lower()

    def test_no_key_material_reaches_the_log(self, isolated, tmp_path):
        from houseband import loop

        _make_reference(isolated, tmp_path)
        run_dir = loop.run(
            prompt="test",
            teams=1,
            rounds=1,
            run_id="scrub",
            config=isolated,
            client=StubClient(),
            echo=False,
            max_turns=3,
        )
        text = (run_dir / "events.jsonl").read_text()
        assert "sk-ant-stub-not-a-real-key" not in text
        assert "sk-ant" not in text


class TestCalibrationReporting:
    """Guards the branch that only runs when the judges look wrong.

    A real bug lived here: loop.py wrote ``+ calibration.summary`` without the
    call, so a miscalibrated round raised TypeError and killed the run right
    after judging. Every existing test missed it because the stub scored every
    candidate identically, which makes the reference *tie* rather than lose, and
    a tie is not a breach. The happy path was covered and the alarm path was not.
    """

    def _verdict(self, candidate_id, team, score, is_reference=False):
        from houseband.types import DIMENSIONS, ScoredDimension

        return CandidateVerdict(
            candidate_id=candidate_id,
            team=team,
            is_reference=is_reference,
            dimensions=[
                ScoredDimension(dimension=d, score=score, rationale="stub")
                for d in DIMENSIONS
            ],
        )

    def test_agent_beating_the_reference_is_reported(self):
        from houseband.judges import check_calibration

        report = check_calibration(
            {
                "c1": self._verdict("c1", "crate", 9),
                "ref": self._verdict("ref", "reference", 3, is_reference=True),
            }
        )
        assert report.ok is False
        assert report.breaches

    def test_report_survives_what_the_loop_does_to_it(self):
        """The exact two operations loop.py performs on the report."""
        from houseband.judges import check_calibration

        report = check_calibration(
            {
                "c1": self._verdict("c1", "crate", 9),
                "ref": self._verdict("ref", "reference", 3, is_reference=True),
            }
        )
        # Concatenation: this is what raised TypeError in production.
        message = "JUDGE CALIBRATION SUSPECT: " + report.summary()
        assert isinstance(message, str)
        assert len(message) > len("JUDGE CALIBRATION SUSPECT: ")
        # And it must be JSON-serialisable for the event payload.
        assert isinstance(report.model_dump(), dict)

    def test_missing_reference_does_not_read_as_a_pass(self):
        """A check that could not run must not look like a check that passed."""
        from houseband.judges import check_calibration

        report = check_calibration({"c1": self._verdict("c1", "crate", 7)})
        assert report.has_reference is False
        assert report.ok is False
        assert isinstance(report.summary(), str)

    def test_miscalibrated_round_does_not_kill_the_run(self, isolated, tmp_path):
        """End to end, with the reference scored below the agents.

        The stub returns a declining score per distinct candidate, and the loop
        judges the reference last, so the reference comes out lowest and the
        alarm path actually executes inside a real run.
        """
        from houseband import loop

        class DecliningStub(StubClient):
            def __init__(self):
                super().__init__()
                self.messages = _DecliningMessages(self)

        class _DecliningMessages(StubMessages):
            def __init__(self, owner):
                super().__init__(owner)
                self.order: list[str] = []

            def parse(self, **kwargs):
                fmt = getattr(kwargs.get("output_format"), "__name__", "")
                if fmt == "DimensionVerdict":
                    body = str(kwargs.get("messages"))
                    key = body[:200]
                    if key not in self.order:
                        self.order.append(key)
                    rank = self.order.index(key)
                    return _Parsed(
                        DimensionVerdict(
                            score=max(1, 9 - rank * 3),
                            rationale="declining stub",
                            findings=[],
                        )
                    )
                return super().parse(**kwargs)

        _make_reference(isolated, tmp_path)
        run_dir = loop.run(
            prompt="test",
            teams=2,
            rounds=1,
            run_id="miscal",
            config=isolated,
            client=DecliningStub(),
            echo=False,
            max_turns=3,
        )
        events = read_events(run_dir / "events.jsonl")
        kinds = [e.kind for e in events]
        assert "run.failed" not in kinds, [
            e.message for e in events if e.kind == "run.failed"
        ]
        assert "run.finished" in kinds
        # And the round still coached, which is what the crash prevented.
        assert "coach.rule_written" in kinds


class TestComposerLoop:
    def test_composer_retries_after_a_rejected_program(self, isolated, tmp_path):
        """A rejected program has to come back as actionable feedback the agent
        can use, not as a dead end."""
        from houseband import composer
        from houseband.events import EventLog

        class RetryStub(StubClient):
            def __init__(self):
                super().__init__()
                self.messages = _RetryMessages(self)

        class _RetryMessages(StubMessages):
            def stream(self, **kwargs):
                call = self.owner.stream_calls
                self.owner.stream_calls += 1
                if call == 0:
                    # An out-of-range bass line: rejected by the gate, not a crash.
                    bad = GOOD_CODE.replace('"A1", "F1", "C2", "G1"', '"A6", "F6", "C6", "G6"')
                    return _Stream(
                        _Message(
                            [
                                _Block(
                                    "tool_use",
                                    id="t0",
                                    name="render_midi",
                                    input={"code": bad, "intent": "draft"},
                                )
                            ],
                            stop_reason="tool_use",
                        )
                    )
                if call == 1:
                    return _Stream(
                        _Message(
                            [
                                _Block(
                                    "tool_use",
                                    id="t1",
                                    name="render_midi",
                                    input={"code": GOOD_CODE, "intent": "fixed the bass register"},
                                )
                            ],
                            stop_reason="tool_use",
                        )
                    )
                return _Stream(_Message([_Block("text", text="Submitting.")]))

        log = EventLog(tmp_path / "events.jsonl")
        result = composer.compose(
            team="crate",
            brief=Brief(prompt="test"),
            criteria="- three tiers",
            playbook="",
            workdir=tmp_path / "work",
            log=log,
            client=RetryStub(),
            config=isolated,
            max_turns=4,
        )
        assert result.ok
        assert result.render_attempts == 2
        results = [e for e in read_events(tmp_path / "events.jsonl") if e.kind == "composer.tool_result"]
        assert results[0].data["ok"] is False
        assert results[1].data["ok"] is True

    def test_composer_gives_up_cleanly_when_nothing_validates(self, isolated, tmp_path):
        from houseband import composer
        from houseband.events import EventLog

        class AlwaysBad(StubClient):
            def __init__(self):
                super().__init__()
                self.messages = _BadMessages(self)

        class _BadMessages(StubMessages):
            def stream(self, **kwargs):
                self.owner.stream_calls += 1
                return _Stream(
                    _Message(
                        [
                            _Block(
                                "tool_use",
                                id=f"t{self.owner.stream_calls}",
                                name="render_midi",
                                input={"code": "import os\nos.system('true')\n"},
                            )
                        ],
                        stop_reason="tool_use",
                    )
                )

        log = EventLog(tmp_path / "events.jsonl")
        result = composer.compose(
            team="arena",
            brief=Brief(prompt="test"),
            criteria="",
            playbook="",
            workdir=tmp_path / "work2",
            log=log,
            client=AlwaysBad(),
            config=isolated,
            max_turns=2,
        )
        assert not result.ok
        assert "No program passed validation" in result.error
        # The import allowlist did the rejecting, before any code ran.
        results = [e for e in read_events(tmp_path / "events.jsonl") if e.kind == "composer.tool_result"]
        assert all(e.data["ok"] is False for e in results)
