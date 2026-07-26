#!/usr/bin/env bash
#
# Fresh clone to a working render, in one command.
#
# Idempotent: safe to run on an existing checkout with a populated venv and an
# installed soundfont. Every step checks before it acts.
#
# The last thing it does is actually render examples/good_program.py. That is the
# point of the script. A setup that reports success without producing audio is a
# setup that has told you nothing, because every interesting failure here --
# missing FluidSynth, missing soundfont, a matplotlib backend that needs a
# display -- only shows up at render time.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

VENV="$REPO_ROOT/.venv"
PY="$VENV/bin/python"
MIN_PYTHON="3.11"

# Colour only when attached to a terminal, so piping to a file or a CI log does
# not produce escape-code soup.
if [ -t 1 ]; then
    BOLD=$'\033[1m'; GREEN=$'\033[32m'; RED=$'\033[31m'; YELLOW=$'\033[33m'; OFF=$'\033[0m'
else
    BOLD=''; GREEN=''; RED=''; YELLOW=''; OFF=''
fi

step() { printf '\n%s==> %s%s\n' "$BOLD" "$1" "$OFF"; }
ok()   { printf '    %sok%s   %s\n' "$GREEN" "$OFF" "$1"; }
warn() { printf '    %swarn%s %s\n' "$YELLOW" "$OFF" "$1"; }
die()  { printf '\n%sFAILED%s %s\n\n' "$RED" "$OFF" "$1" >&2; exit 1; }

# ---------------------------------------------------------------------------
# 1. Python
# ---------------------------------------------------------------------------

step "Checking Python"

find_python() {
    # Newest first. A user with 3.11 and 3.13 installed should get 3.13.
    for candidate in python3.14 python3.13 python3.12 python3.11 python3; do
        if command -v "$candidate" >/dev/null 2>&1; then
            if "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
                command -v "$candidate"
                return 0
            fi
        fi
    done
    return 1
}

if [ -x "$PY" ]; then
    ok "existing venv at .venv ($("$PY" --version 2>&1))"
else
    SYSTEM_PYTHON="$(find_python)" || die \
"No Python $MIN_PYTHON or newer found on PATH.
  macOS:         brew install python@3.13
  Debian/Ubuntu: apt-get install python3 python3-venv"
    ok "found $SYSTEM_PYTHON ($("$SYSTEM_PYTHON" --version 2>&1))"

    step "Creating virtualenv at .venv"
    "$SYSTEM_PYTHON" -m venv "$VENV" || die \
"Could not create a virtualenv. On Debian/Ubuntu this usually means the
  python3-venv package is missing: apt-get install python3-venv"
    ok "created"
fi

# ---------------------------------------------------------------------------
# 2. Python dependencies
# ---------------------------------------------------------------------------

step "Installing Python dependencies"

# Quiet unless something goes wrong: on a warm venv this is a no-op and the
# resolver's output is pure noise, but on a failure it is the only useful
# information there is.
if PIP_LOG="$("$PY" -m pip install --quiet --upgrade pip 2>&1 && \
              "$PY" -m pip install --quiet -r requirements.txt 2>&1)"; then
    ok "requirements.txt satisfied"
else
    printf '%s\n' "$PIP_LOG" >&2
    die "pip install failed. See the output above."
fi

# ---------------------------------------------------------------------------
# 3. FluidSynth
# ---------------------------------------------------------------------------

step "Checking FluidSynth"

if command -v fluidsynth >/dev/null 2>&1; then
    ok "$(command -v fluidsynth) ($(fluidsynth --version 2>&1 | head -1))"
else
    # Deliberately instruct rather than install. Installing a system package
    # without being asked is the kind of thing a setup script should not do on
    # someone else's machine.
    case "$(uname -s)" in
        Darwin)
            HINT="brew install fluid-synth"
            if ! command -v brew >/dev/null 2>&1; then
                HINT="install Homebrew from https://brew.sh, then: brew install fluid-synth"
            fi
            ;;
        Linux)
            # The package is 'fluidsynth' on Debian/Ubuntu; note the difference
            # from Homebrew's 'fluid-synth', which trips people up.
            if command -v apt-get >/dev/null 2>&1; then
                HINT="sudo apt-get install -y fluidsynth"
            elif command -v dnf >/dev/null 2>&1; then
                HINT="sudo dnf install -y fluidsynth"
            elif command -v pacman >/dev/null 2>&1; then
                HINT="sudo pacman -S fluidsynth"
            else
                HINT="install the 'fluidsynth' package with your distribution's package manager"
            fi
            ;;
        *)
            HINT="install FluidSynth 2.x for your platform: https://www.fluidsynth.org"
            ;;
    esac
    die "FluidSynth is not on PATH. Install it and re-run this script:

    $HINT"
fi

# ---------------------------------------------------------------------------
# 4. Soundfont
# ---------------------------------------------------------------------------

step "Installing a soundfont"

# The fetch script prints its license to stderr and the installed path to stdout,
# and is itself idempotent, so this is a plain call rather than a presence check.
if ! "$PY" scripts/fetch_soundfont.py >/dev/null; then
    warn "soundfont download failed."
    warn "houseband will fall back to the 307K bank bundled with FluidSynth,"
    warn "which renders but sounds dated. Re-run scripts/fetch_soundfont.py when"
    warn "you have network access, or set HOUSEBAND_SF2 to a bank you already have."
fi

SOUNDFONT="$(PYTHONPATH=. "$PY" -c 'from houseband import config; print(config.find_soundfont() or "")')"
[ -n "$SOUNDFONT" ] || die \
"No soundfont found even after fetching. Set HOUSEBAND_SF2 to a .sf2/.sf3 file."
ok "using $SOUNDFONT"

# ---------------------------------------------------------------------------
# 5. Verify by rendering
# ---------------------------------------------------------------------------

step "Rendering examples/good_program.py"

OUT_DIR="$REPO_ROOT/runs/setup-check"
rm -rf "$OUT_DIR"

PYTHONPATH="$REPO_ROOT" "$PY" - "$OUT_DIR" <<'PYCODE' || die \
"The render check failed. The output above says which stage broke:
  program execution -> a bug in houseband.house or the example
  audio             -> FluidSynth or the soundfont
  piano roll        -> matplotlib"
import sys
from pathlib import Path

from houseband import render, validator

out_dir = Path(sys.argv[1])
code = Path("examples/good_program.py").read_text()

result = render.execute_program(code, out_dir)
if not result.ok:
    print(result.feedback(), file=sys.stderr)
    raise SystemExit(1)
print(f"    ok   program executed -> {result.midi_path}")

report = validator.validate_score(result.midi_path, result.sidecar_path)
print(
    f"    ok   validator: {report.track_count} tracks, {report.note_count} notes, "
    f"{report.duration:.0f}s"
)
if not report.ok:
    print(report.feedback(), file=sys.stderr)
    raise SystemExit(1)

artifacts = render.render_all(result.midi_path, out_dir, result.sidecar_path)
print(f"    ok   piano roll -> {artifacts.piano_roll}")
if artifacts.audio is None:
    print(f"    FAIL audio: {artifacts.audio_error}", file=sys.stderr)
    raise SystemExit(1)
size_kb = artifacts.audio.stat().st_size // 1024
print(f"    ok   audio -> {artifacts.audio} ({size_kb} KB)")
PYCODE

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------

printf '\n%s%s Setup complete.%s\n' "$BOLD" "$GREEN" "$OFF"

PYTHONPATH="$REPO_ROOT" "$PY" -c '
from houseband import config
print()
print(config.load().describe())
'

cat <<EOF

Artifacts from the render check are in:
  runs/setup-check/

Next steps
----------
1. Give houseband a credential. It never writes one to disk or to the event log.

     export ANTHROPIC_API_KEY=sk-ant-...

   or, if you use OAuth profiles, nothing to export at all:

     ant auth login

   or leave it unset and paste a key into the web UI, where it stays in server
   process memory for the life of the process.

2. Start the web app and open it:

     PYTHONPATH=. .venv/bin/uvicorn houseband.server:app --reload --port 8000

   then http://127.0.0.1:8000

3. Or work headless. The CLI needs no browser and the event log at
   runs/<id>/events.jsonl is the complete record of a run.

Before deploying anywhere other than your own machine, read docs/security.md:
composer agents write Python and this system executes it.
EOF
