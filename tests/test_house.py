"""Tests for the composition library.

The timing tests matter more than they look. Every judge finding cites a bar
range, and a composer has to be able to act on that citation, so if bar/beat to
seconds conversion is off by an epsilon anywhere then the whole feedback loop is
quietly pointing at the wrong music.
"""

from __future__ import annotations

import json

import pytest

from houseband.house import Score, chord_pitches, note_number
from houseband.timing import TempoMap


class TestPitchParsing:
    def test_middle_c(self):
        assert note_number("C4") == 60
        assert note_number(60) == 60

    def test_accidentals(self):
        assert note_number("C#4") == 61
        assert note_number("Db4") == 61
        assert note_number("Bb3") == 58

    def test_default_octave(self):
        assert note_number("A") == note_number("A4")

    def test_rejects_nonsense(self):
        with pytest.raises(ValueError):
            note_number("H4")
        with pytest.raises(ValueError):
            note_number("Cx")


class TestChordParsing:
    def test_triads(self):
        assert chord_pitches("C", octave=4) == [60, 64, 67]
        assert chord_pitches("Cm", octave=4) == [60, 63, 67]

    def test_sevenths(self):
        assert chord_pitches("Am7", octave=3) == [57, 60, 64, 67]
        assert chord_pitches("Cmaj7", octave=4) == [60, 64, 67, 71]

    def test_extensions_stack_upward(self):
        # The ninth must be an octave above the second, not a compressed cluster.
        assert chord_pitches("C9", octave=4) == [60, 64, 67, 70, 74]

    def test_slash_chord_adds_bass_below(self):
        pitches = chord_pitches("C/G", octave=4)
        assert pitches[0] == note_number("G3")
        assert pitches[0] < pitches[1]

    def test_longest_quality_match_wins(self):
        # "m7b5" must not be parsed as "m7" with junk left over.
        assert chord_pitches("Bm7b5", octave=3) == [59, 62, 65, 69]

    def test_rejects_unknown_quality(self):
        with pytest.raises(ValueError):
            chord_pitches("Cwobble")


class TestTiming:
    def test_bar_and_beat_to_seconds(self):
        s = Score(bpm=120, time_sig=(4, 4))
        # At 120bpm a quarter note is 0.5s, so a 4/4 bar is 2s.
        assert s.time_at(0, 1) == pytest.approx(0.0)
        assert s.time_at(0, 3) == pytest.approx(1.0)
        assert s.time_at(1, 1) == pytest.approx(2.0)
        assert s.time_at(4, 1) == pytest.approx(8.0)

    def test_beat_overflow_crosses_the_bar_line(self):
        # Durations that run past the end of a bar are expressed as beat > 4.
        s = Score(bpm=120, time_sig=(4, 4))
        assert s.time_at(0, 5) == pytest.approx(s.time_at(1, 1))

    def test_tempo_change_does_not_displace_earlier_bars(self):
        s = Score(bpm=120, time_sig=(4, 4))
        before = s.time_at(1, 1)
        s.tempo(4, 60)
        assert s.time_at(1, 1) == pytest.approx(before)
        # After the change a bar takes twice as long.
        assert s.time_at(5, 1) - s.time_at(4, 1) == pytest.approx(4.0)

    def test_ramp_is_monotonic(self):
        s = Score(bpm=60, time_sig=(4, 4))
        s.ramp_tempo(0, 8, 60, 120)
        widths = [s.time_at(b + 1, 1) - s.time_at(b, 1) for b in range(8)]
        assert all(a >= b for a, b in zip(widths, widths[1:])), widths

    def test_round_trip_lands_on_the_downbeat(self):
        """The regression this exists for.

        A note written at bar N beat 1 previously came back as bar N-1 beat 5,
        because float error put it a hair under the boundary. That silently
        shifted every judge citation by one bar.
        """
        s = Score(bpm=68, time_sig=(4, 4))
        s.ramp_tempo(10, 20, 68, 78)
        tempo = TempoMap.from_structure(s.structure())
        for bar in range(0, 40):
            seconds = s.time_at(bar, 1)
            got_bar, got_beat = tempo.seconds_to_bar_beat(seconds)
            assert (got_bar, round(got_beat, 3)) == (bar, 1.0), (
                f"bar {bar} at {seconds}s came back as {got_bar}/{got_beat}"
            )

    def test_beat_never_reaches_bar_length_plus_one(self):
        s = Score(bpm=100, time_sig=(4, 4))
        tempo = TempoMap.from_structure(s.structure())
        for i in range(200):
            _, beat = tempo.seconds_to_bar_beat(i * 0.137)
            assert 1.0 <= beat < 5.0, beat


class TestScore:
    def test_rejects_empty(self, tmp_path):
        s = Score()
        with pytest.raises(ValueError, match="no tracks"):
            s.write(str(tmp_path / "out.mid"))

        s.track("piano")
        with pytest.raises(ValueError, match="no notes"):
            s.write(str(tmp_path / "out.mid"))

    def test_rejects_duplicate_track_names(self):
        s = Score()
        s.track("piano")
        with pytest.raises(ValueError, match="duplicate"):
            s.track("piano")

    def test_rejects_bad_pan_and_patch(self):
        s = Score()
        with pytest.raises(ValueError, match="pan"):
            s.track("a", pan=2.0)
        with pytest.raises(ValueError, match="unknown patch"):
            s.track("b", patch="tuba_of_doom")

    def test_zero_duration_note_rejected(self):
        s = Score()
        t = s.track("piano")
        with pytest.raises(ValueError, match="dur"):
            t.note(0, 1, "C4", 0)

    def test_total_bars_without_sections(self):
        """A score with no declared sections still reports a real length."""
        s = Score()
        t = s.track("piano")
        t.note(15, 1, "C4", 1)
        assert s.total_bars == 16

    def test_total_bars_takes_the_max(self):
        s = Score()
        s.mark_section("long", 0, 32)
        t = s.track("piano")
        t.note(3, 1, "C4", 1)
        assert s.total_bars == 32

    def test_write_emits_sidecar(self, tmp_path):
        s = Score(bpm=90, key="Dm")
        s.mark_section("a", 0, 4)
        t = s.track("keys", patch="electric_piano", pan=-0.5)
        for bar in range(4):
            t.chord(bar, 1, symbol="Dm7", dur=3.5, vel=70)
        path = tmp_path / "out.mid"
        s.write(str(path))

        sidecar = path.with_suffix(".score.json")
        assert sidecar.exists()
        data = json.loads(sidecar.read_text())
        assert data["key"] == "Dm"
        assert data["sections"] == [{"name": "a", "start_bar": 0, "bars": 4}]
        assert data["tracks"][0]["pan"] == -0.5
        assert data["tracks"][0]["note_count"] == 16

    def test_drum_hits_need_a_drum_track(self):
        s = Score()
        melodic = s.track("piano")
        with pytest.raises(ValueError, match="not a drum track"):
            melodic.hit(0, 1, "kick")

        drums = s.drum_track()
        drums.hit(0, 1, "kick")
        with pytest.raises(ValueError, match="unknown drum"):
            drums.hit(0, 1, "bongo_of_theseus")

    def test_chord_requires_exactly_one_source(self):
        s = Score()
        t = s.track("piano")
        with pytest.raises(ValueError, match="exactly one"):
            t.chord(0, 1, symbol="C", pitches=["C4"])
        with pytest.raises(ValueError, match="exactly one"):
            t.chord(0, 1)

    def test_spread_offsets_voices_in_time(self):
        s = Score(bpm=120)
        t = s.track("gtr")
        t.chord(0, 1, symbol="C", dur=2.0, vel=64, spread=0.25)
        starts = sorted(n[1] for n in t.notes)
        assert len(set(starts)) == 3, "spread should stagger every voice"
        assert starts[1] > starts[0]
