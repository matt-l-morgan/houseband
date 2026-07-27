"""The web server: launch runs, then read the log like any other observer.

Two responsibilities, deliberately not three:

* **launch** -- start ``python -m houseband.loop`` as a detached child process
* **observe** -- replay and tail ``runs/<id>/events.jsonl`` over SSE

It never imports or runs the pipeline in-process. That separation is what makes
a browser optional rather than load-bearing: a composer that crashes the
pipeline cannot take the UI down, the server can restart mid-run without losing
a single event, and a page can attach to a run that some other terminal started.
The event log is the only thing the two halves share, and it is append-only, so
"replay from ``from_seq`` then tail" is all a reload costs.

On credentials, three properties this module is responsible for:

* A key submitted through the UI lives in one module-level dict and nowhere
  else. It is never written to disk and never logged.
* No endpoint returns it, not even the one that submitted it. The browser is
  told a *source name* (:func:`cfg.credential_source`), because a name is enough
  to render "configured" and a value would only be a liability.
* The pipeline receives it through the child process environment, which is the
  narrowest channel available that the Anthropic SDK already knows how to read.

On serving artifacts: every path from the browser is resolved and checked
against the run directory before anything is opened. Run ids and staged function
names are pattern-matched rather than sanitised, because an allowlist fails
closed and a blocklist fails whenever someone thinks of a new encoding.

On producer feedback, this module writes one event kind of its own. A rubric
score is a proxy for usefulness; a producer keeping or binning a stem *is*
usefulness, and the pipeline is long dead by the time anyone auditions its
output, so the server is the only thing that can record it. It goes to
``runs/<id>/feedback.jsonl`` first (that file is the durable record) and then
into the event log, so the live view, a replay, and the coach all learn about it
through the one channel they already read.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import re
import secrets
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from importlib.util import find_spec
from pathlib import Path
from typing import Any, Iterator

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, field_validator

from houseband import config as cfg
from houseband.events import Event, read_events, scrub, tail_events
from houseband.types import (
    BAR_CHOICES,
    BARS_DEFAULT,
    DIMENSION_TITLES,
    DIMENSION_WEIGHTS,
    DIMENSIONS,
    ProducerFeedback,
)

WEB_DIR = cfg.REPO_ROOT / "web"

# Kinds after which no further events can arrive. tail_events() returns on these
# too; the SSE endpoint needs the same set so a completed run does not leave a
# reader polling a file that will never grow.
TERMINAL_KINDS = frozenset({"run.finished", "run.failed", "run.budget_exceeded"})

STATUS_FOR_KIND = {
    "run.finished": "finished",
    "run.failed": "failed",
    "run.budget_exceeded": "budget_exceeded",
}

# Comment frames keep proxies and load balancers from treating a thinking
# composer as a dead connection. Well under the usual 30-60s idle timeouts.
HEARTBEAT_SECONDS = 15.0

# Anchored patterns, not sanitisers: anything not matching is rejected outright.
RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
FUNCTION_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
# Ids the pipeline actually mints: "c1", "r2c3", "preview-carbide-r1". No slashes,
# no dots leading anywhere, so a candidate id can be pasted into a filename lookup
# without a second traversal check having to save us.
CANDIDATE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

# The ids the loop used to mint for the reference candidate, before references
# were removed. Anchored so it cannot match a real candidate.
LEGACY_REFERENCE_ID_RE = re.compile(r"^(?:r\d+)?ref$")

# A producer's note is free text, but it lands in an append-only log that the
# coach reads back into a prompt, so it gets a ceiling rather than being trusted
# to be short. Rejecting is better than silently truncating what someone typed.
MAX_NOTE_CHARS = 4000

# Published so the UI's cost readout and this module cannot drift apart. Cache
# writes cost 1.25x input and cache reads 0.1x, per the API's pricing model.
_MODEL_PRICING: dict[str, dict[str, float]] = {
    "claude-opus-5": {"input": 5.0, "output": 25.0},
    "claude-opus-4-8": {"input": 5.0, "output": 25.0},
    "claude-opus-4-7": {"input": 5.0, "output": 25.0},
    "claude-fable-5": {"input": 10.0, "output": 50.0},
    "claude-sonnet-5": {"input": 3.0, "output": 15.0},
    "claude-sonnet-4-6": {"input": 3.0, "output": 15.0},
    "claude-haiku-4-5": {"input": 1.0, "output": 5.0},
}

CONTENT_TYPES = {
    ".oga": "audio/ogg",
    ".ogg": "audio/ogg",
    ".opus": "audio/ogg",
    ".flac": "audio/flac",
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".svg": "image/svg+xml",
    ".mid": "audio/midi",
    ".midi": "audio/midi",
    ".json": "application/json",
    ".jsonl": "application/x-ndjson",
    ".zip": "application/zip",
    # Python and Markdown are served as plain text so a browser renders them
    # instead of prompting a download. These are artifacts you read.
    ".py": "text/plain; charset=utf-8",
    ".md": "text/plain; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".log": "text/plain; charset=utf-8",
}


# ---------------------------------------------------------------------------
# In-memory state
# ---------------------------------------------------------------------------

# The submitted credential, and nothing else. Never persisted; a server restart
# deliberately forgets it, because a key that outlives the process the user
# started is a key they have lost track of.
_CREDENTIAL: dict[str, str] = {}

# run_id -> Popen, for runs this process launched. Not the authority on whether a
# run is alive: see _process_alive, which also adopts a run from the pid recorded
# in its directory, so a restart mid-run neither loses track of a live pipeline
# nor takes away its Cancel button.
_PROCESSES: dict[str, subprocess.Popen] = {}
# Runs we signalled, so the watcher can say "cancelled" rather than reporting a
# deliberate kill as a mysterious negative exit code.
_CANCELLED: set[str] = set()
_PROCESS_LOCK = threading.Lock()

# Serialises the read-max-seq-then-append dance in _append_event.
_WRITE_LOCK = threading.Lock()

# Separate from _WRITE_LOCK so recording feedback never blocks on, or is blocked
# by, an event append: the two files are independent and both are append-only.
_FEEDBACK_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class CredentialIn(BaseModel):
    api_key: str


class RunIn(BaseModel):
    prompt: str
    teams: int = Field(default=3, ge=1, le=8)
    rounds: int = Field(default=3, ge=1, le=20)
    # Omitted means the configured default. Validated against the known-pricing
    # table rather than passed through, so a typo becomes a 400 here instead of a
    # 404 from the API three minutes into a run.
    model: str | None = None
    # Output-token allowance per round. Bounded rather than free-form: the floor
    # stops a budget so small that no round can finish, and the ceiling stops a
    # fat-fingered extra zero from becoming a surprise bill.
    budget: int | None = Field(default=None, ge=20_000, le=20_000_000)
    # Clip length in bars. Restricted to the three lengths the rubrics and the
    # DAW-readiness check were written against, because an arbitrary bar count
    # produces a clip that does not loop cleanly on a 4-bar phrase boundary and
    # the loop-usability judge would be marking down our own arithmetic.
    bars: int | None = None

    @field_validator("bars")
    @classmethod
    def _known_length(cls, value: int | None) -> int | None:
        if value is not None and value not in BAR_CHOICES:
            raise ValueError(
                f"bars must be one of {', '.join(str(b) for b in BAR_CHOICES)}"
            )
        return value


# ---------------------------------------------------------------------------
# Paths and log reading
# ---------------------------------------------------------------------------


def _runs_dir() -> Path:
    path = cfg.load().runs_dir
    path.mkdir(parents=True, exist_ok=True)
    return path.resolve()


def _run_dir(run_id: str, must_exist: bool = True) -> Path:
    """Resolve a run directory, rejecting anything that escapes ``runs/``."""
    if not RUN_ID_RE.match(run_id):
        raise HTTPException(status_code=400, detail="Malformed run id.")
    root = _runs_dir()
    path = (root / run_id).resolve()
    if path != root and root not in path.parents:
        raise HTTPException(status_code=403, detail="Run id escapes the runs directory.")
    if must_exist and not path.is_dir():
        raise HTTPException(status_code=404, detail=f"No such run: {run_id}")
    return path


def _safe_path(run_dir: Path, path: str) -> Path | None:
    """Resolve a run-relative path, or ``None`` if it leaves the run directory.

    ``resolve()`` runs before the containment check so ``..`` and symlinks are
    already collapsed by the time we compare. Returns ``None`` rather than
    raising, because several callers treat "outside the run" and "not there" the
    same way and only one of them is answering an HTTP request.
    """
    text = str(path or "")
    if not text or text.startswith("/"):
        return None
    target = (run_dir / text).resolve()
    if target != run_dir and run_dir not in target.parents:
        return None
    return target


def _tail_lines(path: Path, window: int = 64 * 1024) -> list[str]:
    """Last complete lines of a file, without reading the whole thing.

    Run listings only need the final event, and a long run's log is large enough
    that parsing all of it per request would make the runs page quadratic.
    """
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            handle.seek(max(0, size - window))
            chunk = handle.read()
    except OSError:
        return []
    text = chunk.decode("utf-8", errors="ignore")
    if size > window:
        # The first line is probably truncated mid-character or mid-JSON.
        text = text.partition("\n")[2]
    return [line for line in text.splitlines() if line.strip()]


def _head_lines(path: Path, window: int = 16 * 1024) -> list[str]:
    try:
        with path.open("rb") as handle:
            chunk = handle.read(window)
    except OSError:
        return []
    return [line for line in chunk.decode("utf-8", errors="ignore").splitlines() if line.strip()]


def _parse_last(lines: list[str], kinds: frozenset[str] | None = None) -> Event | None:
    for line in reversed(lines):
        try:
            event = Event.model_validate_json(line)
        except Exception:
            continue
        if kinds is None or event.kind in kinds:
            return event
    return None


def _parse_first(lines: list[str]) -> Event | None:
    for line in lines:
        try:
            return Event.model_validate_json(line)
        except Exception:
            continue
    return None


def _last_event(run_dir: Path) -> Event | None:
    return _parse_last(_tail_lines(run_dir / "events.jsonl"))


def _terminal_event(run_dir: Path, lines: list[str] | None = None) -> Event | None:
    """The event that ended the run, ignoring anything appended after it.

    "The last line" stopped being a safe proxy for "how the run ended" the moment
    the log gained a kind that outlives the pipeline: producer feedback can arrive
    hours later, and reading it as the run's final word would report every
    finished run someone bothered to rate as mysteriously interrupted.
    """
    path = run_dir / "events.jsonl"
    if lines is None:
        lines = _tail_lines(path)
    found = _parse_last(lines, TERMINAL_KINDS)
    if found is None and any('"producer.feedback"' in line for line in lines):
        # Enough feedback to fill the default window would push the lifecycle
        # event out of it. Widen once, and only in the case that can need it.
        found = _parse_last(_tail_lines(path, window=4 * 1024 * 1024), TERMINAL_KINDS)
    return found


def _recorded_pid(run_dir: Path) -> int | None:
    try:
        return int((run_dir / "child.pid").read_text().strip())
    except (OSError, ValueError):
        return None


def _pid_is_this_run(pid: int, run_id: str) -> bool:
    """Is that pid still the pipeline for this run, and not a recycled number?

    Checked against the process's own command line rather than trusting the pid
    alone. Pids are recycled, and signalling a stranger because a number came
    back around is the one failure mode a cancel button must not have.
    """
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    try:
        listing = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        # Without corroboration, decline to claim it. A run wrongly reported as
        # finished is recoverable by reloading; a signal sent to the wrong
        # process is not.
        return False
    line = listing.stdout.strip()
    return "houseband.loop" in line and run_id in line


def _process_alive(run_id: str, run_dir: Path | None = None) -> bool:
    """Is this run's pipeline still going?

    Deliberately not just "did *this* server start it". The module's whole claim
    is that the browser is optional and the log is the only shared state, which
    means a run outlives the server that launched it and a page must be able to
    attach to one this process has never seen. Reading liveness out of
    ``_PROCESSES`` alone breaks that the moment the server restarts mid-run: the
    run keeps composing and the UI calls it interrupted.
    """
    with _PROCESS_LOCK:
        proc = _PROCESSES.get(run_id)
    if proc is not None:
        return proc.poll() is None
    if run_dir is None:
        return False
    pid = _recorded_pid(run_dir)
    return pid is not None and _pid_is_this_run(pid, run_id)


def _status_of(run_dir: Path, last: Event | None, lines: list[str] | None = None) -> str:
    """Infer a run's state from its lifecycle events plus any process we hold."""
    terminal = _terminal_event(run_dir, lines)
    if terminal is not None:
        return STATUS_FOR_KIND[terminal.kind]
    if _process_alive(run_dir.name, run_dir):
        return "running"
    if last is None:
        return "starting" if (run_dir / "request.json").exists() else "empty"
    # Log stops mid-run with nothing running: either the child died without
    # saying so, or a server was restarted while a run was in flight and the
    # child has since gone too.
    return "interrupted"


def _append_event(run_dir: Path, kind: str, message: str, **data: Any) -> Event:
    """Append one event on the pipeline's behalf.

    Reserved for the things the pipeline was never alive to say: a launch or
    child-process failure that happened before or after it ran, and producer
    feedback, which arrives once someone has actually listened. Everything goes
    through ``scrub`` because the payload can include a child's stderr or a
    human's typing, and both are places a credential can surface.
    """
    path = run_dir / "events.jsonl"
    with _WRITE_LOCK:
        last = _parse_last(_tail_lines(path))
        event = Event(
            kind=kind,  # type: ignore[arg-type]
            seq=(last.seq if last else 0) + 1,
            message=scrub(message),
            data=scrub(data),
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(event.model_dump_json(exclude_none=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    return event


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title="houseband", docs_url=None, redoc_url=None)


@app.get("/")
def index() -> FileResponse:
    page = WEB_DIR / "index.html"
    if not page.exists():
        raise HTTPException(status_code=500, detail="web/index.html is missing.")
    return FileResponse(page, media_type="text/html; charset=utf-8")


@app.get("/api/config")
def get_config() -> dict[str, Any]:
    config = cfg.load()
    source = _credential_source()
    return {
        "model": config.model,
        "soundfont": {
            "present": config.soundfont is not None,
            "path": str(config.soundfont) if config.soundfont else None,
        },
        "fluidsynth": {
            "present": config.fluidsynth is not None,
            "path": config.fluidsynth,
        },
        "credential": {"configured": source is not None, "source": source},
        # Shipped from the contract rather than duplicated in the page, so the
        # judge grid cannot drift from houseband.types.
        "dimensions": [{"key": key, "title": DIMENSION_TITLES.get(key, key)} for key in DIMENSIONS],
        "round_token_budget": config.round_token_budget,
        "pricing": _pricing(config.model),
        "models": _model_choices(config.model),
        # Clip lengths, with the seconds each works out to. Bars are what the
        # composer and the DAW grid speak in, but a producer thinks in seconds,
        # and the answer depends on tempo, so the page gets the range rather than
        # a single number that would be wrong for two thirds of genres.
        "lengths": _length_choices(),
    }


def _length_choices() -> list[dict[str, Any]]:
    # The same two tempi the composer prompt quotes, taken from config rather than
    # restated, so the page and the instruction the composer reads cannot disagree
    # about how long a clip is.
    slow, fast = cfg.TEMPO_SLOW, cfg.TEMPO_FAST
    choices = []
    for bars in BAR_CHOICES:
        profile = cfg.profile_for(bars)
        choices.append(
            {
                "bars": bars,
                "default": bars == BARS_DEFAULT,
                "seconds_fast": round(profile.target_seconds(fast), 1),
                "seconds_slow": round(profile.target_seconds(slow), 1),
                "label": f"{bars} bars",
                "note": profile.approx_seconds,
            }
        )
    return choices


# Ordered cheapest-first, with a one-line note on the tradeoff. A run makes a lot
# of calls, so the price difference between tiers is the difference between
# running this freely and rationing it, and that is worth putting in front of the
# person paying rather than burying in a config file.
_MODEL_NOTES: dict[str, str] = {
    "claude-haiku-4-5": "Cheapest. Expect weak composition and unreliable judging.",
    "claude-sonnet-5": "Default. Good balance for repeated runs.",
    "claude-opus-4-8": "Stronger composition and judging, roughly 1.7x the cost.",
    "claude-opus-5": "Best composition and judging. Use when a run matters.",
    "claude-fable-5": "Most capable, and the most expensive by a wide margin.",
}


def _model_choices(current: str) -> list[dict[str, Any]]:
    order = list(_MODEL_NOTES)
    known = [m for m in order if m in _MODEL_PRICING]
    return [
        {
            "id": name,
            "note": _MODEL_NOTES[name],
            "input_per_mtok": _MODEL_PRICING[name]["input"],
            "output_per_mtok": _MODEL_PRICING[name]["output"],
            "default": name == current,
        }
        for name in known
    ]


def _pricing(model: str) -> dict[str, Any]:
    """Per-million-token rates for the live cost readout.

    Users spend their own credential here, so the number on screen has to be
    grounded in something. Unknown models return nulls rather than a guess: a
    blank readout is honest, an invented rate is not.
    """
    base = None
    for name, rates in _MODEL_PRICING.items():
        if model == name or model.startswith(name):
            base = rates
            break
    if base is None:
        return {"known": False, "model": model}
    return {
        "known": True,
        "model": model,
        "input_per_mtok": base["input"],
        "output_per_mtok": base["output"],
        "cache_write_per_mtok": round(base["input"] * 1.25, 4),
        "cache_read_per_mtok": round(base["input"] * 0.1, 4),
    }


# -- credential -------------------------------------------------------------


def _credential_source() -> str | None:
    if _CREDENTIAL.get("api_key"):
        return "submitted in this session"
    return cfg.credential_source()


@app.get("/api/credential")
def get_credential() -> dict[str, Any]:
    source = _credential_source()
    return {"configured": source is not None, "source": source}


@app.post("/api/credential")
def post_credential(body: CredentialIn) -> dict[str, Any]:
    key = body.api_key.strip()
    if not key:
        raise HTTPException(status_code=400, detail="Empty api_key.")
    _CREDENTIAL["api_key"] = key
    # Deliberately echoes only the fact of configuration. Never the value, and
    # never a prefix or suffix of it either.
    return {"configured": True, "source": _credential_source()}


@app.delete("/api/credential")
def delete_credential() -> dict[str, Any]:
    """Forget a submitted key without restarting the server."""
    _CREDENTIAL.pop("api_key", None)
    source = _credential_source()
    return {"configured": source is not None, "source": source}


# -- runs -------------------------------------------------------------------


@app.get("/api/runs")
def list_runs() -> dict[str, Any]:
    runs: list[dict[str, Any]] = []
    for entry in sorted(_runs_dir().iterdir(), reverse=True):
        if not entry.is_dir():
            continue
        request = _read_json(entry / "request.json", {})
        # One read of the tail, shared by the "last event" readout and the status
        # inference, so a long runs list stays one file read per run.
        lines = _tail_lines(entry / "events.jsonl")
        last = _parse_last(lines)
        prompt = request.get("prompt")
        if not prompt:
            first = _parse_first(_head_lines(entry / "events.jsonl"))
            if first is not None:
                prompt = first.data.get("prompt")
        runs.append(
            {
                "run_id": entry.name,
                "created": request.get("created") or _created_at(entry),
                "status": _status_of(entry, last, lines),
                "prompt": prompt or "",
                "teams": request.get("teams"),
                "rounds": request.get("rounds"),
                "last_kind": last.kind if last else None,
                "last_ts": last.ts if last else None,
                "events": last.seq if last else 0,
                "live": _process_alive(entry.name, entry),
            }
        )
    runs.sort(key=lambda run: run.get("created") or "", reverse=True)
    return {"runs": runs}


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _created_at(path: Path) -> str:
    try:
        stamp = path.stat().st_mtime
    except OSError:
        return ""
    return datetime.fromtimestamp(stamp, tz=timezone.utc).isoformat()


@app.post("/api/runs")
def create_run(body: RunIn) -> dict[str, Any]:
    prompt = body.prompt.strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="Empty prompt.")

    config = cfg.load()
    model = _validated_model(body.model) or config.model
    budget = body.budget or config.round_token_budget
    bars = body.bars or BARS_DEFAULT

    run_id = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(2)}"
    run_dir = _run_dir(run_id, must_exist=False)
    run_dir.mkdir(parents=True, exist_ok=True)

    # Recorded before launch so the run is listable (with its prompt) even if
    # the child never gets far enough to emit run.started.
    (run_dir / "request.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "prompt": prompt,
                "teams": body.teams,
                "rounds": body.rounds,
                "created": datetime.now(timezone.utc).isoformat(),
                "model": model,
                "budget": budget,
                "bars": bars,
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    _launch(
        run_id, run_dir, prompt, body.teams, body.rounds, model, budget, bars
    )
    return {"run_id": run_id, "model": model, "budget": budget, "bars": bars}


def _validated_model(model: str | None) -> str | None:
    """Accept only a model we publish a price for.

    Restrictive on purpose. The alternative is forwarding an arbitrary string to
    the API and surfacing its 404 partway into a paid run, and an unpriced model
    would also blank the cost readout the whole UI is built around.
    """
    if not model:
        return None
    name = model.strip()
    if not name:
        return None
    if name not in _MODEL_PRICING:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown model {name!r}. Known: {', '.join(sorted(_MODEL_PRICING))}",
        )
    return name


def _launch(
    run_id: str,
    run_dir: Path,
    prompt: str,
    teams: int,
    rounds: int,
    model: str | None = None,
    budget: int | None = None,
    bars: int | None = None,
) -> None:
    """Start the pipeline as a detached child, or record why we could not.

    A missing module or a failed exec is reported as a ``run.failed`` event
    rather than a 500, because the UI already knows how to display an event
    stream and does not know how to display a traceback. The run id is returned
    either way, so the failure shows up where the user is already looking.
    """
    if find_spec("houseband.loop") is None:
        _append_event(
            run_dir,
            "run.failed",
            "houseband.loop is not importable, so no pipeline was started.",
            reason="module_not_found",
            module="houseband.loop",
        )
        return

    command = [
        sys.executable,
        "-m",
        "houseband.loop",
        "--run-id",
        run_id,
        "--prompt",
        prompt,
        "--teams",
        str(teams),
        "--rounds",
        str(rounds),
    ]
    if model:
        command += ["--model", model]
    if budget:
        command += ["--budget", str(budget)]
    if bars:
        command += ["--bars", str(bars)]

    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(cfg.REPO_ROOT), env.get("PYTHONPATH")) if part
    )
    key = _CREDENTIAL.get("api_key")
    if key:
        # The only place a submitted key ever goes.
        env["ANTHROPIC_API_KEY"] = key

    log_path = run_dir / "child.log"
    try:
        handle = log_path.open("ab")
    except OSError as error:
        _append_event(run_dir, "run.failed", f"Could not open {log_path.name}: {error}")
        return

    try:
        proc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
            command,
            cwd=str(cfg.REPO_ROOT),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=handle,
            stderr=subprocess.STDOUT,
            # Its own process group, so cancelling reaches the programs the
            # pipeline itself spawns rather than orphaning them.
            start_new_session=True,
        )
    except OSError as error:
        handle.close()
        _append_event(
            run_dir,
            "run.failed",
            f"Could not start the pipeline: {error}",
            reason="spawn_failed",
            command=command[1:],
        )
        return
    finally:
        # The child holds its own duplicate of the descriptor.
        if not handle.closed:
            handle.close()

    # Written so liveness and cancellation survive this server process. A run is
    # a detached child by design, so the pid is the only handle a *replacement*
    # server has on it, and without this a restart mid-run makes a composing
    # pipeline look interrupted and makes its Cancel button a lie.
    try:
        (run_dir / "child.pid").write_text(f"{proc.pid}\n", encoding="utf-8")
    except OSError:
        # Not fatal: this server still holds the Popen. Only a restart loses it.
        pass

    with _PROCESS_LOCK:
        _PROCESSES[run_id] = proc
    threading.Thread(target=_watch, args=(run_id, run_dir, proc), daemon=True).start()


def _watch(run_id: str, run_dir: Path, proc: subprocess.Popen) -> None:
    """Turn a silent child death into a terminal event.

    Without this, a pipeline killed by the OS (or one that dies before it can
    write anything) leaves every SSE reader polling forever. A log that always
    ends in a terminal event is what lets the UI say "this is over" honestly.
    """
    code = proc.wait()
    with _PROCESS_LOCK:
        cancelled = run_id in _CANCELLED
        _CANCELLED.discard(run_id)
    if _terminal_event(run_dir) is not None:
        return
    tail = "\n".join(_tail_lines(run_dir / "child.log", window=8 * 1024)[-40:])
    message = (
        "Run cancelled: the pipeline process was terminated."
        if cancelled
        else f"Pipeline process exited with code {code} without finishing."
    )
    _append_event(
        run_dir,
        "run.failed",
        message,
        reason="cancelled" if cancelled else "child_exited",
        returncode=code,
        child_log_tail=tail,
    )


@app.post("/api/runs/{run_id}/cancel")
def cancel_run(run_id: str) -> dict[str, Any]:
    run_dir = _run_dir(run_id)
    with _PROCESS_LOCK:
        proc = _PROCESSES.get(run_id)

    # A run this server did not launch is still cancellable, via the pid the
    # launching server recorded. Refusing would mean a restart leaves a paying
    # user watching a run they can no longer stop.
    pid: int | None = None
    if proc is not None and proc.poll() is None:
        pid = proc.pid
    elif proc is None:
        recorded = _recorded_pid(run_dir)
        if recorded is not None and _pid_is_this_run(recorded, run_id):
            pid = recorded

    if pid is None:
        return {
            "run_id": run_id,
            "cancelled": False,
            "detail": "No live process for this run. It has already finished or exited.",
            "status": _status_of(run_dir, _last_event(run_dir)),
        }

    with _PROCESS_LOCK:
        _CANCELLED.add(run_id)
    try:
        # The child is its own process group leader (start_new_session), so this
        # also stops the composer programs it has spawned.
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        if proc is not None:
            proc.terminate()
        else:
            # Adopted run: no Popen to fall back on, so signal the pid directly.
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                with _PROCESS_LOCK:
                    _CANCELLED.discard(run_id)
                return {
                    "run_id": run_id,
                    "cancelled": False,
                    "detail": "The pipeline process could not be signalled.",
                    "status": _status_of(run_dir, _last_event(run_dir)),
                }

    if proc is None:
        # Nothing is wait()ing on an adopted child, so no _watch thread will turn
        # its death into a terminal event. Say so here or the log never ends and
        # every SSE reader tails a file that will not grow.
        threading.Thread(
            target=_watch_adopted, args=(run_id, run_dir, pid), daemon=True
        ).start()
    return {"run_id": run_id, "cancelled": True, "detail": "Termination signalled."}


def _watch_adopted(run_id: str, run_dir: Path, pid: int, timeout: float = 30.0) -> None:
    """Close out a run we signalled but never spawned.

    ``_watch`` relies on ``proc.wait()``, which only the parent can call. For an
    adopted child the best available signal is that the pid stops answering, so
    poll for that and then write the terminal event the pipeline never got to.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _pid_is_this_run(pid, run_id):
            break
        time.sleep(0.5)
    with _PROCESS_LOCK:
        _CANCELLED.discard(run_id)
    if _terminal_event(run_dir) is not None:
        return
    _append_event(
        run_dir,
        "run.failed",
        "Run cancelled: the pipeline process was terminated.",
        reason="cancelled",
        pid=pid,
    )


@app.get("/api/runs/{run_id}/status")
def run_status(run_id: str) -> dict[str, Any]:
    run_dir = _run_dir(run_id)
    last = _last_event(run_dir)
    return {
        "run_id": run_id,
        "status": _status_of(run_dir, last),
        "live": _process_alive(run_id, run_dir),
        "last_kind": last.kind if last else None,
        "last_seq": last.seq if last else 0,
        "request": _read_json(run_dir / "request.json", {}),
    }


# -- event stream -----------------------------------------------------------


@app.get("/api/runs/{run_id}/events")
def stream_run_events(run_id: str, from_seq: int = 0) -> StreamingResponse:
    """Replay from ``from_seq``, then follow the log live.

    Replay-then-tail is the whole reason a reload costs nothing: ``tail_events``
    rescans the file from the start and filters on ``seq``, so an event written
    between the replay and the tail is picked up rather than skipped, and one
    already replayed is not sent twice.
    """
    run_dir = _run_dir(run_id)
    return StreamingResponse(
        _sse_frames(run_dir / "events.jsonl", max(0, from_seq)),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def _frame(event: Event) -> str:
    return f"id: {event.seq}\ndata: {event.model_dump_json(exclude_none=True)}\n\n"


def _sse_frames(path: Path, from_seq: int) -> Iterator[str]:
    yield ": open\n\n"

    last_seq = from_seq
    finished = False
    for event in read_events(path):
        if event.seq <= from_seq:
            continue
        last_seq = max(last_seq, event.seq)
        yield _frame(event)
        if event.kind in TERMINAL_KINDS:
            finished = True

    if finished:
        # tail_events only returns on a terminal event it *yields*, and it will
        # not re-yield one already below last_seq -- so a finished run has to be
        # recognised here or the reader would poll a dead file forever.
        yield "event: end\ndata: {}\n\n"
        return

    # tail_events blocks between events, which would starve the heartbeat, so it
    # runs in a thread and this generator waits on a queue with a timeout. The
    # thread is a daemon and exits on the run's terminal event; a client that
    # disconnects mid-run leaves it parked until then, which is the price of
    # tail_events having no cancellation. Bounded by concurrent viewers.
    stop = threading.Event()
    channel: queue.Queue[Event | BaseException | None] = queue.Queue()

    def pump() -> None:
        try:
            for event in tail_events(path, from_seq=last_seq):
                channel.put(event)
                if stop.is_set():
                    return
        except BaseException as error:  # noqa: BLE001 - relayed to the client
            channel.put(error)
        finally:
            channel.put(None)

    threading.Thread(target=pump, daemon=True).start()

    try:
        while True:
            try:
                item = channel.get(timeout=HEARTBEAT_SECONDS)
            except queue.Empty:
                yield ": ping\n\n"
                continue
            if item is None:
                yield "event: end\ndata: {}\n\n"
                return
            if isinstance(item, BaseException):
                payload = json.dumps({"message": str(item) or type(item).__name__})
                yield f"event: error\ndata: {payload}\n\n"
                return
            yield _frame(item)
    finally:
        stop.set()


# -- artifacts --------------------------------------------------------------


@app.get("/api/runs/{run_id}/files/{path:path}")
def get_run_file(run_id: str, path: str) -> FileResponse:
    """Serve an artifact from inside one run directory, and only from there.

    ``resolve()`` collapses ``..`` and follows symlinks *before* the containment
    check, so neither traversal nor a symlink planted in the run directory gets
    out. The check is on the resolved parents, not on the request string.
    """
    run_dir = _run_dir(run_id)
    if not path or path.startswith("/"):
        raise HTTPException(status_code=400, detail="Path must be relative.")
    target = _safe_path(run_dir, path)
    if target is None:
        raise HTTPException(status_code=403, detail="Path escapes the run directory.")
    if not target.is_file():
        raise HTTPException(status_code=404, detail="No such file in this run.")
    return FileResponse(
        target,
        media_type=CONTENT_TYPES.get(target.suffix.lower(), "application/octet-stream"),
        headers={"Cache-Control": "no-cache"},
    )


# -- candidates --------------------------------------------------------------

# The four artifact paths an artifact.rendered event carries. Named here so the
# candidates endpoint and the page agree on the shape without either guessing.
ARTIFACT_KEYS = ("audio", "piano_roll", "midi", "program", "daw_bundle")

# A MIDI file we parse only because no sidecar was written. Above this size the
# parse costs more than the metadata is worth on a request that has to stay
# snappy, and anything this large is not a starter clip anyway.
_MIDI_PARSE_LIMIT = 2 * 1024 * 1024


def _run_relative(run_dir: Path, raw: Any) -> str | None:
    """Normalise an artifact path from the log into a run-relative one.

    The pipeline writes run-relative paths today, but it has written absolute and
    repo-relative ones before, and the page's own ``relPath`` already forgives
    all three. Doing the same here means a change of mind upstream costs nothing.
    """
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.replace("\\", "/").strip()
    marker = f"/runs/{run_dir.name}/"
    if marker in text:
        text = text.split(marker, 1)[1]
    prefix = f"runs/{run_dir.name}/"
    if text.startswith(prefix):
        text = text[len(prefix) :]
    return text.lstrip("/") or None


def _candidate_index(run_dir: Path) -> dict[str, dict[str, Any]]:
    """Everything the log says about each candidate, in first-seen order.

    Replayed from the event log rather than read from a summary file, because the
    log is the only thing the pipeline is guaranteed to have written: a run that
    died in round three still has two rounds of candidates worth auditioning, and
    auditioning them is the entire point of this endpoint.

    Ids seen only in a pairwise verdict get a stub entry. They are kept so that
    feedback on them validates, and dropped from the listing by the caller,
    because a card with nothing to play on it is not worth drawing.

    Keyed on candidate id *and* round, because ids are only round-unique by
    convention. Current runs mint ``r2c3``, but older ones reused ``c1`` every
    round, and keying on the id alone would fold six takes into three and show
    the last round's artifacts against the first round's scores.
    """
    index: dict[str, dict[str, Any]] = {}

    def entry(candidate_id: str, event: Event) -> dict[str, Any]:
        data = event.data or {}
        round_no = event.round
        if round_no is None and isinstance(data.get("round"), int):
            round_no = data["round"]

        key = f"{candidate_id}#{round_no}" if round_no is not None else candidate_id
        found = index.get(key)
        if found is None and round_no is None:
            # An event that does not say which round it belongs to still belongs
            # to a candidate we may already know. Attach to it rather than fork.
            for existing in index.values():
                if existing["candidate_id"] == candidate_id:
                    found = existing
                    break
        if found is None:
            found = index[key] = {
                "candidate_id": candidate_id,
                "team": None,
                "round": round_no,
                "preview": False,
                "first_seq": event.seq,
                "artifacts": {},
                "scores": {},
                "gate": None,
                # Track names the judges used. Not metadata, but the only track
                # list some runs have. See _score_meta.
                "finding_tracks": [],
            }
        team = data.get("team") or event.team
        if team and not found["team"]:
            found["team"] = str(team)
        return found

    for event in read_events(run_dir / "events.jsonl"):
        data = event.data or {}
        if event.kind == "artifact.rendered":
            # The team fallback mirrors the page's own tolerance: a renamed key
            # should cost a label, not a whole card.
            candidate_id = str(data.get("candidate_id") or event.team or "")
            if not candidate_id:
                continue
            found = entry(candidate_id, event)
            # Per-team previews are rendered before the pool is blinded, so they
            # are a different thing from a judged candidate and are marked as
            # such rather than quietly mixed in with them.
            if data.get("preview") is True:
                found["preview"] = True
            for key in ARTIFACT_KEYS:
                relative = _run_relative(run_dir, data.get(key))
                if relative:
                    found["artifacts"][key] = relative

        elif event.kind == "judge.verdict":
            candidate_id = str(data.get("candidate_id") or "")
            if not candidate_id:
                continue
            found = entry(candidate_id, event)
            dimension = str(event.dimension or data.get("dimension") or "unknown")
            findings = data.get("findings")
            findings = findings if isinstance(findings, list) else []
            samples = data.get("samples")
            found["scores"][dimension] = {
                "dimension": dimension,
                "title": DIMENSION_TITLES.get(dimension, dimension),
                "score": data.get("score") if isinstance(data.get("score"), int) else None,
                "samples": [s for s in samples if isinstance(s, int)]
                if isinstance(samples, list)
                else [],
                "spread": data.get("spread") if isinstance(data.get("spread"), int) else None,
                "rationale": str(data.get("rationale") or ""),
                # A count here; the text is merged in from verdicts.json by
                # _merge_disk_verdicts. The event log deliberately records only
                # the count, because one round of full findings is over 100KB and
                # the log is replayed in full on every page load.
                "findings": len(findings),
            }
            for finding in findings:
                track = finding.get("track") if isinstance(finding, dict) else None
                if isinstance(track, str) and track.strip():
                    name = track.strip()
                    if name not in found["finding_tracks"]:
                        found["finding_tracks"].append(name)

        elif event.kind in ("gate.passed", "gate.rejected"):
            # Keyed on candidate_id only. A gate event without one carries a team
            # name, and a team name would collide with a preview's id.
            candidate_id = str(data.get("candidate_id") or "")
            if not candidate_id:
                continue
            found = entry(candidate_id, event)
            found["gate"] = {"ok": event.kind == "gate.passed", "message": event.message}

        elif event.kind == "pairwise.verdict":
            for key in ("a", "b"):
                candidate_id = data.get(key)
                if isinstance(candidate_id, str) and candidate_id:
                    entry(candidate_id, event)

    _merge_disk_verdicts(run_dir, index)
    _fold_previews_into_judged(index)
    return index


def _merge_disk_verdicts(run_dir: Path, index: dict[str, dict[str, Any]]) -> None:
    """Fill in the rationale and findings the event log does not carry.

    ``judge.verdict`` records a score and a finding *count*, not the text: one
    round of findings is over 100KB and the log is replayed in full on every page
    load. The full verdicts are written to ``round<N>/verdicts.json``, which is
    the authoritative record, so read them from there.

    Without this a card had a score and nothing else -- no rationale, no
    bar-anchored claim, no suggested revision -- which is the entire substance of
    the feedback loop and the only reason to look at a verdict at all.
    """
    for path in sorted(run_dir.glob("round*/verdicts.json")):
        payload = _read_json(path, {})
        verdicts = payload.get("verdicts")
        if not isinstance(verdicts, dict):
            continue
        round_no = payload.get("round")
        for candidate_id, verdict in verdicts.items():
            if not isinstance(verdict, dict):
                continue
            found = _find_candidate(
                index, str(candidate_id), round_no if isinstance(round_no, int) else None
            )
            if found is None:
                continue
            for scored in verdict.get("dimensions") or []:
                if not isinstance(scored, dict):
                    continue
                dimension = str(scored.get("dimension") or "")
                target = found["scores"].get(dimension)
                if target is None:
                    continue
                rationale = scored.get("rationale")
                if isinstance(rationale, str) and rationale.strip():
                    target["rationale"] = rationale
                findings = scored.get("findings")
                if isinstance(findings, list):
                    target["findings"] = len(findings)
                    target["finding_list"] = [f for f in findings if isinstance(f, dict)]
                    for finding in target["finding_list"]:
                        track = finding.get("track")
                        if isinstance(track, str) and track.strip():
                            name = track.strip()
                            if name not in found["finding_tracks"]:
                                found["finding_tracks"].append(name)


def _fold_previews_into_judged(index: dict[str, dict[str, Any]]) -> None:
    """Mark each preview as superseded once its judged counterpart appears.

    A preview and the judged candidate for the same team and round are the *same
    music*: the loop renders a clip the moment a composer finishes so there is
    something to audition immediately, then renders it again under a blinded id
    for the panel. Listing both gave a producer six cards for three takes and no
    way to tell which pair was a duplicate, which is worse than useless when the
    job is deciding which take to keep.

    Superseded entries are marked rather than deleted, because this index is also
    what resolves an incoming feedback POST. A page that loaded mid-composition
    holds preview ids, and rating a take must not start failing the moment
    judging happens to catch up.
    """
    by_take: dict[tuple[str, int | None], list[dict[str, Any]]] = {}
    for found in index.values():
        team = found.get("team")
        if not team:
            # An entry with no team attribution cannot be matched to a preview
            # without guessing.
            continue
        by_take.setdefault((str(team), found["round"]), []).append(found)

    for entries in by_take.values():
        previews = [e for e in entries if e["preview"]]
        # Any blinded render counts, scored or not. Requiring scores meant that
        # while a round was being judged the page showed both the preview and its
        # blinded twin -- five or six cards for three takes, resolving to three
        # only once the last verdict landed. The music is identical from the
        # moment the blinded render exists, and any artifact the blinded event
        # omits is copied up from the preview below, so the merged card is always
        # playable.
        judged = [e for e in entries if not e["preview"] and (e["artifacts"] or e["scores"])]
        if not previews or not judged:
            continue
        # Prefer a scored entry, then the earliest. Its id is the one the panel,
        # the Elo table and the coach all use, so it is what feedback should key
        # to.
        winner = min(judged, key=lambda e: (not e["scores"], e["first_seq"]))
        for preview in previews:
            for key, value in preview["artifacts"].items():
                # Fill gaps only. The judged render is the artifact the scores
                # actually describe, but the preview carries things the judged
                # event never mentions, the DAW bundle among them.
                winner["artifacts"].setdefault(key, value)
            if not winner["gate"] and preview["gate"]:
                winner["gate"] = preview["gate"]
            winner.setdefault("superseded_ids", []).append(preview["candidate_id"])
            preview["superseded_by"] = winner["candidate_id"]
            # Keep the card where the producer last saw it. The preview appeared
            # first, so inheriting its sequence means the take upgrades in place
            # instead of jumping to the end of the row when its scores land.
            winner["first_seq"] = min(winner["first_seq"], preview["first_seq"])


def _find_candidate(
    index: dict[str, dict[str, Any]], candidate_id: str, round_no: int | None
) -> dict[str, Any] | None:
    """Look a candidate up by id, preferring the round the caller named.

    The id alone is what judges and the Elo table use, so it stays the public
    handle. The round only breaks the tie left by runs that reused ids.
    """
    fallback: dict[str, Any] | None = None
    for entry in index.values():
        if entry["candidate_id"] != candidate_id:
            continue
        if round_no and entry["round"] == round_no:
            return entry
        if fallback is None:
            fallback = entry
    return fallback


def _score_meta(run_dir: Path, entry: dict[str, Any]) -> dict[str, Any]:
    """Key, tempo, bar count and track list for one candidate.

    Three sources in descending order of authority: the sidecar the composer
    wrote next to its MIDI, the MIDI file itself, and the track names the judges
    used in their findings. The last is a genuine fallback rather than a guess --
    "the pads are muddying the mid" tells you there is a track called pad -- and
    it is what keeps the per-track feedback rows usable for a run whose sidecar
    was never written.
    """
    meta: dict[str, Any] = {
        "key": "",
        "time_sig": None,
        "tempo": None,
        "total_bars": None,
        "duration": None,
        "sections": [],
        "tracks": [],
        "tracks_from": "none",
    }
    relative = entry["artifacts"].get("midi")
    midi_path = _safe_path(run_dir, relative) if relative else None

    if midi_path is not None:
        sidecar = midi_path.with_suffix(".score.json")
        payload = _read_json(sidecar, None) if sidecar.is_file() else None
        if isinstance(payload, dict):
            meta["key"] = str(payload.get("key") or "")
            signature = payload.get("time_sig")
            if isinstance(signature, list) and len(signature) == 2:
                meta["time_sig"] = f"{signature[0]}/{signature[1]}"
            tempo_map = payload.get("tempo_map")
            if isinstance(tempo_map, list) and tempo_map:
                first = tempo_map[0]
                if isinstance(first, list) and len(first) == 2:
                    meta["tempo"] = first[1]
            if isinstance(payload.get("total_bars"), int):
                meta["total_bars"] = payload["total_bars"]
            if isinstance(payload.get("duration"), (int, float)):
                meta["duration"] = round(float(payload["duration"]), 2)
            sections = payload.get("sections")
            if isinstance(sections, list):
                meta["sections"] = [
                    {
                        "name": str(s.get("name") or ""),
                        "start_bar": s.get("start_bar"),
                        "bars": s.get("bars"),
                    }
                    for s in sections
                    if isinstance(s, dict)
                ]
            tracks = payload.get("tracks")
            if isinstance(tracks, list) and tracks:
                meta["tracks"] = [
                    {
                        "name": str(t.get("name") or ""),
                        "program": t.get("patch", t.get("program")),
                        "is_drum": bool(t.get("is_drum")),
                        "note_count": t.get("note_count"),
                    }
                    for t in tracks
                    if isinstance(t, dict) and str(t.get("name") or "")
                ]
                meta["tracks_from"] = "sidecar"

    if not meta["tracks"] and midi_path is not None and midi_path.is_file():
        meta.update(_meta_from_midi(midi_path, meta))

    if not meta["tracks"] and entry["finding_tracks"]:
        meta["tracks"] = [
            {"name": name, "program": None, "is_drum": None, "note_count": None}
            for name in entry["finding_tracks"]
        ]
        meta["tracks_from"] = "judge findings"

    return meta


def _meta_from_midi(midi_path: Path, current: dict[str, Any]) -> dict[str, Any]:
    """Read track names and duration straight out of a MIDI file.

    Only reached when no sidecar exists. Failure is not interesting enough to
    report: a candidate whose MIDI will not parse still has audio to audition and
    a verdict to record, and an empty track list says so honestly.
    """
    out: dict[str, Any] = {}
    try:
        if midi_path.stat().st_size > _MIDI_PARSE_LIMIT:
            return out
        import pretty_midi

        midi = pretty_midi.PrettyMIDI(str(midi_path))
    except Exception:
        return out
    tracks = []
    for index, instrument in enumerate(midi.instruments):
        name = (instrument.name or "").strip() or (
            "drums" if instrument.is_drum else f"track{index}_program{instrument.program}"
        )
        tracks.append(
            {
                "name": name,
                "program": instrument.program,
                "is_drum": bool(instrument.is_drum),
                "note_count": len(instrument.notes),
            }
        )
    if tracks:
        out["tracks"] = tracks
        out["tracks_from"] = "midi"
    if current.get("duration") is None:
        try:
            out["duration"] = round(float(midi.get_end_time()), 2)
        except Exception:
            pass
    if current.get("tempo") is None:
        try:
            _, tempi = midi.get_tempo_changes()
            if len(tempi):
                out["tempo"] = round(float(tempi[0]), 2)
        except Exception:
            pass
    return out


def _weighted_total(scores: list[dict[str, Any]]) -> float | None:
    """Weighted mean on the 1-10 scale, matching CandidateVerdict.weighted_total."""
    total = weight_sum = 0.0
    for score in scores:
        value = score.get("score")
        if not isinstance(value, int):
            continue
        weight = DIMENSION_WEIGHTS.get(str(score.get("dimension")), 1.0)
        total += value * weight
        weight_sum += weight
    return round(total / weight_sum, 3) if weight_sum else None


@app.get("/api/runs/{run_id}/candidates")
def list_candidates(run_id: str) -> dict[str, Any]:
    """Every candidate in one run, with artifacts, scores and any feedback.

    What the variation browser reads. Ordered as generated rather than ranked:
    for idea generation a producer is better served by six varied takes than by
    one Elo winner, and a payload that arrived pre-sorted by score would be
    quietly arguing the opposite.
    """
    run_dir = _run_dir(run_id)
    index = _candidate_index(run_dir)
    records = _read_feedback(run_dir)
    zips = _export_zips(run_dir)

    candidates: list[dict[str, Any]] = []
    for entry in index.values():
        if not entry["artifacts"] and not entry["scores"] and entry["gate"] is None:
            continue
        if entry.get("superseded_by"):
            # Its judged counterpart is listed instead, carrying this preview's
            # artifacts and any feedback recorded against it. Drawing both is how
            # three takes became six cards.
            continue
        candidate_id = entry["candidate_id"]
        # Runs recorded before references were removed from the tool still have a
        # reference entry per round on disk. It was a judging control, not a take
        # on offer, and its artifacts live outside the candidate path so the card
        # had nothing to play. Kept out of the listing so an old run reads the
        # same as a new one.
        #
        # Matched against the exact ids the loop used to mint, not a suffix: an
        # endswith("ref") test also swallowed ordinary candidates whose ids happen
        # to end that way, which is how a real candidate called "cref" vanished.
        if entry.get("team") == "reference" or LEGACY_REFERENCE_ID_RE.match(candidate_id):
            continue
        meta = _score_meta(run_dir, entry)
        # Published in rubric order, with anything unrecognised appended, so the
        # page can draw a fixed set of bars and a new dimension still shows up.
        ordered = [entry["scores"][key] for key in DIMENSIONS if key in entry["scores"]]
        ordered += [value for key, value in entry["scores"].items() if key not in DIMENSIONS]
        values = [s["score"] for s in ordered if isinstance(s.get("score"), int)]
        export = _match_export(zips, candidate_id)
        # Feedback recorded against a superseded preview belongs to this take. A
        # producer who rated a clip before judging finished has not rated a
        # different piece of music, and losing that rating would silently drop the
        # signal the coach values most.
        feedback, feedback_count = _feedback_for(
            records,
            candidate_id,
            entry["round"],
            also=entry.get("superseded_ids") or (),
        )
        candidates.append(
            {
                "candidate_id": candidate_id,
                "team": entry["team"],
                "round": entry["round"],
                "preview": entry["preview"],
                # Ids this card absorbed, so a page holding a preview id can still
                # tell which card its rating landed on.
                "superseded_ids": entry.get("superseded_ids") or [],
                "first_seq": entry["first_seq"],
                "artifacts": entry["artifacts"],
                "gate": entry["gate"],
                "scores": ordered,
                "mean_score": round(sum(values) / len(values), 3) if values else None,
                "weighted_total": _weighted_total(ordered),
                "key": meta["key"],
                "tempo": meta["tempo"],
                "time_sig": meta["time_sig"],
                "total_bars": meta["total_bars"],
                "duration": meta["duration"],
                "sections": meta["sections"],
                "tracks": meta["tracks"],
                "tracks_from": meta["tracks_from"],
                "feedback": feedback,
                "feedback_count": feedback_count,
                "export": {
                    "available": export is not None,
                    # Relative, not absolute: the browser has no use for this
                    # server's directory layout and no business knowing it.
                    "file": export.relative_to(run_dir).as_posix() if export else None,
                },
            }
        )

    candidates.sort(key=lambda item: (item["first_seq"], item["candidate_id"]))
    return {
        "run_id": run_id,
        "candidates": candidates,
        "feedback_count": len(records),
    }


# -- producer feedback -------------------------------------------------------


def _feedback_path(run_dir: Path) -> Path:
    return run_dir / "feedback.jsonl"


def _read_feedback(run_dir: Path) -> list[dict[str, Any]]:
    """Every feedback record for a run, oldest first. Bad lines are skipped."""
    path = _feedback_path(run_dir)
    records: list[dict[str, Any]] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return records
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def _feedback_for(
    records: list[dict[str, Any]],
    candidate_id: str,
    round_no: int | None,
    also: tuple[str, ...] | list[str] = (),
) -> tuple[dict[str, Any] | None, int]:
    """The newest record for one candidate, and how many there are.

    Append-only, so the last matching write is the producer's current opinion and
    the earlier ones are the history of them changing their mind. A record whose
    round is unset matches any round, because that is a record from a client that
    did not know, not a record about a different take.

    ``also`` names ids this take absorbed, currently the preview rendered from the
    same program. Those ratings are about this music and count as this take's.
    """
    wanted = {candidate_id, *also}
    latest: dict[str, Any] | None = None
    count = 0
    for record in records:
        if record.get("candidate_id") not in wanted:
            continue
        recorded = record.get("round")
        if isinstance(recorded, int) and recorded and round_no and recorded != round_no:
            continue
        count += 1
        latest = record
    return latest, count


def _clean_names(names: list[str], limit: int = 128) -> list[str]:
    """Strip, drop blanks, and dedupe while keeping the producer's order."""
    out: list[str] = []
    for name in names:
        if not isinstance(name, str):
            continue
        text = name.strip()
        if text and text not in out:
            out.append(text)
        if len(out) >= limit:
            break
    return out


def _append_feedback(run_dir: Path, record: dict[str, Any]) -> None:
    with _FEEDBACK_LOCK:
        path = _feedback_path(run_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())


@app.get("/api/runs/{run_id}/feedback")
def get_run_feedback(run_id: str) -> dict[str, Any]:
    """Every feedback record for a run, oldest first.

    The whole history rather than a current-opinion summary: a producer who kept a
    stem on Monday and binned it on Tuesday has told the coach something, and a
    view that collapsed the two would lose it. The candidates endpoint does the
    collapsing for the cards that need it.
    """
    run_dir = _run_dir(run_id)
    records = _read_feedback(run_dir)
    return {"run_id": run_id, "feedback": records, "count": len(records)}


@app.post("/api/runs/{run_id}/feedback")
def post_run_feedback(run_id: str, body: ProducerFeedback) -> dict[str, Any]:
    """Record what the producer thought, to disk and to the log.

    ``feedback.jsonl`` is written first because it is the durable record; the
    event is the notification, and a notification without a record behind it
    would be the worse of the two failures. Append-only in both places: a
    producer changing their mind is itself evidence, and overwriting it would
    throw away the fact that a second listen went differently.
    """
    run_dir = _run_dir(run_id)
    candidate_id = body.candidate_id.strip()
    if not CANDIDATE_ID_RE.match(candidate_id):
        raise HTTPException(status_code=400, detail="Malformed candidate id.")
    if len(body.note) > MAX_NOTE_CHARS:
        raise HTTPException(
            status_code=400, detail=f"Note is longer than {MAX_NOTE_CHARS} characters."
        )

    entry = _find_candidate(_candidate_index(run_dir), candidate_id, body.round)
    if entry is None:
        # An id this run's log never mentions is a stale page or a typo. Storing
        # it would put a record in the coach's evidence that no artifact backs.
        raise HTTPException(
            status_code=400, detail=f"No candidate {candidate_id!r} in run {run_id}."
        )

    feedback = ProducerFeedback(
        candidate_id=candidate_id,
        # The log already knows the team and the round, so the page does not have
        # to be right about them for the record to be complete and attributable.
        round=body.round or int(entry["round"] or 0),
        team=body.team.strip() or (entry["team"] or ""),
        verdict=body.verdict,
        kept_tracks=_clean_names(body.kept_tracks),
        discarded_tracks=_clean_names(body.discarded_tracks),
        note=body.note.strip(),
    )

    recorded_at = datetime.now(timezone.utc).isoformat()
    record = scrub({"recorded_at": recorded_at, **feedback.model_dump()})
    _append_feedback(run_dir, record)
    event = _append_event(
        run_dir,
        "producer.feedback",
        f"{candidate_id}: {feedback.as_evidence()}",
        recorded_at=recorded_at,
        **feedback.model_dump(),
    )
    return {
        "run_id": run_id,
        "candidate_id": candidate_id,
        "recorded": record,
        "event_seq": event.seq,
    }


# -- DAW export --------------------------------------------------------------


def _export_zips(run_dir: Path) -> list[Path]:
    """Every zip inside one run directory, containment re-checked after resolve."""
    found: list[Path] = []
    try:
        for path in run_dir.rglob("*.zip"):
            resolved = path.resolve()
            if resolved.is_file() and run_dir in resolved.parents:
                found.append(resolved)
    except OSError:
        return []
    return sorted(set(found))


def _match_export(zips: list[Path], candidate_id: str) -> Path | None:
    """Find a candidate's bundle without pinning the exporter's naming scheme."""
    for path in zips:
        if path.stem == candidate_id:
            return path
    for path in zips:
        if candidate_id in path.stem:
            return path
    return None


def _generate_export(run_dir: Path, entry: dict[str, Any]) -> tuple[Path | None, list[str]]:
    """Ask ``houseband.export`` for a bundle, and report why if it declines.

    Returns the zip and no complaints, or no zip and whatever the exporter said
    about it. The import is guarded because this server has to run in a checkout
    where ``houseband.export`` is absent, and the call is guarded because this
    server does not own the export format: a bundle it cannot get is a 404, and a
    traceback out of someone else's module is noise.

    The exporter's own ``problems`` are passed through, though. It refuses to
    write a bundle it thinks a producer would not trust, and "MIDI contains no
    notes" is a far more useful 404 than "not found".

    All candidates share one ``exports/`` directory, distinguished by stem, which
    is the layout ``export_bundle`` documents for exactly this caller.
    """
    try:
        from houseband.export import export_bundle
    except ImportError:
        return None, []

    relative = entry["artifacts"].get("midi")
    midi_path = _safe_path(run_dir, relative) if relative else None
    if midi_path is None or not midi_path.is_file():
        return None, ["This candidate has no MIDI file to export."]
    sidecar = midi_path.with_suffix(".score.json")

    try:
        result = export_bundle(
            midi_path=midi_path,
            sidecar_path=sidecar if sidecar.is_file() else None,
            out_dir=run_dir / "exports",
            stem=entry["candidate_id"],
        )
    except Exception as error:  # noqa: BLE001 - relayed to the client as a 404
        return None, [f"The exporter raised {type(error).__name__}: {error}"]

    zip_path = getattr(result, "zip_path", None)
    if zip_path is not None and Path(zip_path).is_file():
        return Path(zip_path).resolve(), []
    problems = getattr(result, "problems", None)
    return None, [str(problem) for problem in problems] if isinstance(problems, list) else []


@app.get("/api/runs/{run_id}/export/{candidate_id}")
def get_export(run_id: str, candidate_id: str) -> FileResponse:
    """Serve the DAW export zip for one candidate, if there is one."""
    run_dir = _run_dir(run_id)
    if not CANDIDATE_ID_RE.match(candidate_id):
        raise HTTPException(status_code=400, detail="Malformed candidate id.")
    problems: list[str] = []
    path = _match_export(_export_zips(run_dir), candidate_id)
    if path is None:
        entry = _find_candidate(_candidate_index(run_dir), candidate_id, None)
        if entry is not None:
            path, problems = _generate_export(run_dir, entry)
    if path is None:
        detail = f"No DAW export bundle for {candidate_id} in this run."
        if problems:
            detail += " " + " ".join(problems[:4])
        raise HTTPException(status_code=404, detail=detail)
    return FileResponse(
        path,
        media_type="application/zip",
        filename=path.name,
        headers={"Cache-Control": "no-cache"},
    )


# -- staged library functions -----------------------------------------------


@app.get("/api/runs/{run_id}/staged")
def list_staged(run_id: str) -> dict[str, Any]:
    """List candidate house-library functions awaiting human approval.

    The source is read as text and shown, never imported or executed. Approval
    is a human reading it; a server that ran the code to "check" it would have
    already done the thing approval exists to gate.
    """
    run_dir = _run_dir(run_id)
    staged_dir = run_dir / "staged"
    functions: list[dict[str, Any]] = []
    if staged_dir.is_dir():
        for entry in sorted(staged_dir.glob("*.json")):
            payload = _read_json(entry, None)
            if not isinstance(payload, dict):
                continue
            name = str(payload.get("name") or entry.stem)
            functions.append(
                {
                    "name": name,
                    "rationale": payload.get("rationale", ""),
                    "source": payload.get("source", ""),
                    "test_source": payload.get("test_source", ""),
                    "approved": (staged_dir / f"{entry.stem}.approved").exists(),
                    "file": entry.name,
                }
            )
    return {"run_id": run_id, "staged": functions}


@app.post("/api/runs/{run_id}/staged/{name}/approve")
def approve_staged(run_id: str, name: str) -> dict[str, Any]:
    run_dir = _run_dir(run_id)
    if not FUNCTION_NAME_RE.match(name):
        raise HTTPException(status_code=400, detail="Malformed function name.")
    staged_dir = run_dir / "staged"
    if not (staged_dir / f"{name}.json").is_file():
        raise HTTPException(status_code=404, detail=f"Nothing staged under {name}.")
    marker = staged_dir / f"{name}.approved"
    marker.write_text(
        json.dumps({"name": name, "approved_at": datetime.now(timezone.utc).isoformat()}) + "\n",
        encoding="utf-8",
    )
    return {"run_id": run_id, "name": name, "approved": True, "marker": marker.name}


@app.exception_handler(404)
def not_found(_request: Any, exc: Any) -> JSONResponse:
    detail = getattr(exc, "detail", "Not found.")
    return JSONResponse(status_code=404, content={"detail": detail})


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Serve the houseband UI.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args(argv)

    import uvicorn

    print(cfg.load().describe(), flush=True)
    uvicorn.run(
        "houseband.server:app" if args.reload else app,
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
