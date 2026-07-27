#!/usr/bin/env python3
"""Summarise a finished run: did the agents actually get better?

Reads a run's event log and per-round verdicts and prints the trend. The point is
to make the central claim checkable rather than assumed, so it deliberately
reports the things that would show the claim is *false* alongside the ones that
would support it:

* Elo and weighted score per team per round, so a flat line is visible as flat.
* The held-out dimension's trend separately. Agents optimise against whatever the
  coach tells them about, so the dimension the coach never saw is the closest
  thing here to an uncontaminated read.
* Judge noise, as the observed spread on sampled dimensions. A score gain smaller
  than the noise floor is not a gain.
* Where each round's winning audio is, because the honest final test is listening
  to round 1 against round N.

    python scripts/report_run.py                 # most recent run
    python scripts/report_run.py <run_id>
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
from houseband.events import read_events  # noqa: E402
from houseband.types import (  # noqa: E402
    DIMENSION_TITLES,
    DIMENSIONS,
    CandidateVerdict,
)


def _latest_run(runs_dir: Path) -> Path | None:
    candidates = [p for p in runs_dir.iterdir() if p.is_dir() and (p / "events.jsonl").exists()]
    if not candidates:
        return None
    return max(candidates, key=lambda p: (p / "events.jsonl").stat().st_mtime)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_id", nargs="?", default=None)
    args = parser.parse_args(argv)

    config = cfg.load()
    run_dir = config.runs_dir / args.run_id if args.run_id else _latest_run(config.runs_dir)
    if run_dir is None or not run_dir.exists():
        print("No run found. Start one with 'python -m houseband.loop --prompt ...'.")
        return 2

    events = read_events(run_dir / "events.jsonl")
    if not events:
        print(f"{run_dir.name} has no events.")
        return 2

    meta = {}
    if (run_dir / "meta.json").exists():
        meta = json.loads((run_dir / "meta.json").read_text())

    held_out = meta.get("held_out_dimension", "")
    print(f"run       {run_dir.name}")
    print(f"prompt    {meta.get('prompt', '(unknown)')[:100]}")
    print(f"model     {meta.get('model', '?')}")
    print(f"teams     {', '.join(meta.get('teams', []))}")
    print(f"held out  {held_out or '(none)'}  <- not shown to the coach")

    terminal = [e for e in events if e.kind in {"run.finished", "run.failed", "run.budget_exceeded"}]
    status = terminal[-1].kind if terminal else "still running or interrupted"
    print(f"status    {status}")
    if terminal and terminal[-1].kind != "run.finished":
        print(f"          {terminal[-1].message[:200]}")

    # -- per-round tables --------------------------------------------------
    rounds = sorted(
        int(p.name.removeprefix("round"))
        for p in run_dir.glob("round*")
        if p.is_dir() and (p / "verdicts.json").exists()
    )
    if not rounds:
        print("\nNo completed rounds with verdicts.")
        return 1

    by_round: dict[int, dict] = {}
    for round_no in rounds:
        by_round[round_no] = json.loads(
            (run_dir / f"round{round_no}" / "verdicts.json").read_text()
        )

    teams = sorted(
        {
            team
            for data in by_round.values()
            for team in data["id_to_team"].values()
        }
    )

    print("\n" + "=" * 76)
    print("WEIGHTED SCORE BY ROUND  (1-10)")
    print("=" * 76)
    header = "team".ljust(16) + "".join(f"{'round ' + str(r):>12}" for r in rounds)
    header += f"{'change':>12}"
    print(header)
    print("-" * len(header))

    for team in teams:
        row = team.ljust(16)
        values: list[float] = []
        for round_no in rounds:
            data = by_round[round_no]
            candidate_id = next(
                (cid for cid, t in data["id_to_team"].items() if t == team), None
            )
            verdict = data["verdicts"].get(candidate_id) if candidate_id else None
            if verdict is None:
                row += f"{'-':>12}"
                continue
            # Rehydrated rather than re-averaged here. This used to walk the
            # weight table itself, which meant two implementations of the run's
            # headline number that could disagree after any weighting change --
            # and a report that quietly contradicts the pipeline is worse than
            # no report.
            score = CandidateVerdict.model_validate(verdict).weighted_total
            values.append(score)
            row += f"{score:>12.2f}"
        if len(values) >= 2:
            delta = values[-1] - values[0]
            row += f"{delta:>+12.2f}"
        else:
            row += f"{'-':>12}"
        print(row)

    # -- elo ---------------------------------------------------------------
    elo_events = [e for e in events if e.kind == "elo.updated" and e.data.get("ratings")]
    if elo_events:
        print("\n" + "=" * 76)
        # Elo is zero-sum with nothing pinned, so it ranks within a round and
        # says little across them. The weighted score above is the absolute
        # measure, because those rubrics are anchored to written descriptors.
        print("ELO BY ROUND  (within-round ranking; read progress from the score above)")
        print("=" * 76)
        all_names = sorted({n for e in elo_events for n in e.data["ratings"]})
        header = "team".ljust(16) + "".join(
            f"{'round ' + str(e.round):>12}" for e in elo_events
        )
        print(header)
        print("-" * len(header))
        for name in all_names:
            row = name.ljust(16)
            for event in elo_events:
                value = event.data["ratings"].get(name)
                row += f"{value:>12.0f}" if value is not None else f"{'-':>12}"
            print(row)

    # -- held-out dimension ------------------------------------------------
    if held_out:
        print("\n" + "=" * 76)
        print(f"HELD-OUT DIMENSION: {DIMENSION_TITLES.get(held_out, held_out)}")
        print("The coach never saw findings from this dimension, so it is the least")
        print("contaminated signal available. If the coached dimensions rise and this")
        print("one does not, the agents are learning the rubric rather than the music.")
        print("=" * 76)
        header = "team".ljust(16) + "".join(f"{'round ' + str(r):>12}" for r in rounds)
        print(header)
        print("-" * len(header))
        for team in teams:
            row = team.ljust(16)
            for round_no in rounds:
                data = by_round[round_no]
                candidate_id = next(
                    (cid for cid, t in data["id_to_team"].items() if t == team), None
                )
                verdict = data["verdicts"].get(candidate_id) if candidate_id else None
                score = None
                if verdict:
                    score = next(
                        (
                            d["score"]
                            for d in verdict["dimensions"]
                            if d["dimension"] == held_out
                        ),
                        None,
                    )
                row += f"{score:>12}" if score is not None else f"{'-':>12}"
            print(row)

    # -- judge noise -------------------------------------------------------
    spreads: list[int] = []
    for data in by_round.values():
        for verdict in data["verdicts"].values():
            for dimension in verdict["dimensions"]:
                samples = dimension.get("samples") or []
                if len(samples) > 1:
                    spreads.append(max(samples) - min(samples))
    if spreads:
        print("\n" + "=" * 76)
        print("JUDGE NOISE")
        print("=" * 76)
        print(f"sampled dimensions:  {len(spreads)}")
        print(f"mean spread:         {statistics.mean(spreads):.2f} points")
        print(f"max spread:          {max(spreads)} points")
        print(
            "\nAny score change smaller than the mean spread is inside the judge's own\n"
            "noise and should not be read as improvement."
        )

    # -- coach -------------------------------------------------------------
    rules = [e for e in events if e.kind == "coach.rule_written" and not e.data.get("deprecated")]
    dropped = [e for e in events if e.kind == "coach.rule_written" and e.data.get("deprecated")]
    staged = [e for e in events if e.kind == "coach.library_staged"]
    print("\n" + "=" * 76)
    print(f"COACH  ({len(rules)} rules written, {len(dropped)} deprecated, "
          f"{len(staged)} library functions staged)")
    print("=" * 76)
    for event in rules:
        print(f"  r{event.round} [{event.team}/{event.data.get('role', '?')}] {event.message}")
        if event.data.get("because"):
            print(f"       because: {event.data['because'][:100]}")
    for event in staged:
        print(f"\n  STAGED r{event.round} by {event.team}: {event.data.get('name')}")
        print(f"       {event.message[:160]}")
        print("       Review and approve it in the UI, or with:")
        print(f"       cat {run_dir}/staged/{event.data.get('name')}.json")

    # -- calibration warnings ----------------------------------------------
    warnings = [e for e in events if e.kind == "warning" and "CALIBRATION" in e.message]
    if warnings:
        print("\n" + "=" * 76)
        print("CALIBRATION WARNINGS  (read these before trusting anything above)")
        print("=" * 76)
        for event in warnings:
            print(f"  r{event.round}: {event.message[:400]}")

    # -- listening -------------------------------------------------------
    print("\n" + "=" * 76)
    print("THE ACTUAL TEST")
    print("=" * 76)
    print("Numbers are the cheap part. Listen to the first and last round back to")
    print("back and decide whether the improvement is audible.\n")
    for round_no in rounds:
        artifacts = run_dir / f"round{round_no}" / "artifacts"
        audio = sorted(artifacts.glob("*.oga")) + sorted(artifacts.glob("*.wav"))
        if audio:
            data = by_round[round_no]
            for path in audio:
                team = data["id_to_team"].get(path.stem, "?")
                print(f"  round {round_no}  {team:<16} {path}")

    usage = [e for e in events if e.usage]
    if usage:
        out = sum(e.usage.output_tokens for e in usage)
        inp = sum(e.usage.input_tokens for e in usage)
        cached = sum(e.usage.cache_read_input_tokens for e in usage)
        print(f"\ntokens: {out:,} output, {inp:,} input, {cached:,} cache read "
              f"across {len(usage)} calls")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
