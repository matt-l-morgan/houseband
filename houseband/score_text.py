"""Render a MIDI score as compact text for the judges to read.

Two design decisions carry this module.

**Repeat detection is the compression scheme.** A six-minute arrangement is
thousands of notes, which is both expensive and hard to read. Rather than
truncating (which would hide exactly the late-song material where development is
supposed to happen), identical bars are collapsed to ``= bar N``. The compression
is lossless for the reader's purposes *and* it surfaces the single most common
failure in machine-composed music -- eight good bars looped sixteen times -- as a
fact on the page instead of something a judge has to infer by scanning.

**Everything is anchored to bar numbers.** Judges are required to cite bar ranges
in their findings, and a composer has to be able to act on that citation, so the
representation the judge reads is indexed the same way the composer writes.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import pretty_midi

from houseband.house.core import GM
from houseband.timing import TempoMap

_PROGRAM_NAMES = {v: k for k, v in GM.items()}
_PITCH_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


def pitch_name(pitch: int) -> str:
    return f"{_PITCH_NAMES[pitch % 12]}{pitch // 12 - 1}"


# ---------------------------------------------------------------------------
# Chord detection
# ---------------------------------------------------------------------------

# A deliberately small template set. Detection is for orienting the harmony judge,
# so a confident common answer beats an exotic precise one.
_DETECT_TEMPLATES: dict[str, tuple[int, ...]] = {
    "5": (0, 7),
    "": (0, 4, 7),
    "m": (0, 3, 7),
    "dim": (0, 3, 6),
    "aug": (0, 4, 8),
    "sus4": (0, 5, 7),
    "sus2": (0, 2, 7),
    "6": (0, 4, 7, 9),
    "m6": (0, 3, 7, 9),
    "7": (0, 4, 7, 10),
    "maj7": (0, 4, 7, 11),
    "m7": (0, 3, 7, 10),
    "m7b5": (0, 3, 6, 10),
    "9": (0, 4, 7, 10, 2),
    "m9": (0, 3, 7, 10, 2),
    "maj9": (0, 4, 7, 11, 2),
}


def detect_chord(weights: dict[int, float], min_confidence: float = 0.55) -> str | None:
    """Best-fitting chord symbol for a weighted pitch-class profile.

    ``weights`` maps pitch class to sounding duration. Returns ``None`` when
    nothing fits well enough, which is more useful to a judge than a confident
    wrong answer.
    """
    total = sum(weights.values())
    if total <= 0:
        return None

    best_symbol: str | None = None
    best_score = 0.0
    for root in range(12):
        for suffix, intervals in _DETECT_TEMPLATES.items():
            members = {(root + i) % 12 for i in intervals}
            inside = sum(w for pc, w in weights.items() if pc in members)
            outside = sum(w for pc, w in weights.items() if pc not in members)
            # Reward coverage, penalise foreign tones, and mildly prefer simpler
            # templates so a triad is not always beaten by a ninth chord that
            # happens to contain it.
            score = (inside - 0.7 * outside) / total - 0.02 * len(intervals)
            if weights.get(root, 0.0) > 0:
                score += 0.05  # a sounding root is good evidence
            if score > best_score:
                best_score = score
                best_symbol = f"{_PITCH_NAMES[root]}{suffix}"
    return best_symbol if best_score >= min_confidence else None


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


@dataclass
class BarNote:
    beat: float
    pitch: int
    dur_beats: float
    velocity: int

    def fingerprint(self) -> tuple:
        # Quantise to a 32nd-note grid so trivial float noise does not defeat
        # repeat detection, but a genuinely different velocity still counts.
        return (
            round(self.beat * 8),
            self.pitch,
            round(self.dur_beats * 8),
            round(self.velocity / 4),
        )


@dataclass
class ScoreView:
    """Everything needed to describe a score, derived once."""

    structure: dict
    tempo: TempoMap
    total_bars: int
    duration: float
    # track name -> bar index -> notes
    bars: dict[str, dict[int, list[BarNote]]]
    track_meta: dict[str, dict]

    def repeat_fraction(self) -> float:
        """Fraction of non-empty bars that exactly repeat an earlier bar."""
        repeated = considered = 0
        for per_bar in self.bars.values():
            seen: set[tuple] = set()
            for bar in range(self.total_bars):
                notes = per_bar.get(bar)
                if not notes:
                    continue
                considered += 1
                fp = tuple(n.fingerprint() for n in notes)
                if fp in seen:
                    repeated += 1
                seen.add(fp)
        return repeated / considered if considered else 0.0


def load_view(midi_path: Path, sidecar_path: Path | None = None) -> ScoreView:
    """Parse a MIDI file (plus optional sidecar) into a bar-indexed view."""
    midi = pretty_midi.PrettyMIDI(str(midi_path))

    structure: dict = {}
    if sidecar_path is None:
        candidate = Path(midi_path).with_suffix(".score.json")
        sidecar_path = candidate if candidate.exists() else None
    if sidecar_path and Path(sidecar_path).exists():
        try:
            structure = json.loads(Path(sidecar_path).read_text())
        except (OSError, json.JSONDecodeError):
            structure = {}

    tempo = (
        TempoMap.from_structure(structure)
        if structure.get("tempo_map")
        else TempoMap.from_midi(midi)
    )

    pan_by_name = {t["name"]: t.get("pan", 0.0) for t in structure.get("tracks", [])}

    def _pan_from_cc(inst) -> float:
        """Recover pan from CC10 when no sidecar records it.

        Without this, any MIDI we did not write ourselves (every reference piece)
        is reported to the judges as entirely centre-panned. That is a false claim
        about the mix, and it costs the reference marks on orchestration and
        production for a stereo image it actually has.
        """
        values = [cc.value for cc in inst.control_changes if cc.number == 10]
        if not values:
            return 0.0
        # 0 is hard left, 64 centre, 127 hard right.
        return round(values[0] / 127.0 * 2.0 - 1.0, 3)

    bars: dict[str, dict[int, list[BarNote]]] = {}
    track_meta: dict[str, dict] = {}
    duration = 0.0
    max_bar = 0

    for index, inst in enumerate(midi.instruments):
        name = (inst.name or "").strip() or (
            "drums" if inst.is_drum else f"track{index}_program{inst.program}"
        )
        if name in bars:  # defend against duplicate names in foreign MIDI
            name = f"{name}_{index}"

        per_bar: dict[int, list[BarNote]] = defaultdict(list)
        pitches: list[int] = []
        velocities: list[int] = []
        for note in inst.notes:
            bar, beat = tempo.seconds_to_bar_beat(note.start)
            seconds_per_beat = tempo.bar_seconds(bar) / tempo.beats_per_bar
            dur_beats = (note.end - note.start) / seconds_per_beat if seconds_per_beat else 0.0
            per_bar[bar].append(
                BarNote(beat=beat, pitch=note.pitch, dur_beats=dur_beats, velocity=note.velocity)
            )
            pitches.append(note.pitch)
            velocities.append(note.velocity)
            duration = max(duration, note.end)
            max_bar = max(max_bar, bar)

        for notes in per_bar.values():
            notes.sort(key=lambda n: (n.beat, n.pitch))

        bars[name] = dict(per_bar)
        track_meta[name] = {
            "program": inst.program,
            "is_drum": inst.is_drum,
            "pan": pan_by_name.get(name, _pan_from_cc(inst)),
            "note_count": len(inst.notes),
            "low": min(pitches) if pitches else None,
            "high": max(pitches) if pitches else None,
            "vel_min": min(velocities) if velocities else None,
            "vel_max": max(velocities) if velocities else None,
            "vel_mean": (sum(velocities) / len(velocities)) if velocities else None,
        }

    total_bars = max(int(structure.get("total_bars") or 0), max_bar + 1)
    return ScoreView(
        structure=structure,
        tempo=tempo,
        total_bars=total_bars,
        duration=structure.get("duration") or duration,
        bars=bars,
        track_meta=track_meta,
    )


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _fmt_beat(beat: float) -> str:
    """Two decimals at most. Sub-tick precision is noise to a reader."""
    return f"{round(beat, 2):g}"


def _fmt_tempo(tempo: TempoMap) -> str:
    """Summarise the tempo map, collapsing ramps.

    ``ramp_tempo`` writes one entry per bar, so a naive listing produces dozens
    of near-identical entries and buries the one fact a judge wants: that the
    tempo moves, by how much, and where.
    """
    # Community MIDI often carries several tempo events inside one bar (and on
    # non-zero tracks, which is technically malformed). Left alone that renders as
    # "79 from bar 82, 80 from bar 82, 81 from bar 82, ..." and buries the one
    # fact a judge wants in noise it has to pay tokens to read. Last value per bar
    # wins, matching how bpm_at already resolves it.
    collapsed: dict[int, float] = {}
    for bar, bpm in tempo.entries:
        collapsed[bar] = bpm
    entries = sorted(collapsed.items())
    if len(entries) == 1:
        return f"{entries[0][1]:.0f}"

    # Collapse runs of consecutive bars whose tempo moves monotonically.
    runs: list[tuple[int, int, float, float]] = []
    start_bar, start_bpm = entries[0]
    prev_bar, prev_bpm = entries[0]
    for bar, bpm in entries[1:]:
        contiguous = bar == prev_bar + 1
        same_direction = (bpm - prev_bpm) * (prev_bpm - start_bpm) >= 0
        if contiguous and (same_direction or prev_bpm == start_bpm):
            prev_bar, prev_bpm = bar, bpm
            continue
        runs.append((start_bar, prev_bar, start_bpm, prev_bpm))
        start_bar, start_bpm = bar, bpm
        prev_bar, prev_bpm = bar, bpm
    runs.append((start_bar, prev_bar, start_bpm, prev_bpm))

    parts: list[str] = []
    for a, b, bpm_a, bpm_b in runs:
        if abs(bpm_a - bpm_b) < 0.5:
            parts.append(f"{bpm_a:.0f} from bar {a}")
        else:
            parts.append(f"{bpm_a:.0f}->{bpm_b:.0f} over bars {a}-{b}")
    return ", ".join(parts)


def _render_bar(notes: list[BarNote], is_drum: bool) -> str:
    from houseband.house.core import DRUMS

    drum_names = {v: k for k, v in DRUMS.items()}
    parts: list[str] = []
    for note in notes:
        label = drum_names.get(note.pitch, str(note.pitch)) if is_drum else pitch_name(note.pitch)
        if is_drum:
            parts.append(f"{_fmt_beat(note.beat)}:{label}@{note.velocity}")
        else:
            parts.append(
                f"{_fmt_beat(note.beat)}:{label}/{note.dur_beats:.2g}@{note.velocity}"
            )
    return " ".join(parts)


def _section_for_bar(structure: dict, bar: int) -> str | None:
    for section in structure.get("sections", []):
        if section["start_bar"] <= bar < section["start_bar"] + section["bars"]:
            return section["name"]
    return None


def render(
    midi_path: Path,
    sidecar_path: Path | None = None,
    include_notes: bool = True,
    max_note_bars: int = 400,
) -> str:
    """Produce the judge-readable score text."""
    view = load_view(midi_path, sidecar_path)
    s = view.structure
    out: list[str] = []

    # -- header ------------------------------------------------------------
    num, den = s.get("time_sig", [view.tempo.beats_per_bar, 4])
    tempo_desc = _fmt_tempo(view.tempo)
    minutes, seconds = divmod(int(view.duration), 60)
    out.append(
        f"KEY {s.get('key', 'unspecified')}   TIME {num}/{den}   "
        f"BPM {tempo_desc}   BARS {view.total_bars}   LENGTH {minutes}:{seconds:02d}"
    )
    out.append("")

    # -- sections ----------------------------------------------------------
    if s.get("sections"):
        out.append("SECTIONS")
        for section in s["sections"]:
            start, length = section["start_bar"], section["bars"]
            out.append(
                f"  bars {start:>3}-{start + length - 1:<3}  {section['name']:<16} ({length} bars)"
            )
    else:
        out.append("SECTIONS  none declared (form must be inferred from the notes)")
    out.append("")

    # -- tracks ------------------------------------------------------------
    out.append("TRACKS")
    for name, meta in view.track_meta.items():
        if meta["is_drum"]:
            kind = "drum kit"
        else:
            program_name = _PROGRAM_NAMES.get(meta["program"], f"program {meta['program']}")
            kind = f"{program_name} ({meta['program']})"
        span = (
            f"{pitch_name(meta['low'])}-{pitch_name(meta['high'])}"
            if meta["low"] is not None and not meta["is_drum"]
            else "-"
        )
        vel = (
            f"{meta['vel_min']}-{meta['vel_max']} (mean {meta['vel_mean']:.0f})"
            if meta["vel_min"] is not None
            else "-"
        )
        out.append(
            f"  {name:<18} {kind:<26} pan {meta['pan']:+.2f}  "
            f"{meta['note_count']:>5} notes  range {span:<9} vel {vel}"
        )
    out.append("")

    # -- density -----------------------------------------------------------
    # Notes per bar per track per section. This is where "the arrangement never
    # changes" and "everything enters at once" both become obvious.
    if s.get("sections"):
        names = list(view.track_meta)
        out.append("DENSITY  notes per bar, by section")
        header = "  " + "section".ljust(16) + "".join(n[:11].rjust(13) for n in names)
        out.append(header)
        for section in s["sections"]:
            start, length = section["start_bar"], section["bars"]
            cells = []
            for name in names:
                count = sum(
                    len(view.bars.get(name, {}).get(b, []))
                    for b in range(start, start + length)
                )
                cells.append(f"{count / length:.1f}".rjust(13))
            out.append("  " + section["name"][:16].ljust(16) + "".join(cells))
        out.append("")

    # -- harmony -----------------------------------------------------------
    chords: list[str] = []
    for bar in range(view.total_bars):
        weights: dict[int, float] = defaultdict(float)
        for name, per_bar in view.bars.items():
            if view.track_meta[name]["is_drum"]:
                continue
            for note in per_bar.get(bar, []):
                weights[note.pitch % 12] += max(note.dur_beats, 0.1)
        chords.append(detect_chord(weights) or "-")

    if any(c != "-" for c in chords):
        out.append("HARMONY  detected chord per bar (approximate)")
        line_bars = 8
        for start in range(0, view.total_bars, line_bars):
            chunk = chords[start : start + line_bars]
            label = f"  bar {start:>3}"
            out.append(f"{label}  " + " | ".join(c.ljust(6) for c in chunk))
        out.append("")

    # -- repetition summary ------------------------------------------------
    repeat = view.repeat_fraction()
    out.append(
        f"REPETITION  {repeat:.0%} of sounding bars are exact repeats of an earlier bar "
        "in the same track"
    )
    out.append("")

    if not include_notes:
        return "\n".join(out)

    # -- notes, with repeats collapsed -------------------------------------
    out.append(
        "NOTES  per track, bar by bar. Format beat:pitch/duration_in_beats@velocity. "
        "'= bar N' means this bar is identical to bar N in the same track."
    )
    out.append("")

    for name, per_bar in view.bars.items():
        meta = view.track_meta[name]
        out.append(f"{name}")
        first_seen: dict[tuple, int] = {}
        rendered = 0
        current_section: str | None = None
        for bar in range(view.total_bars):
            notes = per_bar.get(bar)
            if not notes:
                continue

            section = _section_for_bar(s, bar)
            if section != current_section:
                current_section = section
                if section:
                    out.append(f"  -- {section} --")

            fp = tuple(n.fingerprint() for n in notes)
            if fp in first_seen:
                out.append(f"  bar {bar:>3} = bar {first_seen[fp]}")
                continue
            first_seen[fp] = bar

            if rendered >= max_note_bars:
                out.append(
                    f"  bar {bar:>3} ... (further distinct bars omitted for length)"
                )
                break
            rendered += 1
            out.append(f"  bar {bar:>3} | {_render_bar(notes, meta['is_drum'])}")
        out.append("")

    return "\n".join(out)


def render_compact(midi_path: Path, sidecar_path: Path | None = None) -> str:
    """Header, structure and summaries only. For pairwise comparison prompts."""
    return render(midi_path, sidecar_path, include_notes=False)
