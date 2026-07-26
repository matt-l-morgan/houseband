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
from datetime import datetime, timezone
from importlib.util import find_spec
from pathlib import Path
from typing import Any, Iterator

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from houseband import config as cfg
from houseband.events import Event, read_events, scrub, tail_events
from houseband.types import DIMENSION_TITLES, DIMENSIONS

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

# run_id -> Popen. Only covers runs this process launched: a run started
# elsewhere is still fully observable, just not cancellable from here.
_PROCESSES: dict[str, subprocess.Popen] = {}
# Runs we signalled, so the watcher can say "cancelled" rather than reporting a
# deliberate kill as a mysterious negative exit code.
_CANCELLED: set[str] = set()
_PROCESS_LOCK = threading.Lock()

# Serialises the read-max-seq-then-append dance in _append_event.
_WRITE_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class CredentialIn(BaseModel):
    api_key: str


class RunIn(BaseModel):
    prompt: str
    teams: int = Field(default=3, ge=1, le=8)
    rounds: int = Field(default=3, ge=1, le=20)
    reference: str | None = None


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


def _parse_last(lines: list[str]) -> Event | None:
    for line in reversed(lines):
        try:
            return Event.model_validate_json(line)
        except Exception:
            continue
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


def _process_alive(run_id: str) -> bool:
    with _PROCESS_LOCK:
        proc = _PROCESSES.get(run_id)
    return proc is not None and proc.poll() is None


def _status_of(run_dir: Path, last: Event | None) -> str:
    """Infer a run's state from its final event plus any process we still hold."""
    if last is not None and last.kind in STATUS_FOR_KIND:
        return STATUS_FOR_KIND[last.kind]
    if _process_alive(run_dir.name):
        return "running"
    if last is None:
        return "starting" if (run_dir / "request.json").exists() else "empty"
    # Log stops mid-run with nothing running: either the child died without
    # saying so, or this server was restarted while a run was in flight.
    return "interrupted"


def _append_event(run_dir: Path, kind: str, message: str, **data: Any) -> Event:
    """Append one event on the pipeline's behalf.

    Only used to report a launch or child-process failure that the pipeline
    itself was never alive to report. Everything goes through ``scrub`` because
    the payload can include a child's stderr, and stderr is exactly where a
    misconfigured credential tends to surface.
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
    references: list[str] = []
    if config.references_dir.is_dir():
        references = sorted(
            entry.name
            for entry in config.references_dir.iterdir()
            if entry.is_file() and entry.suffix.lower() in {".mid", ".midi"}
        )
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
        "references": references,
        # Shipped from the contract rather than duplicated in the page, so the
        # judge grid cannot drift from houseband.types.
        "dimensions": [{"key": key, "title": DIMENSION_TITLES.get(key, key)} for key in DIMENSIONS],
        "round_token_budget": config.round_token_budget,
        "pricing": _pricing(config.model),
    }


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
        last = _last_event(entry)
        prompt = request.get("prompt")
        if not prompt:
            first = _parse_first(_head_lines(entry / "events.jsonl"))
            if first is not None:
                prompt = first.data.get("prompt")
        runs.append(
            {
                "run_id": entry.name,
                "created": request.get("created") or _created_at(entry),
                "status": _status_of(entry, last),
                "prompt": prompt or "",
                "teams": request.get("teams"),
                "rounds": request.get("rounds"),
                "reference": request.get("reference"),
                "last_kind": last.kind if last else None,
                "last_ts": last.ts if last else None,
                "events": last.seq if last else 0,
                "live": _process_alive(entry.name),
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
    reference = _validated_reference(body.reference, config.references_dir)

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
                "reference": reference,
                "created": datetime.now(timezone.utc).isoformat(),
                "model": config.model,
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    _launch(run_id, run_dir, prompt, body.teams, body.rounds, reference)
    return {"run_id": run_id}


def _validated_reference(reference: str | None, references_dir: Path) -> str | None:
    """Accept only a plain filename that actually exists under references/."""
    if not reference:
        return None
    name = reference.strip()
    if not name:
        return None
    if Path(name).name != name or name.startswith("."):
        raise HTTPException(status_code=400, detail="Reference must be a bare filename.")
    if not (references_dir / name).is_file():
        raise HTTPException(status_code=400, detail=f"No such reference: {name}")
    return name


def _launch(
    run_id: str,
    run_dir: Path,
    prompt: str,
    teams: int,
    rounds: int,
    reference: str | None,
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
    if reference:
        command += ["--reference", reference]

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
    last = _last_event(run_dir)
    if last is not None and last.kind in TERMINAL_KINDS:
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
    if proc is None or proc.poll() is not None:
        return {
            "run_id": run_id,
            "cancelled": False,
            "detail": "No live process here. The run has finished, or it was started elsewhere.",
            "status": _status_of(run_dir, _last_event(run_dir)),
        }
    with _PROCESS_LOCK:
        _CANCELLED.add(run_id)
    try:
        # The child is its own process group leader (start_new_session), so this
        # also stops the composer programs it has spawned.
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        proc.terminate()
    return {"run_id": run_id, "cancelled": True, "detail": "Termination signalled."}


@app.get("/api/runs/{run_id}/status")
def run_status(run_id: str) -> dict[str, Any]:
    run_dir = _run_dir(run_id)
    last = _last_event(run_dir)
    return {
        "run_id": run_id,
        "status": _status_of(run_dir, last),
        "live": _process_alive(run_id),
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
    target = (run_dir / path).resolve()
    if target != run_dir and run_dir not in target.parents:
        raise HTTPException(status_code=403, detail="Path escapes the run directory.")
    if not target.is_file():
        raise HTTPException(status_code=404, detail="No such file in this run.")
    return FileResponse(
        target,
        media_type=CONTENT_TYPES.get(target.suffix.lower(), "application/octet-stream"),
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
