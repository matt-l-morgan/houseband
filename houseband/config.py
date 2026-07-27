"""Runtime configuration.

Resolution order for every setting: explicit argument, then environment
variable, then ``config.toml`` in the repo root, then a built-in default.

Nothing here reads or stores a credential. The Anthropic SDK's zero-argument
constructor already resolves ``ANTHROPIC_API_KEY``, ``ANTHROPIC_AUTH_TOKEN``,
and ``ant auth login`` profiles on its own; the web server passes a
user-supplied key to its child process via the environment and never writes it
anywhere. See :func:`credential_source` for the read-only check used to give
users a clear error instead of a stack trace.
"""

from __future__ import annotations

import os
import shutil
import tomllib
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Model defaults. Kept in config rather than inline so that swapping models (or
# later, providers) is a one-line change instead of a grep.
#
# Sonnet 5 is the default because a run makes a lot of calls: three composers
# iterating, then nine rubric dimensions per candidate with three of them
# sampled three times, then a both-orders pairwise tournament, then coaching. At
# Opus 5 rates ($5/$25 per MTok against Sonnet 5's $3/$15) a three-round run gets
# expensive enough to discourage the repeated runs this system is supposed to
# invite. Override with HOUSEBAND_MODEL or config.toml when a run matters more
# than its cost.
DEFAULT_MODEL = "claude-sonnet-5"
COMPOSER_EFFORT = "xhigh"
JUDGE_EFFORT = "high"

# Both Sonnet 5 and the Opus 5 family run adaptive thinking by default, and
# thinking counts against max_tokens, so composers need real headroom or they
# truncate mid-program. Composer calls stream for that reason; judge calls stay
# under the SDK's non-streaming duration guard (which trips around 21k).
#
# 64k was not enough. At xhigh effort a composer can spend the entire budget
# reasoning (one drafted a whole program inside its thinking block) and get cut
# off before emitting the tool call. The loop now retries a truncated turn, but
# giving it room in the first place is the cheaper fix. Both models support 128k
# output, so this leaves headroom rather than sitting at the ceiling.
COMPOSER_MAX_TOKENS = 96_000
JUDGE_MAX_TOKENS = 16_000

# Judge dimensions that drive the learning loop get sampled repeatedly and the
# median taken, because a single LLM score is noisy enough to teach the coach
# something untrue.
#
# These are the two highest-weighted dimensions plus melody. Groove and loop
# usability decide whether a producer keeps the clip, so noise there is the most
# expensive kind: it is what the coach writes rules about. This list previously
# named form_arrangement, which no longer exists as a dimension, so the sampling
# was silently applying to one dimension instead of three.
MEDIAN_SAMPLED_DIMENSIONS = ("rhythm_groove", "loop_usability", "melody")
MEDIAN_SAMPLES = 3


@cache
def _toml() -> dict:
    path = REPO_ROOT / "config.toml"
    if not path.exists():
        return {}
    try:
        return tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def _setting(env: str, toml_key: str, default):
    if (value := os.environ.get(env)) is not None:
        return value
    return _toml().get(toml_key, default)


# ---------------------------------------------------------------------------
# Soundfont discovery
# ---------------------------------------------------------------------------

# FluidSynth bundles a 307K freeware bank (VintageDreamsWaves, Ian Wilson 1996,
# redistributable with its notice). It is a synth-waveform set for AWE cards
# rather than a real acoustic GM bank, so it sounds dated -- but it means a
# fresh clone can render audio with zero network access. scripts/fetch_soundfont.py
# installs something modern; this is only the floor.
_BUNDLED_GLOBS = (
    "/opt/homebrew/Cellar/fluid-synth/*/share/fluid-synth/sf2/VintageDreamsWaves-v2.sf2",
    "/usr/local/Cellar/fluid-synth/*/share/fluid-synth/sf2/VintageDreamsWaves-v2.sf2",
    "/usr/share/sounds/sf2/*.sf2",
    "/usr/share/soundfonts/*.sf2",
    "/usr/share/soundfonts/*.sf3",
)

PREFERRED_SOUNDFONT_NAMES = (
    "MuseScore_General.sf3",
    "MuseScore_General.sf2",
    "FluidR3_GM.sf2",
    "GeneralUser-GS.sf2",
)


def find_soundfont() -> Path | None:
    """Locate a soundfont, preferring a modern bank over the bundled fallback.

    Order: ``HOUSEBAND_SF2`` env or ``config.toml``, then anything
    ``scripts/fetch_soundfont.py`` has installed under ``soundfonts/``, then
    whatever FluidSynth or the distro bundled.
    """
    explicit = _setting("HOUSEBAND_SF2", "soundfont", None)
    if explicit:
        path = Path(explicit).expanduser()
        return path if path.exists() else None

    local = REPO_ROOT / "soundfonts"
    if local.is_dir():
        for name in PREFERRED_SOUNDFONT_NAMES:
            if (candidate := local / name).exists():
                return candidate
        for pattern in ("*.sf3", "*.sf2"):
            found = sorted(local.glob(pattern))
            if found:
                return found[0]

    from glob import glob

    for pattern in _BUNDLED_GLOBS:
        matches = sorted(glob(pattern))
        if matches:
            return Path(matches[-1])
    return None


def credential_source() -> str | None:
    """Name the credential the SDK will use, without reading its value.

    Returns a human-readable source name, or ``None`` if the SDK would have
    nothing to authenticate with. Used to fail fast with a useful message.
    """
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "ANTHROPIC_API_KEY"
    if os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return "ANTHROPIC_AUTH_TOKEN"
    config_dir = Path(
        os.environ.get("ANTHROPIC_CONFIG_DIR", Path.home() / ".config" / "anthropic")
    )
    if (config_dir / "credentials").is_dir() and any(
        (config_dir / "credentials").glob("*.json")
    ):
        profile = os.environ.get("ANTHROPIC_PROFILE", "active")
        return f"ant auth profile ({profile})"
    return None


# ---------------------------------------------------------------------------
# The snippet profile
# ---------------------------------------------------------------------------

# This tool makes one thing: a short loopable clip a producer imports into a DAW
# and builds on. Sixteen bars of 4/4 is about 30 seconds at 128bpm, 22 at
# drum-and-bass tempo and 43 at 90, so bar count is the knob rather than seconds.
# Bars are what a composer and a DAW both think in, and pinning seconds would
# force awkward tempos.
#
# Speed is a feature. Someone auditioning ideas will not wait six minutes per
# take, so effort is medium and turns are capped low. That is a real trade: fewer
# turns means fewer chances to apply a playbook rule, so the loop learns more
# slowly per round and makes up for it by making rounds cheap enough to run many.

# The tempi used to quote a clip's length as seconds. Drum and bass sits near 174
# and boom-bap near 90, which brackets nearly everything anyone asks for, so one
# figure would be wrong for most genres. Kept here rather than inline so the
# composer prompt, the UI picker and the docs all quote the same range. Public
# because the server reads them to label the clip-length picker.
TEMPO_FAST = 174
TEMPO_SLOW = 90


@dataclass(frozen=True)
class SnippetProfile:
    """How long a clip is, and how much thinking to spend getting there."""

    bars: int = 16
    effort: str = "medium"
    max_turns: int = 3
    # Thinking counts against this and a truncated turn costs a whole retry, so
    # there is deliberate headroom above what the program itself needs.
    max_tokens: int = 48_000

    def target_seconds(self, bpm: float, beats_per_bar: int = 4) -> float:
        """How long ``bars`` actually runs at a given tempo."""
        return self.bars * beats_per_bar * 60.0 / bpm if bpm else 0.0

    @property
    def approx_seconds(self) -> str:
        return (
            f"about {self.target_seconds(TEMPO_FAST):.0f} to "
            f"{self.target_seconds(TEMPO_SLOW):.0f} seconds depending on tempo"
        )

    def length_instruction(self) -> str:
        return (
            f"Write exactly {self.bars} bars. The material must loop: bar "
            f"{self.bars - 1} has to lead back into bar 0 without a seam, and "
            f"nothing may sound past the end of bar {self.bars - 1} except a short "
            "release tail."
            # Named as a consequence of the tempo the composer chooses, not as a
            # second target. Stating a seconds figure as a goal invites padding
            # the bar count or slowing the tempo to hit it, and the bar count is
            # the thing the DAW grid and the loop points actually depend on.
            f"\n\nAt {TEMPO_FAST} that runs about "
            f"{self.target_seconds(TEMPO_FAST):.0f} seconds, and at "
            f"{TEMPO_SLOW} about {self.target_seconds(TEMPO_SLOW):.0f}. "
            "Choose the tempo the music wants; the bar count is what is fixed."
        )


def profile_for(bars: int | None = None) -> SnippetProfile:
    """The snippet profile, optionally with a different bar count."""
    return SnippetProfile(bars=bars) if bars else SnippetProfile()


@dataclass
class Config:
    """Everything a run needs to know that is not part of the creative brief."""

    model: str = field(default_factory=lambda: str(_setting("HOUSEBAND_MODEL", "model", DEFAULT_MODEL)))
    soundfont: Path | None = field(default_factory=find_soundfont)
    fluidsynth: str | None = field(default_factory=lambda: shutil.which("fluidsynth"))

    runs_dir: Path = field(default_factory=lambda: REPO_ROOT / "runs")
    playbooks_dir: Path = field(default_factory=lambda: REPO_ROOT / "playbooks")

    # Per-round output-token ceiling. A runaway composer loop is the realistic
    # way a user burns real money on their own key, so the loop halts the round
    # rather than trusting every agent to behave.
    round_token_budget: int = field(
        default_factory=lambda: int(_setting("HOUSEBAND_ROUND_BUDGET", "round_token_budget", 400_000))
    )

    # Hard stop on executing a composer's program.
    program_timeout_s: float = field(
        default_factory=lambda: float(_setting("HOUSEBAND_PROGRAM_TIMEOUT", "program_timeout_s", 30.0))
    )

    def require_render_deps(self) -> None:
        """Raise a clear error if audio rendering cannot possibly work."""
        if self.fluidsynth is None:
            raise RuntimeError(
                "fluidsynth not found on PATH. Install it with "
                "'brew install fluid-synth' (macOS) or "
                "'apt-get install fluidsynth' (Debian/Ubuntu)."
            )
        if self.soundfont is None:
            raise RuntimeError(
                "No soundfont found. Run 'python scripts/fetch_soundfont.py' or "
                "set HOUSEBAND_SF2 to a .sf2/.sf3 file."
            )

    def describe(self) -> str:
        return "\n".join(
            [
                f"model         {self.model}",
                f"fluidsynth    {self.fluidsynth or 'MISSING'}",
                f"soundfont     {self.soundfont or 'MISSING'}",
                f"credential    {credential_source() or 'MISSING'}",
                f"round budget  {self.round_token_budget:,} output tokens",
            ]
        )


def load() -> Config:
    return Config()
