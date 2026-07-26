"""The composer agent: writes a program, renders it, fixes what broke, submits.

Deliberately a hand-written tool loop rather than ``client.beta.messages.tool_runner``.
Three reasons, all of which the runner would fight:

* Every tool call has to become an event the moment it happens, because the whole
  point of the UI is watching agents work. The runner executes tools for you,
  which is exactly the seam we need to instrument.
* The round-level token budget has to be enforced *between* turns, and abandoning
  a composer mid-loop is a normal outcome rather than an error.
* This is a public repo, and the runner is a beta API surface. A loop over
  ``messages.stream`` is stable ground.

Streaming is not optional: composer calls run with a large ``max_tokens`` because
Opus 5 thinks by default and thinking counts against the same ceiling, and the
SDK refuses non-streaming requests that large.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from houseband import config as cfg
from houseband import render, score_text, validator
from houseband.events import EventLog, Usage
from houseband.types import Brief, Candidate

PROMPTS_DIR = Path(__file__).parent / "prompts"

# The one tool a composer has. Kept to a single tool on purpose: the agent's job
# is to write music, and a wider tool surface mostly invites detours.
RENDER_TOOL = {
    "name": "render_midi",
    "description": (
        "Run your Python program, write out.mid, and get back what happened: "
        "validation errors, structural warnings, and a summary of the resulting "
        "score. Call this as many times as you need to get the piece right. "
        "The last program that renders successfully is what gets submitted."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "code": {
                "type": "string",
                "description": (
                    "The complete Python program. Must end with s.write(\"out.mid\"). "
                    "Send the whole program every time, not a patch."
                ),
            },
            "intent": {
                "type": "string",
                "description": (
                    "One sentence on what you changed and why, for the run log. "
                    "On your first call, what you are going for."
                ),
            },
        },
        "required": ["code"],
    },
}


# Three teams with genuinely different aesthetics. Diversity is the point: three
# agents with the same taste produce three of the same piece, and the Elo
# separation the whole run exists to show never appears.
PERSONAS: dict[str, str] = {
    "conservatory": (
        "You are a formally trained composer. You think in terms of motivic "
        "development, voice leading, and harmonic function. A theme should return "
        "transformed rather than merely repeated. You care about inner voices and "
        "you avoid parallel fifths and octaves between outer parts unless the idiom "
        "calls for them. Your risk is writing something correct but bloodless, so "
        "make sure the piece has a pulse and a hook, not only good grammar."
    ),
    "crate": (
        "You are a producer with a crate-digger's ear. You think groove first: "
        "pocket, syncopation, the space between hits. You build from a rhythmic "
        "core outward, you like extended and rootless voicings, and you would "
        "rather a part be simple and sit right than be clever. Your risk is "
        "writing eight great bars and looping them, so make sure the piece "
        "actually develops and goes somewhere across its full length."
    ),
    "arena": (
        "You are an arranger who writes for scale. You think in dynamics and "
        "instrumentation tiers: what enters when, what drops out to make the next "
        "entry land, where the roof comes off. You want a hook that a crowd could "
        "sing and a climax that is clearly the climax. Your risk is being loud and "
        "undifferentiated throughout, so protect the quiet sections that make the "
        "big ones work."
    ),
}


@dataclass
class ComposerResult:
    """One composer's output for one round."""

    team: str
    ok: bool
    code: str = ""
    midi_path: Path | None = None
    sidecar_path: Path | None = None
    intent: str = ""
    turns: int = 0
    usage: Usage = field(default_factory=Usage)
    error: str = ""
    render_attempts: int = 0


def _read_prompt(name: str) -> str:
    return (PROMPTS_DIR / name).read_text(encoding="utf-8")


def build_system_prompt(
    team: str,
    brief: Brief,
    criteria: str,
    playbook: str,
    learned_helpers: list[str] | None = None,
) -> list[dict]:
    """Assemble the composer's system prompt as cacheable blocks.

    Ordered stable-first so the cache breakpoint covers the library reference and
    role instructions, which are byte-identical across every team and every
    round. The per-team persona and playbook come after, since those change.
    """
    library = _read_prompt("house_library.md")
    role = _read_prompt("composer_system.md")

    helpers_note = ""
    if learned_helpers:
        helpers_note = (
            "\n\n## Helpers added in earlier rounds\n\n"
            "These exist in `houseband.house` because judges kept flagging the "
            "problem each one solves. Use them.\n\n"
            + "\n".join(f"- `{h}`" for h in learned_helpers)
        )

    return [
        # Stable across the whole run: cache boundary goes at the end of this.
        {
            "type": "text",
            "text": f"{role}\n\n# Library reference\n\n{library}{helpers_note}",
            "cache_control": {"type": "ephemeral"},
        },
        # Varies per team and per round.
        {
            "type": "text",
            "text": (
                f"# Your sensibility\n\n{PERSONAS.get(team, '')}\n\n"
                f"# The brief\n\n{brief.render()}\n\n"
                f"# Structural criteria for this genre\n\n{criteria}\n\n"
                f"# Your playbook\n\n{playbook or '(empty: this is your first round)'}"
            ),
        },
    ]


# How often to surface partial progress from a streaming turn. Composer turns run
# for minutes at xhigh effort, so without this the log is silent for the entire
# time the model is thinking and writing, which is indistinguishable from a hang.
PROGRESS_INTERVAL_S = 3.0


def _relay_progress(stream, log: EventLog, round: int, team: str, turn: int) -> None:
    """Consume the stream, emitting throttled progress as the turn unfolds.

    Streaming was originally adopted only to dodge the SDK's non-streaming
    duration guard, and the incremental visibility it also provides was being
    discarded. Draining the events here is what turns "six minutes of silence"
    into a readable account of what the composer is doing.

    Throttled rather than per-delta on purpose: every event is a line in the log
    and a message to the browser, and a token-by-token feed would swamp both.
    """
    import time

    last_emit = 0.0
    thinking: list[str] = []
    text: list[str] = []
    code_chars = 0
    phase = ""

    def flush(force: bool = False) -> None:
        nonlocal last_emit
        now = time.monotonic()
        if not force and now - last_emit < PROGRESS_INTERVAL_S:
            return
        last_emit = now
        if phase == "thinking" and thinking:
            body = "".join(thinking).strip()
            if body:
                log.emit(
                    "composer.thinking",
                    body[-700:],
                    round=round,
                    team=team,
                    turn=turn,
                    phase="thinking",
                    partial=True,
                )
        elif phase == "code":
            log.emit(
                "composer.thinking",
                f"writing program... {code_chars:,} characters",
                round=round,
                team=team,
                turn=turn,
                phase="writing_code",
                code_chars=code_chars,
                partial=True,
            )
        elif phase == "text" and text:
            body = "".join(text).strip()
            if body:
                log.emit(
                    "composer.thinking",
                    body[-700:],
                    round=round,
                    team=team,
                    turn=turn,
                    phase="writing",
                    partial=True,
                )

    try:
        for event in stream:
            kind = getattr(event, "type", "")
            if kind == "content_block_start":
                block_type = getattr(getattr(event, "content_block", None), "type", "")
                if block_type == "thinking":
                    phase = "thinking"
                elif block_type == "tool_use":
                    phase = "code"
                elif block_type == "text":
                    phase = "text"
                flush(force=True)
            elif kind == "content_block_delta":
                delta = getattr(event, "delta", None)
                delta_type = getattr(delta, "type", "")
                if delta_type == "thinking_delta":
                    thinking.append(getattr(delta, "thinking", "") or "")
                elif delta_type == "text_delta":
                    text.append(getattr(delta, "text", "") or "")
                elif delta_type == "input_json_delta":
                    code_chars += len(getattr(delta, "partial_json", "") or "")
                flush()
            elif kind == "content_block_stop":
                flush(force=True)
                thinking.clear()
                text.clear()
    except Exception:
        # Progress reporting must never be able to fail a turn. The authoritative
        # result comes from get_final_message(), which the caller still awaits.
        pass


def _handle_render(
    code: str,
    workdir: Path,
    config: cfg.Config,
    reference_midis: list[Path],
) -> tuple[str, render.ProgramResult]:
    """Execute a program and turn the outcome into feedback the agent can act on."""
    result = render.execute_program(code, workdir, config=config)
    if not result.ok:
        return result.feedback(), result

    gate = validator.gate(result.midi_path, result.sidecar_path, reference_midis)
    summary = score_text.render(
        result.midi_path, result.sidecar_path, include_notes=False
    )

    if not gate.ok:
        return (
            "Your program ran, but the result was rejected.\n\n"
            f"{gate.feedback()}\n\nScore summary:\n{summary}"
        ), render.ProgramResult(ok=False, error="rejected by gate", stdout=result.stdout)

    parts = ["Rendered and passed validation."]
    if gate.validation.warnings:
        parts.append(gate.validation.feedback())
    parts.append(f"Score summary:\n{summary}")
    parts.append(
        "If this is the piece you want to submit, stop calling tools and give a "
        "one-paragraph description of what you wrote. Otherwise revise and call "
        "render_midi again."
    )
    return "\n\n".join(parts), result


def compose(
    team: str,
    brief: Brief,
    criteria: str,
    playbook: str,
    workdir: Path,
    log: EventLog,
    round: int = 0,
    client=None,
    config: cfg.Config | None = None,
    reference_midis: list[Path] | None = None,
    max_turns: int = 8,
    learned_helpers: list[str] | None = None,
    budget_remaining: int | None = None,
) -> ComposerResult:
    """Run one composer to a submitted piece, or to exhaustion.

    Returns the last program that rendered and passed the gate. Failing to
    produce one is a normal outcome that the round absorbs, not an exception.
    """
    config = config or cfg.load()
    reference_midis = reference_midis or []
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)

    if client is None:
        import anthropic

        client = anthropic.Anthropic()

    system = build_system_prompt(team, brief, criteria, playbook, learned_helpers)
    messages: list[dict] = [
        {
            "role": "user",
            "content": (
                "Write the piece. Call render_midi when you have a complete "
                "program, read the feedback, and revise until you are satisfied. "
                "Then stop calling tools and describe what you wrote."
            ),
        }
    ]

    result = ComposerResult(team=team, ok=False)
    log.emit("composer.started", f"{team} starting", round=round, team=team)

    best: render.ProgramResult | None = None
    best_code = ""

    for turn in range(max_turns):
        result.turns = turn + 1

        if budget_remaining is not None and result.usage.output_tokens >= budget_remaining:
            result.error = "Stopped: round token budget exhausted."
            log.warn(result.error, round=round, team=team)
            break

        try:
            with client.messages.stream(
                model=config.model,
                max_tokens=cfg.COMPOSER_MAX_TOKENS,
                output_config={"effort": cfg.COMPOSER_EFFORT},
                # Summarised reasoning is the point of the live view: the default
                # is "omitted", which streams thinking blocks with empty text and
                # leaves the UI with nothing to show for the longest part of a turn.
                thinking={"type": "adaptive", "display": "summarized"},
                system=system,
                tools=[RENDER_TOOL],
                messages=messages,
            ) as stream:
                _relay_progress(stream, log, round, team, turn + 1)
                message = stream.get_final_message()
        except Exception as exc:  # network, rate limit, refusal, anything
            result.error = f"{type(exc).__name__}: {exc}"
            log.emit(
                "composer.failed",
                f"{team} API call failed: {result.error}",
                round=round,
                team=team,
            )
            break

        turn_usage = Usage.from_response(message)
        result.usage = result.usage + turn_usage

        # Opus 5 can decline a request outright; that arrives as a 200, not an
        # exception, so it has to be checked before reading content.
        if getattr(message, "stop_reason", None) == "refusal":
            result.error = "Model declined the request."
            log.emit("composer.failed", f"{team}: {result.error}", round=round, team=team)
            break

        text_blocks = [b.text for b in message.content if b.type == "text"]
        if text_blocks:
            log.emit(
                "composer.thinking",
                text_blocks[-1][:600],
                round=round,
                team=team,
                usage=turn_usage,
            )

        messages.append({"role": "assistant", "content": message.content})

        tool_uses = [b for b in message.content if b.type == "tool_use"]

        # A truncated turn is not a finished turn. Thinking counts against
        # max_tokens, so a composer that reasons at length can spend the whole
        # budget drafting the program inside its own head and get cut off before
        # it ever emits the tool call. Treating that as "the agent is done" threw
        # away a composer that was working perfectly well, so it is retried with
        # an explicit nudge instead.
        if getattr(message, "stop_reason", None) == "max_tokens" and not tool_uses:
            log.warn(
                f"{team} turn {turn + 1} hit the token ceiling before calling "
                "render_midi; retrying with a shorter-planning nudge.",
                round=round,
                team=team,
            )
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "You ran out of output budget before calling render_midi. "
                        "Do not draft the program in your reasoning: plan briefly, "
                        "then call render_midi with the complete program straight "
                        "away. You can revise it once it has rendered."
                    ),
                }
            )
            continue

        if not tool_uses:
            # No more tools: the agent is done. Its final text is its own account
            # of the piece, which is useful context for the run log.
            result.intent = "\n".join(text_blocks).strip()[:2000]
            break

        tool_results = []
        for block in tool_uses:
            code = (block.input or {}).get("code", "")
            intent = (block.input or {}).get("intent", "")
            result.render_attempts += 1
            log.emit(
                "composer.tool_call",
                intent or "render_midi",
                round=round,
                team=team,
                tool="render_midi",
                attempt=result.render_attempts,
                code_chars=len(code),
            )

            feedback, program_result = _handle_render(
                code, workdir, config, reference_midis
            )
            if program_result.ok:
                best, best_code = program_result, code
                # Keep a copy of each accepted program, so a run is auditable
                # after the fact rather than only in the moment.
                (workdir / f"accepted_turn{turn + 1}.py").write_text(code)

            log.emit(
                "composer.tool_result",
                ("accepted" if program_result.ok else "rejected")
                + f": {feedback.splitlines()[0][:200]}",
                round=round,
                team=team,
                ok=program_result.ok,
                attempt=result.render_attempts,
            )
            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": feedback,
                    "is_error": not program_result.ok,
                }
            )

        messages.append({"role": "user", "content": tool_results})

    if best is not None and best.midi_path and Path(best.midi_path).exists():
        result.ok = True
        result.code = best_code
        result.midi_path = Path(best.midi_path)
        result.sidecar_path = best.sidecar_path
        log.emit(
            "composer.finished",
            f"{team} submitted after {result.turns} turns "
            f"({result.render_attempts} render attempts)",
            round=round,
            team=team,
            usage=result.usage,
        )
    else:
        if not result.error:
            # Report the turns actually taken, not the limit. The earlier version
            # interpolated max_turns and so claimed "8 turns" for a composer that
            # had run exactly one, which sent me looking in the wrong place.
            result.error = (
                f"No program passed validation in {result.turns} turn(s) "
                f"({result.render_attempts} render attempts)."
            )
        log.emit(
            "composer.failed",
            f"{team}: {result.error}",
            round=round,
            team=team,
            usage=result.usage,
        )

    (workdir / "composer_result.json").write_text(
        json.dumps(
            {
                "team": result.team,
                "ok": result.ok,
                "turns": result.turns,
                "render_attempts": result.render_attempts,
                "intent": result.intent,
                "error": result.error,
                "usage": result.usage.model_dump(),
            },
            indent=2,
        )
    )
    return result


def to_candidate(
    result: ComposerResult,
    candidate_id: str,
    round: int,
    out_dir: Path,
    config: cfg.Config | None = None,
) -> Candidate | None:
    """Render artifacts and build the blind candidate the judges will see."""
    if not result.ok or result.midi_path is None:
        return None

    artifacts = render.render_all(
        result.midi_path,
        out_dir,
        result.sidecar_path,
        stem=candidate_id,
        config=config,
        title=f"candidate {candidate_id}",
    )
    return Candidate(
        candidate_id=candidate_id,
        team=result.team,
        midi_path=result.midi_path,
        sidecar_path=result.sidecar_path,
        program_code=result.code,
        score_text=score_text.render(result.midi_path, result.sidecar_path),
        piano_roll=artifacts.piano_roll,
        audio=artifacts.audio,
        round=round,
        notes=result.intent,
    )
