"""Turn a finished score into a bundle a producer can drag into a DAW.

The deliverable of this system is not a MIDI file, it is a *starting point in
somebody's session*. Those are different artifacts. ``Score.write()`` produces a
single ``out.mid`` with named tracks, which is correct as an interchange file and
useless as a starting point: importing it into Ableton gives you one clip you then
have to split by hand, and importing it into Pro Tools gives you a tempo that is
wrong. This module closes that gap.

Three things here are worth explaining, because each of them is a bug we would
otherwise ship.

**The tempo map is rebuilt from the sidecar, not read from the MIDI.**
``Score.write()`` emits the full map, but this module also has to read MIDI it did
not write, and there the map can only be inferred from the
file's own tempo changes under a guess at the bar length. ``out.score.json`` is
the lossless record of what the composer actually declared, so that is where the
map is read from whenever there is one, and every file this module writes carries
it in full.

**Note positions are computed in musical time, then converted once.**
A note's seconds are turned into a fractional bar via :class:`TempoMap` and only
then into ticks. Going straight from seconds to ticks would bake the *old* tempo
into the positions and then play them back under the *new* map, which displaces
everything after the first tempo change.

**Files are written with mido rather than pretty_midi.** pretty_midi's writer
picks its own channels, rounds tempo through a float round-trip, and offers no way
to emit a multi-entry tempo map from an object you built in memory. Track names do
in fact survive a pretty_midi write/read round-trip at 0.2.11, so mido is not
needed to rescue those -- it is needed for the tempo map, the channel assignment,
and byte-for-byte reproducibility. That claim is pinned by a test rather than
left as folklore, because it is exactly the sort of thing a dependency bump
quietly breaks.
"""

from __future__ import annotations

import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import mido
import pretty_midi

from houseband.house.core import DRUMS
from houseband.score_text import format_tempo, load_view, pitch_name
from houseband.timing import TempoMap
from houseband.validator import same_pitch_overlaps

__all__ = [
    "ExportResult",
    "check_daw_ready",
    "export_bundle",
    "TICKS_PER_QUARTER",
]

# 480 is what every DAW in the brief uses internally, so 16th and 32nd triplets
# all land on integers. pretty_midi's own default of 220 does not divide by 3.
TICKS_PER_QUARTER = 480

# A fixed zip timestamp. Zip stores an mtime per entry, so without this two
# exports of identical input differ in their bytes and nobody can tell whether a
# bundle changed because the music changed.
ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)

# Ordering of same-tick events. Meta before program before controllers before
# notes, and note-off strictly before note-on so a legato handoff on one pitch
# does not silence the note that is arriving.
_EVENT_RANK = {
    "track_name": 0,
    "time_signature": 1,
    "key_signature": 2,
    "set_tempo": 3,
    "marker": 4,
    "program_change": 5,
    "control_change": 6,
    "note_off": 7,
    "note_on": 8,
}

_DRUM_VOICE_NAMES = {number: name for name, number in DRUMS.items()}

# Non-drum channels, in assignment order. Channel 9 is reserved for percussion by
# General MIDI and every drum rack expects to find it there.
_MELODIC_CHANNELS = [c for c in range(16) if c != 9]
_DRUM_CHANNEL = 9


# ---------------------------------------------------------------------------
# Internal model
# ---------------------------------------------------------------------------


@dataclass
class ExportResult:
    ok: bool
    zip_path: Path | None
    combined_midi: Path | None          # type-1 multitrack, all parts, named
    part_files: dict[str, Path]         # track name -> single-part .mid
    readme_path: Path | None
    problems: list[str]                 # hard blockers
    warnings: list[str]

    def feedback(self) -> str:
        """Human-readable outcome, in the same voice as ValidationReport."""
        lines: list[str] = []
        if self.problems:
            lines.append("NOT DAW-READY:")
            lines += [f"  - {p}" for p in self.problems]
        if self.warnings:
            lines.append("Warnings (exported anyway):")
            lines += [f"  - {w}" for w in self.warnings]
        if self.ok:
            lines.append(
                f"Exported {len(self.part_files)} parts to {self.zip_path}."
            )
        return "\n".join(lines)


@dataclass
class _Part:
    """One instrument line, with its notes in the order we will write them."""

    name: str
    program: int
    is_drum: bool
    pan: float
    notes: list  # pretty_midi.Note, sorted

    @property
    def pitches(self) -> list[int]:
        return [n.pitch for n in self.notes]

    @property
    def velocities(self) -> list[int]:
        return [n.velocity for n in self.notes]


@dataclass
class _Doc:
    """Everything the writers and the README need, derived once."""

    parts: list[_Part]
    tempo: TempoMap
    time_sig: tuple[int, int]
    key: str
    sections: list[dict]
    total_bars: int
    duration: float
    ticks_per_bar: float = field(init=False)

    def __post_init__(self) -> None:
        self.ticks_per_bar = self.tempo.quarters_per_bar * TICKS_PER_QUARTER

    def tick(self, seconds: float) -> int:
        """Seconds to ticks, by way of musical position.

        The intermediate step is the point: :meth:`TempoMap.seconds_to_bar` knows
        the map the composer declared, so a note two thirds of the way through
        bar 41 stays two thirds of the way through bar 41 no matter what the
        tempo does there.
        """
        return int(round(self.tempo.seconds_to_bar(seconds) * self.ticks_per_bar))

    def bar_of(self, seconds: float) -> int:
        return int(self.tempo.seconds_to_bar(seconds))

    def sixteenth_seconds(self, bar: int) -> float:
        """One sixteenth note at the tempo in force at ``bar``."""
        return 60.0 / self.tempo.bpm_at(max(bar, 0)) / 4.0


def _load(midi_path: Path, sidecar_path: Path | None = None) -> _Doc:
    """Read a score into the export model. Raises on an unparseable file.

    ``load_view`` already resolves the sidecar, the tempo map, track naming and
    pan recovery from CC10, so it does that work here too. It does not retain
    note times in seconds (it converts to bars and beats, which loses precision
    across a tempo change), so the notes come from a second pretty_midi parse.
    Parsing twice costs milliseconds and keeps ``load_view``'s contract intact.
    """
    midi_path = Path(midi_path)
    view = load_view(midi_path, sidecar_path)
    midi = pretty_midi.PrettyMIDI(str(midi_path))

    structure = view.structure
    if structure.get("time_sig"):
        num, den = structure["time_sig"]
    elif midi.time_signature_changes:
        first = midi.time_signature_changes[0]
        num, den = first.numerator, first.denominator
    else:
        num, den = 4, 4

    # load_view builds track_meta by walking midi.instruments in order, so the
    # names line up positionally with the instruments here.
    names = list(view.track_meta)
    parts: list[_Part] = []
    for index, inst in enumerate(midi.instruments):
        name = names[index] if index < len(names) else f"track{index}"
        meta = view.track_meta.get(name, {})
        parts.append(
            _Part(
                name=name,
                program=inst.program,
                is_drum=inst.is_drum,
                pan=float(meta.get("pan", 0.0)),
                notes=sorted(
                    inst.notes, key=lambda n: (n.start, n.pitch, n.end, n.velocity)
                ),
            )
        )

    return _Doc(
        parts=parts,
        tempo=view.tempo,
        time_sig=(int(num), int(den)),
        key=str(structure.get("key") or ""),
        sections=list(structure.get("sections") or []),
        total_bars=view.total_bars,
        duration=float(view.duration or 0.0),
    )


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------

# How much trailing silence before a loop stops looping cleanly. Under a bar is
# a producer leaving air at the end of a phrase; over a bar is a starter that did
# not fill the length it claimed.
SHORTFALL_BARS = 1.0

# Bars to name before summarising. Long enough to be actionable, short enough that
# a systematically broken track does not produce a wall of numbers.
_MAX_LISTED_BARS = 8


def _bar_list(bars: list[int]) -> str:
    unique = sorted(set(bars))
    shown = ", ".join(str(b) for b in unique[:_MAX_LISTED_BARS])
    if len(unique) > _MAX_LISTED_BARS:
        shown += f", ... (+{len(unique) - _MAX_LISTED_BARS} more)"
    return shown


def _raw_stream_problems(midi_path: Path) -> list[str]:
    """Defects that vanish the moment pretty_midi parses the file.

    pretty_midi discards any note whose note-off lands on the same tick as its
    note-on, and it forgets a note-on that never receives a note-off at all. Both
    are real problems for a producer -- the first is a note that may or may not
    survive the import, the second hangs until they stop the transport -- and
    neither is visible in the parsed view, because by then the evidence is gone.
    So this pass reads the raw event stream instead.
    """
    try:
        raw = mido.MidiFile(str(midi_path))
    except Exception as exc:
        return [f"MIDI event stream will not parse: {exc}"]

    problems: list[str] = []
    for index, track in enumerate(raw.tracks):
        label = track.name.strip() or f"track {index}"
        open_notes: dict[tuple[int, int], list[int]] = {}
        tick = 0
        zero_length = 0
        for message in track:
            tick += message.time
            if message.type == "note_on" and message.velocity > 0:
                open_notes.setdefault((message.channel, message.note), []).append(tick)
            elif message.type == "note_off" or (
                message.type == "note_on" and message.velocity == 0
            ):
                pending = open_notes.get((message.channel, message.note))
                if pending:
                    # Close the oldest open note-on, which is how pretty_midi and
                    # every synth resolve the ambiguity.
                    if pending.pop(0) == tick:
                        zero_length += 1

        if zero_length:
            problems.append(
                f"{label!r}: {zero_length} notes have their note-off on the same "
                "tick as their note-on. They have no length, and a DAW will "
                "either drop them or import a click."
            )
        hanging = sum(len(ticks) for ticks in open_notes.values())
        if hanging:
            problems.append(
                f"{label!r}: {hanging} note-ons never receive a note-off, so those "
                "notes sound until the transport stops."
            )
    return problems


def check_daw_ready(
    midi_path: Path,
    sidecar_path: Path | None = None,
    expect_bars: int | None = None,
) -> tuple[list[str], list[str]]:
    """Returns (problems, warnings).

    Stricter than :func:`houseband.validator.validate_score` on purpose. That
    function decides whether a candidate is a well-formed submission, and a
    submission gets another turn. This one decides whether a file is safe to hand
    to someone who will open it in Ableton, and there is no next turn: whatever is
    wrong in the file is wrong in their session.
    """
    problems: list[str] = []
    warnings: list[str] = []

    try:
        doc = _load(midi_path, sidecar_path)
    except Exception as exc:  # pretty_midi raises a variety of parse errors
        return [f"MIDI file will not parse: {exc}"], []

    # Before the emptiness check, because a file whose note-ons never get a
    # note-off parses as empty and "there are no notes" would hide the real cause.
    problems += _raw_stream_problems(Path(midi_path))

    if not any(part.notes for part in doc.parts):
        problems.append("MIDI contains no notes, so there is nothing to export.")
        return problems, warnings

    problems += _check_parts(doc, warnings)

    if expect_bars is not None:
        problems.extend(_check_loop_boundary(doc, expect_bars, warnings))

    return problems, warnings


def _check_parts(doc: _Doc, warnings: list[str]) -> list[str]:
    """Note-level checks over the parsed view.

    Split out from :func:`check_daw_ready` so the checks can be exercised against
    a document built in memory. Two of them -- a zero-length note and a note
    starting before bar 0 -- are unreachable through a pretty_midi parse, since
    pretty_midi drops the first and cannot produce the second. They stay because
    they are cheap and because the parsed view is not the only way a document can
    reach here; :func:`_raw_stream_problems` is what catches the file-level case.
    """
    problems: list[str] = []

    for part in doc.parts:
        if not part.notes:
            warnings.append(f"track {part.name!r} has no notes and was skipped.")
            continue

        # Stuck notes. The single most damaging defect in an exported file: the
        # producer hears a tone that never releases and cannot tell which of the
        # overlapping notes to delete. The detection lives in the validator so
        # there is one definition of the rule; only the consequence differs. It
        # reads ``.notes`` off whatever it is given, which a _Part also has.
        overlaps = same_pitch_overlaps(part)
        if overlaps:
            bars = [doc.bar_of(start) for _, start, _ in overlaps]
            problems.append(
                f"track {part.name!r}: {len(overlaps)} overlapping same-pitch "
                f"notes at bars {_bar_list(bars)}. In a DAW the second note-on "
                "arrives before the first note-off, which sounds like a stuck, "
                "droning tone. Shorten the earlier note or move the later one."
            )

        dead = [n for n in part.notes if n.end - n.start <= 1e-6]
        if dead:
            problems.append(
                f"track {part.name!r}: {len(dead)} notes of zero or negative "
                f"length at bars {_bar_list([doc.bar_of(n.start) for n in dead])}. "
                "Most DAWs drop these silently, so the part imports incomplete."
            )

        out_of_range = [n for n in part.notes if not 0 <= n.pitch <= 127]
        if out_of_range:
            problems.append(
                f"track {part.name!r}: {len(out_of_range)} notes outside MIDI "
                f"pitch 0-127 (lowest {min(part.pitches)}, highest "
                f"{max(part.pitches)}). These cannot be written to a MIDI file."
            )

        early = [n for n in part.notes if n.start < -1e-6]
        if early:
            problems.append(
                f"track {part.name!r}: {len(early)} notes start before bar 0, so "
                "they would be clipped off the front of the clip."
            )

        # Dead-grid dynamics. Not a defect in the file, but a producer who drops
        # in a part where every hit is velocity 100 hears a machine and deletes
        # it, so it is worth saying out loud.
        if len(part.notes) > 1 and len(set(part.velocities)) == 1:
            warnings.append(
                f"track {part.name!r}: every one of {len(part.notes)} notes is "
                f"velocity {part.velocities[0]}. Flat dynamics read as "
                "programmed rather than played."
            )

    if len(doc.parts) == 1:
        warnings.append(
            "Only one track, so the bundle is a single part file. A producer "
            "expects separable stems."
        )

    if not doc.sections:
        warnings.append(
            "No sections declared, so the README cannot show a section map and "
            "the producer has to count bars to find the arrangement."
        )

    return problems


def _check_loop_boundary(doc: _Doc, expect_bars: int, warnings: list[str]) -> list[str]:
    """Does the material actually fill, and stop at, ``expect_bars``?

    A starter that runs past its loop point does not loop: the tail collides with
    the top of the next repetition. A starter that stops short leaves dead air.
    Both are silent failures at export time and obvious the moment the producer
    hits play, which is the worst possible place to find out.
    """
    problems: list[str] = []
    loop_end = doc.tempo.bar_start_seconds(expect_bars)
    # A release tail is how a real instrument stops, so tolerate up to a 16th.
    tail_allowance = doc.sixteenth_seconds(expect_bars - 1)

    for part in doc.parts:
        past = [n for n in part.notes if n.end > loop_end + tail_allowance + 1e-6]
        if past:
            problems.append(
                f"track {part.name!r}: {len(past)} notes extend past the bar "
                f"{expect_bars} loop point (bars "
                f"{_bar_list([doc.bar_of(n.start) for n in past])}). The tail will "
                "collide with the top of the loop."
            )
        tails = [
            n
            for n in part.notes
            if loop_end + 1e-6 < n.end <= loop_end + tail_allowance + 1e-6
        ]
        if tails and not past:
            warnings.append(
                f"track {part.name!r}: {len(tails)} notes ring just past bar "
                f"{expect_bars} (under a 16th note). Normal for a release tail; "
                "trim them if you want a hard loop."
            )

    ends = [n.end for part in doc.parts for n in part.notes]
    if ends:
        last_bar = doc.tempo.seconds_to_bar(max(ends))
        if expect_bars - last_bar > SHORTFALL_BARS:
            problems.append(
                f"material stops at bar {last_bar:.2f} but {expect_bars} bars "
                f"were asked for, leaving {expect_bars - last_bar:.1f} bars of "
                "silence before the loop point. Fill the length or export fewer bars."
            )
    return problems


# ---------------------------------------------------------------------------
# Writing MIDI
# ---------------------------------------------------------------------------


_Event = tuple[int, int, int, "mido.messages.BaseMessage"]


def _track_from_events(events: list[_Event]) -> mido.MidiTrack:
    """Absolute-tick events to a delta-timed mido track.

    The sort key carries an explicit sequence tiebreaker so identical input always
    produces identical bytes. Relying on sort stability instead would make the
    output depend on the order we happened to append in, which is exactly the kind
    of accident that breaks a byte-comparison test six months later.
    """
    track = mido.MidiTrack()
    previous = 0
    for tick, _, _, message in sorted(events, key=lambda e: (e[0], e[1], e[2])):
        track.append(message.copy(time=tick - previous))
        previous = tick
    track.append(mido.MetaMessage("end_of_track", time=1 if events else 0))
    return track


def _event(tick: int, message, seq: int) -> _Event:
    return (tick, _EVENT_RANK.get(message.type, 99), seq, message)


def _timing_events(doc: _Doc, seq_start: int = 0) -> list[_Event]:
    """Time signature, key signature, the whole tempo map, and section markers.

    Markers are cheap and land as locators in both Ableton and Pro Tools, which
    means the section names the composer declared survive into the session rather
    than living only in the README.
    """
    events: list[_Event] = []
    seq = seq_start
    num, den = doc.time_sig
    events.append(
        _event(0, mido.MetaMessage("time_signature", numerator=num, denominator=den), seq)
    )
    seq += 1

    key = _mido_key(doc.key)
    if key:
        events.append(_event(0, mido.MetaMessage("key_signature", key=key), seq))
        seq += 1

    # Last value per bar wins, and repeats of the same tempo are dropped. A
    # ramp_tempo writes one entry per bar, so a naive dump is dozens of events
    # and some DAWs draw every one of them in the tempo lane.
    collapsed: dict[int, float] = {}
    for bar, bpm in doc.tempo.entries:
        collapsed[int(bar)] = float(bpm)
    previous_bpm: float | None = None
    for bar in sorted(collapsed):
        bpm = collapsed[bar]
        if previous_bpm is not None and abs(bpm - previous_bpm) < 1e-9:
            continue
        previous_bpm = bpm
        events.append(
            _event(
                int(round(bar * doc.ticks_per_bar)),
                # Microseconds per quarter note, the only tempo unit MIDI has.
                mido.MetaMessage("set_tempo", tempo=int(round(60_000_000.0 / bpm))),
                seq,
            )
        )
        seq += 1

    for section in doc.sections:
        events.append(
            _event(
                int(round(int(section["start_bar"]) * doc.ticks_per_bar)),
                mido.MetaMessage("marker", text=str(section["name"])),
                seq,
            )
        )
        seq += 1

    return events


def _mido_key(key: str) -> str | None:
    """Map a Score key string onto a key signature mido will accept.

    Returns ``None`` rather than raising for anything unrecognised: a key we
    cannot spell is worth losing, and is not worth failing an export over.
    """
    from mido.midifiles.meta import _key_signature_encode

    text = (key or "").strip()
    if not text:
        return None
    if text in _key_signature_encode:
        return text
    # "Amin", "A minor", "a-minor" all mean Am.
    lowered = text.lower().replace("-", " ")
    root = text[:2] if len(text) > 1 and text[1] in "#b" else text[:1]
    root = root[0].upper() + root[1:]
    minor = any(token in lowered for token in ("min", "m ", "aeolian")) or lowered.endswith("m")
    candidate = f"{root}m" if minor else root
    return candidate if candidate in _key_signature_encode else None


def _note_events(
    doc: _Doc, part: _Part, channel: int, seq_start: int
) -> list[_Event]:
    """Program change, pan, and the notes, on one channel."""
    events: list[_Event] = []
    seq = seq_start
    events.append(
        _event(
            0,
            mido.Message(
                "program_change",
                # GM has no program for the drum channel; the kit is selected by
                # the channel itself, so 0 is the conventional filler.
                program=0 if part.is_drum else max(0, min(127, part.program)),
                channel=channel,
            ),
            seq,
        )
    )
    seq += 1
    pan_value = max(0, min(127, int(round((part.pan + 1.0) / 2.0 * 127))))
    events.append(
        _event(
            0,
            mido.Message("control_change", control=10, value=pan_value, channel=channel),
            seq,
        )
    )
    seq += 1

    for note in part.notes:
        start = doc.tick(note.start)
        end = doc.tick(note.end)
        # A note shorter than one tick would arrive as a note-off at the same
        # position as its note-on, which some DAWs discard. Give it one tick.
        end = max(end, start + 1)
        pitch = max(0, min(127, int(note.pitch)))
        velocity = max(1, min(127, int(note.velocity)))
        events.append(
            _event(
                start,
                mido.Message("note_on", note=pitch, velocity=velocity, channel=channel),
                seq,
            )
        )
        seq += 1
        events.append(
            _event(end, mido.Message("note_off", note=pitch, velocity=0, channel=channel), seq)
        )
        seq += 1
    return events


def _channel_for(part: _Part, melodic_index: int) -> int:
    if part.is_drum:
        return _DRUM_CHANNEL
    # Beyond 15 melodic parts channels have to repeat. Harmless here: each part is
    # its own track in a type-1 file, so a DAW separates them by track regardless.
    return _MELODIC_CHANNELS[melodic_index % len(_MELODIC_CHANNELS)]


def write_combined(doc: _Doc, path: Path, sequence_name: str = "") -> Path:
    """Type-1 multitrack: a conductor track, then one named track per part."""
    midi = mido.MidiFile(type=1, ticks_per_beat=TICKS_PER_QUARTER)

    # By convention the track name on track 0 of a type-1 file is the name of the
    # sequence, not of an instrument, and several DAWs show it in the import
    # dialog. Track 0 carries no notes, so nothing reads it as a part.
    conductor: list[_Event] = []
    if sequence_name.strip():
        conductor.append(
            _event(0, mido.MetaMessage("track_name", name=sequence_name.strip()), 0)
        )
    conductor += _timing_events(doc, seq_start=1)
    midi.tracks.append(_track_from_events(conductor))

    melodic_index = 0
    for part in doc.parts:
        if not part.notes:
            continue
        channel = _channel_for(part, melodic_index)
        if not part.is_drum:
            melodic_index += 1
        events = [_event(0, mido.MetaMessage("track_name", name=part.name), 0)]
        events += _note_events(doc, part, channel, seq_start=1)
        midi.tracks.append(_track_from_events(events))

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    midi.save(str(path))
    return path


def write_part(doc: _Doc, part: _Part, path: Path) -> Path:
    """One part, self-contained, as a type-0 file.

    Type 0 rather than type 1 because a type-1 file with a separate conductor
    track imports as two tracks in some DAWs, one of them empty, and the whole
    point of a part file is that it is exactly one track. Everything a type-0
    file needs -- track name, time signature, the full tempo map -- is legal in
    its single track.

    A part keeps its absolute position, so a line that enters at bar 8 carries
    eight bars of leading silence. That is what makes all the part files line up
    when the producer drops them at the same point in the session.
    """
    events = [_event(0, mido.MetaMessage("track_name", name=part.name), 0)]
    events += _timing_events(doc, seq_start=1)
    channel = _DRUM_CHANNEL if part.is_drum else 0
    events += _note_events(doc, part, channel, seq_start=1000)

    midi = mido.MidiFile(type=0, ticks_per_beat=TICKS_PER_QUARTER)
    midi.tracks.append(_track_from_events(events))

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    midi.save(str(path))
    return path


# ---------------------------------------------------------------------------
# Naming and ordering
# ---------------------------------------------------------------------------

_SAFE_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_")

# GM bass programs. Used only for file ordering, so the boundary being slightly
# arbitrary costs nothing.
_BASS_PROGRAMS = range(32, 40)


def safe_name(name: str) -> str:
    """A filename fragment that survives macOS, Windows and a zip round-trip."""
    out: list[str] = []
    for char in name.strip():
        out.append(char if char in _SAFE_CHARS else "_")
    text = "".join(out)
    while "__" in text:
        text = text.replace("__", "_")
    return text.strip("_") or "track"


def _order_key(index: int, part: _Part) -> tuple[int, int]:
    """Drums, then bass, then everything else in the composer's own order.

    A producer opening the parts folder is nearly always looking for the rhythm
    section first, so the numbering puts it there instead of making them read
    every filename.
    """
    if part.is_drum:
        rank = 0
    elif part.program in _BASS_PROGRAMS or "bass" in part.name.lower():
        rank = 1
    else:
        rank = 2
    return (rank, index)


def _part_filenames(doc: _Doc) -> list[tuple[_Part, str]]:
    ordered = sorted(
        ((index, part) for index, part in enumerate(doc.parts) if part.notes),
        key=lambda item: _order_key(*item),
    )
    used: set[str] = set()
    result: list[tuple[_Part, str]] = []
    for position, (_, part) in enumerate(ordered, start=1):
        stem = safe_name(part.name)
        candidate = f"{position:02d}_{stem}.mid"
        # Score forbids duplicate track names, but a MIDI file we did not write
        # may well have them, and two files cannot share a name.
        suffix = 2
        while candidate.lower() in used:
            candidate = f"{position:02d}_{stem}_{suffix}.mid"
            suffix += 1
        used.add(candidate.lower())
        result.append((part, candidate))
    return result


# ---------------------------------------------------------------------------
# README
# ---------------------------------------------------------------------------


def _wrap(text: str, width: int = 76, indent: str = "  ") -> list[str]:
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        if current and len(current) + 1 + len(word) > width:
            lines.append(indent + current)
            current = word
        else:
            current = f"{current} {word}".strip()
    if current:
        lines.append(indent + current)
    return lines


def _instrument_label(part: _Part) -> str:
    if part.is_drum:
        return "GM drum kit (channel 10)"
    try:
        return f"{pretty_midi.program_to_instrument_name(part.program)} (GM {part.program})"
    except Exception:
        return f"GM program {part.program}"


def _range_label(part: _Part) -> str:
    if not part.notes:
        return "-"
    low, high = min(part.pitches), max(part.pitches)
    if part.is_drum:
        # Note names are meaningless for percussion; the key numbers are what a
        # drum rack is mapped against.
        return f"keys {low}-{high}"
    return f"{pitch_name(low)}-{pitch_name(high)}"


def build_readme(
    doc: _Doc,
    part_files: list[tuple[_Part, str]],
    stem: str,
    title: str = "",
    brief: str = "",
    expect_bars: int | None = None,
) -> str:
    minutes, seconds = divmod(int(round(doc.duration)), 60)
    lines: list[str] = []

    heading = title.strip() or "houseband starter"
    lines.append(heading)
    lines.append("=" * len(heading))
    lines.append("")
    lines.append("A MIDI starting point, not a finished track. Every part is separable.")
    lines.append("")

    if brief.strip():
        lines.append("BRIEF")
        lines += _wrap(brief.strip())
        lines.append("")

    lines.append("THE BASICS")
    lines.append(f"  Key             {doc.key or 'unspecified'}")
    lines.append(f"  Tempo (BPM)     {format_tempo(doc.tempo)}")
    lines.append(f"  Time signature  {doc.time_sig[0]}/{doc.time_sig[1]}")
    bars_note = f"  Bars            {doc.total_bars}"
    if expect_bars is not None and expect_bars != doc.total_bars:
        bars_note += f" (exported against a {expect_bars}-bar loop)"
    lines.append(bars_note)
    lines.append(f"  Length          {minutes}:{seconds:02d}")
    lines.append(f"  Ticks per beat  {TICKS_PER_QUARTER}")
    lines.append("")

    lines.append("SECTIONS")
    if doc.sections:
        for section in doc.sections:
            start, length = int(section["start_bar"]), int(section["bars"])
            # Bar numbers here are 0-indexed to match the composer's own program.
            lines.append(
                f"  bars {start:>3}-{start + length - 1:<3}  "
                f"{str(section['name']):<16} ({length} bars)"
            )
        lines.append("")
        lines.append("  Section names are also written as markers in every MIDI file")
        lines.append("  here, so they arrive as locators whichever one you import.")
    else:
        lines.append("  None declared.")
    lines.append("")

    lines.append("PARTS")
    lines.append(
        f"  {'file':<28} {'instrument':<34} {'notes':>6}  "
        f"{'range':<11} {'velocity':<9} pan"
    )
    for part, filename in part_files:
        velocities = part.velocities
        vel = f"{min(velocities)}-{max(velocities)}" if velocities else "-"
        lines.append(
            f"  {'parts/' + filename:<28} {_instrument_label(part):<34} "
            f"{len(part.notes):>6}  {_range_label(part):<11} {vel:<9} {part.pan:+.2f}"
        )
        if part.is_drum:
            voices = sorted({n.pitch for n in part.notes})
            named = ", ".join(_DRUM_VOICE_NAMES.get(p, str(p)) for p in voices)
            lines += _wrap(f"voices: {named}", indent="      ")
    lines.append("")

    lines.append("FILES")
    lines.append(f"  {stem}_full.mid")
    lines.append("      Every part in one type-1 MIDI file, each track named.")
    lines.append("      Carries the tempo map, time signature and section markers.")
    lines.append("  parts/*.mid")
    lines.append("      One file per part. Each is self-contained: it has the full")
    lines.append("      tempo map and time signature, so it lands at the right tempo")
    lines.append("      on its own. Drums are on channel 10 with GM key mapping.")
    lines.append("  README.txt")
    lines.append("      This file.")
    lines.append("")

    lines.append("HOW TO USE THIS")
    lines.append("")
    lines.append("Ableton Live")
    lines.append("  1. Set the project tempo first: Live takes the tempo from the")
    lines.append("     first MIDI file you drop in and ignores it on later ones.")
    lines.append(f"     This one starts at {doc.tempo.bpm_at(0):.2f} BPM.")
    lines.append(f"  2. Drag {stem}_full.mid onto an empty area of the Arrangement")
    lines.append("     view. Live creates one MIDI track per part, already named.")
    lines.append("  3. Or drag individual files from parts/ to pick up only the parts")
    lines.append("     you want. Drop them all at bar 1 and they stay in sync.")
    lines.append("  4. Load a drum rack on the drums track. The kit follows the GM")
    lines.append("     map, so 36 is kick, 38 snare, 42 closed hat, 46 open hat.")
    lines.append("")
    lines.append("Pro Tools")
    lines.append("  1. File > Import > MIDI, choose the file, and select")
    lines.append('     "New Track" as the destination.')
    lines.append('  2. Tick "Import Tempo Map from MIDI File" on the first import,')
    lines.append("     then leave it unticked for any further part files so the")
    lines.append("     session tempo is not overwritten.")
    lines.append("  3. Location: Song Start, so the parts keep their bar positions.")
    lines.append("")
    lines.append("Logic Pro and FL Studio")
    lines.append("  Both read the combined file directly and will create one track")
    lines.append("  per part with the names intact. Logic picks up the tempo map on")
    lines.append("  import; in FL Studio choose the tempo-import option in the")
    lines.append("  MIDI import dialog.")
    lines.append("")
    lines.append("Anything else")
    lines.append("  Every part file keeps its absolute position in the arrangement, so")
    lines.append("  a line that enters at bar 8 has eight bars of silence at the front")
    lines.append("  of its file. Drop them all at the same point and they line up.")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Bundling
# ---------------------------------------------------------------------------


def _write_zip(zip_path: Path, entries: list[tuple[str, Path]]) -> Path:
    """Deterministic zip: fixed mtimes, fixed modes, sorted entries.

    Without the fixed timestamp two exports of identical input produce different
    bytes, and then nobody can tell from a checksum whether a bundle changed
    because the music changed.
    """
    zip_path = Path(zip_path)
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for arcname, source in sorted(entries, key=lambda item: item[0]):
            info = zipfile.ZipInfo(arcname, date_time=ZIP_TIMESTAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, Path(source).read_bytes())
    return zip_path


def export_bundle(
    midi_path: Path,
    sidecar_path: Path | None = None,
    out_dir: Path | None = None,
    stem: str = "starter",
    expect_bars: int | None = None,
    title: str = "",
    brief: str = "",
) -> ExportResult:
    """Produce the DAW bundle: combined file, part files, README, zip.

    Refuses to write anything when :func:`check_daw_ready` reports a problem. A
    half-good bundle is worse than none: the producer imports it, hears a stuck
    note or a loop that will not loop, and stops trusting the tool. Warnings do
    not block, because "your hats are all velocity 96" is information, not a
    defect in the file.

    Layout, given ``out_dir=E`` and ``stem=S``::

        E/S.zip                 the bundle, containing all of the below under S/
        E/S/S_full.mid          type-1 multitrack, every part, named
        E/S/parts/NN_name.mid   one self-contained file per part
        E/S/README.txt

    The loose files sit in their own ``S/`` directory rather than directly in
    ``out_dir`` for two reasons: it mirrors the zip exactly, and it means several
    stems can be exported into one shared ``out_dir`` without their ``parts/``
    directories overwriting each other. ``houseband.server`` does exactly that,
    passing one ``exports/`` directory and a different stem per candidate.

    ``out_dir`` defaults to an ``export/`` directory beside ``midi_path``.
    """
    midi_path = Path(midi_path)
    out_dir = Path(out_dir) if out_dir is not None else midi_path.parent / "export"
    stem = safe_name(stem)

    problems, warnings = check_daw_ready(midi_path, sidecar_path, expect_bars=expect_bars)
    if problems:
        return ExportResult(
            ok=False,
            zip_path=None,
            combined_midi=None,
            part_files={},
            readme_path=None,
            problems=problems,
            warnings=warnings,
        )

    doc = _load(midi_path, sidecar_path)
    bundle_dir = out_dir / stem
    bundle_dir.mkdir(parents=True, exist_ok=True)

    combined = write_combined(
        doc, bundle_dir / f"{stem}_full.mid", sequence_name=title.strip() or stem
    )

    named = _part_filenames(doc)
    part_files: dict[str, Path] = {}
    written_files = [combined]
    for part, filename in named:
        written = write_part(doc, part, bundle_dir / "parts" / filename)
        part_files[part.name] = written
        written_files.append(written)

    readme_path = bundle_dir / "README.txt"
    readme_path.write_text(
        build_readme(
            doc, named, stem=stem, title=title, brief=brief, expect_bars=expect_bars
        )
    )
    written_files.append(readme_path)

    entries = [
        (f"{stem}/{path.relative_to(bundle_dir).as_posix()}", path)
        for path in written_files
    ]
    zip_path = _write_zip(out_dir / f"{stem}.zip", entries)

    return ExportResult(
        ok=True,
        zip_path=zip_path,
        combined_midi=combined,
        part_files=part_files,
        readme_path=readme_path,
        problems=[],
        warnings=warnings,
    )
