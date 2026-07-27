# Security

houseband asks a language model to write a Python program, and then it runs that program.
That is not a side effect of the design, it is the design: the argument for composers emitting code rather than raw MIDI is that code is the only artifact where a judge's lesson can become a reusable function.

On the author's own laptop, executing a model's Python is a shrug.
In a public repo that other people clone, and considerably more so in anything deployed, it is not.
This document says what the mitigations actually are, and where they stop.

## The short version

The mitigations in the code are real and they raise the cost of a mistake.
**They are not a sandbox.**
A determined adversarial program can defeat a static import allowlist.
If you are running this anywhere that matters, the container is your isolation boundary, not the validator.

Run deployed instances single-user.
Do not put one on the public internet with your own key configured.

## What executes, and where

`houseband/render.py::execute_program` is the only place model-written code runs.
Every composer turn goes through it, and it does five things before anything is executed.

**1. Static import allowlist, via AST.**
`houseband/validator.py::check_imports` parses the program with `ast.parse` and walks it.
It rejects, with a message the composer agent reads and retries against:

- Any `import` or `from ... import` outside `ALLOWED_IMPORTS`, which is `houseband`, `houseband.house*`, and the pure-computation half of the standard library: `math`, `random`, `itertools`, `functools`, `collections`, `dataclasses`, `typing`, `statistics`, `copy`, `enum`, `fractions`.
  Notably absent: `os`, `sys`, `subprocess`, `socket`, `pathlib`, `shutil`, `importlib`, `ctypes`, and everything else with a filesystem or network verb.
- Any reference to a name in `FORBIDDEN_NAMES`: `__import__`, `eval`, `exec`, `compile`, `open`, `globals`, `locals`, `vars`, `input`, `breakpoint`, `memoryview`.
  These are builtins with no legitimate use in a composition program and an obvious role in escaping one.
- Any dunder attribute access outside `{__name__, __doc__, __init__, __all__}`.
  This is the classic route out of a restricted namespace: `().__class__.__bases__[0].__subclasses__()` walks to arbitrary types, and `__builtins__` or `__globals__` on any function object hands back everything the allowlist removed.

The check runs *before* the file is executed, and a rejection means the subprocess never starts.

**2. A subprocess, not `exec`.**
The program runs as `subprocess.run([sys.executable, "program.py"])`.
Nothing the program does to its own interpreter state, and no exception or `sys.exit` it raises, can affect the orchestrator.

**3. A hard timeout.**
`config.program_timeout_s` (default 30s, settable via `HOUSEBAND_PROGRAM_TIMEOUT` or `config.toml`) is passed to `subprocess.run`, and a `TimeoutExpired` becomes an ordinary validation failure the composer is told about.
An infinite loop is the single most likely thing a composer writes by accident, and this makes it a wasted turn instead of a hung run.

**4. A scratch working directory.**
`cwd` is the candidate's own directory under `runs/<id>/`.
A program that writes a relative path writes there.
This is containment of accidents, not of intent: `cwd` constrains relative paths and nothing else.

**5. A deliberately minimal environment.**
The child gets exactly `PATH`, `HOME`, `PYTHONPATH`, `PYTHONDONTWRITEBYTECODE` and `MPLBACKEND`.
**No credential is passed.**
A program that goes looking for `ANTHROPIC_API_KEY` in its environment finds nothing, whether it went looking on purpose or because the model hallucinated an API call.

## Where this stops

Every one of the above is worth having, and none of them is a security boundary in the sense that word usually implies.

**A static allowlist is a static analysis.**
It sees the AST of the file as written.
`check_imports` blocks the well-known escapes, but "we blocked the escapes we thought of" is a different claim from "there is no escape", and only the second one is a sandbox.
The allowlisted modules are themselves a surface: `random` and `functools` are pure computation, but the `houseband.house` library is ordinary Python and anything reachable through its objects is reachable.

**The subprocess is a normal process.**
It runs as the same user, with the same filesystem permissions, on the same network.
Nothing stops it from reading your home directory, because `HOME` is in its environment and the operating system is the only thing enforcing anything.
There is no seccomp filter, no namespace, no resource limit beyond wall-clock time, and no filesystem restriction.

**The threat model that is actually covered** is a model that writes wrong code: infinite loops, imports of things that are not there, attempts to fetch a sample library off the internet, writes to paths it should not.
Those are what happen in practice and they are all handled.

**The threat model that is not covered** is a model that has been made adversarial, whether by prompt injection through a user-supplied brief or by a supply-chain problem upstream of the weights.
If that is your concern, the process boundary above is not enough and you should not rely on it.

## Deployment

**The container is the real isolation boundary.**
`Dockerfile` builds an image that runs as a non-root user (uid 10001) with no credential baked in and only `/app/runs` and `/app/playbooks` writable.
Add whatever your host offers on top:

```bash
docker run --rm -p 8000:8000 \
  -e ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY" \
  -v "$PWD/runs:/app/runs" \
  --memory 2g --pids-limit 256 \
  --cap-drop ALL --security-opt no-new-privileges \
  houseband
```

`--pids-limit` is the one worth adding deliberately: it closes the fork bomb, which the wall-clock timeout on `execute_program` does not.
`--memory` bounds the other unbounded resource, since nothing stops a composer program from allocating until the host swaps.

**`--read-only` is tempting and does not work here**, which is worth knowing before you try it.
The library-evolution half of the learning loop writes to `houseband/house/learned.py`, and the coach writes to `playbooks/`, both inside `/app`.
A fully read-only root filesystem disables the part of the system that converts feedback into capability.
If you want it anyway, run with `--read-only --tmpfs /tmp` and mount writable volumes over `/app/runs`, `/app/playbooks` and `/app/houseband/house`, and accept that a learned function then lives in a volume rather than in a file you can commit.

**Run it single-user.**
There is no authentication, no authorization, and no per-user isolation in the server, deliberately: it is a local app that happens to be deployable.
An instance on the public internet with your key configured is an open invitation to spend your money and to execute arbitrary Python inside your container.
If you want to share it, put it behind whatever your platform offers for access control and treat every user as having shell access to the container.

**Do not accept untrusted briefs.**
A user's prompt goes almost verbatim into a composer's system prompt, which makes it a prompt-injection surface into an agent whose one tool executes Python.

## Credentials

The design goal is that your key never leaves your machine and never lands in anything you might later paste into a bug report.

**Nothing in the repo reads or stores a credential.**
`houseband/config.py` deliberately holds no key.
The Anthropic SDK's zero-argument `Anthropic()` constructor already resolves `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, and `ant auth login` profiles, so the code never has to touch the value.
`config.credential_source()` reports *which* source the SDK will use, by name, without reading it: that is enough to render "configured" in the UI and to fail fast with a clear message instead of a stack trace.

**A key submitted through the web UI lives in process memory only.**
`houseband/server.py` holds it in one module-level dict.
`POST /api/credential` stores it and returns only `{"configured": true, "source": ...}` -- not the value, and not a prefix or suffix of it.
`GET /api/credential` returns the same shape.
`DELETE /api/credential` forgets it without a restart.
It is never written to disk.
It reaches the pipeline through the child process environment, which is the narrowest channel the SDK already knows how to read.

Two consequences worth being explicit about.
Restarting the server forgets a UI-submitted key, on purpose.
And "process memory only" means exactly that: a core dump or a debugger attached to the process would find it, and the environment of the child process is readable by the user who owns it.

**The event log is scrubbed on the way out, as a backstop.**
`houseband/events.py::scrub` runs over every event payload and message before it is written.
It redacts values under sensitive key names (`api_key`, `authorization`, `token`, `secret`, `credential`, and friends) wholesale, and pattern-matches key-shaped strings anywhere in the payload: `sk-ant-*`, generic `sk-*`, `Bearer *`, plus a few other providers' prefixes.

This is belt and braces.
The pipeline is not supposed to put a key in an event in the first place.
But "not supposed to" is not a guarantee, and `runs/<id>/events.jsonl` is exactly the file a user attaches to a bug report, so a leak there is not a recoverable mistake.

The guarantee is tested at the file level, not just asserted.
`tests/test_events.py::test_message_and_data_are_both_scrubbed` emits key-shaped material as both an event message and a payload field, then reads `events.jsonl` back off disk and fails if any of it survived.
`tests/test_integration.py::test_no_key_material_reaches_the_log` makes the same assertion across a whole run.
Both check the written bytes rather than the return value of `scrub`, which is the only version of that test worth having.

## Reporting something

If you find a way for a composer program to escape `check_imports`, that is worth a report, and it is worth fixing even though the allowlist was never claimed to be a sandbox: every escape closed is one fewer accident.
Open an issue, or if you would rather not do so publicly, contact the repository owner directly.
