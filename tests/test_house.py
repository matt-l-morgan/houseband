"""Tests for the composition library.

The timing tests matter more than they look. Every judge finding cites a bar
range, and a composer has to be able to act on that citation, so if bar/beat to
seconds conversion is off by an epsilon anywhere then the whole feedback loop is
quietly pointing at the wrong music.

:class:`TestWrittenFile` reads the bytes back rather than asserting on in-memory
objects, for the same reason ``tests/test_export.py`` does: the deliverable is a
file another program opens, and a test that only inspects our own objects would
pass happily while shipping a file whose bar grid is wrong.
"""

from __future__ import annotations

import json

import mido
import pretty_midi
import pytest

from houseband.house import Score, chord_pitches, note_number
from houseband.house.core import TICKS_PER_QUARTER, NoteEvent
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


# ---------------------------------------------------------------------------
# The written file
# ---------------------------------------------------------------------------

# How far a note may move between the Score and the file it is written to, in
# seconds. Exact equality is not reachable at any resolution, so the bound is
# stated rather than assumed, and two separate things set it.
#
# A note not written on an exact tick boundary is rounded to the nearest tick,
# which is half a tick at worst: 0.92ms at 480 PPQ and 68 BPM. The strummed keys
# part in the score below is deliberately built that way (0.07 of a beat is 33.6
# ticks) so that the tests exercise the rounding instead of only asserting on
# positions that happen to be exact. Separately, MIDI stores tempo as an integer
# number of microseconds per quarter note, so a 68.5 BPM bar is a hair off and the
# error accumulates bar by bar; measured across the 68-bar ramp below that term is
# 0.02ms, three orders of magnitude under the other one.
#
# 2ms covers both with headroom and is still well under the ~10ms where a
# listener starts to hear a note as early.
SECONDS_TOLERANCE = 0.002

# For notes that do sit on an exact tick, the tick rounding term disappears and
# only the tempo quantisation is left, so those can be held to a much tighter
# bound. Keeping the two apart is what makes a regression in either one visible.
GRID_SECONDS_TOLERANCE = 0.0001


def _score(ramp: bool = True) -> Score:
    """A 68-bar score, with or without a tempo ramp over the same material.

    68 BPM through bar 40, ramping to 78 by bar 60, flat after that. Notes sit on
    the downbeat of every bar so that a test can name the exact tick each one has
    to land on, and the keys part is strummed so that some notes fall between
    ticks. Passing ``ramp=False`` gives the identical notes under a constant
    tempo, which is what makes the two comparable.
    """
    s = Score(bpm=68, key="Am", time_sig=(4, 4))
    s.mark_section("verse", 0, 40)
    s.mark_section("lift", 40, 28)
    if ramp:
        s.ramp_tempo(40, 60, 68, 78)

    lead = s.track("lead", patch="saw_lead", pan=-0.4)
    keys = s.track("keys", patch="electric_piano", pan=0.25)
    drums = s.drum_track("drums")
    for bar in range(68):
        lead.note(bar, 1, 64 + (bar % 5), 1.0, 70 + (bar % 7))
        keys.chord(bar, 1, symbol="Am7", dur=3.5, vel=60 + (bar % 5), octave=4, spread=0.07)
        drums.hit(bar, 1, "kick", 96)
        drums.hit(bar, 3, "snare", 84)
    return s


def _absolute(track: mido.MidiTrack) -> list[tuple[int, mido.Message]]:
    """A track's messages paired with their absolute tick.

    MIDI files store delta times, and every assertion below is about where an
    event sits on the grid, so undoing the deltas is the first thing any of them
    has to do.
    """
    tick = 0
    out = []
    for message in track:
        tick += message.time
        out.append((tick, message))
    return out


def _note_on_ticks(path, name: str) -> list[int]:
    """Absolute ticks of every note-on in the named track of a written file."""
    for track in mido.MidiFile(str(path)).tracks:
        if track.name == name:
            return [
                tick
                for tick, message in _absolute(track)
                if message.type == "note_on" and message.velocity > 0
            ]
    raise AssertionError(f"no track named {name!r} in {path}")


def _meta(path, kind: str) -> list[tuple[int, mido.MetaMessage]]:
    """Absolute-ticked meta events of one type from the conductor track."""
    conductor = mido.MidiFile(str(path)).tracks[0]
    return [(tick, m) for tick, m in _absolute(conductor) if m.type == kind]


def _downbeat(bar: int) -> int:
    """The tick a 4/4 bar's downbeat has to be at, spelled out the long way."""
    return bar * 4 * TICKS_PER_QUARTER


class TestWrittenFile:
    """The file a DAW opens, read back with mido and pretty_midi.

    The bug these exist for: ``write()`` used to hand pretty_midi note times in
    seconds and a single opening tempo, and pretty_midi converted those seconds to
    ticks at that one flat tempo. Ticks are musical time, so under the ramp below
    the notes walked steadily off the bar grid: the downbeat of bar 67 arrived 8.7
    beats early, and the error grew with the length of the piece. The seconds were
    right the whole time, which is exactly why nothing caught it until somebody
    imported the file.
    """

    def test_a_note_keeps_both_its_seconds_and_its_musical_position(self):
        """The parallel record is what makes a correct file writable at all.

        Seconds cannot be turned back into an exact grid position once a tempo
        change sits between the note and bar 0, so the bar and beat the composer
        wrote are kept as given. ``notes`` keeps its tuple shape because
        ``houseband.export`` and ``houseband.validator`` duck-type against it.
        """
        s = Score(bpm=120)
        s.tempo(2, 60)
        t = s.track("piano")
        t.note(2, 3, "C4", 1.5, 80)

        assert t.notes == [(60, s.time_at(2, 3), s.time_at(2, 4.5), 80)]
        assert t.events == [NoteEvent(pitch=60, bar=2, beat=3.0, dur=1.5, velocity=80)]

    def test_downbeats_land_on_musical_ticks_under_a_tempo_ramp(self, tmp_path):
        """The regression this whole class exists for."""
        s = _score()
        path = tmp_path / "out.mid"
        s.write(str(path))

        assert mido.MidiFile(str(path)).ticks_per_beat == TICKS_PER_QUARTER
        # One lead note per bar, on the downbeat, so the whole list can be named.
        assert _note_on_ticks(path, "lead") == [_downbeat(bar) for bar in range(68)]
        # Literal ticks for one bar inside the ramp and one past it, so the
        # comparison above is anchored to real numbers instead of to the same
        # arithmetic it is checking.
        assert s.tick_at(50, 1) == 96000
        assert s.tick_at(67, 1) == 128640

        # And the drums, which arrive through hit() rather than note(), so the
        # musical position survives that path too. Kick on 1, snare on 3.
        assert _note_on_ticks(path, "drums") == [
            tick
            for bar in range(68)
            for tick in (_downbeat(bar), _downbeat(bar) + 2 * TICKS_PER_QUARTER)
        ]

        # What the old writer did, kept as a number so that a regression cannot be
        # mistaken for noise: seconds scaled by the opening tempo alone. Restated
        # at this resolution rather than the 220 PPQ pretty_midi defaulted to, so
        # that the two tick counts are comparable; as beats it is 8.7 either way.
        flat_seconds_per_tick = 60.0 / 68.0 / TICKS_PER_QUARTER
        drifted = round(s.time_at(67, 1) / flat_seconds_per_tick)
        assert _downbeat(67) - drifted == 4174

    def test_the_note_grid_does_not_move_when_the_tempo_does(self, tmp_path):
        """Same notes, two tempo maps, identical ticks.

        This is what it means for the file to be written in musical time, and it
        is the property the old writer lacked: there, every tick came out of a
        seconds value, so editing the tempo map silently moved the notes relative
        to the bars they were written in.
        """
        ramped, flat = tmp_path / "ramped.mid", tmp_path / "flat.mid"
        _score(ramp=True).write(str(ramped))
        _score(ramp=False).write(str(flat))

        for name in ("lead", "keys", "drums"):
            assert _note_on_ticks(ramped, name) == _note_on_ticks(flat, name), name

    def test_every_tempo_in_the_map_is_written_at_its_own_tick(self, tmp_path):
        s = _score()
        path = tmp_path / "out.mid"
        s.write(str(path))

        written = [(tick, m.tempo) for tick, m in _meta(path, "set_tempo")]

        # ramp_tempo() restates the opening tempo at the bar it starts on and the
        # writer drops that repeat, so the expectation drops it the same way.
        expected: list[tuple[int, float]] = []
        for bar, bpm in zip(s._tempo_bars, s._tempo_bpms):
            if expected and abs(bpm - expected[-1][1]) < 1e-9:
                continue
            expected.append((bar, bpm))
        assert written == [(_downbeat(bar), mido.bpm2tempo(bpm)) for bar, bpm in expected]

        # Which comes to 68 BPM at bar 0 and then one entry per bar from 41 to 60.
        assert len(written) == 21
        assert written[0] == (0, 882353)          # 60_000_000 / 68
        assert written[1] == (_downbeat(41), mido.bpm2tempo(68.5))
        assert written[-1] == (_downbeat(60), mido.bpm2tempo(78.0))
        assert [tick for tick, _ in written] == sorted({tick for tick, _ in written})

        # And what pretty_midi makes of it, which is what every reader in this
        # repo sees. A hundredth of a BPM is far below anything a DAW displays.
        times, tempi = pretty_midi.PrettyMIDI(str(path)).get_tempo_changes()
        assert len(tempi) == 21
        assert tempi[0] == pytest.approx(68.0, abs=0.01)
        assert tempi[-1] == pytest.approx(78.0, abs=0.01)
        # The last change lands where bar 60 lands in seconds, too.
        assert times[-1] == pytest.approx(s.time_at(60, 1), abs=SECONDS_TOLERANCE)

    def test_the_time_signature_is_written(self, tmp_path):
        s = _score()
        path = tmp_path / "out.mid"
        s.write(str(path))

        assert [(tick, m.numerator, m.denominator) for tick, m in _meta(path, "time_signature")] == [
            (0, 4, 4)
        ]
        parsed = pretty_midi.PrettyMIDI(str(path)).time_signature_changes
        assert len(parsed) == 1
        assert (parsed[0].numerator, parsed[0].denominator) == (4, 4)

    def test_a_compound_time_signature_grids_in_its_own_bar_length(self, tmp_path):
        """6/8 is three quarter notes to the bar, not four.

        Worth its own test because the bar length is the one place where the tick
        grid and the beat count disagree, and a writer that assumed four quarters
        everywhere would still pass every 4/4 assertion above.
        """
        s = Score(bpm=96, key="Em", time_sig=(6, 8))
        lead = s.track("lead", patch="flute")
        for bar in range(8):
            for beat in (1, 4):
                lead.note(bar, beat, "E5", 1.0, 70)
        path = tmp_path / "six_eight.mid"
        s.write(str(path))

        assert [(m.numerator, m.denominator) for _, m in _meta(path, "time_signature")] == [(6, 8)]
        # A 6/8 bar is 1440 ticks, and beat 4 is the second dotted quarter, three
        # eighth notes in.
        assert _note_on_ticks(path, "lead") == [
            tick for bar in range(8) for tick in (bar * 1440, bar * 1440 + 720)
        ]

    def test_names_programs_channels_and_pan_survive(self, tmp_path):
        s = _score()
        path = tmp_path / "out.mid"
        s.write(str(path))

        parsed = pretty_midi.PrettyMIDI(str(path))
        assert [i.name for i in parsed.instruments] == ["lead", "keys", "drums"]
        assert [(int(i.program), i.is_drum) for i in parsed.instruments] == [
            (81, False),   # saw_lead
            (4, False),    # electric_piano
            (0, True),     # GM picks the kit by channel, so the program is filler
        ]
        # Pan as CC10 at time zero, 64 being centre: -0.4 is 38 and +0.25 is 79.
        assert [
            [c.value for c in i.control_changes if c.number == 10] for i in parsed.instruments
        ] == [[38], [79], [64]]

        raw = mido.MidiFile(str(path))
        assert raw.type == 1
        # Track 0 is the conductor and carries no notes, so a DAW does not make an
        # empty part out of it.
        assert [t.name for t in raw.tracks] == ["", "lead", "keys", "drums"]
        channels = {
            t.name: {m.channel for m in t if m.type in ("note_on", "note_off")}
            for t in raw.tracks[1:]
        }
        assert channels["drums"] == {9}, "drums must be on channel 10 (index 9)"
        assert 9 not in channels["lead"] | channels["keys"]
        assert channels["lead"] != channels["keys"]

    def test_sections_arrive_as_markers(self, tmp_path):
        s = _score()
        path = tmp_path / "out.mid"
        s.write(str(path))
        assert [(tick, m.text) for tick, m in _meta(path, "marker")] == [
            (0, "verse"),
            (_downbeat(40), "lift"),
        ]

    def test_a_name_midi_cannot_spell_does_not_lose_the_write(self, tmp_path):
        """A model-written program will reach for a typographic dash sooner or later.

        MIDI text is latin-1 bytes, so a character outside that set raises on save
        and takes the whole composition with it. The file gets a lossy spelling
        and the sidecar keeps what was actually declared.
        """
        s = Score(bpm=100, key="C")
        s.mark_section("outro — reprise", 0, 4)
        t = s.track("keys ✨", patch="electric_piano")
        t.note(0, 1, "C4", 1.0, 70)
        path = tmp_path / "unicode.mid"
        s.write(str(path))

        assert [track.name for track in mido.MidiFile(str(path)).tracks][1] == "keys ?"
        assert [m.text for _, m in _meta(path, "marker")] == ["outro ? reprise"]

        data = json.loads(path.with_suffix(".score.json").read_text())
        assert data["tracks"][0]["name"] == "keys ✨"
        assert data["sections"][0]["name"] == "outro — reprise"

    def test_note_seconds_survive_the_round_trip(self, tmp_path):
        """Musical ticks must not have cost us the seconds, which were correct.

        Tolerances and the reasoning behind them are at the top of this section.
        """
        s = _score()
        path = tmp_path / "out.mid"
        s.write(str(path))

        parsed = {i.name: i for i in pretty_midi.PrettyMIDI(str(path)).instruments}
        for track in s.tracks:
            original = sorted(track.notes, key=lambda n: (n[1], n[0]))
            written = sorted(parsed[track.name].notes, key=lambda n: (n.start, n.pitch))
            assert len(written) == len(original), track.name
            # The lead and the drums are on exact ticks; only the strummed keys
            # part has to pay for rounding.
            tolerance = SECONDS_TOLERANCE if track.name == "keys" else GRID_SECONDS_TOLERANCE
            for (pitch, start, end, velocity), note in zip(original, written):
                assert (note.pitch, note.velocity) == (pitch, velocity)
                assert note.start == pytest.approx(start, abs=tolerance)
                assert note.end == pytest.approx(end, abs=tolerance)

    def test_a_constant_tempo_score_is_untouched_by_the_fix(self, tmp_path):
        """The common case, which had nothing wrong with it and still has not.

        At a constant tempo the old writer and this one agree on every position,
        so this is a plain regression guard: one tempo event, notes on exact
        musical ticks, seconds back within the grid tolerance.
        """
        s = _score(ramp=False)
        path = tmp_path / "flat.mid"
        s.write(str(path))

        assert [(tick, m.tempo) for tick, m in _meta(path, "set_tempo")] == [(0, 882353)]
        assert _note_on_ticks(path, "lead") == [_downbeat(bar) for bar in range(68)]

        parsed = {i.name: i for i in pretty_midi.PrettyMIDI(str(path)).instruments}
        for name in ("lead", "drums"):
            track = next(t for t in s.tracks if t.name == name)
            original = sorted(track.notes, key=lambda n: (n[1], n[0]))
            written = sorted(parsed[name].notes, key=lambda n: (n.start, n.pitch))
            for (_, start, end, _), note in zip(original, written):
                assert note.start == pytest.approx(start, abs=GRID_SECONDS_TOLERANCE)
                assert note.end == pytest.approx(end, abs=GRID_SECONDS_TOLERANCE)

    def test_the_same_score_always_writes_the_same_bytes(self, tmp_path):
        """Claimed by the writer's tiebreaking sort, so it is worth pinning.

        Without it nobody can tell from a checksum whether ``out.mid`` changed
        because the music changed.
        """
        first, second, third = (tmp_path / f"{n}.mid" for n in "abc")
        s = _score()
        s.write(str(first))
        s.write(str(second))
        _score().write(str(third))

        assert first.read_bytes() == second.read_bytes()
        assert first.read_bytes() == third.read_bytes()

    def test_the_sidecar_still_carries_the_whole_score(self, tmp_path):
        """The sidecar stays the lossless record, so it keeps what the file drops.

        The written tempo map collapses repeated values; the sidecar does not,
        because it is what every tool in this repo reads to recover exactly what
        the composer declared.
        """
        s = _score()
        path = tmp_path / "out.mid"
        s.write(str(path))

        data = json.loads(path.with_suffix(".score.json").read_text())
        assert data["key"] == "Am"
        assert data["time_sig"] == [4, 4]
        assert data["sections"] == [
            {"name": "verse", "start_bar": 0, "bars": 40},
            {"name": "lift", "start_bar": 40, "bars": 28},
        ]
        assert data["total_bars"] == 68
        assert data["duration"] == pytest.approx(s.duration, abs=0.001)
        assert [t["name"] for t in data["tracks"]] == ["lead", "keys", "drums"]
        assert [t["note_count"] for t in data["tracks"]] == [68, 272, 136]
        assert [t["pan"] for t in data["tracks"]] == [-0.4, 0.25, 0.0]
        assert [t["is_drum"] for t in data["tracks"]] == [False, False, True]

        # 22 entries: bar 0, plus one per bar of the ramp including the bar 40
        # restatement of 68 BPM that the MIDI file drops as a no-op.
        assert len(data["tempo_map"]) == 22
        assert data["tempo_map"][:3] == [[0, 68.0], [40, 68.0], [41, 68.5]]
        assert data["tempo_map"][-1] == [60, 78.0]
