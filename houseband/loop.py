"""Round orchestration: compose, gate, judge, rank, coach, repeat.

Deterministic control flow on purpose. The agents make the creative decisions;
the sequencing, the blinding, the budget, and the bookkeeping are all plain code,
because those are the parts that have to be right every time rather than
usually.

Run headless:

    python -m houseband.loop --prompt "epic long-form rock" --rounds 3

Everything observable about a run goes through the event log, so the web UI is a
reader rather than a participant.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from houseband import analyst, brief as brief_mod, coach as coach_mod, composer
from houseband import config as cfg
from houseband import render, score_text, validator
from houseband.events import EventLog, Usage
from houseband.types import (
    DIMENSIONS,
    DIMENSIONS_FOR_MODE,
    ProducerFeedback,
    Brief,
    Candidate,
    CandidateVerdict,
    Finding,
    TeamState,
)


def new_run_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{random.randint(0x1000, 0xffff):04x}"


def _held_out_dimension(run_id: str, mode: str = "longform") -> str:
    """Pick one dimension the coach never sees, deterministically per run.

    Agents optimise against whatever the coach tells them about, so holding a
    dimension back gives an unpolluted read on whether the piece is actually
    getting better or only getting better at the rubric. Rotating which one
    across runs stops any single dimension being permanently invisible to
    learning.
    """
    dims = DIMENSIONS_FOR_MODE.get(mode, DIMENSIONS)
    return dims[sum(ord(c) for c in run_id) % len(dims)]


def _relative_to(path: Path | None, root: Path) -> str | None:
    """Path relative to the run directory, which is what the files endpoint takes.

    Emitting absolute paths would leak the host filesystem into the event log and
    force the UI to know about it to build a link.
    """
    if not path:
        return None
    try:
        return str(Path(path).resolve().relative_to(Path(root).resolve()))
    except (ValueError, OSError):
        return str(path)


def _producer_feedback_for(run_dir: Path, team: str, round_no: int) -> list[ProducerFeedback]:
    """Read producer feedback the UI has recorded for this team and round.

    Read from disk at coaching time rather than passed in, because the producer
    rates takes while the round is still running and the pipeline has no channel
    back from the browser. The file is append-only, so a late rating simply lands
    in the next round's coaching instead of being lost.

    Feedback on earlier rounds is included: a producer who deleted the pad twice
    running is making a stronger point than one who did it once.
    """
    path = Path(run_dir) / "feedback.jsonl"
    if not path.exists():
        return []
    out: list[ProducerFeedback] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = ProducerFeedback.model_validate_json(line)
        except Exception:
            continue
        if item.team and item.team != team:
            continue
        if item.round and item.round > round_no:
            continue
        out.append(item)
    return out


def _resolve_reference(name: str | None, config: cfg.Config) -> Path | None:
    if not name:
        candidates = sorted(config.references_dir.glob("*.mid")) + sorted(
            config.references_dir.glob("*.midi")
        )
        return candidates[0] if candidates else None
    path = config.references_dir / name
    return path if path.exists() else None


def _build_reference_candidate(
    reference_midi: Path, out_dir: Path, config: cfg.Config, log: EventLog
) -> Candidate | None:
    """Prepare the reference as an ordinary-looking candidate.

    It has to be indistinguishable from an agent submission in the judged pool,
    otherwise the calibration check measures nothing. Same artifacts, same
    opaque id, no marker the judge can see.
    """
    try:
        artifacts = render.render_all(
            reference_midi, out_dir, stem="reference", config=config, title="candidate"
        )
        return Candidate(
            candidate_id="ref",
            team="reference",
            midi_path=reference_midi,
            piano_roll=artifacts.piano_roll,
            audio=artifacts.audio,
            score_text=score_text.render(reference_midi),
            is_reference=True,
        )
    except Exception as exc:
        log.warn(f"Could not prepare the reference candidate: {exc}")
        return None


def _strip_held_out(verdict: CandidateVerdict, held_out: str) -> CandidateVerdict:
    """A copy of the verdict with the held-out dimension removed."""
    return CandidateVerdict(
        candidate_id=verdict.candidate_id,
        team=verdict.team,
        is_reference=verdict.is_reference,
        dimensions=[d for d in verdict.dimensions if d.dimension != held_out],
    )


def run(
    prompt: str,
    teams: int = 3,
    rounds: int = 3,
    reference: str | None = None,
    run_id: str | None = None,
    config: cfg.Config | None = None,
    client=None,
    echo: bool = True,
    max_turns: int | None = None,
    model: str | None = None,
    budget: int | None = None,
    mode: str = "starter",
    bars: int | None = None,
) -> Path:
    """Execute a full run. Returns the run directory.

    ``model`` overrides the configured default for this run only. Users spend
    their own credential, so the choice between a cheaper and a stronger model
    belongs to whoever is paying rather than to a constant in the source.
    """
    config = config or cfg.load()
    if model:
        config.model = model
    if budget:
        config.round_token_budget = budget
    profile = cfg.profile_for(mode, bars)
    run_id = run_id or new_run_id()
    run_dir = config.runs_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    log = EventLog(run_dir / "events.jsonl", echo=echo)

    # Imported here rather than at module scope so that a missing or broken judge
    # module surfaces as a run.failed event in the UI instead of an import error
    # nobody sees.
    from houseband.judges import check_calibration, run_panel, tournament
    from houseband.judges.elo import DEFAULT_RATING, REFERENCE_RATING

    held_out = _held_out_dimension(run_id, mode)
    team_names = list(composer.PERSONAS)[:teams] or ["conservatory"]

    log.emit(
        "run.started",
        f"{prompt[:120]}",
        run_id=run_id,
        teams=team_names,
        rounds=rounds,
        model=config.model,
        held_out_dimension=held_out,
        prompt=prompt,
        mode=mode,
        bars=profile.bars,
    )

    if cfg.credential_source() is None:
        log.emit(
            "run.failed",
            "No Anthropic credential found. Set ANTHROPIC_API_KEY, run "
            "'ant auth login', or paste a key into the web UI.",
        )
        return run_dir

    if client is None:
        import anthropic

        client = anthropic.Anthropic()

    (run_dir / "meta.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "prompt": prompt,
                "teams": team_names,
                "rounds": rounds,
                "model": config.model,
                "held_out_dimension": held_out,
                "mode": mode,
                "bars": profile.bars,
                "created": datetime.now(timezone.utc).isoformat(),
            },
            indent=2,
        )
    )

    try:
        the_brief = brief_mod.build(prompt, client=client, config=config, log=log)

        # -- reference and criteria, once for the whole run ------------------
        reference_midi = _resolve_reference(reference, config)
        reference_candidate: Candidate | None = None
        criteria = ""
        if reference_midi:
            reference_candidate = _build_reference_candidate(
                reference_midi, run_dir / "reference", config, log
            )
            criteria = analyst.derive_criteria(
                reference_midi,
                genre_hint=the_brief.genre,
                client=client,
                config=config,
                log=log,
                cache_path=config.references_dir / f"{reference_midi.stem}.criteria.md",
                piano_roll=reference_candidate.piano_roll if reference_candidate else None,
            )
        else:
            log.warn(
                "No reference MIDI found in references/. Running without a "
                "calibration anchor and with generic structural criteria."
            )
            criteria = analyst._fallback_criteria(the_brief.genre)
        (run_dir / "criteria.md").write_text(criteria)

        state = {
            name: TeamState(
                name=name,
                persona=composer.PERSONAS[name],
                elo=DEFAULT_RATING,
                playbook_path=config.playbooks_dir / f"{name}.md",
            )
            for name in team_names
        }
        playbooks = {
            name: coach_mod.Playbook(name, config.playbooks_dir) for name in team_names
        }
        ratings: dict[str, float] = {name: DEFAULT_RATING for name in team_names}
        if reference_candidate:
            ratings["reference"] = REFERENCE_RATING
        findings_history: dict[str, list[Finding]] = {name: [] for name in team_names}

        for round_no in range(1, rounds + 1):
            round_dir = run_dir / f"round{round_no}"
            round_dir.mkdir(parents=True, exist_ok=True)
            log.emit("round.started", f"Round {round_no} of {rounds}", round=round_no)

            spent_before = log.output_tokens
            # The per-round allowance, shared between the composers running
            # this round. Previously written as "budget - 0", which quietly meant
            # the guard never accounted for anything already spent.
            budget_left = max(0, config.round_token_budget)

            # -- compose, in parallel ---------------------------------------
            def _compose(name: str) -> composer.ComposerResult:
                result = composer.compose(
                    team=name,
                    brief=the_brief,
                    criteria=criteria,
                    playbook=playbooks[name].render(),
                    workdir=round_dir / name,
                    log=log,
                    round=round_no,
                    client=client,
                    config=config,
                    reference_midis=[reference_midi] if reference_midi else [],
                    max_turns=max_turns,
                    learned_helpers=coach_mod.approved_helpers(),
                    budget_remaining=budget_left // max(1, len(team_names)),
                    mode=mode,
                    profile=profile,
                )
                # Render this team's audio now rather than after the whole round.
                # Composers finish minutes apart, and making the first one wait on
                # the slowest before it is even playable is a bad trade: rendering
                # costs about a second and the point of the live view is to hear
                # what just happened.
                if result.ok and result.midi_path:
                    try:
                        artifacts = render.render_all(
                            result.midi_path,
                            round_dir / name,
                            result.sidecar_path,
                            stem="preview",
                            config=config,
                            title=f"{name} round {round_no}",
                        )
                        # The DAW bundle is the actual deliverable, so build it
                        # here rather than on demand: a producer auditioning takes
                        # should never wait on a zip, and the export also
                        # double-checks the loop is importable.
                        bundle = None
                        export_problems: list[str] = []
                        try:
                            from houseband import export as export_mod

                            result_export = export_mod.export_bundle(
                                result.midi_path,
                                result.sidecar_path,
                                out_dir=round_dir / name / "daw",
                                stem=f"{name}_r{round_no}",
                                expect_bars=profile.bars or None,
                                title=f"{name}, round {round_no}",
                                brief=the_brief.prompt,
                            )
                            bundle = result_export.zip_path
                            export_problems = result_export.problems
                        except Exception as exc:
                            log.warn(
                                f"Could not build a DAW bundle for {name}: {exc}",
                                round=round_no,
                                team=name,
                            )

                        log.emit(
                            "artifact.rendered",
                            f"{name} is ready to play",
                            round=round_no,
                            team=name,
                            candidate_id=f"preview-{name}-r{round_no}",
                            preview=True,
                            audio=_relative_to(artifacts.audio, run_dir),
                            piano_roll=_relative_to(artifacts.piano_roll, run_dir),
                            midi=_relative_to(result.midi_path, run_dir),
                            program=_relative_to(round_dir / name / "program.py", run_dir),
                            daw_bundle=_relative_to(bundle, run_dir),
                            daw_problems=export_problems,
                        )
                    except Exception as exc:
                        log.warn(
                            f"Could not render a preview for {name}: {exc}",
                            round=round_no,
                            team=name,
                        )
                return result

            with ThreadPoolExecutor(max_workers=len(team_names)) as pool:
                results = list(pool.map(_compose, team_names))

            # -- blind the pool ---------------------------------------------
            # Opaque ids, shuffled, so neither the team name nor a stable
            # position can leak identity to a judge.
            successful = [r for r in results if r.ok]
            if not successful:
                log.warn(
                    f"Round {round_no}: no team produced a valid submission.",
                    round=round_no,
                )
                log.emit("round.finished", f"Round {round_no} produced nothing", round=round_no)
                continue

            # Ids carry the round so that per-round artifacts stay distinct and a
            # finished run remains browsable round by round. This costs nothing in
            # blindness: every candidate in a given round shares the prefix, so it
            # says nothing about which team produced which piece.
            order = list(range(len(successful)))
            random.shuffle(order)
            candidates: list[Candidate] = []
            id_to_team: dict[str, str] = {}
            for slot, index in enumerate(order, start=1):
                result = successful[index]
                candidate = composer.to_candidate(
                    result,
                    candidate_id=f"r{round_no}c{slot}",
                    round=round_no,
                    out_dir=round_dir / "artifacts",
                    config=config,
                )
                if candidate is None:
                    continue
                candidates.append(candidate)
                id_to_team[candidate.candidate_id] = result.team

                log.emit(
                    "artifact.rendered",
                    f"{candidate.candidate_id} artifacts ready",
                    round=round_no,
                    team=result.team,
                    candidate_id=candidate.candidate_id,
                    piano_roll=_relative_to(candidate.piano_roll, run_dir),
                    audio=_relative_to(candidate.audio, run_dir),
                    # The MIDI is the actual deliverable: audio is one render of
                    # it, and anyone wanting to open the result in a DAW needs this.
                    midi=_relative_to(candidate.midi_path, run_dir),
                    program=_relative_to(round_dir / result.team / "program.py", run_dir),
                )

                gate = validator.gate(
                    candidate.midi_path,
                    candidate.sidecar_path,
                    [reference_midi] if reference_midi else None,
                )
                log.emit(
                    "gate.passed" if gate.ok else "gate.rejected",
                    gate.feedback()[:400],
                    round=round_no,
                    team=result.team,
                    candidate_id=candidate.candidate_id,
                )

            judged = list(candidates)
            if reference_candidate:
                # A fresh id each round for the same reason: the reference is
                # re-judged alongside that round's candidates and its verdict
                # belongs to that round's record.
                reference_candidate.candidate_id = f"r{round_no}ref"
                judged.append(reference_candidate)
                id_to_team[reference_candidate.candidate_id] = "reference"

            # -- judge ------------------------------------------------------
            verdicts = run_panel(
                judged,
                the_brief,
                criteria,
                client=client,
                config=config,
                log=log,
                round=round_no,
                mode=mode,
            )
            for candidate_id, verdict in verdicts.items():
                verdict.team = id_to_team.get(candidate_id, verdict.team)

            # The cheapest check we have on whether the judges are worth
            # listening to: a real recording sitting in the same blind pool
            # should beat three agents on the structural dimensions. If it does
            # not, the scores driving the learning loop are noise, and it is far
            # better to know that now than to read three rounds of Elo as
            # progress.
            calibration = check_calibration(verdicts)
            if not calibration.ok:
                log.warn(
                    "JUDGE CALIBRATION SUSPECT: " + calibration.summary(),
                    round=round_no,
                    calibration=calibration.model_dump(),
                )

            # -- rank -------------------------------------------------------
            # The tournament works in blind candidate ids, so ratings have to be
            # translated into that space and back out again each round. Carrying
            # ratings by team is what makes the Elo trend across rounds mean
            # anything, since the id a team gets is reshuffled every round.
            initial = {
                candidate_id: ratings.get(id_to_team.get(candidate_id, ""), DEFAULT_RATING)
                for candidate_id in (c.candidate_id for c in judged)
            }
            if reference_candidate:
                initial[reference_candidate.candidate_id] = REFERENCE_RATING

            new_ratings = tournament(
                judged,
                the_brief,
                criteria,
                client=client,
                config=config,
                log=log,
                round=round_no,
                initial=initial,
            )
            for candidate_id, rating in new_ratings.items():
                team = id_to_team.get(candidate_id, candidate_id)
                ratings[team] = rating
                if team in state:
                    state[team].elo = rating
            if mode == "starter" and len(candidates) > 1:
                try:
                    from houseband.judges import diversity

                    varied = diversity.select_varied(
                        candidates,
                        verdicts,
                        k=min(len(candidates), 3),
                    )
                    log.emit(
                        "elo.updated",
                        "Most varied usable takes: "
                        + ", ".join(id_to_team.get(c, c) for c in varied),
                        round=round_no,
                        varied_selection=[id_to_team.get(c, c) for c in varied],
                        ratings=ratings,
                    )
                except Exception as exc:
                    log.warn(f"Diversity selection failed: {exc}", round=round_no)

            log.emit(
                "elo.updated",
                ", ".join(f"{k} {v:.0f}" for k, v in sorted(ratings.items())),
                round=round_no,
                ratings=ratings,
            )

            # -- coach ------------------------------------------------------
            for candidate in candidates:
                team = id_to_team[candidate.candidate_id]
                verdict = verdicts.get(candidate.candidate_id)
                if verdict is None:
                    continue
                playbooks[team].record_round(verdict.weighted_total)
                findings_history[team].extend(verdict.all_findings())

                learned_path = Path(coach_mod.__file__).parent / "house" / "learned.py"
                feedback = _producer_feedback_for(run_dir, team, round_no)
                if feedback:
                    log.emit(
                        "coach.started",
                        f"{team}: coaching with {len(feedback)} piece(s) of producer "
                        "feedback, which outrank the judges",
                        round=round_no,
                        team=team,
                        producer_feedback_count=len(feedback),
                    )
                _, staged = coach_mod.coach_team(
                    team=team,
                    verdict=_strip_held_out(verdict, held_out),
                    round=round_no,
                    playbook=playbooks[team],
                    log=log,
                    prior_findings=findings_history[team][:-len(verdict.all_findings()) or None],
                    learned_source=learned_path.read_text() if learned_path.exists() else "",
                    client=client,
                    config=config,
                    allow_staging=round_no > 1,
                    producer_feedback=feedback,
                )
                for function in staged:
                    coach_mod.stage_function(
                        function, run_dir / "staged", log, round_no, team
                    )

            # -- persist ----------------------------------------------------
            (round_dir / "verdicts.json").write_text(
                json.dumps(
                    {
                        "round": round_no,
                        "held_out_dimension": held_out,
                        "id_to_team": id_to_team,
                        "ratings": ratings,
                        "verdicts": {k: v.model_dump() for k, v in verdicts.items()},
                    },
                    indent=2,
                )
            )
            (round_dir / "composers.json").write_text(
                json.dumps(
                    [
                        {
                            "team": r.team,
                            "ok": r.ok,
                            "turns": r.turns,
                            "render_attempts": r.render_attempts,
                            "error": r.error,
                            "usage": r.usage.model_dump(),
                        }
                        for r in results
                    ],
                    indent=2,
                )
            )

            spent = log.output_tokens - spent_before
            if spent > config.round_token_budget:
                log.warn(
                    f"Round {round_no} used {spent:,} output tokens against a "
                    f"{config.round_token_budget:,} allowance.",
                    round=round_no,
                )
            log.emit(
                "round.finished",
                f"Round {round_no} done, {spent:,} output tokens",
                round=round_no,
                ratings=ratings,
                spent_output_tokens=spent,
            )

            if log.output_tokens > config.round_token_budget * rounds:
                log.emit(
                    "run.budget_exceeded",
                    f"Stopping: {log.output_tokens:,} output tokens exceeds the "
                    f"configured budget.",
                )
                return run_dir

        log.emit(
            "run.finished",
            "Final ratings: "
            + ", ".join(f"{k} {v:.0f}" for k, v in sorted(ratings.items(), key=lambda x: -x[1])),
            ratings=ratings,
            total_usage=log.total_usage.model_dump(),
        )
    except Exception as exc:
        import traceback

        log.emit(
            "run.failed",
            f"{type(exc).__name__}: {exc}",
            traceback=traceback.format_exc()[-4000:],
        )
        raise

    return run_dir


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run an agentic composition loop.")
    parser.add_argument("--prompt", required=True, help="The song idea.")
    parser.add_argument("--teams", type=int, default=3)
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--reference", default=None, help="Filename in references/.")
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--max-turns", type=int, default=None)
    parser.add_argument(
        "--model",
        default=None,
        help=f"Override the model for this run (default {cfg.DEFAULT_MODEL}).",
    )
    parser.add_argument(
        "--budget",
        type=int,
        default=None,
        help="Output-token allowance per round (default "
        f"{cfg.Config().round_token_budget:,}). The run halts past budget x rounds.",
    )
    parser.add_argument(
        "--mode",
        default=cfg.DEFAULT_MODE,
        choices=sorted(cfg.PROFILES),
        help="starter (a loopable ~30s clip for a DAW) or longform (a whole piece).",
    )
    parser.add_argument(
        "--bars", type=int, default=None, help="Override the starter bar count."
    )
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    config = cfg.load()
    if args.model:
        config.model = args.model
    if cfg.credential_source() is None:
        print(
            "No Anthropic credential found.\n"
            "Set ANTHROPIC_API_KEY, or run 'ant auth login'.",
            file=sys.stderr,
        )
        return 2
    try:
        config.require_render_deps()
    except RuntimeError as exc:
        print(f"Warning: {exc}\nContinuing; audio will be skipped.", file=sys.stderr)

    run_dir = run(
        prompt=args.prompt,
        teams=args.teams,
        rounds=args.rounds,
        reference=args.reference,
        run_id=args.run_id,
        config=config,
        echo=not args.quiet,
        max_turns=args.max_turns,
        model=args.model,
        budget=args.budget,
        mode=args.mode,
        bars=args.bars,
    )
    print(f"\nRun complete: {run_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
