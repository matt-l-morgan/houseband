"""The event log: the contract between the pipeline and everything watching it.

Every stage of a run appends a typed JSON line to ``runs/<id>/events.jsonl``. The
pipeline never talks to the UI directly; the UI is a pure reader of this file.
That buys three things:

* the CLI works headless, and the log is the complete record of a run
* the live visualisation is almost free, and any past run replays identically
* a failed run is diagnosable after the fact instead of only in the moment

Two invariants this module enforces rather than merely documents:

* **Nothing key-shaped is ever written.** Users run this with their own
  credential, so every payload passes through :func:`scrub` on the way out. See
  ``tests/test_events.py``.
* **Every LLM call reports its token usage.** Users are spending their own money,
  so a running cost readout is a trust feature, and the per-round budget guard
  needs the same numbers to halt a runaway round.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Literal

from pydantic import BaseModel, Field

SCHEMA_VERSION = 1


# ---------------------------------------------------------------------------
# Credential scrubbing
# ---------------------------------------------------------------------------

# Anthropic keys and OAuth tokens have recognisable prefixes. The generic
# high-entropy pattern is a backstop for anything else that looks secret.
_SECRET_PATTERNS = (
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"sk-[A-Za-z0-9_\-]{20,}"),
    re.compile(r"whsec_[A-Za-z0-9_\-]{8,}"),
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bBearer\s+[A-Za-z0-9_\-\.]{20,}", re.IGNORECASE),
)

REDACTED = "[REDACTED]"

# Field names whose values are dropped wholesale regardless of shape.
_SENSITIVE_KEYS = {
    "api_key",
    "apikey",
    "anthropic_api_key",
    "auth_token",
    "authorization",
    "token",
    "secret",
    "password",
    "credential",
}


def scrub(value: Any) -> Any:
    """Recursively remove anything that looks like a credential.

    Belt and braces: the pipeline is not supposed to put a key in an event in the
    first place, but "not supposed to" is not a guarantee, and a leaked key in a
    log file that users may well paste into a bug report is not a recoverable
    mistake.
    """
    if isinstance(value, str):
        out = value
        for pattern in _SECRET_PATTERNS:
            out = pattern.sub(REDACTED, out)
        return out
    if isinstance(value, dict):
        return {
            key: (REDACTED if str(key).lower() in _SENSITIVE_KEYS else scrub(item))
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [scrub(item) for item in value]
    return value


# ---------------------------------------------------------------------------
# Event model
# ---------------------------------------------------------------------------

EventKind = Literal[
    # run lifecycle
    "run.started",
    "run.finished",
    "run.failed",
    "run.budget_exceeded",
    # per-round
    "round.started",
    "round.finished",
    # brief and criteria, once per run
    "brief.finished",
    "criteria.derived",
    # composers
    "composer.started",
    "composer.thinking",
    "composer.tool_call",
    "composer.tool_result",
    "composer.finished",
    "composer.failed",
    # deterministic gate
    "gate.passed",
    "gate.rejected",
    # artifacts
    "artifact.rendered",
    # judges
    "judge.started",
    "judge.verdict",
    "judge.failed",
    "pairwise.verdict",
    "elo.updated",
    # coach
    "coach.started",
    "coach.rule_written",
    "coach.library_staged",
    "coach.finished",
    # the producer
    #
    # The only kind not written by the pipeline. It arrives from the web server
    # once a human has actually listened, which can be minutes or days after the
    # run ended, so anything inferring a run's state from its final event has to
    # skip past this one. It outranks every judge verdict above it: a rubric score
    # is a proxy for usefulness, and a producer keeping or binning a stem is the
    # thing itself.
    "producer.feedback",
    # diagnostics
    "warning",
    "usage",
]


class Usage(BaseModel):
    """Token usage from one LLM response."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0

    @classmethod
    def from_response(cls, response: Any) -> "Usage":
        """Build from an Anthropic response, tolerating missing fields."""
        usage = getattr(response, "usage", None)
        if usage is None:
            return cls()
        return cls(
            input_tokens=getattr(usage, "input_tokens", 0) or 0,
            output_tokens=getattr(usage, "output_tokens", 0) or 0,
            cache_creation_input_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
            cache_read_input_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
        )

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_creation_input_tokens=self.cache_creation_input_tokens
            + other.cache_creation_input_tokens,
            cache_read_input_tokens=self.cache_read_input_tokens
            + other.cache_read_input_tokens,
        )


class Event(BaseModel):
    """One line in the log."""

    schema_version: int = SCHEMA_VERSION
    seq: int = 0
    ts: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    kind: EventKind
    round: int | None = None
    team: str | None = None
    dimension: str | None = None
    message: str = ""
    data: dict[str, Any] = Field(default_factory=dict)
    usage: Usage | None = None


# ---------------------------------------------------------------------------
# Writer
# ---------------------------------------------------------------------------


class EventLog:
    """Append-only writer, safe across threads.

    Composers run concurrently, so ordering matters: ``seq`` is assigned under
    the same lock as the write, which makes the file's line order authoritative
    and lets a reader dedupe on ``seq`` after a reconnect.
    """

    def __init__(self, path: Path, echo: bool = False):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._seq = 0
        self._echo = echo
        self.total_usage = Usage()

    def emit(
        self,
        kind: EventKind,
        message: str = "",
        *,
        round: int | None = None,
        team: str | None = None,
        dimension: str | None = None,
        usage: Usage | None = None,
        **data: Any,
    ) -> Event:
        event = Event(
            kind=kind,
            message=message,
            round=round,
            team=team,
            dimension=dimension,
            usage=usage,
            data=scrub(data),
        )
        event.message = scrub(event.message)

        with self._lock:
            self._seq += 1
            event.seq = self._seq
            if usage is not None:
                self.total_usage = self.total_usage + usage
            line = event.model_dump_json(exclude_none=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        if self._echo:
            label = f"[{event.kind}]"
            suffix = f" ({event.team})" if event.team else ""
            print(f"{label:<24}{suffix} {event.message}", flush=True)
        return event

    # -- convenience -------------------------------------------------------

    def warn(self, message: str, **data: Any) -> Event:
        return self.emit("warning", message, **data)

    @property
    def output_tokens(self) -> int:
        return self.total_usage.output_tokens


# ---------------------------------------------------------------------------
# Reader
# ---------------------------------------------------------------------------


def read_events(path: Path) -> list[Event]:
    """Read a whole log. Malformed trailing lines are skipped, not fatal.

    A run killed mid-write can leave a partial final line; a reader that dies on
    that would make every interrupted run unreadable.
    """
    events: list[Event] = []
    if not Path(path).exists():
        return events
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(Event.model_validate_json(line))
        except Exception:
            continue
    return events


def tail_events(
    path: Path,
    from_seq: int = 0,
    poll_interval: float = 0.25,
    stop_after_idle: float | None = None,
) -> Iterator[Event]:
    """Yield events as they are appended, starting after ``from_seq``.

    Used by the server's SSE endpoint. Polling a file rather than holding an
    in-process queue is what lets the UI attach to a run it did not start, and
    reattach after a page reload, without the pipeline knowing anything about it.
    """
    path = Path(path)
    offset = 0
    last_seq = from_seq
    idle_since = time.monotonic()

    while True:
        if path.exists():
            with path.open("r", encoding="utf-8") as handle:
                handle.seek(offset)
                chunk = handle.read()
                # Only advance past complete lines, so a partially flushed final
                # line is re-read rather than dropped.
                if chunk:
                    complete, _, remainder = chunk.rpartition("\n")
                    if complete:
                        offset += len(complete.encode("utf-8")) + 1
                        for line in complete.splitlines():
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                event = Event.model_validate_json(line)
                            except Exception:
                                continue
                            if event.seq > last_seq:
                                last_seq = event.seq
                                idle_since = time.monotonic()
                                yield event
                                if event.kind in {
                                    "run.finished",
                                    "run.failed",
                                    "run.budget_exceeded",
                                }:
                                    return
        if stop_after_idle is not None and time.monotonic() - idle_since > stop_after_idle:
            return
        time.sleep(poll_interval)


def load_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default
