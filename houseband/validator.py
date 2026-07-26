"""The deterministic gate. Not a judge.

This is a compiler check: it decides whether a candidate is a well-formed
submission at all, and says nothing about whether the music is any good. That
separation is deliberate. The judges are all LLMs, which is a design choice with
real upside, but LLMs cannot reliably notice a bass line sitting an octave above
its playable range and genuinely cannot compute n-gram overlap against a
reference. Those two checks need arithmetic, so they live here and score
nothing.

Three responsibilities:

* :func:`check_imports` -- static allowlist over model-written code, run before
  execution. A mitigation, not a sandbox (see ``docs/security.md``).
* :func:`validate_score` -- structural sanity on the produced MIDI.
* :func:`check_originality` -- melodic n-gram overlap against a reference, which
  is what stops "reward similarity to the reference" from quietly becoming
  "reward plagiarism".
"""

from __future__ import annotations

import ast
import json
from dataclasses import dataclass, field
from pathlib import Path

import pretty_midi

# ---------------------------------------------------------------------------
# Static check on generated code
# ---------------------------------------------------------------------------

ALLOWED_IMPORTS = {
    "houseband",
    "houseband.house",
    "houseband.house.core",
    "houseband.house.learned",
    "math",
    "random",
    "itertools",
    "functools",
    "collections",
    "dataclasses",
    "typing",
    "statistics",
    "copy",
    "enum",
    "fractions",
}

# Builtins with no legitimate use in a composition program, and an obvious role
# in escaping one.
FORBIDDEN_NAMES = {
    "__import__",
    "eval",
    "exec",
    "compile",
    "open",
    "globals",
    "locals",
    "vars",
    "input",
    "breakpoint",
    "memoryview",
}

# Dunder attribute access is the classic route out of a restricted namespace
# (``().__class__.__bases__`` and friends). Allow the few that are innocuous.
ALLOWED_DUNDERS = {"__name__", "__doc__", "__init__", "__all__"}


def check_imports(code: str) -> list[str]:
    """Return a list of problems, empty if the code passes.

    Written to give the composer agent a message it can act on, since it will
    read the rejection and try again.
    """
    problems: list[str] = []
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return [f"SyntaxError on line {exc.lineno}: {exc.msg}"]

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if alias.name not in ALLOWED_IMPORTS and root not in ALLOWED_IMPORTS:
                    problems.append(
                        f"line {node.lineno}: import of {alias.name!r} is not allowed. "
                        f"Allowed: {', '.join(sorted(ALLOWED_IMPORTS))}"
                    )
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            root = module.split(".")[0]
            if module not in ALLOWED_IMPORTS and root not in ALLOWED_IMPORTS:
                problems.append(
                    f"line {node.lineno}: import from {module!r} is not allowed. "
                    f"Allowed: {', '.join(sorted(ALLOWED_IMPORTS))}"
                )
        elif isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
            problems.append(f"line {node.lineno}: use of {node.id!r} is not allowed.")
        elif isinstance(node, ast.Attribute):
            name = node.attr
            if name.startswith("__") and name not in ALLOWED_DUNDERS:
                problems.append(
                    f"line {node.lineno}: access to dunder attribute {name!r} is not allowed."
                )
    return problems


# ---------------------------------------------------------------------------
# Instrument ranges
# ---------------------------------------------------------------------------

# Practical playable ranges as (low, high) MIDI numbers, by GM program. Not
# exhaustive: anything unlisted falls back to a permissive default, because a
# false rejection costs a composer turn and teaches it nothing.
_RANGES: dict[range, tuple[int, int]] = {
    range(0, 8): (21, 108),     # pianos
    range(8, 16): (53, 108),    # chromatic percussion
    range(16, 24): (36, 96),    # organs
    range(24, 32): (40, 88),    # guitars
    range(32, 40): (28, 67),    # basses
    range(40, 48): (36, 96),    # strings
    range(48, 56): (36, 96),    # ensemble
    range(56, 64): (34, 94),    # brass
    range(64, 72): (44, 94),    # reeds
    range(72, 80): (60, 103),   # pipes
    range(80, 88): (24, 108),   # synth lead
    range(88, 96): (24, 108),   # synth pad
    range(96, 128): (21, 108),  # effects and the rest
}

# Slack in semitones. Beyond this a note is not an interpretive choice, it is a
# mistake -- a bass line written an octave too high, say.
RANGE_SLACK = 6


def playable_range(program: int) -> tuple[int, int]:
    for span, bounds in _RANGES.items():
        if program in span:
            return bounds
    return (21, 108)


# ---------------------------------------------------------------------------
# Structural validation
# ---------------------------------------------------------------------------


@dataclass
class ValidationReport:
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    note_count: int = 0
    duration: float = 0.0
    track_count: int = 0

    def feedback(self) -> str:
        lines: list[str] = []
        if self.errors:
            lines.append("REJECTED:")
            lines += [f"  - {e}" for e in self.errors]
        if self.warnings:
            lines.append("Warnings (not blocking, but judges will likely notice):")
            lines += [f"  - {w}" for w in self.warnings]
        if not lines:
            lines.append(
                f"Valid: {self.track_count} tracks, {self.note_count} notes, "
                f"{self.duration:.1f}s."
            )
        return "\n".join(lines)


def validate_score(
    midi_path: Path, sidecar_path: Path | None = None, min_duration: float = 5.0
) -> ValidationReport:
    """Check that a produced MIDI is a well-formed submission."""
    errors: list[str] = []
    warnings: list[str] = []

    try:
        midi = pretty_midi.PrettyMIDI(str(midi_path))
    except Exception as exc:  # pretty_midi raises a variety of parse errors
        return ValidationReport(ok=False, errors=[f"MIDI file will not parse: {exc}"])

    all_notes = [(inst, n) for inst in midi.instruments for n in inst.notes]
    if not all_notes:
        return ValidationReport(ok=False, errors=["MIDI contains no notes."])

    duration = max(n.end for _, n in all_notes)
    note_count = len(all_notes)

    zero_length = sum(1 for _, n in all_notes if n.end - n.start <= 1e-6)
    if zero_length:
        errors.append(f"{zero_length} notes have zero or negative duration.")

    bad_pitch = sum(1 for _, n in all_notes if not 0 <= n.pitch <= 127)
    if bad_pitch:
        errors.append(f"{bad_pitch} notes fall outside MIDI pitch 0-127.")

    if duration < min_duration:
        errors.append(
            f"Piece is only {duration:.1f}s long; minimum is {min_duration:.0f}s."
        )

    # Range checking, per non-drum track.
    for inst in midi.instruments:
        if inst.is_drum or not inst.notes:
            continue
        lo, hi = playable_range(inst.program)
        pitches = [n.pitch for n in inst.notes]
        below = [p for p in pitches if p < lo - RANGE_SLACK]
        above = [p for p in pitches if p > hi + RANGE_SLACK]
        name = inst.name or f"program {inst.program}"
        if below or above:
            errors.append(
                f"track {name!r} (program {inst.program}, playable {lo}-{hi}): "
                f"{len(below) + len(above)} notes far outside range "
                f"(lowest {min(pitches)}, highest {max(pitches)}). "
                "Move the part into the instrument's register."
            )
        else:
            marginal = [p for p in pitches if p < lo or p > hi]
            if marginal:
                warnings.append(
                    f"track {name!r}: {len(marginal)} notes just outside the "
                    f"nominal {lo}-{hi} range."
                )

    # Stuck notes: the same pitch retriggered while already sounding on one track.
    for inst in midi.instruments:
        by_pitch: dict[int, list[tuple[float, float]]] = {}
        for n in inst.notes:
            by_pitch.setdefault(n.pitch, []).append((n.start, n.end))
        overlaps = 0
        for spans in by_pitch.values():
            spans.sort()
            for (s1, e1), (s2, _) in zip(spans, spans[1:]):
                if s2 < e1 - 1e-6:
                    overlaps += 1
        if overlaps:
            warnings.append(
                f"track {inst.name or inst.program!r}: {overlaps} overlapping "
                "same-pitch notes, which will sound like stuck notes."
            )

    if len(midi.instruments) == 1:
        warnings.append(
            "Only one track. Unless a solo instrument is the intent, the "
            "arrangement and orchestration judges will score this low."
        )

    # Structural metadata is optional but its absence hurts the form judge.
    if sidecar_path and Path(sidecar_path).exists():
        try:
            structure = json.loads(Path(sidecar_path).read_text())
            if not structure.get("sections"):
                warnings.append(
                    "No sections declared. Call s.mark_section(...) so form is "
                    "explicit rather than something the judges have to guess."
                )
        except (OSError, json.JSONDecodeError):
            warnings.append("Structural sidecar will not parse.")

    return ValidationReport(
        ok=not errors,
        errors=errors,
        warnings=warnings,
        note_count=note_count,
        duration=duration,
        track_count=len(midi.instruments),
    )


# ---------------------------------------------------------------------------
# Originality
# ---------------------------------------------------------------------------


def _interval_ngrams(midi_path: Path, n: int = 8) -> set[tuple[int, ...]]:
    """Melodic interval n-grams across every non-drum track.

    Intervals rather than absolute pitches, so transposing a lifted melody does
    not disguise it.
    """
    midi = pretty_midi.PrettyMIDI(str(midi_path))
    grams: set[tuple[int, ...]] = set()
    for inst in midi.instruments:
        if inst.is_drum:
            continue
        notes = sorted(inst.notes, key=lambda x: (x.start, x.pitch))
        # Monophonic reduction: highest pitch at each distinct onset.
        top: list[int] = []
        for note in notes:
            if top and abs(note.start - notes[notes.index(note) - 1].start) < 1e-6:
                top[-1] = max(top[-1], note.pitch)
            else:
                top.append(note.pitch)
        intervals = [b - a for a, b in zip(top, top[1:])]
        for i in range(len(intervals) - n + 1):
            grams.add(tuple(intervals[i : i + n]))
    return grams


@dataclass
class OriginalityReport:
    ok: bool
    overlap_fraction: float
    shared_ngrams: int
    candidate_ngrams: int
    detail: str = ""


def check_originality(
    candidate_midi: Path,
    reference_midis: list[Path],
    n: int = 8,
    threshold: float = 0.12,
) -> OriginalityReport:
    """Reject a candidate that reproduces long melodic spans from a reference.

    The reference is used to calibrate judges and to derive structural criteria,
    never to reward similarity, so this gate is what makes that distinction
    enforceable rather than aspirational.
    """
    candidate = _interval_ngrams(candidate_midi, n=n)
    if not candidate:
        return OriginalityReport(
            ok=True,
            overlap_fraction=0.0,
            shared_ngrams=0,
            candidate_ngrams=0,
            detail="No melodic material to compare.",
        )

    reference: set[tuple[int, ...]] = set()
    for path in reference_midis:
        try:
            reference |= _interval_ngrams(path, n=n)
        except Exception:
            continue

    if not reference:
        return OriginalityReport(
            ok=True,
            overlap_fraction=0.0,
            shared_ngrams=0,
            candidate_ngrams=len(candidate),
            detail="No reference material available to compare against.",
        )

    shared = candidate & reference
    fraction = len(shared) / len(candidate)
    ok = fraction <= threshold
    detail = (
        f"{len(shared)}/{len(candidate)} melodic {n}-gram windows "
        f"({fraction:.1%}) also appear in the reference; threshold {threshold:.0%}."
    )
    if not ok:
        detail += (
            " Rejected: this reproduces the reference's melodic material rather "
            "than meeting its structural criteria with original material."
        )
    return OriginalityReport(
        ok=ok,
        overlap_fraction=fraction,
        shared_ngrams=len(shared),
        candidate_ngrams=len(candidate),
        detail=detail,
    )


# ---------------------------------------------------------------------------
# Combined gate
# ---------------------------------------------------------------------------


@dataclass
class GateResult:
    ok: bool
    validation: ValidationReport
    originality: OriginalityReport | None = None

    def feedback(self) -> str:
        parts = [self.validation.feedback()]
        if self.originality and not self.originality.ok:
            parts.append(f"ORIGINALITY REJECTED: {self.originality.detail}")
        return "\n\n".join(parts)


def gate(
    midi_path: Path,
    sidecar_path: Path | None = None,
    reference_midis: list[Path] | None = None,
) -> GateResult:
    """Run every deterministic check a candidate must pass to reach the judges."""
    validation = validate_score(midi_path, sidecar_path)
    originality = None
    if validation.ok and reference_midis:
        originality = check_originality(midi_path, reference_midis)
    ok = validation.ok and (originality is None or originality.ok)
    return GateResult(ok=ok, validation=validation, originality=originality)
