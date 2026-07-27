"""Tests for the deterministic gate.

This module exists because LLM judges cannot do this job: they will not reliably
notice a bass part an octave out of its playable range, and asking them to check
facts spends their attention there instead of on the music. So these tests guard
the checks the rest of the system deliberately does not ask a model to perform.

There used to be a third check here, melodic n-gram overlap against a reference
recording, which stopped "meet the criteria" collapsing into "reproduce the
reference". References were removed from the tool, so it had nothing left to
compare against and went with them.
"""

from __future__ import annotations

from houseband.house import Score
from houseband.validator import (
    check_imports,
    gate,
    playable_range,
    validate_score,
)


def _write(tmp_path, name="out.mid", **kwargs):
    """Build a small valid score, overridable for the failure cases."""
    s = Score(bpm=kwargs.get("bpm", 100), key="C")
    s.mark_section("main", 0, kwargs.get("bars", 16))
    bass = s.track("bass", patch="fingered_bass")
    keys = s.track("keys", patch="grand_piano")
    for bar in range(kwargs.get("bars", 16)):
        bass.note(bar, 1, kwargs.get("bass_pitch", "E1"), 2.0, 70)
        keys.chord(bar, 1, symbol="C", dur=3.5, vel=60 + (bar % 5) * 4)
    path = tmp_path / name
    s.write(str(path))
    return path, path.with_suffix(".score.json")


class TestImportAllowlist:
    def test_clean_program_passes(self):
        code = "from houseband.house import Score\nimport math\ns = Score()\n"
        assert check_imports(code) == []

    def test_blocks_filesystem_and_network(self):
        for module in ("os", "subprocess", "socket", "urllib.request", "shutil", "pathlib"):
            problems = check_imports(f"import {module}\n")
            assert problems, f"{module} should be rejected"
            assert module.split(".")[0] in problems[0]

    def test_blocks_from_imports_too(self):
        assert check_imports("from os import system\n")

    def test_blocks_dangerous_builtins(self):
        for name in ("eval", "exec", "__import__", "open", "compile", "globals"):
            assert check_imports(f"x = {name}\n"), f"{name} should be rejected"

    def test_blocks_dunder_attribute_escape(self):
        # The classic route out of a restricted namespace.
        problems = check_imports("x = ().__class__.__bases__\n")
        assert problems
        assert "dunder" in problems[0]

    def test_allows_benign_dunders(self):
        assert check_imports("print(__name__)\n") == []

    def test_syntax_error_is_reported_not_raised(self):
        problems = check_imports("def broken(:\n")
        assert len(problems) == 1
        assert "SyntaxError" in problems[0]

    def test_reports_line_numbers(self):
        problems = check_imports("import math\nimport socket\n")
        assert "line 2" in problems[0]


class TestValidation:
    def test_good_score_passes(self, tmp_path):
        midi, sidecar = _write(tmp_path)
        report = validate_score(midi, sidecar)
        assert report.ok, report.feedback()
        assert report.track_count == 2
        assert report.note_count > 0

    def test_unparseable_file_is_rejected_not_raised(self, tmp_path):
        bad = tmp_path / "junk.mid"
        bad.write_bytes(b"this is not a midi file at all")
        report = validate_score(bad)
        assert not report.ok
        assert "parse" in report.errors[0].lower()

    def test_far_out_of_range_part_is_rejected(self, tmp_path):
        # A bass line written two octaves too high: the canonical failure.
        midi, sidecar = _write(tmp_path, bass_pitch="E5")
        report = validate_score(midi, sidecar)
        assert not report.ok
        assert any("bass" in e for e in report.errors)

    def test_marginally_out_of_range_only_warns(self, tmp_path):
        lo, hi = playable_range(33)
        midi, sidecar = _write(tmp_path, bass_pitch=hi + 2)
        report = validate_score(midi, sidecar)
        assert report.ok, report.errors
        assert any("outside" in w for w in report.warnings)

    def test_too_short_is_rejected(self, tmp_path):
        midi, sidecar = _write(tmp_path, bars=1, bpm=240)
        report = validate_score(midi, sidecar, min_duration=5.0)
        assert not report.ok
        assert any("long" in e for e in report.errors)

    def test_single_track_warns(self, tmp_path):
        s = Score(bpm=100)
        s.mark_section("a", 0, 16)
        t = s.track("piano")
        for bar in range(16):
            t.chord(bar, 1, symbol="C", dur=3.5, vel=70)
        path = tmp_path / "solo.mid"
        s.write(str(path))
        report = validate_score(path, path.with_suffix(".score.json"))
        assert report.ok
        assert any("one track" in w for w in report.warnings)

    def test_missing_sections_warns(self, tmp_path):
        s = Score(bpm=100)
        a = s.track("a")
        b = s.track("b")
        for bar in range(16):
            a.note(bar, 1, "C4", 1, 70)
            b.note(bar, 2, "E4", 1, 70)
        path = tmp_path / "nosections.mid"
        s.write(str(path))
        report = validate_score(path, path.with_suffix(".score.json"))
        assert any("section" in w.lower() for w in report.warnings)


class TestCombinedGate:
    def test_a_valid_score_passes(self, tmp_path):
        midi, sidecar = _write(tmp_path, name="cand.mid")
        result = gate(midi, sidecar)
        assert result.ok
        assert result.validation.ok

    def test_an_unreadable_file_is_rejected_rather_than_raising(self, tmp_path):
        """The composer sees this text and gets a chance to fix it, so a crash
        here would cost the turn instead of correcting it."""
        bad = tmp_path / "junk.mid"
        bad.write_bytes(b"nope")
        result = gate(bad, None)
        assert not result.ok
        assert result.feedback()
