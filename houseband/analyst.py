"""Derive structural criteria from a reference piece.

This is the half of reference-anchoring that changes what composers write. The
other half (dropping the reference into the judged pool blind, to check the
judges) needs no analysis at all.

The output is deliberately **structural facts, never notes**. "Four instrumentation
tiers, a bare opening, climax in the final third, six to nine minutes" is a target
a composer can meet with entirely original material. Handing over the reference's
melody would instead make imitation the winning strategy, which is why composers
see only this file and never the reference score, and why
``validator.check_originality`` rejects submissions that reproduce reference
melodic material anyway.

Run once per genre and cached on disk: the reference does not change, so
re-analysing it every round is pure spend.
"""

from __future__ import annotations

from pathlib import Path

from houseband import config as cfg
from houseband import score_text
from houseband.events import EventLog, Usage

SYSTEM = """You analyse a reference piece and write structural criteria that a
composer could meet with completely original material.

You will receive a score in text form, possibly with a piano-roll image.

Write criteria about SHAPE, never about content. Good criteria describe how many
instrumentation tiers the arrangement builds through, whether there is a passage
reduced to one or two instruments and where, where the climax sits as a fraction
of total length, the overall duration, how tempo behaves, how much contrast there
is between sections, and how repetitive the material is allowed to be.

Never quote or describe a specific melody, riff, chord progression, or lyric. A
composer reading your criteria must not be able to reconstruct the reference's
material from it. If you find yourself writing "the main theme goes...", stop.

Format as markdown with a short intro sentence and then a bulleted list of
concrete, checkable targets. Aim for 10 to 16 bullets. Each bullet should be
something you could look at a different piece and say yes or no to.

End with a section headed "## Deliberately not specified" listing the creative
choices a composer is free to make however they like. This matters: it tells the
composer where its own voice belongs."""


def _image_block(path: Path | None) -> dict | None:
    if not path or not Path(path).exists():
        return None
    import base64

    data = base64.standard_b64encode(Path(path).read_bytes()).decode()
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": data},
    }


def derive_criteria(
    reference_midi: Path,
    genre_hint: str = "",
    client=None,
    config: cfg.Config | None = None,
    log: EventLog | None = None,
    cache_path: Path | None = None,
    piano_roll: Path | None = None,
) -> str:
    """Return structural criteria for the reference, using a cached file if present."""
    config = config or cfg.load()
    reference_midi = Path(reference_midi)

    if cache_path and Path(cache_path).exists():
        if log:
            log.emit(
                "analyst.finished",
                f"Reusing cached criteria for {reference_midi.name}",
                cached=True,
            )
        return Path(cache_path).read_text(encoding="utf-8")

    if log:
        log.emit("analyst.started", f"Analysing {reference_midi.name}")

    if client is None:
        import anthropic

        client = anthropic.Anthropic()

    try:
        text = score_text.render(reference_midi, include_notes=True, max_note_bars=120)
    except Exception as exc:
        message = f"Could not read the reference ({exc}); proceeding without criteria."
        if log:
            log.warn(message)
        return _fallback_criteria(genre_hint)

    content: list[dict] = []
    if (image := _image_block(piano_roll)) is not None:
        content.append(image)
    content.append(
        {
            "type": "text",
            "text": (
                f"Genre context: {genre_hint or 'unspecified'}\n\n"
                f"Reference score:\n\n{text}"
            ),
        }
    )

    try:
        response = client.messages.create(
            model=config.model,
            max_tokens=cfg.JUDGE_MAX_TOKENS,
            system=SYSTEM,
            messages=[{"role": "user", "content": content}],
        )
        if getattr(response, "stop_reason", None) == "refusal":
            raise RuntimeError("model declined to analyse the reference")
        criteria = "".join(b.text for b in response.content if b.type == "text").strip()
        if not criteria:
            raise RuntimeError("empty analysis")
    except Exception as exc:
        if log:
            log.warn(f"Reference analysis failed ({exc}); using generic criteria.")
        return _fallback_criteria(genre_hint)

    if cache_path:
        Path(cache_path).parent.mkdir(parents=True, exist_ok=True)
        Path(cache_path).write_text(criteria, encoding="utf-8")

    if log:
        log.emit(
            "analyst.finished",
            f"Derived {len(criteria.splitlines())} lines of criteria",
            usage=Usage.from_response(response),
        )
    return criteria


def _fallback_criteria(genre_hint: str) -> str:
    """Generic structural targets, for when no reference is available.

    Not as good as real derived criteria, but far better than nothing: these are
    the failure modes that show up without any structural pressure at all.
    """
    return f"""# Structural criteria{f" ({genre_hint})" if genre_hint else ""}

No reference piece was available, so these are general targets for long-form
music rather than criteria derived from a specific recording.

- Build the arrangement through at least three distinct instrumentation tiers.
- Open with fewer instruments than the piece ends with.
- Include at least one passage reduced to one or two instruments, positioned so
  the following entry has impact.
- Place the loudest and densest passage in the final third.
- Give each section material that differs in more than dynamics: change the
  harmony, the register, the rhythmic subdivision, or the melodic content.
- No more than half the sounding bars should be exact repeats of an earlier bar.
- Vary velocity within and between sections rather than holding one value.
- Use the full register: do not confine every part to a single octave.
- Give the piece a clear ending rather than stopping mid-loop.

## Deliberately not specified

Key, tempo, chord progressions, melodic material, exact instrumentation, and the
number and naming of sections are all yours to choose.
"""
