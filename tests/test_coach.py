"""Tests for the learning loop's bookkeeping.

The coach's LLM call is not tested here (that needs a credential); what is tested
is everything that decides whether the playbook stays useful. A playbook that
only grows becomes a wall of platitudes that crowds out the brief and makes the
composer worse, so the caps and the deprecation logic are the parts that have to
be right.
"""

from __future__ import annotations

import json

from houseband.coach import (
    GRACE_ROUNDS,
    MAX_RULES_PER_ROLE,
    MAX_RULES_TOTAL,
    CoachOutput,
    LedgerEntry,
    Playbook,
    approved_helpers,
    stage_function,
)
from houseband.events import EventLog
from houseband.types import PlaybookRule, StagedFunction


def _rule(role="arranger", text="Strip to one instrument for 4 bars before the final chorus."):
    return PlaybookRule(role=role, rule=text, because="bars 40-56 never thin out")


class TestPlaybookPersistence:
    def test_add_save_reload(self, tmp_path):
        pb = Playbook("team", tmp_path)
        added, removed = pb.apply(CoachOutput(rules=[_rule()]), round=1, baseline=5.0)
        pb.save()
        assert len(added) == 1 and not removed

        reloaded = Playbook("team", tmp_path)
        assert len(reloaded.entries) == 1
        assert reloaded.entries[0].added_round == 1
        assert reloaded.entries[0].baseline == 5.0

    def test_render_groups_by_role(self, tmp_path):
        pb = Playbook("team", tmp_path)
        pb.apply(
            CoachOutput(
                rules=[
                    _rule("arranger", "Arranger rule about section contrast."),
                    _rule("rhythm", "Rhythm rule about hat velocity."),
                ]
            ),
            round=1,
            baseline=5.0,
        )
        text = pb.render()
        assert "## arranger" in text and "## rhythm" in text
        # Sections follow ROLES order (songwriter, rhythm, arranger, mix), which
        # reads as a rough pipeline order rather than alphabetically.
        assert text.index("## rhythm") < text.index("## arranger")

    def test_empty_playbook_renders_empty(self, tmp_path):
        assert Playbook("fresh", tmp_path).render() == ""

    def test_markdown_records_rule_status(self, tmp_path):
        pb = Playbook("team", tmp_path)
        pb.apply(CoachOutput(rules=[_rule()]), round=1, baseline=5.0)
        pb.save()
        text = pb.md_path.read_text()
        assert "Rule status" in text
        assert "unproven" in text


class TestDeprecation:
    def test_loose_match_removes_a_rule(self, tmp_path):
        """The coach echoes rule text back, and near-verbatim has to be enough:
        requiring an exact string match would make deprecation almost never fire."""
        pb = Playbook("team", tmp_path)
        pb.apply(CoachOutput(rules=[_rule()]), round=1, baseline=5.0)
        _, removed = pb.apply(
            CoachOutput(deprecate=["Strip to one instrument for 4 bars"]),
            round=2,
            baseline=5.0,
        )
        assert len(removed) == 1
        assert pb.entries == []

    def test_unmatched_deprecation_is_harmless(self, tmp_path):
        pb = Playbook("team", tmp_path)
        pb.apply(CoachOutput(rules=[_rule()]), round=1, baseline=5.0)
        _, removed = pb.apply(
            CoachOutput(deprecate=["something that was never a rule"]),
            round=2,
            baseline=5.0,
        )
        assert removed == []
        assert len(pb.entries) == 1

    def test_deprecated_rules_are_retained_for_audit(self, tmp_path):
        pb = Playbook("team", tmp_path)
        pb.apply(CoachOutput(rules=[_rule()]), round=1, baseline=5.0)
        pb.apply(CoachOutput(deprecate=["Strip to one instrument"]), round=2, baseline=5.0)
        pb.save()
        ledger = json.loads(pb.ledger_path.read_text())
        assert len(ledger["deprecated"]) == 1
        assert ledger["deprecated"][0]["removed_round"] == 2

    def test_duplicate_rules_are_not_added_twice(self, tmp_path):
        pb = Playbook("team", tmp_path)
        pb.apply(CoachOutput(rules=[_rule()]), round=1, baseline=5.0)
        added, _ = pb.apply(CoachOutput(rules=[_rule()]), round=2, baseline=5.0)
        assert added == []
        assert len(pb.entries) == 1

    def test_at_most_three_rules_per_round(self, tmp_path):
        pb = Playbook("team", tmp_path)
        added, _ = pb.apply(
            CoachOutput(rules=[_rule("mix", f"Rule {i} about panning.") for i in range(6)]),
            round=1,
            baseline=5.0,
        )
        assert len(added) == 3


class TestCaps:
    def test_per_role_cap(self, tmp_path):
        pb = Playbook("team", tmp_path)
        for round_no in range(1, 5):
            pb.apply(
                CoachOutput(
                    rules=[_rule("mix", f"Mix rule {round_no}-{i}.") for i in range(3)]
                ),
                round=round_no,
                baseline=5.0,
            )
        assert len([e for e in pb.entries if e.role == "mix"]) <= MAX_RULES_PER_ROLE

    def test_total_cap(self, tmp_path):
        pb = Playbook("team", tmp_path)
        roles = ["songwriter", "rhythm", "arranger", "mix"]
        for round_no in range(1, 8):
            pb.apply(
                CoachOutput(
                    rules=[
                        _rule(roles[i % 4], f"Rule {round_no}-{i} for {roles[i % 4]}.")
                        for i in range(3)
                    ]
                ),
                round=round_no,
                baseline=5.0,
            )
        assert len(pb.entries) <= MAX_RULES_TOTAL

    def test_unearning_rules_are_dropped_first(self, tmp_path):
        """A rule that demonstrably did not help should lose its slot before one
        that has not been evaluated yet."""
        pb = Playbook("team", tmp_path)
        pb.apply(
            CoachOutput(rules=[_rule("mix", f"Mix rule {i}.") for i in range(3)]),
            round=1,
            baseline=5.0,
        )
        loser = pb.entries[0]
        loser.scores_while_active = [3.0, 3.0]
        assert loser.verdict() == "not earning"

        # Push past the cap so eviction actually has to choose. Three plus three
        # is six against a cap of four, so two rules must go.
        pb.apply(
            CoachOutput(rules=[_rule("mix", f"Mix rule new {i}.") for i in range(3)]),
            round=2,
            baseline=5.0,
        )
        assert len([e for e in pb.entries if e.role == "mix"]) == MAX_RULES_PER_ROLE
        assert loser not in pb.entries, "a rule shown not to help should be evicted first"


class TestLedgerVerdict:
    def test_unproven_within_grace_period(self):
        entry = LedgerEntry(rule="r", role="mix", because="b", added_round=1, baseline=5.0)
        for _ in range(GRACE_ROUNDS - 1):
            entry.scores_while_active.append(9.0)
        assert entry.verdict() == "unproven"

    def test_earning_when_scores_improve(self):
        entry = LedgerEntry(rule="r", role="mix", because="b", added_round=1, baseline=5.0)
        entry.scores_while_active = [5.5, 6.0]
        assert entry.verdict() == "earning"

    def test_not_earning_when_scores_fall(self):
        entry = LedgerEntry(rule="r", role="mix", because="b", added_round=1, baseline=5.0)
        entry.scores_while_active = [4.0, 4.5]
        assert entry.verdict() == "not earning"

    def test_flat_is_unproven_not_earning(self):
        entry = LedgerEntry(rule="r", role="mix", because="b", added_round=1, baseline=5.0)
        entry.scores_while_active = [5.0, 5.05]
        assert entry.verdict() == "unproven"

    def test_no_baseline_stays_unproven(self):
        entry = LedgerEntry(rule="r", role="mix", because="b", added_round=1, baseline=None)
        entry.scores_while_active = [9.0, 9.0]
        assert entry.verdict() == "unproven"

    def test_record_round_attributes_to_active_rules_only(self, tmp_path):
        pb = Playbook("team", tmp_path)
        pb.apply(CoachOutput(rules=[_rule()]), round=1, baseline=5.0)
        pb.record_round(6.0)
        assert pb.entries[0].scores_while_active == [6.0]

        pb.apply(CoachOutput(rules=[_rule("mix", "A later rule.")]), round=2, baseline=6.0)
        pb.record_round(7.0)
        assert pb.entries[0].scores_while_active == [6.0, 7.0]
        assert pb.entries[1].scores_while_active == [7.0]


class TestStaging:
    def test_staged_function_is_written_not_executed(self, tmp_path):
        log = EventLog(tmp_path / "events.jsonl")
        function = StagedFunction(
            name="humanize",
            rationale="judges flagged mechanical velocity in rounds 1 and 2",
            source="def humanize(track):\n    raise RuntimeError('should never run')\n",
            test_source="def test_humanize():\n    assert True\n",
        )
        path = stage_function(function, tmp_path / "staged", log, round=2, team="crate")

        data = json.loads(path.read_text())
        assert data["name"] == "humanize"
        assert data["approved"] is False
        assert data["proposed_round"] == 2
        # The whole point of staging is that the source sits inert on disk.
        assert "should never run" in data["source"]

    def test_staging_emits_an_event(self, tmp_path):
        log = EventLog(tmp_path / "events.jsonl")
        stage_function(
            StagedFunction(name="f", rationale="r", source="def f(): pass", test_source="x"),
            tmp_path / "staged",
            log,
            round=2,
            team="arena",
        )
        text = (tmp_path / "events.jsonl").read_text()
        assert "coach.library_staged" in text


class TestApprovedHelpers:
    def test_reads_public_functions(self, tmp_path):
        module = tmp_path / "learned.py"
        module.write_text(
            "def humanize(track):\n    pass\n\n"
            "def _internal():\n    pass\n\n"
            "def swing(track, amount):\n    pass\n"
        )
        assert approved_helpers(module) == ["humanize", "swing"]

    def test_empty_module_gives_nothing(self, tmp_path):
        module = tmp_path / "learned.py"
        module.write_text("__all__ = []\n")
        assert approved_helpers(module) == []

    def test_missing_or_broken_module_is_not_fatal(self, tmp_path):
        assert approved_helpers(tmp_path / "nope.py") == []
        broken = tmp_path / "broken.py"
        broken.write_text("def oops(:\n")
        assert approved_helpers(broken) == []

    def test_shipped_learned_module_has_no_humanize(self):
        """learned.py deliberately ships without a humanize helper.

        Judges reliably flag mechanical rhythm in early rounds, and the coach
        closing that specific gap on its own is the clearest evidence the learning
        loop does anything. Pre-supplying it would remove the demonstration.
        """
        assert "humanize" not in approved_helpers()
