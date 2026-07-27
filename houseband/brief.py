"""Turn a free-text song idea into a structured brief.

A single cheap call, but worth making rather than passing raw text through. The
prompt-adherence judge needs something specific to check against, and "epic long
rock song" only becomes checkable once it has been read as a target length, a
tempo range, and an instrumentation list. Doing that once, up front, also means
all three composers and the judge are working from the same reading of the
request rather than each inventing their own.
"""

from __future__ import annotations

from houseband import config as cfg
from houseband.events import EventLog, Usage
from houseband.types import Brief

SYSTEM = """You read a user's song request and restate it as a structured brief.

Be faithful, not creative. Your job is to make explicit what the request already
implies, not to add artistic decisions the user did not ask for.

Guidance per field:
- genre: the genre or style, as specifically as the request supports.
- mood: the emotional character.
- tempo_hint: a BPM range, inferred from the genre and mood if not stated.
- instrumentation: the instruments implied. Infer sensibly from genre when the
  user does not say (a rock request implies drums, bass, guitars).
- target_length: a duration in minutes. "Epic" or "long-form" means 6 or more
  minutes; a request with no length cue means 3 to 4 minutes.
- structure_notes: any structural requirement stated or strongly implied, such as
  a building arrangement, a quiet opening, or a specific section order.

Leave a field empty only when the request genuinely gives you nothing to work
with and inference would be invention."""


def build(
    prompt: str,
    client=None,
    config: cfg.Config | None = None,
    log: EventLog | None = None,
) -> Brief:
    """Structure a user prompt. Falls back to a bare brief if the call fails.

    A failure here should not sink a run: the raw prompt alone is still a usable
    brief, just a less checkable one.
    """
    config = config or cfg.load()
    if client is None:
        import anthropic

        client = anthropic.Anthropic()

    try:
        response = client.messages.parse(
            model=config.model,
            max_tokens=cfg.JUDGE_MAX_TOKENS,
            system=SYSTEM,
            messages=[{"role": "user", "content": prompt}],
            output_format=Brief,
        )
        brief = response.parsed_output
        if brief is None:
            raise ValueError("structured output was empty")
        # The model does not own the prompt field; the user's words go through
        # verbatim so nothing downstream is reading a paraphrase.
        brief.prompt = prompt
        if log:
            log.emit(
                "brief.finished",
                f"Brief: {brief.genre or 'unspecified genre'}, "
                f"{brief.target_length or 'unspecified length'}",
                usage=Usage.from_response(response),
                brief=brief.model_dump(),
            )
        return brief
    except Exception as exc:
        if log:
            log.warn(f"Could not structure the brief ({exc}); using the raw prompt.")
        return Brief(prompt=prompt)
