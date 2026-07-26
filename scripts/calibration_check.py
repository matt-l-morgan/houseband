#!/usr/bin/env python3
"""Does the judge panel actually discriminate?

This is the gate the whole system rests on. Everything downstream (the Elo
trend, the coach's rules, the claim that agents improve) is built on the
assumption that these rubrics can tell good music from bad. If they cannot, then
a rising score across rounds means nothing, and it is far better to find that out
here than after reading three rounds of noise as progress.

The test is deliberately blunt: score a competent hand-written piece and a
deliberately terrible one, blind, through the full eight-dimension panel. The
competent piece should win on every dimension, and by a clear margin on the
structural ones. A panel that cannot separate these two is not going to separate
three agent submissions from each other.

    python scripts/calibration_check.py                  # good vs bad
    python scripts/calibration_check.py --reference X.mid # include a real piece

Adding a real reference from ``references/`` makes the check much stronger: a real
recording should beat both. That is the version worth trusting.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from houseband import config as cfg  # noqa: E402
from houseband import render, score_text  # noqa: E402
from houseband.events import EventLog  # noqa: E402
from houseband.judges import run_panel  # noqa: E402
from houseband.types import (  # noqa: E402
    DIMENSION_TITLES,
    DIMENSIONS,
    Brief,
    Candidate,
)

# The dimensions where the gap should be unmistakable. A bad piece can accidentally
# score adequately on, say, harmony (a I-IV-V loop is not *wrong*), but it cannot
# accidentally have good form.
STRUCTURAL = ("form_arrangement", "melody", "rhythm_groove", "orchestration_register")

# Minimum mean gap on the structural dimensions for the panel to be trusted.
REQUIRED_STRUCTURAL_GAP = 2.0


def _build(path: Path, candidate_id: str, out_dir: Path, config: cfg.Config) -> Candidate:
    """Render a program and package it as a blind candidate."""
    code = path.read_text()
    work = out_dir / candidate_id
    result = render.execute_program(code, work, config=config)
    if not result.ok:
        raise SystemExit(f"{path.name} failed to render:\n{result.feedback()}")
    artifacts = render.render_all(
        result.midi_path, work, result.sidecar_path, stem=candidate_id, config=config
    )
    return Candidate(
        candidate_id=candidate_id,
        team=path.stem,
        midi_path=result.midi_path,
        sidecar_path=result.sidecar_path,
        score_text=score_text.render(result.midi_path, result.sidecar_path),
        piano_roll=artifacts.piano_roll,
        audio=artifacts.audio,
    )


def _from_midi(path: Path, candidate_id: str, out_dir: Path, config: cfg.Config) -> Candidate:
    artifacts = render.render_all(
        path, out_dir / candidate_id, stem=candidate_id, config=config
    )
    return Candidate(
        candidate_id=candidate_id,
        team="reference",
        midi_path=path,
        score_text=score_text.render(path),
        piano_roll=artifacts.piano_roll,
        audio=artifacts.audio,
        is_reference=True,
    )


def _reference_health(midi_path: Path) -> list[str]:
    """Flag transcription artifacts that make a reference unfair to score.

    Community MIDI is usually transcribed for pitch and rhythm and not for
    performance, so dynamics and stereo placement are frequently absent. A judge
    reading that file is right to mark down production and groove, but the piece
    it was transcribed from does not deserve it. Naming the cause here is the
    difference between "the judges are broken" and "this anchor cannot be scored
    on dynamics".
    """
    import pretty_midi

    notes: list[int] = []
    pans: list[int] = []
    try:
        midi = pretty_midi.PrettyMIDI(str(midi_path))
    except Exception as exc:
        return [f"could not re-read the reference to check it: {exc}"]

    for inst in midi.instruments:
        notes += [n.velocity for n in inst.notes]
        pans += [cc.value for cc in inst.control_changes if cc.number == 10]

    problems: list[str] = []
    if notes:
        spread = max(notes) - min(notes)
        if spread <= 16:
            problems.append(
                f"velocities span only {spread} of 127 (min {min(notes)}, max {max(notes)}): "
                "this transcription encodes no dynamics, so production and rhythm "
                "scores for it are not meaningful"
            )
    if not pans or len(set(pans)) <= 1:
        problems.append(
            "every track is panned to the same position, so orchestration and "
            "production have no stereo image to reward"
        )
    if not problems:
        problems.append(
            "no obvious transcription artifact found, so this may be a genuine "
            "rubric problem rather than a bad anchor"
        )
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--reference",
        default=None,
        help="Filename in references/ to include. Strongly recommended.",
    )
    parser.add_argument("--out", default="runs/calibration")
    args = parser.parse_args(argv)

    config = cfg.load()
    if cfg.credential_source() is None:
        print(
            "No Anthropic credential found.\n"
            "Run 'ant auth login', or export ANTHROPIC_API_KEY.",
            file=sys.stderr,
        )
        return 2
    print(f"credential: {cfg.credential_source()}")
    print(f"model:      {config.model}\n")

    out_dir = REPO_ROOT / args.out
    out_dir.mkdir(parents=True, exist_ok=True)
    log = EventLog(out_dir / "events.jsonl", echo=False)

    # Ids give nothing away: the panel cannot tell which is which.
    print("rendering candidates...")
    good = _build(REPO_ROOT / "examples" / "good_program.py", "cA", out_dir, config)
    bad = _build(REPO_ROOT / "examples" / "bad_program.py", "cB", out_dir, config)
    candidates = [good, bad]
    labels = {"cA": "competent", "cB": "deliberately bad"}

    if args.reference:
        path = config.references_dir / args.reference
        if not path.exists():
            print(f"reference not found: {path}", file=sys.stderr)
            return 2
        reference = _from_midi(path, "cC", out_dir, config)
        candidates.append(reference)
        labels["cC"] = f"real ({args.reference})"

    print(f"judging {len(candidates)} candidates on {len(DIMENSIONS)} dimensions...\n")
    brief = Brief(
        prompt="A long-form rock piece that builds through several instrumentation "
        "tiers, with a quiet passage before a final climax.",
        genre="rock",
        target_length="4 to 7 minutes",
        structure_notes="Building arrangement, bare opening, climax in the final third.",
    )
    criteria_text = (
        "- Build through at least three distinct instrumentation tiers.\n"
        "- Include a passage reduced to one or two instruments.\n"
        "- Place the densest passage in the final third.\n"
        "- No more than half the sounding bars should be exact repeats.\n"
        "- Vary velocity within and between sections.\n"
    )

    verdicts = run_panel(candidates, brief, criteria_text, config=config, log=log)

    # Persisted because the event log records only a finding *count*, and when
    # this gate fails the first question is always "what did the judge actually
    # say". Without this the answer requires re-running the panel.
    (out_dir / "verdicts.json").write_text(
        json.dumps(
            {
                "model": config.model,
                "labels": labels,
                "verdicts": {k: v.model_dump() for k, v in verdicts.items()},
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    # -- report ------------------------------------------------------------
    width = max(len(DIMENSION_TITLES[d]) for d in DIMENSIONS) + 2
    header = "dimension".ljust(width) + "".join(
        f"{labels[c.candidate_id][:16]:>18}" for c in candidates
    )
    print(header)
    print("-" * len(header))

    gaps: list[float] = []
    structural_gaps: list[float] = []
    for dimension in DIMENSIONS:
        row = DIMENSION_TITLES[dimension].ljust(width)
        scores: dict[str, int | None] = {}
        for candidate in candidates:
            verdict = verdicts.get(candidate.candidate_id)
            scored = verdict.by_dimension().get(dimension) if verdict else None
            scores[candidate.candidate_id] = scored.score if scored else None
            cell = "-" if scored is None else str(scored.score)
            if scored and scored.spread:
                cell += f" (±{scored.spread})"
            row += f"{cell:>18}"
        print(row)

        if scores.get("cA") is not None and scores.get("cB") is not None:
            gap = scores["cA"] - scores["cB"]
            gaps.append(gap)
            if dimension in STRUCTURAL:
                structural_gaps.append(gap)

    print("-" * len(header))
    totals = "weighted total".ljust(width) + "".join(
        f"{verdicts[c.candidate_id].weighted_total:>18.2f}"
        if c.candidate_id in verdicts
        else f"{'-':>18}"
        for c in candidates
    )
    print(totals)

    # -- verdict -----------------------------------------------------------
    print()
    losses = [d for d, g in zip(DIMENSIONS, gaps) if g <= 0]
    mean_structural = statistics.mean(structural_gaps) if structural_gaps else 0.0
    mean_all = statistics.mean(gaps) if gaps else 0.0

    print(f"mean gap (all dimensions):        {mean_all:+.2f}")
    print(f"mean gap (structural dimensions): {mean_structural:+.2f}  "
          f"(need >= {REQUIRED_STRUCTURAL_GAP})")

    if args.reference and "cC" in verdicts:
        ref_total = verdicts["cC"].weighted_total
        good_total = verdicts["cA"].weighted_total
        print(f"\nreal reference vs competent:      {ref_total:.2f} vs {good_total:.2f}")
        if ref_total <= good_total:
            print(
                "  A hand-written test piece matched or beat a real recording.\n"
                "  Before blaming the rubrics, check the reference itself: the two\n"
                "  usual causes are a transcription that encoded no dynamics, and a\n"
                "  brief the reference was never written to satisfy."
            )
            for line in _reference_health(candidates[-1].midi_path):
                print(f"    - {line}")

    ok = not losses and mean_structural >= REQUIRED_STRUCTURAL_GAP
    print()
    if ok:
        print("GATE PASSED. The panel separates competent from bad decisively.")
    else:
        print("GATE FAILED.")
        if losses:
            print(
                "  The bad piece matched or beat the competent one on: "
                + ", ".join(DIMENSION_TITLES[d] for d in losses)
            )
        if mean_structural < REQUIRED_STRUCTURAL_GAP:
            print(
                f"  Structural gap is only {mean_structural:+.2f}, which is too "
                "narrow to build a learning loop on."
            )
        print("\n  Fix the rubrics in houseband/judges/rubrics/ before running a "
              "full loop. A learning loop over a panel this noisy trains on nothing.")

    print(f"\nartifacts: {out_dir}")
    print(f"tokens:    {log.total_usage.output_tokens:,} output, "
          f"{log.total_usage.input_tokens:,} input, "
          f"{log.total_usage.cache_read_input_tokens:,} cache read")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
