"""Turn a composer's program into artifacts: MIDI, audio, and a piano roll.

Three separable steps, so a failure is always attributable:

1. :func:`execute_program` runs model-written Python in a subprocess and expects
   it to produce ``out.mid`` plus its sidecar.
2. :func:`render_audio` shells out to FluidSynth for something listenable.
3. :func:`render_piano_roll` draws the score for the judges' eyes.

Step 3 matters more than it looks. Repetition, register collision, dynamic
flatness and "eight bars looped sixteen times" are all immediately obvious in a
picture and genuinely laborious to spot in a note list, so the piano roll is a
first-class judge input rather than a debugging nicety.

On executing model-written code: the mitigations here are real but they are
mitigations, not a sandbox. See ``docs/security.md``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from houseband import config as cfg
from houseband.timing import TempoMap

# The program is expected to end with s.write("out.mid").
PROGRAM_FILENAME = "program.py"
MIDI_FILENAME = "out.mid"


@dataclass
class ProgramResult:
    """Outcome of executing one composer program."""

    ok: bool
    midi_path: Path | None = None
    sidecar_path: Path | None = None
    stdout: str = ""
    stderr: str = ""
    error: str | None = None
    timed_out: bool = False

    def feedback(self) -> str:
        """A short, actionable message for the composer agent to read.

        Deliberately terse and specific: this text is the whole basis on which
        the agent decides how to fix its program, so noise here becomes wasted
        turns.
        """
        if self.ok:
            return "Program ran and wrote out.mid successfully."
        parts: list[str] = []
        if self.timed_out:
            parts.append(
                f"Program exceeded the {cfg.load().program_timeout_s:.0f}s time limit. "
                "Check for an unbounded loop."
            )
        if self.error:
            parts.append(self.error)
        if self.stderr.strip():
            # The traceback tail is the useful part; the harness frames are not.
            tail = "\n".join(self.stderr.strip().splitlines()[-12:])
            parts.append(f"stderr:\n{tail}")
        if self.stdout.strip():
            parts.append(f"stdout:\n{self.stdout.strip()[-800:]}")
        return "\n\n".join(parts) or "Program failed with no output."


def execute_program(
    code: str,
    workdir: Path,
    config: cfg.Config | None = None,
    validate: bool = True,
) -> ProgramResult:
    """Write ``code`` to ``workdir`` and run it, expecting ``out.mid``.

    ``validate`` runs the static import allowlist first. It defaults on; the
    only reason to disable it is testing the executor itself.
    """
    config = config or cfg.load()
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)

    program_path = workdir / PROGRAM_FILENAME
    program_path.write_text(code)

    if validate:
        from houseband.validator import check_imports

        problems = check_imports(code)
        if problems:
            return ProgramResult(
                ok=False,
                error="Program rejected before execution:\n"
                + "\n".join(f"  - {p}" for p in problems),
            )

    # A deliberately minimal environment. No credential reaches the child, so a
    # program that goes looking for one finds nothing.
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "PYTHONPATH": str(cfg.REPO_ROOT),
        "PYTHONDONTWRITEBYTECODE": "1",
        "MPLBACKEND": "Agg",
    }

    try:
        proc = subprocess.run(
            [sys.executable, PROGRAM_FILENAME],
            cwd=workdir,
            env=env,
            capture_output=True,
            text=True,
            timeout=config.program_timeout_s,
        )
    except subprocess.TimeoutExpired as exc:
        return ProgramResult(
            ok=False,
            timed_out=True,
            stdout=exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or ""),
            stderr=exc.stderr.decode() if isinstance(exc.stderr, bytes) else (exc.stderr or ""),
        )

    midi_path = workdir / MIDI_FILENAME
    sidecar_path = midi_path.with_suffix(".score.json")

    if proc.returncode != 0:
        return ProgramResult(
            ok=False,
            stdout=proc.stdout,
            stderr=proc.stderr,
            error=f"Program exited with code {proc.returncode}.",
        )
    if not midi_path.exists():
        return ProgramResult(
            ok=False,
            stdout=proc.stdout,
            stderr=proc.stderr,
            error=(
                f"Program ran cleanly but did not write {MIDI_FILENAME}. "
                'End the program with s.write("out.mid").'
            ),
        )

    return ProgramResult(
        ok=True,
        midi_path=midi_path,
        sidecar_path=sidecar_path if sidecar_path.exists() else None,
        stdout=proc.stdout,
        stderr=proc.stderr,
    )


# ---------------------------------------------------------------------------
# Audio
# ---------------------------------------------------------------------------

# Preference order. A six-minute stereo WAV is ~60MB, which is a poor thing to
# stream to a browser, so use a compressed container when the local FluidSynth
# build's libsndfile supports one. Detected once, at runtime.
_AUDIO_FORMATS = (("oga", ".oga"), ("flac", ".flac"), ("wav", ".wav"))


def _supported_audio_formats(fluidsynth: str) -> set[str]:
    try:
        out = subprocess.run(
            [fluidsynth, "-T", "help"], capture_output=True, text=True, timeout=15
        )
    except (OSError, subprocess.SubprocessError):
        return {"wav"}
    text = (out.stdout + out.stderr).lower()
    found = {name for name, _ in _AUDIO_FORMATS if name in text}
    return found or {"wav"}


def render_audio(
    midi_path: Path,
    out_stem: Path,
    config: cfg.Config | None = None,
    gain: float = 0.9,
    sample_rate: int = 44100,
) -> Path:
    """Render ``midi_path`` to audio beside ``out_stem``. Returns the file written.

    The extension is chosen from what this FluidSynth build can write, so the
    caller must use the returned path rather than assuming one.
    """
    config = config or cfg.load()
    config.require_render_deps()
    assert config.fluidsynth and config.soundfont

    supported = _supported_audio_formats(config.fluidsynth)
    fmt, suffix = next(
        ((f, s) for f, s in _AUDIO_FORMATS if f in supported), ("wav", ".wav")
    )
    out_path = Path(out_stem).with_suffix(suffix)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        config.fluidsynth,
        "-ni",              # no MIDI input, no interactive shell
        "-q",               # quiet
        "-g", str(gain),
        "-r", str(sample_rate),
        "-T", fmt,
        "-F", str(out_path),
        str(config.soundfont),
        str(midi_path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if proc.returncode != 0 or not out_path.exists():
        raise RuntimeError(
            f"fluidsynth failed (exit {proc.returncode}).\n"
            f"stdout: {proc.stdout[-500:]}\nstderr: {proc.stderr[-500:]}"
        )
    return out_path


# ---------------------------------------------------------------------------
# Piano roll
# ---------------------------------------------------------------------------


def _load_structure(sidecar_path: Path | None) -> dict:
    if sidecar_path and Path(sidecar_path).exists():
        try:
            return json.loads(Path(sidecar_path).read_text())
        except (OSError, json.JSONDecodeError):
            pass
    return {}


def render_piano_roll(
    midi_path: Path,
    out_path: Path,
    sidecar_path: Path | None = None,
    title: str | None = None,
) -> Path:
    """Draw a piano roll: bars across, pitch up, one colour per track.

    Velocity maps to opacity so dynamic flatness is visible at a glance, and
    labelled section boundaries make form legible without counting bars. When no
    sidecar is available (a MIDI we did not author) the x-axis falls back to the
    file's own initial tempo.
    """
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.patches as mpatches
    import matplotlib.pyplot as plt
    import pretty_midi

    midi = pretty_midi.PrettyMIDI(str(midi_path))
    structure = _load_structure(sidecar_path)
    tempo = (
        TempoMap.from_structure(structure)
        if structure.get("tempo_map")
        else TempoMap.from_midi(midi)
    )

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    fig, ax = plt.subplots(figsize=(16, 7), dpi=110)
    cmap = plt.get_cmap("tab10")
    handles: list[mpatches.Patch] = []
    max_bar = 0.0
    pitches_seen: list[int] = []

    for i, inst in enumerate(midi.instruments):
        colour = cmap(i % 10)
        label = inst.name.strip() or (
            "drums" if inst.is_drum else f"program {inst.program}"
        )
        handles.append(mpatches.Patch(color=colour, label=label))
        for note in inst.notes:
            start_bar = tempo.seconds_to_bar(note.start)
            end_bar = tempo.seconds_to_bar(note.end)
            width = max(end_bar - start_bar, 0.02)
            max_bar = max(max_bar, end_bar)
            pitches_seen.append(note.pitch)
            ax.add_patch(
                mpatches.Rectangle(
                    (start_bar, note.pitch - 0.42),
                    width,
                    0.84,
                    facecolor=colour,
                    edgecolor="none",
                    # Velocity as opacity: a wall of identical alpha is exactly
                    # the "mechanical, no dynamics" failure judges should catch.
                    alpha=0.25 + 0.75 * (note.velocity / 127.0),
                )
            )

    if not pitches_seen:
        ax.text(0.5, 0.5, "no notes", ha="center", va="center", transform=ax.transAxes)
        lo, hi, max_bar = 48, 72, 1.0
    else:
        lo, hi = min(pitches_seen), max(pitches_seen)

    for section in structure.get("sections", []):
        start = section["start_bar"]
        ax.axvline(start, color="0.35", linestyle="--", linewidth=1.0, zorder=3)
        ax.text(
            start + 0.15,
            hi + 2.6,
            section["name"],
            fontsize=9,
            color="0.2",
            va="bottom",
            rotation=0,
        )

    ax.set_xlim(0, max(max_bar, 1.0) * 1.01)
    ax.set_ylim(lo - 3, hi + 5)
    ax.set_xlabel("bar")
    ax.set_ylabel("MIDI pitch")

    total_bars = int(max(max_bar, 1.0)) + 1
    step = max(1, round(total_bars / 32 / 4) * 4) if total_bars > 40 else 4
    ax.set_xticks(range(0, total_bars + 1, step))
    ax.grid(axis="x", color="0.9", linewidth=0.6, zorder=0)
    ax.grid(axis="y", color="0.95", linewidth=0.4, zorder=0)

    heading = title or midi_path.stem
    if structure:
        heading += (
            f"   key={structure.get('key', '?')}"
            f"  {structure.get('time_sig', [4, 4])[0]}/{structure.get('time_sig', [4, 4])[1]}"
            f"  bpm={tempo.bpm_at(0):.0f}"
            f"  {structure.get('duration', 0):.0f}s"
        )
    ax.set_title(heading, fontsize=11, loc="left")

    if handles:
        ax.legend(handles=handles, loc="upper right", fontsize=8, framealpha=0.9, ncol=2)

    fig.tight_layout()
    fig.savefig(out_path)
    plt.close(fig)
    return out_path


# ---------------------------------------------------------------------------
# Convenience
# ---------------------------------------------------------------------------


@dataclass
class Artifacts:
    """Everything one candidate produces."""

    midi: Path
    sidecar: Path | None
    audio: Path | None
    piano_roll: Path
    audio_error: str | None = None


def render_all(
    midi_path: Path,
    out_dir: Path,
    sidecar_path: Path | None = None,
    stem: str = "candidate",
    config: cfg.Config | None = None,
    title: str | None = None,
) -> Artifacts:
    """Produce audio and a piano roll for an existing MIDI file.

    Audio failure is non-fatal and recorded: the judges read the score and the
    piano roll, so a missing soundfont should not be able to sink a whole round.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    if sidecar_path is None:
        candidate = Path(midi_path).with_suffix(".score.json")
        sidecar_path = candidate if candidate.exists() else None

    roll = render_piano_roll(
        midi_path, out_dir / f"{stem}.png", sidecar_path=sidecar_path, title=title
    )

    audio: Path | None = None
    audio_error: str | None = None
    try:
        audio = render_audio(midi_path, out_dir / stem, config=config)
    except (RuntimeError, subprocess.SubprocessError, OSError) as exc:
        audio_error = str(exc)

    return Artifacts(
        midi=Path(midi_path),
        sidecar=sidecar_path,
        audio=audio,
        piano_roll=roll,
        audio_error=audio_error,
    )
