#!/usr/bin/env python3
"""Does the judge panel actually discriminate?

This is the gate the whole system rests on. Everything downstream (the Elo trend,
the coach's rules, the claim that agents improve) is built on the assumption that
these rubrics can tell good music from bad. If they cannot, then a rising score
across rounds means nothing, and it is far better to find that out here than
after reading three rounds of noise as progress.

The test is deliberately blunt: score a competent hand-written clip and a
deliberately terrible one, blind, through the full nine-dimension panel. The
competent one should win on every dimension, and by a clear margin on the
structural ones. A panel that cannot separate these two is not going to separate
three agent submissions from each other.

    python scripts/calibration_check.py

This used to accept ``--reference`` and drop a commercial recording into the
blind pool as a third, higher anchor. That went with the rest of the reference
machinery, and it was not the loss it sounds like: a transcription of a
six-minute song is not a 16-bar loop, so it lost on prompt adherence and loop
usability however good the panel was, and reading that as "the judges are broken"
was the wrong conclusion. Competent versus bad is the comparison that actually
tests the rubrics.
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
from houseband import criteria as criteria_mod  # noqa: E402
from houseband import render, score_text  # noqa: E402
from houseband.events import EventLog  # noqa: E402
from houseband.judges import run_panel  # noqa: E402
from houseband.types import (  # noqa: E402
    DIMENSION_TITLES,
    DIMENSIONS,
    Brief,
    Candidate,
)

# The dimensions where the gap should be unmistakable. A bad clip can accidentally
# score adequately on harmony (a I-IV-V loop is not *wrong*) but it cannot
# accidentally groove, loop cleanly, or sit in sensible registers.
STRUCTURAL = (
    "rhythm_groove",
    "loop_usability",
    "melody",
    "harmony_voice_leading",
    "orchestration_register",
)

# Minimum mean gap on the structural dimensions for the panel to be trusted.
REQUIRED_STRUCTURAL_GAP = 2.0

# These are keys into the live dimension list, so a rename has to break here
# loudly rather than degrade into a gate that silently measures fewer things.
if not set(STRUCTURAL) <= set(DIMENSIONS):
    raise SystemExit(
        "calibration_check.STRUCTURAL names dimensions that no longer exist: "
        + ", ".join(sorted(set(STRUCTURAL) - set(DIMENSIONS)))
    )


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
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

    print(f"judging {len(candidates)} candidates on {len(DIMENSIONS)} dimensions...\n")
    # The same brief and criteria a real run builds, so the gate tests the rubrics
    # under the conditions they actually operate in.
    profile = cfg.profile_for()
    brief = Brief(
        prompt="A loopable rock clip built on a guitar riff, with drums and bass "
        "locked to it and space left for a vocal.",
        genre="rock",
        target_length=f"{profile.bars} bars, {profile.approx_seconds}",
        structure_notes="One continuous idea that loops cleanly. No intro, no outro.",
    )
    criteria_text = criteria_mod.for_brief(brief, profile)

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
                cell += f" (+/-{scored.spread})"
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

    ok = not losses and mean_structural >= REQUIRED_STRUCTURAL_GAP
    print()
    if ok:
        print("GATE PASSED. The panel separates competent from bad decisively.")
    else:
        print("GATE FAILED.")
        if losses:
            print(
                "  The bad clip matched or beat the competent one on: "
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
