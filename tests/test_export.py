"""Tests for the DAW export bundle.

Two things distinguish these tests from the rest of the suite.

**They read the files back with pretty_midi and mido rather than trusting the
writer.** The entire value of this module is that another program -- Ableton, Pro
Tools -- can open what it produces. A test that only asserts on our own in-memory
objects would pass happily while shipping a file no DAW can read, which is the
exact failure the module exists to prevent.

**They assert an asymmetry on purpose.** An overlapping same-pitch note is a
warning from ``validate_score`` and a hard problem for ``check_daw_ready``. That
is not an inconsistency, it is the design: a composer that gets warned still
reaches the judges and can fix it next turn, whereas a producer who opens a file
with a stuck note in it has no next turn.
"""

from __future__ import annotations

import zipfile

import mido
import pretty_midi
import pytest

from houseband.export import (
    TICKS_PER_QUARTER,
    check_daw_ready,
    export_bundle,
    safe_name,
)
from houseband.house import Score
from houseband.validator import validate_score

BARS = 16


def _starter(tmp_path, name="out.mid", bars=BARS, bpm=100, sections=True):
    """A clean, loopable starter: drums, bass, keys, pad. 16 bars by default."""
    s = Score(bpm=bpm, key="Am", time_sig=(4, 4))
    if sections:
        s.mark_section("groove", 0, bars // 2)
        s.mark_section("lift", bars // 2, bars - bars // 2)

    drums = s.drum_track("drums")
    bass = s.track("bass", patch="fingered_bass", pan=0.0)
    keys = s.track("keys", patch="electric_piano", pan=0.25)
    pad = s.track("pad", patch="warm_pad", pan=-0.3)

    progression = ["Am7", "F", "Cmaj7", "G"]
    for bar in range(bars):
        chord = progression[bar % 4]
        root = ["A1", "F1", "C2", "G1"][bar % 4]

        drums.hit(bar, 1, "kick", 96 + (bar % 3))
        drums.hit(bar, 3, "snare", 84 + (bar % 5))
        for beat in (1, 2, 3, 4):
            drums.hit(bar, beat, "hat_closed", 50 if beat % 2 else 40)

        bass.note(bar, 1, root, 1.5, 80 + (bar % 4))
        bass.note(bar, 3, root, 0.9, 70 + (bar % 3))
        keys.chord(bar, 1, symbol=chord, dur=3.5, vel=60 + (bar % 6), octave=4)
        pad.chord(bar, 1, symbol=chord, dur=3.9, vel=44 + (bar % 4), octave=5)

    path = tmp_path / name
    s.write(str(path))
    return path, path.with_suffix(".score.json")


def _two_track(tmp_path, name="out.mid"):
    """The smallest score that is not a single-track score."""
    s = Score(bpm=120, key="C")
    s.mark_section("main", 0, 8)
    a = s.track("lead", patch="saw_lead")
    b = s.track("bass", patch="fingered_bass")
    for bar in range(8):
        a.note(bar, 1, 72 + (bar % 5), 1.0, 70 + bar)
        b.note(bar, 1, "C2", 2.0, 80 - bar)
    path = tmp_path / name
    s.write(str(path))
    return path, path.with_suffix(".score.json")


# ---------------------------------------------------------------------------
# Bundle shape
# ---------------------------------------------------------------------------


class TestBundle:
    def test_clean_score_exports_zip_combined_and_one_part_per_track(self, tmp_path):
        midi, sidecar = _starter(tmp_path)
        result = export_bundle(midi, sidecar, out_dir=tmp_path / "export", stem="starter")

        assert result.ok, result.feedback()
        assert result.problems == []
        assert result.zip_path is not None and result.zip_path.exists()
        assert result.combined_midi is not None and result.combined_midi.exists()
        assert result.readme_path is not None and result.readme_path.exists()

        assert set(result.part_files) == {"drums", "bass", "keys", "pad"}
        for path in result.part_files.values():
            assert path.exists()
            assert path.parent.name == "parts"

    def test_zip_contains_every_artifact_under_one_folder(self, tmp_path):
        midi, sidecar = _starter(tmp_path)
        result = export_bundle(midi, sidecar, out_dir=tmp_path / "export", stem="starter")

        with zipfile.ZipFile(result.zip_path) as archive:
            names = sorted(archive.namelist())

        assert names == [
            "starter/README.txt",
            "starter/parts/01_drums.mid",
            "starter/parts/02_bass.mid",
            "starter/parts/03_keys.mid",
            "starter/parts/04_pad.mid",
            "starter/starter_full.mid",
        ]

    def test_parts_are_numbered_drums_then_bass_then_the_rest(self, tmp_path):
        """A producer looking in parts/ wants the rhythm section first."""
        midi, sidecar = _starter(tmp_path)
        result = export_bundle(midi, sidecar, out_dir=tmp_path / "export")
        assert result.part_files["drums"].name.startswith("01_")
        assert result.part_files["bass"].name.startswith("02_")

    def test_default_out_dir_sits_beside_the_source(self, tmp_path):
        midi, sidecar = _starter(tmp_path)
        result = export_bundle(midi, sidecar)
        assert result.zip_path == tmp_path / "export" / "starter.zip"

    def test_disk_layout_mirrors_the_zip(self, tmp_path):
        midi, sidecar = _starter(tmp_path)
        out = tmp_path / "export"
        result = export_bundle(midi, sidecar, out_dir=out, stem="starter")

        assert result.zip_path == out / "starter.zip"
        assert result.combined_midi == out / "starter" / "starter_full.mid"
        assert result.readme_path == out / "starter" / "README.txt"
        assert result.part_files["drums"] == out / "starter" / "parts" / "01_drums.mid"

    def test_two_stems_share_an_out_dir_without_colliding(self, tmp_path):
        """houseband.server exports every candidate into one exports/ directory."""
        first_midi, first_sidecar = _starter(tmp_path, name="a.mid", bpm=88)
        second_midi, second_sidecar = _two_track(tmp_path, name="b.mid")
        shared = tmp_path / "exports"

        one = export_bundle(first_midi, first_sidecar, out_dir=shared, stem="c1")
        two = export_bundle(second_midi, second_sidecar, out_dir=shared, stem="c2")

        assert one.ok and two.ok
        assert one.zip_path == shared / "c1.zip"
        assert two.zip_path == shared / "c2.zip"
        # The first bundle's parts are still there and still its own.
        for path in one.part_files.values():
            assert path.exists()
        assert set(one.part_files) & set(two.part_files) == {"bass"}
        assert one.part_files["bass"] != two.part_files["bass"]
        assert one.part_files["bass"].read_bytes() != two.part_files["bass"].read_bytes()

    def test_signature_satisfies_the_servers_introspection(self):
        """houseband.server builds the kwargs by name, so the names are a contract.

        ``_generate_export`` gives up entirely if ``export_bundle`` declares a
        required parameter it cannot name a value for, so a rename here silently
        turns the download button in the web UI into a 404.
        """
        import inspect

        parameters = inspect.signature(export_bundle).parameters
        offered = {"midi_path", "sidecar_path", "out_dir", "stem"}
        for name, parameter in parameters.items():
            if parameter.default is parameter.empty:
                assert name in offered, f"{name} is required but the server cannot supply it"

    def test_export_refuses_when_the_file_is_not_daw_ready(self, tmp_path):
        s = Score(bpm=100, key="C")
        s.mark_section("main", 0, 8)
        t = s.track("pad", patch="warm_pad")
        other = s.track("bass", patch="fingered_bass")
        for bar in range(8):
            # Whole-bar pad plus a retrigger of the same pitch halfway through.
            t.note(bar, 1, "C4", 4.0, 60)
            t.note(bar, 3, "C4", 2.0, 60)
            other.note(bar, 1, "C2", 2.0, 70)
        midi = tmp_path / "stuck.mid"
        s.write(str(midi))

        out_dir = tmp_path / "export"
        result = export_bundle(midi, out_dir=out_dir)

        assert not result.ok
        assert result.zip_path is None
        assert result.combined_midi is None
        assert result.part_files == {}
        assert result.readme_path is None
        # Nothing half-written: a partial bundle is worse than none.
        assert not out_dir.exists()

    def test_safe_name_survives_hostile_track_names(self):
        assert safe_name("lead gtr / DI") == "lead_gtr_DI"
        assert safe_name("  ../evil  ") == "evil"
        assert safe_name("***") == "track"


# ---------------------------------------------------------------------------
# Part files, read back
# ---------------------------------------------------------------------------


class TestPartFiles:
    def test_part_files_carry_tempo_note_count_and_drum_flag(self, tmp_path):
        midi, sidecar = _starter(tmp_path, bpm=93)
        source = pretty_midi.PrettyMIDI(str(midi))
        expected = {inst.name: len(inst.notes) for inst in source.instruments}

        result = export_bundle(midi, sidecar, out_dir=tmp_path / "export")

        for track_name, path in result.part_files.items():
            part = pretty_midi.PrettyMIDI(str(path))
            assert len(part.instruments) == 1, f"{track_name} is not a single part"
            inst = part.instruments[0]

            assert inst.name == track_name
            assert len(inst.notes) == expected[track_name]
            assert inst.is_drum == (track_name == "drums")

            # MIDI stores tempo as an integer number of microseconds per quarter,
            # so the value never comes back exactly. A hundredth of a BPM is far
            # below anything a DAW would show.
            times, tempi = part.get_tempo_changes()
            assert tempi[0] == pytest.approx(93.0, abs=0.01)
            assert times[0] == pytest.approx(0.0)

            assert len(part.time_signature_changes) == 1
            assert part.time_signature_changes[0].numerator == 4
            assert part.time_signature_changes[0].denominator == 4

    def test_drums_stay_on_channel_ten_with_gm_key_mapping(self, tmp_path):
        midi, sidecar = _starter(tmp_path)
        result = export_bundle(midi, sidecar, out_dir=tmp_path / "export")

        track = mido.MidiFile(str(result.part_files["drums"]))
        channels = {m.channel for m in track if m.type in ("note_on", "note_off")}
        assert channels == {9}, "drums must be on channel 10 (index 9)"

        pitches = {m.note for m in track if m.type == "note_on"}
        # Untransposed GM percussion keys: kick, snare, closed hat.
        assert pitches == {36, 38, 42}

    def test_part_note_times_match_the_source_in_seconds(self, tmp_path):
        midi, sidecar = _starter(tmp_path, bpm=77)
        result = export_bundle(midi, sidecar, out_dir=tmp_path / "export")

        source = {i.name: i for i in pretty_midi.PrettyMIDI(str(midi)).instruments}
        for track_name, path in result.part_files.items():
            original = sorted(source[track_name].notes, key=lambda n: (n.start, n.pitch))
            written = sorted(
                pretty_midi.PrettyMIDI(str(path)).instruments[0].notes,
                key=lambda n: (n.start, n.pitch),
            )
            for before, after in zip(original, written):
                assert after.pitch == before.pitch
                assert after.velocity == before.velocity
                # One tick at 480 PPQ and 77 BPM is about 1.6ms.
                assert after.start == pytest.approx(before.start, abs=0.005)
                assert after.end == pytest.approx(before.end, abs=0.005)

    def test_part_file_is_type_zero_so_a_daw_makes_exactly_one_track(self, tmp_path):
        midi, sidecar = _starter(tmp_path)
        result = export_bundle(midi, sidecar, out_dir=tmp_path / "export")
        for path in result.part_files.values():
            assert mido.MidiFile(str(path)).type == 0

    def test_part_files_carry_the_whole_tempo_map_not_just_the_first_entry(self, tmp_path):
        """Every file in the bundle carries the map, not only the combined one."""
        s = Score(bpm=80, key="Em")
        s.mark_section("a", 0, 8)
        s.tempo(4, 120.0)
        lead = s.track("lead", patch="saw_lead")
        bass = s.track("bass", patch="fingered_bass")
        for bar in range(8):
            lead.note(bar, 1, 64 + bar, 1.0, 70 + bar)
            bass.note(bar, 1, "E1", 2.0, 80)
        midi = tmp_path / "ramp.mid"
        s.write(str(midi))

        # Score.write() emits the whole map itself now, so the job here is to
        # carry through what it was given rather than to be the only writer that
        # gets this right.
        _, source_tempi = pretty_midi.PrettyMIDI(str(midi)).get_tempo_changes()
        assert len(source_tempi) == 2

        result = export_bundle(midi, out_dir=tmp_path / "export")
        assert result.ok, result.feedback()
        for path in [result.combined_midi, *result.part_files.values()]:
            times, tempi = pretty_midi.PrettyMIDI(str(path)).get_tempo_changes()
            assert [pytest.approx(t, abs=0.01) for t in (80.0, 120.0)] == list(tempi)
            # The change lands on the bar 4 downbeat: four bars at 80 BPM.
            assert times[1] == pytest.approx(4 * 4 * 60.0 / 80.0, abs=0.01)


# ---------------------------------------------------------------------------
# The combined file
# ---------------------------------------------------------------------------


class TestCombinedFile:
    def test_track_names_survive_the_round_trip(self, tmp_path):
        midi, sidecar = _starter(tmp_path)
        result = export_bundle(midi, sidecar, out_dir=tmp_path / "export")

        read_back = pretty_midi.PrettyMIDI(str(result.combined_midi))
        assert [i.name for i in read_back.instruments] == ["drums", "bass", "keys", "pad"]

        # And at the meta-event level, which is what a DAW actually reads.
        raw = mido.MidiFile(str(result.combined_midi))
        assert raw.type == 1
        assert raw.ticks_per_beat == TICKS_PER_QUARTER
        # Track 0 is the conductor; its name is the sequence name, not a part.
        assert [t.name for t in raw.tracks[1:]] == ["drums", "bass", "keys", "pad"]

    def test_pretty_midi_alone_also_preserves_names(self, tmp_path):
        """Documents why mido is here, and why it is not here for the names.

        pretty_midi does round-trip Instrument.name correctly at 0.2.11, so mido
        is used for the tempo map, channel control and reproducible bytes rather
        than to rescue the names. If a future pretty_midi regresses this, the
        assertion below fails and the docstring in houseband/export.py stops
        being a claim nobody checked.
        """
        source = pretty_midi.PrettyMIDI(initial_tempo=100.0)
        for name, program, drum in (("gtr", 25, False), ("kit", 0, True)):
            inst = pretty_midi.Instrument(program=program, is_drum=drum, name=name)
            inst.control_changes.append(
                pretty_midi.ControlChange(number=10, value=64, time=0.0)
            )
            inst.notes.append(
                pretty_midi.Note(velocity=80, pitch=40, start=0.0, end=0.5)
            )
            source.instruments.append(inst)
        path = tmp_path / "names.mid"
        source.write(str(path))

        assert [i.name for i in pretty_midi.PrettyMIDI(str(path)).instruments] == [
            "gtr",
            "kit",
        ]

    def test_combined_contains_every_part_and_every_note(self, tmp_path):
        midi, sidecar = _starter(tmp_path)
        result = export_bundle(midi, sidecar, out_dir=tmp_path / "export")

        source = pretty_midi.PrettyMIDI(str(midi))
        combined = pretty_midi.PrettyMIDI(str(result.combined_midi))
        assert len(combined.instruments) == len(source.instruments)
        assert sum(len(i.notes) for i in combined.instruments) == sum(
            len(i.notes) for i in source.instruments
        )
        assert {i.program for i in combined.instruments if not i.is_drum} == {
            33,
            4,
            89,
        }

    def test_sections_become_markers(self, tmp_path):
        midi, sidecar = _starter(tmp_path)
        result = export_bundle(midi, sidecar, out_dir=tmp_path / "export")
        raw = mido.MidiFile(str(result.combined_midi))
        markers = [m.text for m in raw.tracks[0] if m.type == "marker"]
        assert markers == ["groove", "lift"]

    def test_parts_get_distinct_channels_and_drums_get_nine(self, tmp_path):
        midi, sidecar = _starter(tmp_path)
        result = export_bundle(midi, sidecar, out_dir=tmp_path / "export")
        raw = mido.MidiFile(str(result.combined_midi))
        by_track = {}
        for track in raw.tracks[1:]:
            by_track[track.name] = {m.channel for m in track if m.type == "note_on"}
        assert by_track["drums"] == {9}
        melodic = [next(iter(by_track[n])) for n in ("bass", "keys", "pad")]
        assert 9 not in melodic
        assert len(set(melodic)) == 3


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


class TestDawReadyGate:
    def _overlapping(self, tmp_path):
        s = Score(bpm=100, key="C")
        s.mark_section("main", 0, 8)
        pad = s.track("pad", patch="warm_pad")
        bass = s.track("bass", patch="fingered_bass")
        for bar in range(8):
            pad.note(bar, 1, "C4", 4.0, 60)
            pad.note(bar, 3, "C4", 2.0, 62)   # retriggers while still sounding
            bass.note(bar, 1, "C2", 2.0, 70)
        path = tmp_path / "stuck.mid"
        s.write(str(path))
        return path, path.with_suffix(".score.json")

    def test_overlap_blocks_export_but_only_warns_the_composer(self, tmp_path):
        """The asymmetry is the point, so both halves are asserted together."""
        midi, sidecar = self._overlapping(tmp_path)

        problems, _ = check_daw_ready(midi, sidecar)
        assert any("overlapping same-pitch" in p for p in problems)
        assert any("pad" in p for p in problems)

        report = validate_score(midi, sidecar)
        assert report.ok, report.errors
        assert not any("overlapping" in e for e in report.errors)
        assert any("overlapping same-pitch" in w for w in report.warnings)

    def test_strict_overlaps_promotes_the_warning_to_an_error(self, tmp_path):
        midi, sidecar = self._overlapping(tmp_path)
        report = validate_score(midi, sidecar, strict_overlaps=True)
        assert not report.ok
        assert any("overlapping same-pitch" in e for e in report.errors)
        assert not any("overlapping" in w for w in report.warnings)

    def test_overlap_problem_names_the_track_and_the_bars(self, tmp_path):
        midi, sidecar = self._overlapping(tmp_path)
        problems, _ = check_daw_ready(midi, sidecar)
        message = next(p for p in problems if "overlapping" in p)
        assert "'pad'" in message
        # The retriggers are on beat 3 of every bar from 0 to 7.
        assert "bars 0, 1, 2, 3, 4, 5, 6, 7" in message

    def test_clean_score_has_no_problems(self, tmp_path):
        midi, sidecar = _starter(tmp_path)
        problems, warnings = check_daw_ready(midi, sidecar)
        assert problems == []
        assert warnings == []

    def test_unparseable_file_is_reported_not_raised(self, tmp_path):
        junk = tmp_path / "junk.mid"
        junk.write_bytes(b"definitely not a midi file")
        problems, warnings = check_daw_ready(junk)
        assert problems and "parse" in problems[0].lower()
        assert warnings == []

    def test_zero_length_note_is_a_problem(self, tmp_path):
        """Built with mido, because pretty_midi cannot express the defect.

        ``Score.note`` refuses a non-positive duration, and pretty_midi's reader
        discards any note whose note-off shares its note-on's tick, so a file with
        one in it looks clean by the time the parsed view exists. That is exactly
        why ``check_daw_ready`` also reads the raw event stream.
        """
        raw = mido.MidiFile(type=1, ticks_per_beat=480)
        timing = mido.MidiTrack()
        timing.append(mido.MetaMessage("set_tempo", tempo=600000, time=0))
        timing.append(mido.MetaMessage("end_of_track", time=1))
        raw.tracks.append(timing)

        lead = mido.MidiTrack()
        lead.append(mido.MetaMessage("track_name", name="lead", time=0))
        lead.append(mido.Message("note_on", note=40, velocity=80, channel=0, time=0))
        lead.append(mido.Message("note_off", note=40, velocity=0, channel=0, time=480))
        lead.append(mido.Message("note_on", note=45, velocity=80, channel=0, time=480))
        lead.append(mido.Message("note_off", note=45, velocity=0, channel=0, time=0))
        lead.append(mido.MetaMessage("end_of_track", time=1))
        raw.tracks.append(lead)

        bass = mido.MidiTrack()
        bass.append(mido.MetaMessage("track_name", name="bass", time=0))
        bass.append(mido.Message("note_on", note=33, velocity=80, channel=1, time=0))
        bass.append(mido.Message("note_off", note=33, velocity=0, channel=1, time=960))
        bass.append(mido.MetaMessage("end_of_track", time=1))
        raw.tracks.append(bass)

        path = tmp_path / "dead.mid"
        raw.save(str(path))

        # The parsed view really does lose it, which is the reason for the raw pass.
        parsed = pretty_midi.PrettyMIDI(str(path))
        assert sum(len(i.notes) for i in parsed.instruments) == 2

        problems, _ = check_daw_ready(path)
        assert any("same tick as their note-on" in p for p in problems)
        assert any("'lead'" in p for p in problems)
        assert not export_bundle(path, out_dir=tmp_path / "export").ok

    def test_note_on_without_a_note_off_is_a_problem(self, tmp_path):
        raw = mido.MidiFile(type=1, ticks_per_beat=480)
        timing = mido.MidiTrack()
        timing.append(mido.MetaMessage("set_tempo", tempo=600000, time=0))
        timing.append(mido.MetaMessage("end_of_track", time=1))
        raw.tracks.append(timing)

        pad = mido.MidiTrack()
        pad.append(mido.MetaMessage("track_name", name="pad", time=0))
        pad.append(mido.Message("note_on", note=60, velocity=70, channel=0, time=0))
        pad.append(mido.Message("note_off", note=60, velocity=0, channel=0, time=480))
        pad.append(mido.Message("note_on", note=64, velocity=70, channel=0, time=0))
        pad.append(mido.MetaMessage("end_of_track", time=480))
        raw.tracks.append(pad)

        path = tmp_path / "hanging.mid"
        raw.save(str(path))

        problems, _ = check_daw_ready(path)
        assert any("never receive a note-off" in p for p in problems)

    def test_note_starting_before_zero_is_a_problem(self, tmp_path):
        """Checked against a document, because a MIDI file cannot express it.

        Standard MIDI delta times are unsigned, so no file on disk has a note
        before tick 0 and pretty_midi cannot hand us one. The check is still worth
        having for a document assembled any other way, so the test reaches the
        note-level checker directly rather than pretending the file path works.
        """
        from houseband.export import _check_parts, _load

        midi, sidecar = _two_track(tmp_path)
        doc = _load(midi, sidecar)
        doc.parts[0].notes[0].start = -0.5

        warnings: list[str] = []
        problems = _check_parts(doc, warnings)
        assert any("start before bar 0" in p for p in problems)
        assert any("'lead'" in p for p in problems)

    def test_zero_length_note_in_a_document_is_a_problem(self, tmp_path):
        from houseband.export import _check_parts, _load

        midi, sidecar = _two_track(tmp_path)
        doc = _load(midi, sidecar)
        doc.parts[0].notes[0].end = doc.parts[0].notes[0].start

        problems = _check_parts(doc, [])
        assert any("zero or negative length" in p for p in problems)

    def test_pitch_outside_midi_range_is_a_problem(self, tmp_path):
        from houseband.export import _check_parts, _load

        midi, sidecar = _two_track(tmp_path)
        doc = _load(midi, sidecar)
        doc.parts[0].notes[0].pitch = 140

        problems = _check_parts(doc, [])
        assert any("outside MIDI pitch 0-127" in p for p in problems)

    def test_flat_velocity_track_gets_the_dead_dynamics_warning(self, tmp_path):
        s = Score(bpm=100, key="C")
        s.mark_section("main", 0, 8)
        flat = s.track("keys", patch="electric_piano")
        alive = s.track("bass", patch="fingered_bass")
        for bar in range(8):
            for beat in (1, 2, 3, 4):
                flat.note(bar, beat, 60 + beat, 0.5, 96)   # every note identical
            alive.note(bar, 1, "C2", 2.0, 70 + (bar % 5))
        path = tmp_path / "flat.mid"
        s.write(str(path))

        problems, warnings = check_daw_ready(path, path.with_suffix(".score.json"))
        assert problems == []
        dead = [w for w in warnings if "velocity 96" in w]
        assert len(dead) == 1
        assert "'keys'" in dead[0]
        assert not any("'bass'" in w for w in warnings)

    def test_missing_sections_and_single_track_are_warnings_only(self, tmp_path):
        s = Score(bpm=120, key="C")
        solo = s.track("piano", patch="grand_piano")
        for bar in range(8):
            solo.note(bar, 1, 60 + bar, 2.0, 60 + bar)
        path = tmp_path / "solo.mid"
        s.write(str(path))

        problems, warnings = check_daw_ready(path, path.with_suffix(".score.json"))
        assert problems == []
        assert any("one track" in w.lower() for w in warnings)
        assert any("no sections" in w.lower() for w in warnings)

    def test_works_without_a_sidecar(self, tmp_path):
        midi, sidecar = _two_track(tmp_path)
        sidecar.unlink()
        problems, _ = check_daw_ready(midi)
        assert problems == []
        result = export_bundle(midi, out_dir=tmp_path / "export")
        assert result.ok, result.feedback()
        assert set(result.part_files) == {"lead", "bass"}


# ---------------------------------------------------------------------------
# The loop boundary
# ---------------------------------------------------------------------------


class TestExpectBars:
    def test_clean_sixteen_bar_starter_passes_expect_bars(self, tmp_path):
        midi, sidecar = _starter(tmp_path, bars=16)
        problems, warnings = check_daw_ready(midi, sidecar, expect_bars=16)
        assert problems == []
        assert warnings == []

    def test_material_past_the_loop_point_is_a_problem(self, tmp_path):
        s = Score(bpm=100, key="Am")
        s.mark_section("main", 0, 16)
        lead = s.track("lead", patch="saw_lead")
        bass = s.track("bass", patch="fingered_bass")
        for bar in range(16):
            lead.note(bar, 1, 64 + (bar % 7), 2.0, 70 + (bar % 5))
            bass.note(bar, 1, "A1", 2.0, 80 - (bar % 4))
        # A whole extra bar of lead. The tail lands on top of the loop restart.
        lead.note(16, 1, "C5", 3.0, 72)
        path = tmp_path / "over.mid"
        s.write(str(path))

        problems, _ = check_daw_ready(path, path.with_suffix(".score.json"), expect_bars=16)
        assert any("past the bar 16 loop point" in p for p in problems)
        assert any("'lead'" in p for p in problems)

        # And export refuses, because a starter that will not loop is not a starter.
        assert not export_bundle(path, out_dir=tmp_path / "e", expect_bars=16).ok

    def test_short_release_tail_is_tolerated(self, tmp_path):
        """A note ringing a fraction past the bar line is how instruments stop."""
        s = Score(bpm=100, key="Am")
        s.mark_section("main", 0, 16)
        lead = s.track("lead", patch="saw_lead")
        bass = s.track("bass", patch="fingered_bass")
        for bar in range(16):
            lead.note(bar, 1, 64 + (bar % 7), 2.0, 70 + (bar % 5))
            bass.note(bar, 1, "A1", 2.0, 80 - (bar % 4))
        # Ends a tenth of a beat past bar 16, well under a 16th note.
        lead.note(15, 4, "A4", 1.1, 68)
        path = tmp_path / "tail.mid"
        s.write(str(path))

        problems, warnings = check_daw_ready(
            path, path.with_suffix(".score.json"), expect_bars=16
        )
        assert problems == []
        assert any("ring just past bar 16" in w for w in warnings)
        assert export_bundle(path, out_dir=tmp_path / "e", expect_bars=16).ok

    def test_material_stopping_well_short_is_a_problem(self, tmp_path):
        midi, sidecar = _starter(tmp_path, bars=8)
        problems, _ = check_daw_ready(midi, sidecar, expect_bars=16)
        assert any("material stops at bar" in p for p in problems)
        assert any("16 bars were asked for" in p for p in problems)

    def test_less_than_a_bar_of_trailing_silence_is_fine(self, tmp_path):
        """Ending on beat 3 of the last bar is a phrase, not a truncation."""
        s = Score(bpm=100, key="Am")
        s.mark_section("main", 0, 16)
        lead = s.track("lead", patch="saw_lead")
        bass = s.track("bass", patch="fingered_bass")
        for bar in range(15):
            lead.note(bar, 1, 64 + (bar % 7), 2.0, 70 + (bar % 5))
            bass.note(bar, 1, "A1", 2.0, 80 - (bar % 4))
        lead.note(15, 1, "A4", 2.0, 70)
        path = tmp_path / "short_tail.mid"
        s.write(str(path))

        problems, _ = check_daw_ready(
            path, path.with_suffix(".score.json"), expect_bars=16
        )
        assert problems == []

    def test_expect_bars_is_noted_in_the_readme_when_it_differs(self, tmp_path):
        midi, sidecar = _starter(tmp_path, bars=16)
        result = export_bundle(
            midi, sidecar, out_dir=tmp_path / "export", expect_bars=16
        )
        assert result.ok, result.feedback()
        assert "Bars            16" in result.readme_path.read_text()


# ---------------------------------------------------------------------------
# README
# ---------------------------------------------------------------------------


class TestReadme:
    def test_readme_states_key_tempo_bars_and_every_track_name(self, tmp_path):
        midi, sidecar = _starter(tmp_path, bpm=93, bars=16)
        result = export_bundle(
            midi,
            sidecar,
            out_dir=tmp_path / "export",
            title="Dusty Boom Bap Loop",
            brief="Eight bars of dusty boom bap at 93 with a warm sub bass.",
        )
        text = result.readme_path.read_text()

        assert "Am" in text
        assert "93" in text
        assert "4/4" in text
        assert "Bars            16" in text
        for name in ("drums", "bass", "keys", "pad"):
            assert name in text, f"{name} missing from README"

        assert "Dusty Boom Bap Loop" in text
        assert "dusty boom bap at 93" in text
        assert "Ableton" in text and "Pro Tools" in text

    def test_readme_names_each_instrument_and_its_pitch_range(self, tmp_path):
        midi, sidecar = _starter(tmp_path)
        result = export_bundle(midi, sidecar, out_dir=tmp_path / "export")
        text = result.readme_path.read_text()

        assert "Electric Bass (finger) (GM 33)" in text
        assert "Electric Piano 1 (GM 4)" in text
        assert "GM drum kit (channel 10)" in text
        # Pitch range as note names for melodic parts, key numbers for drums.
        # The bass walks F1 up to C2 across the progression.
        assert "F1-C2" in text
        assert "keys 36-42" in text
        assert "kick, snare, hat_closed" in text

    def test_readme_reports_tempo_changes_not_just_the_opening_tempo(self, tmp_path):
        s = Score(bpm=70, key="Dm")
        s.mark_section("a", 0, 16)
        s.ramp_tempo(8, 12, 70, 90)
        lead = s.track("lead", patch="saw_lead")
        bass = s.track("bass", patch="fingered_bass")
        for bar in range(16):
            lead.note(bar, 1, 62 + (bar % 5), 1.0, 70 + (bar % 4))
            bass.note(bar, 1, "D1", 2.0, 80)
        path = tmp_path / "ramp.mid"
        s.write(str(path))

        result = export_bundle(path, out_dir=tmp_path / "export")
        text = result.readme_path.read_text()
        assert "70->90 over bars 8-12" in text

    def test_readme_lists_every_part_filename(self, tmp_path):
        midi, sidecar = _starter(tmp_path)
        result = export_bundle(midi, sidecar, out_dir=tmp_path / "export")
        text = result.readme_path.read_text()
        for path in result.part_files.values():
            assert f"parts/{path.name}" in text


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


class TestDeterminism:
    def test_repeat_export_produces_identical_zip_bytes(self, tmp_path):
        midi, sidecar = _starter(tmp_path)
        first = export_bundle(midi, sidecar, out_dir=tmp_path / "one", stem="starter")
        second = export_bundle(midi, sidecar, out_dir=tmp_path / "two", stem="starter")

        assert first.ok and second.ok
        assert first.zip_path.read_bytes() == second.zip_path.read_bytes()

    def test_repeat_export_into_the_same_directory_is_stable(self, tmp_path):
        midi, sidecar = _starter(tmp_path)
        out = tmp_path / "export"
        before = export_bundle(midi, sidecar, out_dir=out).zip_path.read_bytes()
        after = export_bundle(midi, sidecar, out_dir=out).zip_path.read_bytes()
        assert before == after

    def test_every_member_file_is_byte_identical_across_runs(self, tmp_path):
        midi, sidecar = _starter(tmp_path)
        first = export_bundle(midi, sidecar, out_dir=tmp_path / "one")
        second = export_bundle(midi, sidecar, out_dir=tmp_path / "two")

        assert first.combined_midi.read_bytes() == second.combined_midi.read_bytes()
        assert first.readme_path.read_text() == second.readme_path.read_text()
        for name, path in first.part_files.items():
            assert path.read_bytes() == second.part_files[name].read_bytes()

    def test_zip_entry_timestamps_are_fixed(self, tmp_path):
        midi, sidecar = _starter(tmp_path)
        result = export_bundle(midi, sidecar, out_dir=tmp_path / "export")
        with zipfile.ZipFile(result.zip_path) as archive:
            assert {i.date_time for i in archive.infolist()} == {
                (1980, 1, 1, 0, 0, 0)
            }
