"""The gate that decides whether the panel's signal is worth learning from.

The whole system rests on an assumption: that the judges can tell good music
from bad. Everything downstream compounds it. The coach distils findings into
playbook rules, composers read the playbook next round, and their scores feed
the coach again. If the panel is wrong, that loop does not fail loudly, it
converges confidently on nonsense.

So we check the assumption directly, using the one candidate whose quality we
already know. The human reference is scored by the same panel with the same
blind prompt as every agent submission, and it should win on the dimensions
where the gap between a professional arrangement and a program is largest and
least arguable: form and arrangement, melody, and harmony and voice leading.
Those three are the structural dimensions. Production is excluded because a
General MIDI render can genuinely beat a reference on velocity shaping, and
originality is excluded because it is the one dimension where an agent can
legitimately out-score a human on a given day.

An agent strictly out-scoring the reference on a structural dimension is not
proof the judge is broken, but it is the strongest cheap evidence available, and
it should stop a run rather than be buried in a report. Hence :attr:`
CalibrationReport.ok` and a summary written to be read.

The report also carries the panel's own noise floor: the mean per-dimension
spread across sampled candidates. A dimension whose samples routinely disagree
by three points cannot support a one-point conclusion, and knowing that number
is what stops the coach from writing a rule off a difference that is smaller
than the measurement error.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from houseband.types import DIMENSION_TITLES, CandidateVerdict

# Where a human arrangement should beat a program, and where "better" is least
# a matter of taste. See the module docstring for why production and originality
# are not here.
STRUCTURAL_DIMENSIONS: tuple[str, ...] = (
    "form_arrangement",
    "melody",
    "harmony_voice_leading",
)


class Breach(BaseModel):
    """One agent candidate out-scoring the reference on a structural dimension."""

    dimension: str
    candidate_id: str
    team: str
    candidate_score: int
    reference_score: int

    @property
    def margin(self) -> int:
        return self.candidate_score - self.reference_score

    def describe(self) -> str:
        title = DIMENSION_TITLES.get(self.dimension, self.dimension)
        return (
            f"{title}: {self.team} ({self.candidate_id}) scored "
            f"{self.candidate_score} against the reference's "
            f"{self.reference_score}, a margin of +{self.margin}"
        )


class CalibrationReport(BaseModel):
    """Whether the panel's output is trustworthy enough to learn from."""

    ok: bool = Field(description="False if any agent beat the reference structurally.")
    has_reference: bool = Field(
        description="False if no candidate was flagged is_reference, which makes "
        "the calibration check vacuous rather than passing."
    )
    reference_id: str = ""
    agent_count: int = 0
    breaches: list[Breach] = Field(default_factory=list)
    ties: list[Breach] = Field(
        default_factory=list,
        description="Agents that equalled the reference. Not a failure, but the "
        "margin is the thing to watch across rounds.",
    )
    # dimension -> mean spread across candidates that were sampled more than once
    noise_floor: dict[str, float] = Field(default_factory=dict)
    reference_structural: dict[str, int] = Field(default_factory=dict)

    @property
    def mean_spread(self) -> float:
        """Mean spread across every sampled dimension. The panel's noise floor."""
        if not self.noise_floor:
            return 0.0
        return sum(self.noise_floor.values()) / len(self.noise_floor)

    def summary(self) -> str:
        """A report meant to be read, and loud when it needs to be."""
        lines: list[str] = []

        if not self.has_reference:
            lines.append(
                "CALIBRATION NOT CHECKED: no candidate was marked as the reference, "
                "so there is nothing to calibrate against. Any conclusion drawn "
                "from this round's scores is unverified."
            )
        elif self.ok:
            lines.append(
                f"Calibration OK: the reference ({self.reference_id}) out-scored all "
                f"{self.agent_count} agent submissions on "
                + ", ".join(
                    DIMENSION_TITLES.get(d, d) for d in STRUCTURAL_DIMENSIONS
                )
                + "."
            )
            if self.ties:
                lines.append(
                    f"{len(self.ties)} tie(s) with the reference, worth watching:"
                )
                lines += [f"  - {t.describe()}" for t in self.ties]
        else:
            lines.append("*** CALIBRATION FAILED ***")
            lines.append(
                f"{len(self.breaches)} agent score(s) beat the human reference "
                f"({self.reference_id}) on a structural dimension. The reference is "
                "judged blind by the same panel with the same prompt, so this is "
                "most likely a miscalibrated judge rather than a superhuman "
                "composer."
            )
            lines += [f"  - {b.describe()}" for b in self.breaches]
            lines.append(
                "Do not train the coach on this round. Check the anchored scales in "
                "houseband/judges/rubrics/ (are the 8 and 10 anchors reachable by a "
                "loop?), check that the reference actually rendered and its score "
                "text is not truncated, and re-run before treating any of this "
                "round's findings as signal."
            )

        if self.reference_structural:
            lines.append(
                "Reference structural scores: "
                + ", ".join(
                    f"{DIMENSION_TITLES.get(d, d)} {s}"
                    for d, s in self.reference_structural.items()
                )
            )

        if self.noise_floor:
            lines.append(
                f"Observed noise floor: mean sampled spread {self.mean_spread:.2f} "
                "points across "
                + ", ".join(
                    f"{DIMENSION_TITLES.get(d, d)} {v:.2f}"
                    for d, v in self.noise_floor.items()
                )
                + "."
            )
            if self.mean_spread >= 2.0:
                lines.append(
                    "That spread is wide enough that differences of two points or "
                    "fewer between candidates are not distinguishable from judge "
                    "noise. Treat rankings inside that band as ties."
                )
        else:
            lines.append(
                "No sampled dimensions in this round, so the panel's noise floor is "
                "unmeasured."
            )

        return "\n".join(lines)


def check_calibration(verdicts: dict[str, CandidateVerdict]) -> CalibrationReport:
    """Decide whether this round's scores can be believed.

    ``verdicts`` is keyed by ``candidate_id``, as returned by
    :func:`houseband.judges.rubric.run_panel`.
    """
    reference = next((v for v in verdicts.values() if v.is_reference), None)
    agents = [v for v in verdicts.values() if not v.is_reference]

    noise_floor = _noise_floor(verdicts)

    if reference is None:
        return CalibrationReport(
            ok=False,
            has_reference=False,
            agent_count=len(agents),
            noise_floor=noise_floor,
        )

    reference_scores = reference.by_dimension()
    reference_structural = {
        d: reference_scores[d].score
        for d in STRUCTURAL_DIMENSIONS
        if d in reference_scores
    }

    breaches: list[Breach] = []
    ties: list[Breach] = []
    # Iterate dimensions outer and candidates inner (sorted) so the report reads
    # in a stable order regardless of dict insertion order.
    for dimension in STRUCTURAL_DIMENSIONS:
        reference_score = reference_structural.get(dimension)
        if reference_score is None:
            continue
        for agent in sorted(agents, key=lambda v: v.candidate_id):
            agent_score = agent.score(dimension)
            if agent_score is None:
                continue
            breach = Breach(
                dimension=dimension,
                candidate_id=agent.candidate_id,
                team=agent.team,
                candidate_score=agent_score,
                reference_score=reference_score,
            )
            if agent_score > reference_score:
                breaches.append(breach)
            elif agent_score == reference_score:
                ties.append(breach)

    return CalibrationReport(
        ok=not breaches,
        has_reference=True,
        reference_id=reference.candidate_id,
        agent_count=len(agents),
        breaches=breaches,
        ties=ties,
        noise_floor=noise_floor,
        reference_structural=reference_structural,
    )


def _noise_floor(verdicts: dict[str, CandidateVerdict]) -> dict[str, float]:
    """Mean per-dimension spread across candidates that were sampled.

    Only dimensions that were actually sampled more than once contribute: an
    unsampled dimension has a spread of zero by definition, and averaging those
    zeros in would report a noise floor far lower than the one we measured.
    """
    spreads: dict[str, list[int]] = {}
    for verdict in verdicts.values():
        for scored in verdict.dimensions:
            if len(scored.samples) > 1:
                spreads.setdefault(scored.dimension, []).append(scored.spread)
    return {
        dimension: sum(values) / len(values)
        for dimension, values in sorted(spreads.items())
    }
