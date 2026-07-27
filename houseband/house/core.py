"""Core composition library.

Composers write short Python programs against this module. The central idea is
that you place music in **musical time** (absolute bar number plus beat within
the bar) and this module does the conversion to seconds exactly once, correctly.
That removes the entire class of timing-drift bugs that would otherwise be
misread downstream as bad musicianship.

The file :meth:`Score.write` produces keeps the musical time as well: notes are
positioned at ticks computed from bars and beats, and the tempo map is written
alongside them. That is what makes the bar grid a DAW derives from ``out.mid``
the same grid the composer wrote against, however much the tempo moves.

Minimal program:

    from houseband.house import Score

    s = Score(bpm=72, key="Am")
    s.mark_section("intro", start_bar=0, bars=8)

    gtr = s.track("acoustic_gtr", patch=25, pan=-0.3)
    for bar in range(8):
        gtr.chord(bar=bar, beat=1, symbol="Am7", dur=3.5, vel=58)

    s.write("out.mid")

Conventions:
  * ``bar`` is absolute and 0-indexed across the whole song.
  * ``beat`` is 1-indexed the way musicians count (1, 2, 3, 4).
  * ``dur`` is measured in beats.
  * ``pan`` runs -1.0 (hard left) to 1.0 (hard right).
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field

import mido
import pretty_midi

__all__ = [
    "Score",
    "Track",
    "DrumTrack",
    "NoteEvent",
    "note_number",
    "chord_pitches",
    "DRUMS",
    "GM",
    "TICKS_PER_QUARTER",
]


# ---------------------------------------------------------------------------
# Pitch and chord parsing
# ---------------------------------------------------------------------------

_SEMITONE = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}


def note_number(pitch: int | str) -> int:
    """Resolve a pitch to a MIDI note number.

    Accepts an int (returned unchanged) or a name like ``"C4"``, ``"F#3"``,
    ``"Bb2"``. Middle C (MIDI 60) is ``"C4"``.
    """
    if isinstance(pitch, int):
        return pitch
    text = pitch.strip()
    if not text:
        raise ValueError("empty pitch")

    letter = text[0].upper()
    if letter not in _SEMITONE:
        raise ValueError(f"bad pitch letter in {pitch!r}")
    value = _SEMITONE[letter]
    i = 1
    while i < len(text) and text[i] in "#b♯♭":
        value += 1 if text[i] in "#♯" else -1
        i += 1

    octave_text = text[i:]
    if not octave_text:
        octave = 4
    else:
        try:
            octave = int(octave_text)
        except ValueError as exc:
            raise ValueError(f"bad octave in {pitch!r}") from exc

    number = value + (octave + 1) * 12
    if not 0 <= number <= 127:
        raise ValueError(f"pitch {pitch!r} resolves to {number}, outside 0-127")
    return number


# Interval sets, in semitones from the root. Ordered longest-key-first at
# lookup time so that "m7b5" is not matched as "m7".
_QUALITIES: dict[str, tuple[int, ...]] = {
    "": (0, 4, 7),
    "maj": (0, 4, 7),
    "M": (0, 4, 7),
    "m": (0, 3, 7),
    "min": (0, 3, 7),
    "-": (0, 3, 7),
    "5": (0, 7),
    "dim": (0, 3, 6),
    "aug": (0, 4, 8),
    "sus2": (0, 2, 7),
    "sus4": (0, 5, 7),
    "6": (0, 4, 7, 9),
    "m6": (0, 3, 7, 9),
    "7": (0, 4, 7, 10),
    "maj7": (0, 4, 7, 11),
    "M7": (0, 4, 7, 11),
    "m7": (0, 3, 7, 10),
    "mmaj7": (0, 3, 7, 11),
    "m7b5": (0, 3, 6, 10),
    "dim7": (0, 3, 6, 9),
    "7sus4": (0, 5, 7, 10),
    "add9": (0, 4, 7, 14),
    "madd9": (0, 3, 7, 14),
    "9": (0, 4, 7, 10, 14),
    "maj9": (0, 4, 7, 11, 14),
    "M9": (0, 4, 7, 11, 14),
    "m9": (0, 3, 7, 10, 14),
    "69": (0, 4, 7, 9, 14),
    "7b9": (0, 4, 7, 10, 13),
    "7s9": (0, 4, 7, 10, 15),
    "7#9": (0, 4, 7, 10, 15),
    "7b5": (0, 4, 6, 10),
    "7s5": (0, 4, 8, 10),
    "7#5": (0, 4, 8, 10),
    "7s11": (0, 4, 7, 10, 18),
    "7#11": (0, 4, 7, 10, 18),
    "11": (0, 7, 10, 14, 17),
    "m11": (0, 3, 7, 10, 14, 17),
    "13": (0, 4, 7, 10, 14, 21),
    "m13": (0, 3, 7, 10, 14, 21),
}

_QUALITY_KEYS = sorted(_QUALITIES, key=len, reverse=True)


def chord_pitches(symbol: str, octave: int = 3) -> list[int]:
    """Expand a chord symbol into MIDI note numbers.

    Handles the common jazz/pop vocabulary plus slash chords::

        chord_pitches("Am7")     -> [57, 60, 64, 67]
        chord_pitches("F#m9")    -> [54, 57, 61, 64, 68]
        chord_pitches("C/G")     -> [55, 60, 64, 67]

    ``octave`` sets where the root lands; extensions stack upward from it.
    """
    text = symbol.strip()
    if not text:
        raise ValueError("empty chord symbol")

    bass: int | None = None
    if "/" in text:
        text, bass_name = text.split("/", 1)
        text = text.strip()
        bass_name = bass_name.strip()
        # A bare letter bass defaults an octave below the chord root.
        bass = note_number(
            bass_name if any(c.isdigit() for c in bass_name) else f"{bass_name}{octave - 1}"
        )

    # Split the root (letter plus accidentals) from the quality.
    i = 1
    while i < len(text) and text[i] in "#b♯♭":
        i += 1
    root_name, quality = text[:i], text[i:]

    if quality not in _QUALITIES:
        match = next((k for k in _QUALITY_KEYS if k and quality.startswith(k)), None)
        if match is None:
            raise ValueError(f"unrecognised chord quality {quality!r} in {symbol!r}")
        quality = match

    root = note_number(f"{root_name}{octave}")
    pitches = [root + step for step in _QUALITIES[quality]]
    if bass is not None and bass not in pitches:
        pitches.insert(0, bass)
    return pitches


# ---------------------------------------------------------------------------
# General MIDI helpers
# ---------------------------------------------------------------------------

# A small, named subset of GM programs. Composers may pass a raw int instead.
GM = {
    "grand_piano": 0,
    "electric_piano": 4,
    "harpsichord": 6,
    "vibraphone": 11,
    "organ": 19,
    "nylon_guitar": 24,
    "acoustic_guitar": 25,
    "jazz_guitar": 26,
    "clean_guitar": 27,
    "overdriven_guitar": 29,
    "distorted_guitar": 30,
    "acoustic_bass": 32,
    "fingered_bass": 33,
    "picked_bass": 34,
    "fretless_bass": 35,
    "violin": 40,
    "cello": 42,
    "strings": 48,
    "choir": 52,
    "trumpet": 56,
    "trombone": 57,
    "sax": 65,
    "flute": 73,
    "recorder": 74,
    "square_lead": 80,
    "saw_lead": 81,
    "warm_pad": 89,
    "sweep_pad": 95,
}

# GM percussion key map (channel 10), friendly names.
DRUMS = {
    "kick": 36,
    "kick2": 35,
    "snare": 38,
    "snare_rim": 37,
    "clap": 39,
    "snare2": 40,
    "tom_low": 41,
    "hat": 42,
    "hat_closed": 42,
    "hat_pedal": 44,
    "hat_open": 46,
    "tom_mid": 47,
    "tom_high": 50,
    "crash": 49,
    "ride": 51,
    "china": 52,
    "ride_bell": 53,
    "splash": 55,
    "cowbell": 56,
    "tambourine": 54,
    "shaker": 82,
}


# ---------------------------------------------------------------------------
# MIDI file plumbing
# ---------------------------------------------------------------------------

# Resolution of every file this module writes. mido spells it ``ticks_per_beat``
# but the unit is the quarter note, hence the name here. 480 divides by both 3
# and 4, so 16th triplets and 32nds all land on integer ticks; pretty_midi's own
# default of 220 does not divide by 3. ``houseband.export`` writes the same
# number, so a bundle and the ``out.mid`` it came from share one grid.
TICKS_PER_QUARTER = 480

# General MIDI puts percussion on channel 10 (index 9) and every drum rack looks
# for it there.
_DRUM_CHANNEL = 9
_MELODIC_CHANNELS = [c for c in range(16) if c != _DRUM_CHANNEL]

# Ordering of events that share a tick. Meta before program before controllers
# before notes, and note-off strictly before note-on so a legato handoff on one
# pitch does not silence the note that is arriving.
#
# This table and the two helpers below mirror the ones in ``houseband.export``.
# They are duplicated rather than shared because ``export`` sits on top of this
# module: it imports ``DRUMS`` from here directly, and again by way of
# ``score_text``. Importing back the other way would be both an import cycle and
# a layering inversion, with the library composers write against depending on the
# delivery pipeline. The right end state is ``export`` importing these from here,
# which is a change for a commit that can touch both files.
_EVENT_RANK = {
    "track_name": 0,
    "time_signature": 1,
    "set_tempo": 2,
    "marker": 3,
    "program_change": 4,
    "control_change": 5,
    "note_off": 6,
    "note_on": 7,
}

_Event = tuple[int, int, int, "mido.messages.BaseMessage"]


def _event(tick: int, message, seq: int) -> _Event:
    return (tick, _EVENT_RANK.get(message.type, 99), seq, message)


def _track_from_events(events: list[_Event]) -> mido.MidiTrack:
    """Absolute-tick events to a delta-timed mido track.

    The sort key carries an explicit sequence tiebreaker rather than leaning on
    sort stability, so the same score always produces the same bytes regardless
    of the order the events happened to be appended in.
    """
    track = mido.MidiTrack()
    previous = 0
    for tick, _, _, message in sorted(events, key=lambda e: (e[0], e[1], e[2])):
        track.append(message.copy(time=tick - previous))
        previous = tick
    track.append(mido.MetaMessage("end_of_track", time=1 if events else 0))
    return track


def _midi_text(text: str) -> str:
    """Text a Standard MIDI File can actually hold.

    MIDI text is bytes, and mido encodes it as latin-1, as does pretty_midi when
    it reads it back. Composer programs are written by a language model, so a
    curly quote or an em dash in a track or section name is entirely likely, and
    without this it raises on save and loses the whole write. The sidecar keeps
    the name exactly as it was declared, so the lossy copy here costs nothing that
    matters.
    """
    return text.encode("latin-1", "replace").decode("latin-1")


# ---------------------------------------------------------------------------
# Score
# ---------------------------------------------------------------------------


@dataclass
class Section:
    """A named span of bars. Judges read these to reason about form."""

    name: str
    start_bar: int
    bars: int

    @property
    def end_bar(self) -> int:
        return self.start_bar + self.bars


@dataclass(frozen=True, slots=True)
class NoteEvent:
    """One note in the musical time it was written in. ``dur`` is in beats.

    This exists because seconds are a lossy record of musical position. Given a
    time in seconds and a tempo map you can recover which bar a note is in, but
    only by replaying the whole map and accepting float error at every bar line,
    and you cannot recover it at all if the map later changes. The composer told
    us the bar and the beat, so we keep them.
    """

    pitch: int
    bar: int
    beat: float
    dur: float
    velocity: int


class Track:
    """A single named instrument line.

    Created via :meth:`Score.track`, not directly.
    """

    def __init__(self, score: "Score", name: str, patch: int, pan: float, is_drum: bool = False):
        self.score = score
        self.name = name
        self.patch = patch
        self.pan = pan
        self.is_drum = is_drum
        self.notes: list[tuple[int, float, float, int]] = []  # pitch, start_s, end_s, vel
        # The same notes, in bars and beats. Both are kept on purpose: seconds
        # are what everything that reads a parsed MIDI file works in (and other
        # modules duck-type against the tuple shape above), while bars and beats
        # are what a DAW's bar grid is made of. Deriving either one from the
        # other after the fact is where timing drift comes from, so neither is
        # derived.
        self.events: list[NoteEvent] = []

    # -- placement ---------------------------------------------------------

    def note(
        self,
        bar: int,
        beat: float,
        pitch: int | str,
        dur: float,
        vel: int = 72,
    ) -> "Track":
        """Place one note. ``dur`` is in beats. Returns self for chaining."""
        if dur <= 0:
            raise ValueError(f"{self.name}: note at bar {bar} beat {beat} has dur={dur}")
        vel = max(1, min(127, int(vel)))
        number = note_number(pitch)
        start = self.score.time_at(bar, beat)
        end = self.score.time_at(bar, beat + dur)
        self.notes.append((number, start, end, vel))
        self.events.append(
            NoteEvent(
                pitch=number,
                bar=int(bar),
                beat=float(beat),
                dur=float(dur),
                velocity=vel,
            )
        )
        self.score._note_max_bar = max(self.score._note_max_bar, int(bar))
        return self

    def chord(
        self,
        bar: int,
        beat: float,
        pitches: list[int | str] | None = None,
        symbol: str | None = None,
        dur: float = 1.0,
        vel: int = 72,
        octave: int = 3,
        spread: float = 0.0,
    ) -> "Track":
        """Place several notes at once.

        Give either an explicit ``pitches`` list or a chord ``symbol``.
        ``spread`` adds a small ascending offset in beats per voice, which
        produces a strummed or rolled feel rather than a block hit.
        """
        if (pitches is None) == (symbol is None):
            raise ValueError("chord() takes exactly one of pitches= or symbol=")
        resolved = chord_pitches(symbol, octave=octave) if symbol else [note_number(p) for p in pitches]
        for i, pitch in enumerate(resolved):
            self.note(bar, beat + i * spread, pitch, max(dur - i * spread, 0.05), vel)
        return self

    def hit(self, bar: int, beat: float, name: str, vel: int = 96, dur: float = 0.25) -> "Track":
        """Place a drum hit by friendly name. Drum tracks only."""
        if not self.is_drum:
            raise ValueError(f"{self.name} is not a drum track; use note() instead")
        if name not in DRUMS:
            raise ValueError(f"unknown drum {name!r}; known: {sorted(DRUMS)}")
        return self.note(bar, beat, DRUMS[name], dur, vel)

    # -- export ------------------------------------------------------------

    def _to_instrument(self) -> pretty_midi.Instrument:
        inst = pretty_midi.Instrument(
            program=0 if self.is_drum else self.patch,
            is_drum=self.is_drum,
            name=self.name,
        )
        for pitch, start, end, vel in self.notes:
            inst.notes.append(
                pretty_midi.Note(velocity=vel, pitch=pitch, start=start, end=end)
            )
        # Pan as CC10 at time zero. 0 = hard left, 64 = centre, 127 = hard right.
        cc_value = int(round((self.pan + 1.0) / 2.0 * 127))
        inst.control_changes.append(
            pretty_midi.ControlChange(number=10, value=max(0, min(127, cc_value)), time=0.0)
        )
        return inst

    def _to_mido_track(self, channel: int) -> mido.MidiTrack:
        """The track as MIDI events on one channel, positioned in musical ticks.

        Every note is placed by :meth:`Score.tick_at` from the bar and beat it
        was written at. Nothing here consults the note's seconds, which is the
        whole point: ticks are musical time, and a tick derived from seconds has
        the tempo baked into it twice.
        """
        events = [_event(0, mido.MetaMessage("track_name", name=_midi_text(self.name)), 0)]
        events.append(
            _event(
                0,
                mido.Message(
                    "program_change",
                    # GM selects the drum kit by channel rather than by program,
                    # so 0 is the conventional filler on the percussion track.
                    program=0 if self.is_drum else self.patch,
                    channel=channel,
                ),
                1,
            )
        )
        # Pan as CC10 at tick zero. 0 = hard left, 64 = centre, 127 = hard right.
        pan_value = max(0, min(127, int(round((self.pan + 1.0) / 2.0 * 127))))
        events.append(
            _event(
                0,
                mido.Message("control_change", control=10, value=pan_value, channel=channel),
                2,
            )
        )

        seq = 3
        for note in self.events:
            start = self.score.tick_at(note.bar, note.beat)
            end = self.score.tick_at(note.bar, note.beat + note.dur)
            # A note shorter than one tick would put its note-off on the same
            # tick as its note-on, which some DAWs drop and others import as a
            # click. Give it the one tick it needs to exist.
            end = max(end, start + 1)
            events.append(
                _event(
                    start,
                    mido.Message(
                        "note_on", note=note.pitch, velocity=note.velocity, channel=channel
                    ),
                    seq,
                )
            )
            seq += 1
            events.append(
                _event(
                    end,
                    mido.Message("note_off", note=note.pitch, velocity=0, channel=channel),
                    seq,
                )
            )
            seq += 1
        return _track_from_events(events)


class DrumTrack(Track):
    """Marker subclass so ``isinstance`` checks read clearly."""


class Score:
    """The song under construction.

    Tempo may change over the song via :meth:`tempo`; bar start times are
    computed by accumulating each bar's duration, so a tempo change never
    displaces material that was already placed.
    """

    def __init__(
        self,
        bpm: float = 120.0,
        key: str = "C",
        time_sig: tuple[int, int] = (4, 4),
    ):
        if bpm <= 0:
            raise ValueError("bpm must be positive")
        self.key = key
        self.time_sig = time_sig
        self.tracks: list[Track] = []
        self.sections: list[Section] = []
        # Tempo map as sorted (start_bar, bpm). Bar 0 always has an entry.
        self._tempo_bars: list[int] = [0]
        self._tempo_bpms: list[float] = [float(bpm)]
        self._bar_times: list[float] = [0.0]  # cumulative start time per bar
        # Highest bar any note has been placed in, so total_bars is meaningful
        # even when a composer declares no sections.
        self._note_max_bar: int = -1

    # -- structure ---------------------------------------------------------

    @property
    def beats_per_bar(self) -> int:
        return self.time_sig[0]

    @property
    def quarters_per_bar(self) -> float:
        num, den = self.time_sig
        return num * 4.0 / den

    def tempo(self, bar: int, bpm: float) -> "Score":
        """Set the tempo from ``bar`` onward."""
        if bar < 0:
            raise ValueError("bar must be >= 0")
        if bpm <= 0:
            raise ValueError("bpm must be positive")
        i = bisect.bisect_left(self._tempo_bars, bar)
        if i < len(self._tempo_bars) and self._tempo_bars[i] == bar:
            self._tempo_bpms[i] = float(bpm)
        else:
            self._tempo_bars.insert(i, bar)
            self._tempo_bpms.insert(i, float(bpm))
        self._bar_times = [0.0]  # invalidate cache
        return self

    def ramp_tempo(self, start_bar: int, end_bar: int, start_bpm: float, end_bpm: float) -> "Score":
        """Step the tempo linearly bar by bar between two values.

        Useful for the gradual acceleration common in long-form rock.
        """
        if end_bar <= start_bar:
            raise ValueError("end_bar must be greater than start_bar")
        span = end_bar - start_bar
        for i in range(span + 1):
            self.tempo(start_bar + i, start_bpm + (end_bpm - start_bpm) * i / span)
        return self

    def bpm_at(self, bar: int) -> float:
        i = bisect.bisect_right(self._tempo_bars, bar) - 1
        return self._tempo_bpms[max(0, i)]

    def mark_section(self, name: str, start_bar: int, bars: int) -> Section:
        """Label a span of bars. Overlaps are allowed but usually a mistake."""
        if bars <= 0:
            raise ValueError("section must span at least one bar")
        section = Section(name=name, start_bar=start_bar, bars=bars)
        self.sections.append(section)
        self.sections.sort(key=lambda s: s.start_bar)
        return section

    def track(
        self,
        name: str,
        patch: int | str = 0,
        pan: float = 0.0,
        drums: bool = False,
    ) -> Track:
        """Add a named instrument line.

        ``patch`` accepts a GM program number or a name from :data:`GM`.
        """
        if any(t.name == name for t in self.tracks):
            raise ValueError(f"duplicate track name {name!r}")
        if isinstance(patch, str):
            if patch not in GM:
                raise ValueError(f"unknown patch name {patch!r}; known: {sorted(GM)}")
            patch = GM[patch]
        if not -1.0 <= pan <= 1.0:
            raise ValueError(f"pan must be between -1.0 and 1.0, got {pan}")
        cls = DrumTrack if drums else Track
        t = cls(self, name, int(patch), float(pan), is_drum=drums)
        self.tracks.append(t)
        return t

    def drum_track(self, name: str = "drums", pan: float = 0.0) -> Track:
        return self.track(name, patch=0, pan=pan, drums=True)

    # -- time --------------------------------------------------------------

    def _bar_start(self, bar: int) -> float:
        """Cumulative seconds at the downbeat of ``bar``."""
        while len(self._bar_times) <= bar:
            b = len(self._bar_times) - 1
            seconds_per_bar = self.quarters_per_bar * 60.0 / self.bpm_at(b)
            self._bar_times.append(self._bar_times[b] + seconds_per_bar)
        return self._bar_times[bar]

    def time_at(self, bar: int, beat: float) -> float:
        """Convert a bar and beat to seconds.

        ``beat`` is 1-indexed and may exceed the bar length, which is how note
        durations that cross a bar line are handled.
        """
        if bar < 0:
            raise ValueError(f"bar must be >= 0, got {bar}")
        whole_bars, rem_beats = divmod(beat - 1.0, float(self.beats_per_bar))
        target_bar = bar + int(whole_bars)
        _, den = self.time_sig
        quarters = rem_beats * 4.0 / den
        return self._bar_start(target_bar) + quarters * 60.0 / self.bpm_at(target_bar)

    def tick_at(self, bar: int, beat: float) -> int:
        """Convert a bar and beat to MIDI ticks.

        The tempo map is not consulted, and that is not an oversight: ticks *are*
        musical time. A DAW draws its bar grid at fixed tick multiples and lays
        the tempo map over the top, so bar 60 beat 1 is at
        ``60 * quarters_per_bar * TICKS_PER_QUARTER`` whatever the tempo does in
        between. Converting a note's seconds into ticks instead would bake the
        tempo in a second time, and every note after the first tempo change would
        land off the grid by however much the tempo had moved.

        ``beat`` is 1-indexed and may exceed the bar length, exactly as in
        :meth:`time_at`.
        """
        if bar < 0:
            raise ValueError(f"bar must be >= 0, got {bar}")
        _, den = self.time_sig
        quarters = bar * self.quarters_per_bar + (beat - 1.0) * 4.0 / den
        return int(round(quarters * TICKS_PER_QUARTER))

    @property
    def total_bars(self) -> int:
        """Song length in bars, from sections and from where notes actually are.

        Taking the max of both means an undeclared-section score still reports a
        real length, and a composer who marks sections beyond the last note still
        gets credit for the structure it declared.
        """
        from_sections = max((s.end_bar for s in self.sections), default=0)
        return max(from_sections, self._note_max_bar + 1)

    @property
    def duration(self) -> float:
        """Song length in seconds, taken from the last note that sounds."""
        ends = [end for t in self.tracks for _, _, end, _ in t.notes]
        return max(ends, default=0.0)

    # -- export ------------------------------------------------------------

    def to_midi(self) -> pretty_midi.PrettyMIDI:
        """The score as an in-memory pretty_midi object, for analysis.

        Note times in seconds are exact. The tempo map is not, and cannot be:
        pretty_midi takes a single ``initial_tempo`` and offers no public way to
        put the rest of a map onto an object you built in memory. So this object
        knows where every note sounds and does not know where the bar lines are
        after the first tempo change.

        That limitation is why :meth:`write` builds the file with mido rather
        than writing this object out. Anything that needs the bar grid, which
        means any DAW, should read the file.
        """
        midi = pretty_midi.PrettyMIDI(initial_tempo=self._tempo_bpms[0])
        for t in self.tracks:
            midi.instruments.append(t._to_instrument())
        return midi

    def _conductor_track(self) -> mido.MidiTrack:
        """Track 0: the time signature, the whole tempo map, section markers.

        Emitting the full map is the half of the fix that is visible in a DAW's
        tempo lane. The other half is that the notes are at musical ticks (see
        :meth:`tick_at`); a correct tempo map laid over seconds-derived note
        positions would be worse than no map at all, because then the grid moves
        and the notes do not.

        Markers are cheap and arrive as locators in both Ableton and Pro Tools,
        so the section names the composer declared survive into the session
        instead of living only in the sidecar.
        """
        num, den = self.time_sig
        events = [
            _event(
                0,
                mido.MetaMessage("time_signature", numerator=num, denominator=den),
                0,
            )
        ]
        seq = 1

        # Repeats of the same tempo are dropped. ramp_tempo() writes one entry
        # per bar, so a ramp that begins at the opening tempo would otherwise
        # emit a redundant event, and some DAWs draw every event in the tempo
        # lane whether or not it changes anything.
        previous: float | None = None
        for bar, bpm in zip(self._tempo_bars, self._tempo_bpms):
            if previous is not None and abs(bpm - previous) < 1e-9:
                continue
            previous = bpm
            events.append(
                _event(
                    self.tick_at(bar, 1),
                    # Microseconds per quarter note is the only tempo unit MIDI
                    # has, and it is an integer, so a BPM never survives exactly.
                    # bpm2tempo's time_signature argument is left at its 4/4
                    # default deliberately: a Score's bpm counts quarter notes
                    # whatever the time signature, which is the same convention
                    # _bar_start() uses.
                    mido.MetaMessage("set_tempo", tempo=mido.bpm2tempo(bpm)),
                    seq,
                )
            )
            seq += 1

        for section in self.sections:
            events.append(
                _event(
                    self.tick_at(section.start_bar, 1),
                    mido.MetaMessage("marker", text=_midi_text(section.name)),
                    seq,
                )
            )
            seq += 1

        return _track_from_events(events)

    def _midi_file(self) -> mido.MidiFile:
        """The Standard MIDI File this score writes.

        Type 1 with a conductor track and then one named track per part, which is
        the same shape pretty_midi's writer produces, so everything downstream
        that reads ``out.mid`` sees the file it already expected. Note-less tracks
        are written too, again matching the old behaviour: a declared but unused
        track is a fact about the arrangement, and ``structure()`` reports it.
        """
        midi = mido.MidiFile(type=1, ticks_per_beat=TICKS_PER_QUARTER)
        midi.tracks.append(self._conductor_track())

        melodic = 0
        for t in self.tracks:
            if t.is_drum:
                channel = _DRUM_CHANNEL
            else:
                # Past 15 melodic parts the channels have to repeat, which is
                # harmless here: each part is its own track in a type-1 file, so
                # a DAW separates them by track regardless.
                channel = _MELODIC_CHANNELS[melodic % len(_MELODIC_CHANNELS)]
                melodic += 1
            midi.tracks.append(t._to_mido_track(channel))
        return midi

    def structure(self) -> dict:
        """Structural metadata that MIDI itself cannot carry.

        Sections, the tempo map, and per-track mix settings are all concepts
        this library has and Standard MIDI Files do not (not in any form that
        survives a round-trip through pretty_midi). They are written to a
        sidecar so the piano roll can put bars on the x-axis and the judges can
        read form directly instead of inferring it from note timings.
        """
        return {
            "key": self.key,
            "time_sig": list(self.time_sig),
            "tempo_map": [
                [bar, bpm] for bar, bpm in zip(self._tempo_bars, self._tempo_bpms)
            ],
            "total_bars": self.total_bars,
            "duration": round(self.duration, 3),
            "sections": [
                {"name": s.name, "start_bar": s.start_bar, "bars": s.bars}
                for s in self.sections
            ],
            "tracks": [
                {
                    "name": t.name,
                    "patch": t.patch,
                    "pan": round(t.pan, 3),
                    "is_drum": t.is_drum,
                    "note_count": len(t.notes),
                }
                for t in self.tracks
            ],
        }

    def write(self, path: str = "out.mid") -> str:
        """Write the MIDI file plus its structural sidecar.

        The sidecar lands beside the MIDI with a ``.score.json`` suffix.
        Returns the MIDI path written.
        """
        if not self.tracks:
            raise ValueError("score has no tracks")
        if not any(t.notes for t in self.tracks):
            raise ValueError("score has no notes")

        import json
        from pathlib import Path as _Path

        self._midi_file().save(str(path))
        sidecar = _Path(path).with_suffix(".score.json")
        sidecar.write_text(json.dumps(self.structure(), indent=2))
        return path

    def summary(self) -> str:
        """One-line-per-fact description, handy when debugging a program."""
        lines = [
            f"key={self.key} time_sig={self.time_sig[0]}/{self.time_sig[1]} "
            f"bpm={self._tempo_bpms[0]:.0f} bars={self.total_bars} "
            f"duration={self.duration:.1f}s"
        ]
        for s in self.sections:
            lines.append(f"  section {s.name}: bars {s.start_bar}-{s.end_bar - 1}")
        for t in self.tracks:
            kind = "drums" if t.is_drum else f"patch={t.patch}"
            lines.append(f"  track {t.name}: {kind} pan={t.pan:+.2f} notes={len(t.notes)}")
        return "\n".join(lines)
