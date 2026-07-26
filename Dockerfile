# The deploy story: build this, run it on any container host, and you have the
# web app. Nothing else to install and no manual setup step inside the image.
#
# 3.13 rather than 3.14 on purpose. The code is 3.11+ compatible and runs on
# 3.14 locally, but a container build is where a missing wheel turns into a
# from-source compile of numpy or matplotlib, and 3.13 has settled wheels for
# everything here.
FROM python:3.13-slim

# PYTHONDONTWRITEBYTECODE: the app runs read-only-ish as a non-root user, so
#   .pyc files would only ever be write attempts that fail or bloat layers.
# PYTHONUNBUFFERED: the event log is the product. Buffered stdout means a
#   container's logs lag behind the run they are describing.
# MPLBACKEND: matplotlib has no display here, and the default backend probe is a
#   slow, noisy way to discover that.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    MPLBACKEND=Agg \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# fluidsynth is the audio renderer, invoked as a subprocess. Its package name is
# `fluidsynth` on Debian, not `fluid-synth` as on Homebrew.
#
# matplotlib needs no system packages beyond what the slim image has once it
# installs from wheels, but it does need fonts to draw axis labels, and slim
# ships none: without fonts-dejavu-core every piano roll renders with boxes
# where the bar numbers should be, which quietly breaks a judge input.
#
# ca-certificates is for the soundfont fetch over HTTPS below.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        fluidsynth \
        fonts-dejavu-core \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies before source, so editing a Python file does not re-resolve pip.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# The soundfont, at build time, so a deployed container needs no manual step.
# Only the fetch script is copied first: the bank is 40MB and the download is the
# slowest layer in the build, so it should not be invalidated by a source edit.
# The script imports nothing outside the standard library, which is what lets it
# run this early.
COPY scripts/fetch_soundfont.py scripts/
RUN python scripts/fetch_soundfont.py

COPY . .

# A build that produces an image which cannot render is a build that should have
# failed. This exercises the whole path -- executing a program in a subprocess,
# FluidSynth, the soundfont, matplotlib and its fonts -- rather than only
# checking that imports resolve, because every one of those is a thing that
# works on the author's Mac and not necessarily in a slim Debian image.
RUN python -c "\
from pathlib import Path; \
from houseband import render; \
r = render.execute_program(Path('examples/good_program.py').read_text(), Path('/tmp/build-check')); \
assert r.ok, r.feedback(); \
a = render.render_all(r.midi_path, Path('/tmp/build-check'), r.sidecar_path); \
assert a.audio and a.audio.stat().st_size > 100_000, f'no audio: {a.audio_error}'; \
assert a.piano_roll.stat().st_size > 10_000, 'piano roll looks empty'; \
print(f'render OK: {a.audio.name} {a.audio.stat().st_size // 1000}KB, {a.piano_roll.name}')" \
    && rm -rf /tmp/build-check

# Non-root. The container is the real isolation boundary for executing
# model-written composer programs (see docs/security.md), and that argument is
# considerably weaker if the program runs as root.
#
# runs/ is created and chowned here rather than left to the app, because a
# non-root process cannot create it inside a root-owned /app at runtime.
RUN useradd --create-home --uid 10001 houseband \
    && mkdir -p /app/runs /app/playbooks \
    && chown -R houseband:houseband /app/runs /app/playbooks /app/houseband

USER houseband

# No credential is baked in. Deliberately: this image is meant to be shareable,
# and a key in a layer is a key in every copy of it. Supply one at runtime with
# `-e ANTHROPIC_API_KEY=...`, or leave it unset and paste a key into the web UI,
# where it lives in server process memory only.
#
# HOUSEBAND_SF2 is not set either: config.find_soundfont() discovers
# /app/soundfonts/MuseScore_General.sf3 on its own, and hardcoding it here would
# defeat mounting a different bank over that directory.

EXPOSE 8000

# No HEALTHCHECK. A liveness probe here would only confirm that uvicorn is
# accepting connections, which tells an operator nothing useful about a
# single-user app whose real failure modes are a missing credential and a
# runaway round. The build-time render check above is the assertion worth making.

# One worker on purpose. The server holds a user-supplied credential in process
# memory, and a second worker would be a second process that does not have it.
# This is a single-user local app that happens to be deployable, not a service.
CMD ["uvicorn", "houseband.server:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
