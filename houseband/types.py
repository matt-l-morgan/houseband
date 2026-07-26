"""Shared data types.

These are the contracts between composers, judges, and the coach. They live in
one module because they are the seams where the parts of the system meet, and a
drifting definition at a seam is the expensive kind of bug.

The judge output schemas are also the JSON Schema handed to the Anthropic API for
structured outputs, so two constraints apply to every field here:

* **No tuples.** They compile to ``prefixItems``, which the structured-output
  schema subset rejects. A bar range is two ``int`` fields, not one tuple.
* **Nothing optional that we actually depend on.** A field the model may omit is
  a field we have to defend against everywhere downstream.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

# The four composer roles a finding can be attributed to. This routing is what
# lets a lesson land on the part of the process responsible for it instead of
# being broadcast to everything, which is how a playbook stays sharp.
Role = Literal["songwriter", "rhythm", "arranger", "mix"]
ROLES: tuple[Role, ...] = ("songwriter", "rhythm", "arranger", "mix")

Severity = Literal["minor", "moderate", "major"]

# The eight judged dimensions. Keys are stable identifiers used in filenames,
# event payloads, and playbook sections, so they must not change casually.
DIMENSIONS: tuple[str, ...] = (
    "prompt_adherence",
    "melody",
    "harmony_voice_leading",
    "rhythm_groove",
    "form_arrangement",
    "orchestration_register",
    "production",
    "originality",
)

DIMENSION_TITLES: dict[str, str] = {
    "prompt_adherence": "Prompt adherence",
    "melody": "Melody",
    "harmony_voice_leading": "Harmony and voice leading",
    "rhythm_groove": "Rhythm and groove",
    "form_arrangement": "Form and arrangement",
    "orchestration_register": "Orchestration and register",
    "production": "Production",
    "originality": "Originality",
}

# Weights for the composite score. Form and adherence carry the most because
# they are where machine-composed music most reliably falls down, and because a
# piece that ignores the brief is not redeemable by good voice leading.
DIMENSION_WEIGHTS: dict[str, float] = {
    "prompt_adherence": 1.5,
    "melody": 1.25,
    "harmony_voice_leading": 1.0,
    "rhythm_groove": 1.0,
    "form_arrangement": 1.5,
    "orchestration_register": 1.0,
    "production": 0.75,
    "originality": 1.0,
}


# ---------------------------------------------------------------------------
# Brief
# ---------------------------------------------------------------------------


class Brief(BaseModel):
    """The creative task, structured from the user's free-text prompt."""

    prompt: str
    genre: str = ""
    mood: str = ""
    tempo_hint: str = ""
    instrumentation: list[str] = Field(default_factory=list)
    target_length: str = ""
    structure_notes: str = ""

    def render(self) -> str:
        lines = [f"USER PROMPT: {self.prompt}"]
        for label, value in (
            ("Genre", self.genre),
            ("Mood", self.mood),
            ("Tempo", self.tempo_hint),
            ("Target length", self.target_length),
            ("Structure notes", self.structure_notes),
        ):
            if value:
                lines.append(f"{label}: {value}")
        if self.instrumentation:
            lines.append("Instrumentation: " + ", ".join(self.instrumentation))
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Candidates
# ---------------------------------------------------------------------------


@dataclass
class Candidate:
    """One submission, plus everything derived from it.

    ``candidate_id`` is what judges see. It is deliberately opaque (``c1``,
    ``c2``) rather than the team name, because judges must be blind to which
    team produced which piece, and the reference has to be indistinguishable
    from an agent's work for the calibration check to mean anything.
    """

    candidate_id: str
    team: str
    midi_path: Path
    sidecar_path: Path | None = None
    program_code: str = ""
    score_text: str = ""
    piano_roll: Path | None = None
    audio: Path | None = None
    is_reference: bool = False
    round: int = 0
    notes: str = ""

    @property
    def display_name(self) -> str:
        return "reference" if self.is_reference else self.team


# ---------------------------------------------------------------------------
# Judge output
# ---------------------------------------------------------------------------


class Finding(BaseModel):
    """One specific, located criticism.

    The anchors are required by prompt rather than by type (the model must be
    able to say "this is a whole-piece observation"), but a finding with no
    anchor and no track is close to useless to a composer, so the rubric prompts
    push hard for them and the coach weights anchored findings higher.
    """

    claim: str = Field(description="What is wrong, in one sentence.")
    bar_start: int | None = Field(
        default=None, description="First bar the claim applies to, 0-indexed."
    )
    bar_end: int | None = Field(
        default=None, description="Last bar the claim applies to, inclusive."
    )
    track: str | None = Field(default=None, description="Track name, if specific to one.")
    severity: Severity = Field(description="How much this hurts the piece.")
    suggested_revision: str = Field(
        description="A concrete change the composer could make. Not 'improve the melody'."
    )
    attributed_role: Role = Field(
        description=(
            "Which composer role is responsible: songwriter (melody, harmony, "
            "key, chords), rhythm (drums, bass, groove, timing), arranger "
            "(sections, form, instrumentation entries and exits, density), "
            "mix (patch choice, velocity, panning, register balance)."
        )
    )

    def anchor(self) -> str:
        if self.bar_start is not None and self.bar_end is not None:
            span = (
                f"bar {self.bar_start}"
                if self.bar_start == self.bar_end
                else f"bars {self.bar_start}-{self.bar_end}"
            )
        elif self.bar_start is not None:
            span = f"bar {self.bar_start}"
        else:
            span = "whole piece"
        return f"{span}, {self.track}" if self.track else span

    @property
    def is_anchored(self) -> bool:
        return self.bar_start is not None or self.track is not None


class DimensionVerdict(BaseModel):
    """One dimension's score for one candidate."""

    score: int = Field(ge=1, le=10, description="Against the anchored scale in the rubric.")
    rationale: str = Field(description="Two or three sentences justifying the score.")
    findings: list[Finding] = Field(default_factory=list)


class ScoredDimension(BaseModel):
    """A dimension verdict after sampling, carrying the spread we observed."""

    dimension: str
    score: int
    rationale: str
    findings: list[Finding] = Field(default_factory=list)
    samples: list[int] = Field(
        default_factory=list,
        description="All sampled scores. Spread here is the judge's own noise floor.",
    )

    @property
    def spread(self) -> int:
        return (max(self.samples) - min(self.samples)) if len(self.samples) > 1 else 0


class CandidateVerdict(BaseModel):
    """The full panel's read on one candidate."""

    candidate_id: str
    team: str
    is_reference: bool = False
    dimensions: list[ScoredDimension] = Field(default_factory=list)

    def by_dimension(self) -> dict[str, ScoredDimension]:
        return {d.dimension: d for d in self.dimensions}

    def score(self, dimension: str) -> int | None:
        found = self.by_dimension().get(dimension)
        return found.score if found else None

    @property
    def weighted_total(self) -> float:
        """Weighted mean across judged dimensions, on the same 1-10 scale."""
        total = weight_sum = 0.0
        for d in self.dimensions:
            weight = DIMENSION_WEIGHTS.get(d.dimension, 1.0)
            total += d.score * weight
            weight_sum += weight
        return total / weight_sum if weight_sum else 0.0

    def all_findings(self) -> list[Finding]:
        return [f for d in self.dimensions for f in d.findings]

    def findings_for_role(self, role: str) -> list[Finding]:
        return [f for f in self.all_findings() if f.attributed_role == role]


class PairwiseVerdict(BaseModel):
    """Which of two candidates is better, and why."""

    winner: Literal["A", "B", "tie"] = Field(
        description="Which candidate is stronger overall, or 'tie' if genuinely equal."
    )
    reason: str = Field(description="Two or three sentences. Cite bars where you can.")


# ---------------------------------------------------------------------------
# Coach output
# ---------------------------------------------------------------------------


class PlaybookRule(BaseModel):
    """A durable, falsifiable instruction distilled from findings.

    "Falsifiable" is the bar that keeps a playbook from filling with platitudes:
    a rule you could not tell whether a piece followed is not a rule, it is a
    mood. ``because`` is retained so a later round can tell whether the rule
    earned its slot.
    """

    role: Role
    rule: str = Field(
        description=(
            "One imperative sentence, specific enough that you could look at a "
            "score and say whether it was followed. Not 'write better melodies'."
        )
    )
    because: str = Field(description="The observed failure this responds to, citing bars.")


class StagedFunction(BaseModel):
    """A house-library helper the coach wants to add.

    Staged rather than committed: it is reviewed before it reaches the shared
    library, because every composer imports that library and a broken helper
    takes the whole round down.
    """

    name: str = Field(description="Function name, snake_case.")
    rationale: str = Field(description="Which recurring finding this makes unnecessary.")
    source: str = Field(description="Complete Python source, including docstring.")
    test_source: str = Field(description="A pytest test proving it does what it claims.")


@dataclass
class TeamState:
    """What a team carries between rounds."""

    name: str
    persona: str
    elo: float = 1200.0
    playbook_path: Path | None = None
    history: list[dict] = field(default_factory=list)
