#!/usr/bin/env python3
"""Write a realistic run into ``runs/<id>/`` without calling any model.

The point of an event-sourced pipeline is that the log *is* the interface, so the
server and the UI can be exercised end to end with no credential, no fluidsynth,
and no LLM. This writes through the real :class:`EventLog`, so sequence numbers,
scrubbing, and line framing are the same code the pipeline uses -- a fixture that
bypassed them would prove nothing about the contract.

    python scripts/synthetic_run.py --run-id synthetic --delay 0.2

Run it while the SSE endpoint is attached to the same run id to watch the live
tail; run it first and attach afterwards to watch the replay.
"""

from __future__ import annotations

import argparse
import base64
import json
import random
import shutil
import time
from pathlib import Path

from houseband import config as cfg
from houseband.events import EventLog, Usage
from houseband.types import DIMENSION_TITLES, DIMENSIONS

# A 1x1 PNG, so the artifact endpoint serves something a browser will actually
# decode rather than bytes that only happen to end in .png.
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8z8AAAwAB/AF/6njoAAAAAElFTkSuQmCC"
)

TEAMS = ["carbide", "lumen"]

TOOL_SCRIPT = [
    ("read_playbook", {"role": "songwriter"}, "12 rules loaded, 3 new since round 1."),
    ("run_program", {"path": "program.py", "timeout_s": 30.0}, "out.mid written, 5 tracks, 96 bars."),
    ("score_text", {"midi": "out.mid"}, "Rendered 96 bars of score text (14.2 KB)."),
]

RATIONALES = [
    "The section boundaries land where the harmony implies them, and the bridge earns its place.",
    "Contour is serviceable but the second phrase restates the first with no development.",
    "Voice leading is clean through the turnaround; the parallel fifths at the lift are the only lapse.",
    "Groove is locked but the hat pattern never varies across 96 bars, which flattens the second half.",
]

CLAIMS = [
    ("Bass sits an octave above its playable range for the whole B section.", "bass", "major", "arranger"),
    ("Lead melody repeats bars 8-15 verbatim four times with no variation.", "lead", "moderate", "songwriter"),
    ("Hi-hat velocity is constant at 100, so the groove has no internal dynamic.", "drums", "minor", "rhythm"),
    ("Pad and strings occupy the same register, muddying the mid.", "pad", "moderate", "mix"),
]

REVISIONS = [
    "Drop the bass part one octave from bar 32 and keep the root motion.",
    "Vary the third repeat: raise the third note a whole step and delay the phrase by an eighth.",
    "Accent beats 1 and 3 at velocity 110 and drop offbeats to 70.",
    "Move the pad up a fifth and thin it to two voices under the strings.",
]


def build(run_dir: Path, log: EventLog, rounds: int, delay: float, rng: random.Random) -> None:
    prompt = "a 90-second dub techno cue: sparse, hypnotic, wide dub chords, deep sub bass"

    def beat(multiplier: float = 1.0) -> None:
        if delay:
            time.sleep(delay * multiplier)

    log.emit(
        "run.started",
        f"Run started with 2 composer teams over {rounds} rounds.",
        prompt=prompt,
        teams=TEAMS,
        rounds=rounds,
        model=cfg.load().model,
    )
    beat()

    elo = {"carbide": 1200.0, "lumen": 1200.0}

    for round_index in range(1, rounds + 1):
        log.emit("round.started", f"Round {round_index} of {rounds}.", round=round_index)
        beat()

        log.emit("brief.started", "Structuring the brief.", round=round_index)
        beat(0.5)
        log.emit(
            "brief.finished",
            "Brief: dub techno, hypnotic, 122 BPM, a 16-bar loop.",
            round=round_index,
            usage=Usage(input_tokens=1_820, output_tokens=430, cache_read_input_tokens=1_024),
            genre="dub techno",
            tempo_hint="120-124 BPM",
            instrumentation=["sub bass", "dub chords", "drums", "tape delay pad"],
        )
        beat()

        candidates: list[tuple[str, str, bool]] = []
        for index, team in enumerate(TEAMS, start=1):
            candidate_id = f"c{index}"
            candidates.append((candidate_id, team))
            work_dir = run_dir / f"r{round_index}" / team
            work_dir.mkdir(parents=True, exist_ok=True)

            log.emit(
                "composer.started",
                f"{team} is writing round {round_index}.",
                round=round_index,
                team=team,
                persona="minimal, dub-leaning, allergic to filler",
            )
            beat(0.5)

            log.emit(
                "composer.thinking",
                "Planning form first: 8-bar intro, 32-bar A, 16-bar dub breakdown, 32-bar A'.",
                round=round_index,
                team=team,
                usage=Usage(input_tokens=6_400, output_tokens=2_100, cache_read_input_tokens=18_000),
            )
            beat(0.5)

            for tool, arguments, result in TOOL_SCRIPT:
                call_id = f"toolu_{round_index}{index}{tool[:4]}"
                log.emit(
                    "composer.tool_call",
                    f"{tool}",
                    round=round_index,
                    team=team,
                    id=call_id,
                    tool=tool,
                    input=arguments,
                    usage=Usage(input_tokens=3_100, output_tokens=740, cache_read_input_tokens=22_000),
                )
                beat(0.4)
                # One deliberate failure, because a UI that has only ever
                # rendered the happy path is a UI that has not been tested.
                failed = round_index == 2 and team == "lumen" and tool == "run_program"
                log.emit(
                    "composer.tool_result",
                    result if not failed else "NameError: name 'chord_cycle' is not defined (line 42)",
                    round=round_index,
                    team=team,
                    id=call_id,
                    tool=tool,
                    ok=not failed,
                    result=None if failed else result,
                    error="NameError: name 'chord_cycle' is not defined" if failed else None,
                )
                beat(0.3)

            program = work_dir / "program.py"
            program.write_text(
                "from houseband.house.core import Score, four_on_the_floor\n\n"
                "s = Score(tempo=122)\n"
                "four_on_the_floor(s, bars=96)\n"
                's.write("out.mid")\n',
                encoding="utf-8",
            )
            (work_dir / "out.mid").write_bytes(b"MThd\x00\x00\x00\x06\x00\x01\x00\x01\x01\xe0MTrk\x00\x00\x00\x04\x00\xff/\x00")
            (work_dir / "candidate.png").write_bytes(_PNG)
            (work_dir / "candidate.oga").write_bytes(b"OggS" + b"\x00" * 60)

            if round_index == 2 and team == "lumen":
                log.emit(
                    "gate.rejected",
                    "Program raised before writing out.mid.",
                    round=round_index,
                    team=team,
                    reasons=[
                        "NameError at program.py line 42",
                        "no out.mid produced",
                    ],
                )
                log.emit(
                    "composer.failed",
                    "Three repair attempts exhausted.",
                    round=round_index,
                    team=team,
                    attempts=3,
                    error="NameError: name 'chord_cycle' is not defined",
                )
                beat()
                continue

            log.emit(
                "composer.finished",
                "Program ran and wrote out.mid successfully.",
                round=round_index,
                team=team,
                bars=96,
                tracks=5,
                usage=Usage(input_tokens=9_200, output_tokens=5_600, cache_creation_input_tokens=4_096),
            )
            beat(0.4)

            log.emit(
                "gate.passed",
                "5 tracks, 16 bars, no range violations.",
                round=round_index,
                team=team,
            )
            beat(0.3)

            log.emit(
                "artifact.rendered",
                f"Rendered artifacts for {candidate_id}.",
                round=round_index,
                team=team,
                candidate_id=candidate_id,
                midi=f"r{round_index}/{team}/out.mid",
                piano_roll=f"r{round_index}/{team}/candidate.png",
                audio=f"r{round_index}/{team}/candidate.oga",
                program=f"r{round_index}/{team}/program.py",
            )
            beat()

        log.emit(
            "judge.started",
            f"Judging {len(candidates)} candidates blind on {len(DIMENSIONS)} dimensions.",
            round=round_index,
            candidates=[cid for cid, _ in candidates],
        )
        beat(0.5)

        verdicts: dict[str, dict[str, int]] = {}
        for candidate_id, team in candidates:
            verdicts[candidate_id] = {}
            for dimension in DIMENSIONS:
                base = rng.randint(4, 8)
                # Nudged up after round one, so the fixture shows the improvement
                # the learning loop is supposed to produce.
                base = min(10, max(1, base + (1 if round_index > 1 else 0)))
                sampled = [
                    min(10, max(1, base + rng.choice([-1, 0, 0, 1])))
                    for _ in range(3 if dimension in cfg.MEDIAN_SAMPLED_DIMENSIONS else 1)
                ]
                score = sorted(sampled)[len(sampled) // 2]
                verdicts[candidate_id][dimension] = score

                findings = []
                if score <= 6:
                    claim, track, severity, role = CLAIMS[rng.randrange(len(CLAIMS))]
                    start = rng.choice([0, 8, 16, 32])
                    findings.append(
                        {
                            "claim": claim,
                            "bar_start": start,
                            "bar_end": start + rng.choice([0, 7, 15]),
                            "track": track,
                            "severity": severity,
                            "suggested_revision": REVISIONS[rng.randrange(len(REVISIONS))],
                            "attributed_role": role,
                        }
                    )

                log.emit(
                    "judge.verdict",
                    f"{DIMENSION_TITLES[dimension]}: {score}/10 for {candidate_id}.",
                    round=round_index,
                    team=team,
                    dimension=dimension,
                    candidate_id=candidate_id,
                    score=score,
                    samples=sampled,
                    spread=max(sampled) - min(sampled),
                    rationale=RATIONALES[rng.randrange(len(RATIONALES))],
                    findings=findings,
                    usage=Usage(
                        input_tokens=12_400,
                        output_tokens=1_450,
                        cache_read_input_tokens=31_000,
                    ),
                )
                beat(0.12)

            (run_dir / f"r{round_index}").mkdir(parents=True, exist_ok=True)
            (run_dir / f"r{round_index}" / f"verdict_{candidate_id}.json").write_text(
                json.dumps(
                    {"candidate_id": candidate_id, "team": team, "dimensions": verdicts[candidate_id]},
                    indent=2,
                ),
                encoding="utf-8",
            )

        ids = [cid for cid, _, _ in candidates]
        for left, right in zip(ids, ids[1:]):
            winner = rng.choice(["A", "B", "tie"])
            log.emit(
                "pairwise.verdict",
                f"{left} vs {right}: {winner}.",
                round=round_index,
                a=left,
                b=right,
                winner=winner,
                reason="B holds tension through the breakdown at bars 40-56; A resolves too early.",
                usage=Usage(input_tokens=9_800, output_tokens=620, cache_read_input_tokens=24_000),
            )
            beat(0.2)

        for name in elo:
            elo[name] += rng.uniform(-24, 34)
        log.emit(
            "elo.updated",
            "Ratings updated after this round's pairwise comparisons.",
            round=round_index,
            ratings=[
                {
                    "team": name,
                    "elo": round(rating, 1),
                    "delta": round(rating - 1200.0, 1),
                    "games": round_index * 2,
                }
                for name, rating in sorted(elo.items(), key=lambda item: -item[1])
            ],
        )
        beat()

        log.emit("coach.started", "Distilling this round's findings.", round=round_index)
        beat(0.5)
        log.emit(
            "coach.rule_written",
            "New playbook rule for the arranger.",
            round=round_index,
            role="arranger",
            rule="Every part must stay inside its instrument's playable range for the whole piece; "
                 "check the bass explicitly at each section change.",
            because="Bass sat an octave above range through the whole B section (bars 32-63) in round "
                    f"{round_index}.",
            usage=Usage(input_tokens=15_600, output_tokens=980, cache_read_input_tokens=41_000),
        )
        beat(0.3)
        log.emit(
            "coach.rule_written",
            "New playbook rule for the songwriter.",
            round=round_index,
            role="songwriter",
            rule="No 8-bar phrase may repeat verbatim more than twice; the third statement must vary "
                 "pitch or rhythm.",
            because="Lead melody repeated bars 8-15 four times unchanged.",
        )
        beat(0.3)

        if round_index == 1:
            staged_dir = run_dir / "staged"
            staged_dir.mkdir(parents=True, exist_ok=True)
            staged = {
                "name": "vary_phrase",
                "rationale": "Removes the recurring 'phrase repeated verbatim' finding by making "
                             "variation the default rather than something the composer must remember.",
                "source": (
                    "def vary_phrase(notes, degree=1):\n"
                    '    """Return notes with the last note of every third phrase displaced.\n\n'
                    "    Keeps the contour recognisable while removing verbatim repetition.\n"
                    '    """\n'
                    "    out = []\n"
                    "    for index, note in enumerate(notes):\n"
                    "        if index and index % 24 == 23:\n"
                    "            note = note.transposed(degree)\n"
                    "        out.append(note)\n"
                    "    return out\n"
                ),
                "test_source": (
                    "def test_vary_phrase_displaces_every_third_phrase():\n"
                    "    notes = [Note(60) for _ in range(48)]\n"
                    "    out = vary_phrase(notes, degree=2)\n"
                    "    assert out[23].pitch == 62\n"
                    "    assert out[0].pitch == 60\n"
                ),
            }
            (staged_dir / "vary_phrase.json").write_text(json.dumps(staged, indent=2), encoding="utf-8")
            log.emit(
                "coach.library_staged",
                "Staged vary_phrase for review.",
                round=round_index,
                name="vary_phrase",
                rationale=staged["rationale"],
                path="staged/vary_phrase.json",
            )
            beat(0.3)

        log.emit("coach.finished", "2 rules written, 1 function staged.", round=round_index)
        beat(0.3)

        if round_index == 2:
            log.warn(
                "Judge sample spread of 3 on melody for c1; treating the median as weak evidence.",
                round=round_index,
                dimension="melody",
                spread=3,
            )
            beat(0.2)

        log.emit(
            "round.finished",
            f"Round {round_index} finished. Leader: {max(elo, key=elo.get)}.",
            round=round_index,
            leader=max(elo, key=elo.get),
        )
        beat()

    best = max(elo, key=lambda name: elo[name])
    log.emit(
        "run.finished",
        f"Run finished after {rounds} rounds. Best entrant: {best}.",
        winner=best,
        total_output_tokens=log.output_tokens,
        ratings={name: round(rating, 1) for name, rating in elo.items()},
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", default="synthetic")
    parser.add_argument("--rounds", type=int, default=2)
    parser.add_argument("--delay", type=float, default=0.15, help="Seconds between events.")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--keep", action="store_true", help="Append instead of starting clean.")
    args = parser.parse_args(argv)

    run_dir = cfg.load().runs_dir / args.run_id
    if run_dir.exists() and not args.keep:
        shutil.rmtree(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)

    (run_dir / "request.json").write_text(
        json.dumps(
            {
                "run_id": args.run_id,
                "prompt": "dub techno loop at 122: sparse, hypnotic, wide dub chords, deep sub bass",
                "teams": len(TEAMS),
                "rounds": args.rounds,
                "bars": 16,
                "created": "2026-07-26T12:00:00+00:00",
                "synthetic": True,
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    path = run_dir / "events.jsonl"
    log = EventLog(path, echo=True)
    build(run_dir, log, args.rounds, args.delay, random.Random(args.seed))
    written = sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
    print(f"\n{written} events written to {path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
