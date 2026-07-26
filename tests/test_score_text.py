"""Tests for the judge-readable score rendering.

Repeat collapsing is the load-bearing behaviour here. It is the compression that
keeps a six-minute piece affordable to judge, and it is simultaneously how the
single most common failure in machine-composed music (good material, looped
forever) becomes a fact on the page rather than something a judge has to notice.
"""

from __future__ import annotations

from houseband import score_text
from houseband.house import Score


def _notes_section(text: str) -> str:
    """Just the per-track note dump.

    Needed because the HARMONY table also has lines starting with "bar" and
    containing "|", and the NOTES preamble explains the "= bar N" notation, so a
    naive whole-document search matches all three.
    """
    _, _, tail = text.partition("NOTES  per track")
    return tail


def _spelled_out_bars(text: str) -> list[str]:
    return [
        line
        for line in _notes_section(text).splitlines()
        if line.strip().startswith("bar ") and "|" in line
    ]


def _looped(tmp_path, bars=32, vary=False):
    s = Score(bpm=120, key="C")
    s.mark_section("main", 0, bars)
    t = s.track("piano", patch="grand_piano")
    for bar in range(bars):
        vel = 60 + (bar % 8) * 5 if vary else 70
        pitch = "C4" if not vary else 60 + (bar % 12)
        t.note(bar, 1, pitch, 2.0, vel)
    path = tmp_path / "out.mid"
    s.write(str(path))
    return path, path.with_suffix(".score.json")


class TestHeader:
    def test_reports_key_time_and_length(self, tmp_path):
        midi, sidecar = _looped(tmp_path)
        text = score_text.render(midi, sidecar)
        assert "KEY C" in text
        assert "TIME 4/4" in text
        assert "BARS 32" in text
        assert "LENGTH" in text

    def test_collapses_a_tempo_ramp(self, tmp_path):
        """ramp_tempo writes one entry per bar; listing them all would bury the
        one fact a judge wants."""
        s = Score(bpm=60, key="C")
        s.mark_section("a", 0, 24)
        s.ramp_tempo(8, 20, 60, 120)
        t = s.track("piano")
        for bar in range(24):
            t.note(bar, 1, "C4", 1.0, 70)
        path = tmp_path / "ramp.mid"
        s.write(str(path))

        text = score_text.render(path, path.with_suffix(".score.json"))
        header = text.splitlines()[0]
        assert "->" in header, header
        assert header.count("bar") <= 3, f"tempo map not collapsed: {header}"


class TestStructure:
    def test_lists_sections_with_bar_ranges(self, tmp_path):
        s = Score(bpm=100)
        s.mark_section("intro", 0, 4)
        s.mark_section("verse", 4, 8)
        t = s.track("piano")
        for bar in range(12):
            t.note(bar, 1, "C4", 1.0, 70)
        path = tmp_path / "s.mid"
        s.write(str(path))

        text = score_text.render(path, path.with_suffix(".score.json"))
        assert "bars   0-3" in text and "intro" in text
        assert "bars   4-11" in text and "verse" in text

    def test_density_table_exposes_a_flat_arrangement(self, tmp_path):
        midi, sidecar = _looped(tmp_path)
        text = score_text.render(midi, sidecar)
        assert "DENSITY" in text

    def test_notes_are_missing_sections_when_undeclared(self, tmp_path):
        s = Score(bpm=100)
        t = s.track("piano")
        for bar in range(8):
            t.note(bar, 1, "C4", 1.0, 70)
        path = tmp_path / "n.mid"
        s.write(str(path))
        text = score_text.render(path, path.with_suffix(".score.json"))
        assert "none declared" in text


class TestRepeatDetection:
    def test_identical_bars_collapse(self, tmp_path):
        midi, sidecar = _looped(tmp_path, bars=32)
        text = score_text.render(midi, sidecar)
        assert "= bar 0" in _notes_section(text)
        # 32 identical bars: exactly one should be spelled out.
        spelled = _spelled_out_bars(text)
        assert len(spelled) == 1, f"expected 1 distinct bar, got {len(spelled)}"

    def test_repetition_percentage_is_reported(self, tmp_path):
        midi, sidecar = _looped(tmp_path, bars=32)
        text = score_text.render(midi, sidecar)
        assert "REPETITION" in text
        assert "97%" in text or "96%" in text, text.split("REPETITION")[1][:60]

    def test_varied_material_does_not_collapse(self, tmp_path):
        midi, sidecar = _looped(tmp_path, bars=24, vary=True)
        text = score_text.render(midi, sidecar)
        view = score_text.load_view(midi, sidecar)
        assert view.repeat_fraction() < 0.5

    def test_velocity_change_defeats_collapsing(self, tmp_path):
        """Two bars with the same notes but different dynamics are not the same
        bar, and a form judge needs to see that."""
        s = Score(bpm=120)
        s.mark_section("a", 0, 2)
        t = s.track("piano")
        t.note(0, 1, "C4", 1.0, 40)
        t.note(1, 1, "C4", 1.0, 110)
        path = tmp_path / "v.mid"
        s.write(str(path))
        text = score_text.render(path, path.with_suffix(".score.json"))
        assert len(_spelled_out_bars(text)) == 2
        assert not any("= bar" in line for line in _spelled_out_bars(text))


class TestNoteRendering:
    def test_format_is_beat_pitch_duration_velocity(self, tmp_path):
        s = Score(bpm=120)
        s.mark_section("a", 0, 1)
        t = s.track("piano")
        t.note(0, 2.5, "F#4", 1.5, 88)
        path = tmp_path / "one.mid"
        s.write(str(path))
        text = score_text.render(path, path.with_suffix(".score.json"))
        assert "2.5:F#4/1.5@88" in text, text

    def test_drums_render_as_names_not_numbers(self, tmp_path):
        s = Score(bpm=120)
        s.mark_section("a", 0, 1)
        d = s.drum_track()
        d.hit(0, 1, "kick", 100)
        d.hit(0, 3, "snare", 90)
        path = tmp_path / "d.mid"
        s.write(str(path))
        text = score_text.render(path, path.with_suffix(".score.json"))
        assert "kick@100" in text and "snare@90" in text

    def test_beats_are_not_reported_with_float_noise(self, tmp_path):
        s = Score(bpm=68)
        s.mark_section("a", 0, 8)
        t = s.track("gtr")
        for bar in range(8):
            t.chord(bar, 1, symbol="Am", dur=3.5, vel=60, spread=0.18)
        path = tmp_path / "spread.mid"
        s.write(str(path))
        text = score_text.render(path, path.with_suffix(".score.json"))
        # Two decimals at most, and never a beat 5 in 4/4.
        checked = 0
        for line in _spelled_out_bars(text):
            for token in line.split("|", 1)[1].split():
                beat = token.split(":")[0]
                if "." in beat:
                    assert len(beat.split(".")[1]) <= 2, token
                assert 1.0 <= float(beat) < 5.0, token
                checked += 1
        assert checked > 0, "no note tokens were checked"

    def test_compact_omits_the_note_dump(self, tmp_path):
        midi, sidecar = _looped(tmp_path)
        full = score_text.render(midi, sidecar)
        compact = score_text.render_compact(midi, sidecar)
        assert "NOTES" in full and "NOTES" not in compact
        assert len(compact) < len(full)


class TestChordDetection:
    def test_detects_a_simple_triad(self, tmp_path):
        s = Score(bpm=120, key="C")
        s.mark_section("a", 0, 4)
        t = s.track("piano")
        for bar in range(4):
            t.chord(bar, 1, symbol="Am", dur=3.5, vel=70, octave=3)
        path = tmp_path / "c.mid"
        s.write(str(path))
        text = score_text.render(path, path.with_suffix(".score.json"))
        assert "HARMONY" in text
        harmony = text.split("HARMONY")[1].split("REPETITION")[0]
        assert "Am" in harmony, harmony

    def test_returns_none_when_nothing_fits(self):
        assert score_text.detect_chord({}) is None
        # A dense chromatic cluster is not a chord; a confident wrong answer here
        # would be worse than admitting it.
        cluster = {pc: 1.0 for pc in range(12)}
        assert score_text.detect_chord(cluster) is None
