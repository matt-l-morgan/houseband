"""The coach: turns judge findings into durable improvements.

Two layers, both fed by findings routed via ``attributed_role``:

**Prompt space.** Rules distilled into ``playbooks/<team>.md``, injected into that
team's next round. Playbooks are per *team*, not per role, because the teams
compete: a shared role-keyed playbook would leak one team's hard-won lesson to its
rivals and dissolve the Elo separation the run exists to demonstrate.

**Capability space.** When the same weakness survives multiple rounds of advice,
advice is not the answer, so the coach stages a house-library function instead. A
rule has to be re-read and re-applied every round; a function is permanent leverage
every composer inherits. Staged rather than committed, because every composer
imports that library and a broken helper takes the whole round down.

The interesting mechanism here is the **ledger**. A playbook that only ever grows
becomes a wall of platitudes that dilutes the prompt and makes the composer worse.
So every rule records the round it arrived and the scores observed while it was
active, and rules that do not correlate with improvement are deprecated. That is
also the honest test of whether this system works at all.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from houseband import config as cfg
from houseband.events import EventLog, Usage
from houseband.types import (
    ROLES,
    CandidateVerdict,
    Finding,
    PlaybookRule,
    Role,
    StagedFunction,
)

# Caps. A playbook past this size stops helping and starts crowding out the brief.
MAX_RULES_TOTAL = 12
MAX_RULES_PER_ROLE = 4

# A rule gets this many rounds to show it helps before it is judged on results.
GRACE_ROUNDS = 2


class CoachOutput(BaseModel):
    """What the coach decides after reading a round's findings."""

    rules: list[PlaybookRule] = Field(
        default_factory=list,
        description=(
            "New rules, at most three. Only add a rule for a problem that is both "
            "real and repeatable. Prefer fewer, sharper rules."
        ),
    )
    deprecate: list[str] = Field(
        default_factory=list,
        description=(
            "Verbatim text of existing rules that should be dropped: superseded, "
            "too vague to check, or contradicted by this round's findings."
        ),
    )
    staged_functions: list[StagedFunction] = Field(
        default_factory=list,
        description=(
            "At most one. Propose a house-library helper only when a weakness has "
            "survived multiple rounds of written advice, so the fix belongs in code "
            "rather than in another instruction."
        ),
    )


SYSTEM = """You are a composition coach. You read judge findings on one team's
piece and decide what that team should carry into the next round.

You are writing for a competent composer who will read your playbook before
writing. Your output changes what they do, so it has to be specific enough to act
on and specific enough to check.

## What makes a good rule

A rule must be falsifiable. Someone should be able to look at a score and say
whether it was followed. "Write more interesting melodies" fails this. "Give the
chorus melody a range of at least an octave and place its highest note in the
second half" passes.

A rule must be durable. It should apply to the next piece in this genre, not only
to the one just judged. Do not write "fix bar 34".

A rule must earn its slot. You have a hard cap, so a new rule has to be worth more
than the weakest existing one. Fewer, sharper rules beat a long list.

Prefer rules that address major and moderate findings that recur, and findings
that carry bar anchors (an anchored finding is evidence; an unanchored one is
often an impression).

## Deprecating

Drop rules that are vague, that repeat another rule, that the findings show were
followed and did not help, or that a new rule supersedes. Copy the text verbatim
into `deprecate` so it can be matched.

## Staging a function

Only when a weakness has persisted across rounds despite written advice. The test
is: could this be fixed once, in code, so nobody has to remember it again?

Mechanical velocity and dead-on-the-grid timing is the canonical example. A rule
saying "vary your velocities" has to be obeyed by hand every time. A function that
does it is obeyed once.

If you stage a function, it must be complete, working Python using only the
standard library and `houseband.house`, operating on a `Track` (whose `notes` is a
list of `(pitch, start_seconds, end_seconds, velocity)` tuples). Include a
docstring explaining when to use it, and a real pytest test.

Do not stage a function on the first round. There is no evidence yet that advice
was insufficient."""


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------


@dataclass
class LedgerEntry:
    rule: str
    role: str
    because: str
    added_round: int
    scores_while_active: list[float] = field(default_factory=list)
    baseline: float | None = None

    def to_dict(self) -> dict:
        return {
            "rule": self.rule,
            "role": self.role,
            "because": self.because,
            "added_round": self.added_round,
            "scores_while_active": self.scores_while_active,
            "baseline": self.baseline,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "LedgerEntry":
        return cls(
            rule=raw.get("rule", ""),
            role=raw.get("role", "songwriter"),
            because=raw.get("because", ""),
            added_round=int(raw.get("added_round", 0)),
            scores_while_active=list(raw.get("scores_while_active", [])),
            baseline=raw.get("baseline"),
        )

    @property
    def rounds_active(self) -> int:
        return len(self.scores_while_active)

    def verdict(self) -> Literal["earning", "unproven", "not earning"]:
        """Whether this rule looks like it is helping.

        A blunt instrument, and labelled as one: attributing a score change to a
        single rule among several is not something this evidence can really
        support. It is still much better than letting the playbook grow forever,
        which reliably makes things worse.
        """
        if self.rounds_active < GRACE_ROUNDS or self.baseline is None:
            return "unproven"
        mean = sum(self.scores_while_active) / len(self.scores_while_active)
        if mean > self.baseline + 0.15:
            return "earning"
        if mean < self.baseline - 0.15:
            return "not earning"
        return "unproven"


class Playbook:
    """A team's accumulated rules, plus the evidence for each."""

    def __init__(self, team: str, directory: Path):
        self.team = team
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.md_path = self.directory / f"{team}.md"
        self.ledger_path = self.directory / f"{team}.ledger.json"
        self.entries: list[LedgerEntry] = []
        self.deprecated: list[dict] = []
        self._load()

    def _load(self) -> None:
        if not self.ledger_path.exists():
            return
        try:
            raw = json.loads(self.ledger_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        self.entries = [LedgerEntry.from_dict(e) for e in raw.get("entries", [])]
        self.deprecated = list(raw.get("deprecated", []))

    def render(self) -> str:
        """The text injected into the composer's prompt."""
        if not self.entries:
            return ""
        lines = [
            "These are lessons from your own previous rounds. They were written",
            "because judges found specific problems in your work. Follow them.",
            "",
        ]
        for role in ROLES:
            role_entries = [e for e in self.entries if e.role == role]
            if not role_entries:
                continue
            lines.append(f"## {role}")
            for entry in role_entries:
                lines.append(f"- {entry.rule}")
                lines.append(f"  (round {entry.added_round}: {entry.because})")
            lines.append("")
        return "\n".join(lines).strip()

    def record_round(self, score: float) -> None:
        """Attribute this round's score to every currently active rule."""
        for entry in self.entries:
            entry.scores_while_active.append(score)

    def apply(self, output: CoachOutput, round: int, baseline: float | None) -> tuple[list[PlaybookRule], list[str]]:
        """Add and remove rules, enforcing the caps. Returns (added, removed)."""
        removed: list[str] = []

        # Explicit deprecations first, matched loosely so near-verbatim works.
        for text in output.deprecate:
            needle = text.strip().lower()[:60]
            for entry in list(self.entries):
                if needle and needle in entry.rule.strip().lower():
                    self.entries.remove(entry)
                    self.deprecated.append(
                        {**entry.to_dict(), "removed_round": round, "reason": "superseded"}
                    )
                    removed.append(entry.rule)
                    break

        added: list[PlaybookRule] = []
        for rule in output.rules[:3]:
            if any(rule.rule.strip().lower() == e.rule.strip().lower() for e in self.entries):
                continue
            self.entries.append(
                LedgerEntry(
                    rule=rule.rule,
                    role=rule.role,
                    because=rule.because,
                    added_round=round,
                    baseline=baseline,
                )
            )
            added.append(rule)

        # Enforce caps, dropping the least-supported rules first: those failing to
        # earn their slot, then the oldest unproven ones.
        def _drop(entry: LedgerEntry, reason: str) -> None:
            self.entries.remove(entry)
            self.deprecated.append(
                {**entry.to_dict(), "removed_round": round, "reason": reason}
            )
            removed.append(entry.rule)

        for role in ROLES:
            role_entries = [e for e in self.entries if e.role == role]
            while len(role_entries) > MAX_RULES_PER_ROLE:
                worst = min(
                    role_entries,
                    key=lambda e: (e.verdict() != "not earning", e.added_round),
                )
                _drop(worst, f"exceeded {MAX_RULES_PER_ROLE} rules for {role}")
                role_entries.remove(worst)

        while len(self.entries) > MAX_RULES_TOTAL:
            worst = min(
                self.entries,
                key=lambda e: (e.verdict() != "not earning", e.added_round),
            )
            _drop(worst, f"exceeded {MAX_RULES_TOTAL} rules total")

        return added, removed

    def save(self) -> None:
        header = f"# Playbook: {self.team}\n\n"
        body = self.render() or "(no rules yet)"
        stats = ["", "", "---", "", "## Rule status", ""]
        for entry in self.entries:
            stats.append(
                f"- `{entry.verdict()}` after {entry.rounds_active} round(s): "
                f"{entry.rule[:80]}"
            )
        if self.deprecated:
            stats += ["", "## Deprecated", ""]
            for entry in self.deprecated[-10:]:
                stats.append(
                    f"- (round {entry.get('removed_round')}, "
                    f"{entry.get('reason')}) {entry.get('rule', '')[:80]}"
                )
        self.md_path.write_text(header + body + "\n".join(stats) + "\n", encoding="utf-8")
        self.ledger_path.write_text(
            json.dumps(
                {
                    "team": self.team,
                    "entries": [e.to_dict() for e in self.entries],
                    "deprecated": self.deprecated,
                },
                indent=2,
            ),
            encoding="utf-8",
        )


# ---------------------------------------------------------------------------
# Coaching
# ---------------------------------------------------------------------------


def _format_findings(verdict: CandidateVerdict) -> str:
    lines: list[str] = []
    for dimension in sorted(verdict.dimensions, key=lambda d: d.score):
        lines.append(f"### {dimension.dimension}: {dimension.score}/10")
        if dimension.spread:
            lines.append(f"(judge sampled {dimension.samples}, spread {dimension.spread})")
        lines.append(dimension.rationale)
        for finding in dimension.findings:
            lines.append(
                f"- [{finding.severity}] [{finding.attributed_role}] "
                f"({finding.anchor()}) {finding.claim}"
                f"\n  suggested: {finding.suggested_revision}"
            )
        lines.append("")
    return "\n".join(lines)


def coach_team(
    team: str,
    verdict: CandidateVerdict,
    round: int,
    playbook: Playbook,
    log: EventLog,
    prior_findings: list[Finding] | None = None,
    learned_source: str = "",
    client=None,
    config: cfg.Config | None = None,
    allow_staging: bool = True,
) -> tuple[list[PlaybookRule], list[StagedFunction]]:
    """Update one team's playbook from this round's verdict.

    ``prior_findings`` are earlier rounds' findings for the same team, which is
    what lets the coach tell a one-off from a pattern. Without that history it
    cannot justify staging a function, so staging is suppressed in round 1.
    """
    config = config or cfg.load()
    if client is None:
        import anthropic

        client = anthropic.Anthropic()

    log.emit("coach.started", f"Coaching {team}", round=round, team=team)

    baseline = verdict.weighted_total
    history = ""
    if prior_findings:
        history = "\n## Findings from earlier rounds (for spotting patterns)\n\n" + "\n".join(
            f"- [{f.severity}] [{f.attributed_role}] {f.claim}" for f in prior_findings[-40:]
        )

    user = f"""## This round's verdict for the team you are coaching

Weighted total: {verdict.weighted_total:.2f}/10

{_format_findings(verdict)}
{history}

## Their current playbook

{playbook.render() or "(empty)"}

## Current contents of the learnable library module

```python
{learned_source or "# empty"}
```

Round number: {round}. {"You may stage a function if the evidence supports it." if allow_staging else "Do NOT stage a function this round."}
"""

    try:
        response = client.messages.parse(
            model=config.model,
            max_tokens=cfg.JUDGE_MAX_TOKENS,
            system=SYSTEM,
            messages=[{"role": "user", "content": user}],
            output_format=CoachOutput,
        )
        output = response.parsed_output or CoachOutput()
        usage = Usage.from_response(response)
    except Exception as exc:
        log.warn(f"Coaching {team} failed: {exc}", round=round, team=team)
        return [], []

    if not allow_staging:
        output.staged_functions = []

    added, removed = playbook.apply(output, round, baseline)
    playbook.save()

    for rule in added:
        log.emit(
            "coach.rule_written",
            rule.rule,
            round=round,
            team=team,
            role=rule.role,
            because=rule.because,
        )
    for text in removed:
        log.emit(
            "coach.rule_written",
            f"DEPRECATED: {text}",
            round=round,
            team=team,
            deprecated=True,
        )

    log.emit(
        "coach.finished",
        f"{team}: +{len(added)} rules, -{len(removed)}, "
        f"{len(output.staged_functions)} staged",
        round=round,
        team=team,
        usage=usage,
    )
    return added, output.staged_functions[:1]


def stage_function(
    function: StagedFunction, staged_dir: Path, log: EventLog, round: int, team: str
) -> Path:
    """Write a proposed helper to disk for human review.

    Never imported or executed here. The approval step is a person reading the
    source, precisely because an unreviewed helper in the shared library is a way
    to break every composer at once.
    """
    staged_dir = Path(staged_dir)
    staged_dir.mkdir(parents=True, exist_ok=True)
    path = staged_dir / f"{function.name}.json"
    path.write_text(
        json.dumps(
            {
                "name": function.name,
                "rationale": function.rationale,
                "source": function.source,
                "test_source": function.test_source,
                "proposed_by": team,
                "proposed_round": round,
                "approved": False,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    log.emit(
        "coach.library_staged",
        f"{function.name}: {function.rationale}",
        round=round,
        team=team,
        name=function.name,
        source=function.source,
    )
    return path


def approved_helpers(learned_path: Path | None = None) -> list[str]:
    """Names of helpers currently live in the learnable library module."""
    learned_path = learned_path or (Path(__file__).parent / "house" / "learned.py")
    if not Path(learned_path).exists():
        return []
    import ast

    try:
        tree = ast.parse(Path(learned_path).read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return []
    return [
        node.name
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and not node.name.startswith("_")
    ]
