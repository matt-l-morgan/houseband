"""The rubric panel: one independent LLM judge per dimension.

Eight dimensions for a long-form piece, nine for a starter, which is not the same
nine minus one: see :data:`houseband.types.DIMENSIONS_FOR_MODE` for why a loop is
judged on its seam instead of its form.

Four decisions carry this module.

**Anchored rubrics, loaded from disk.** Each dimension's criteria live in
``rubrics/<dimension>.md`` as prose, not as Python. An LLM asked to invent a
number will invent a number; an LLM asked to match a description to one of five
written anchors is doing a much easier and much more repeatable job. Keeping the
anchors in editable markdown also means recalibrating the panel is a text edit
rather than a code change, which matters because calibration is the thing most
likely to need tuning after a run.

**One call per dimension.** Asking one judge for eight scores produces halo
effects: a piece the model has decided it likes scores well everywhere. Eight
separate calls, each seeing only its own rubric, gives eight nearly independent
reads, and independence is what makes the spread between them informative.

**Blindness.** The judge sees ``candidate_id`` and nothing else identifying.
Never the team, never whether this is the human reference. The reference is
scored by the same panel with the same prompt, which is the only way
:mod:`houseband.judges.calibration` can mean anything.

**Prompt caching.** The system prompt is built stable-prefix-first (general
instructions, then the brief and criteria, then the dimension rubric) with a
cache breakpoint after the rubric, and the per-candidate score text goes in the
``messages``. Every candidate in a round therefore reuses one cached prefix per
dimension, which is where most of the round's input cost would otherwise go.
"""

from __future__ import annotations

import base64
import statistics
from concurrent.futures import ThreadPoolExecutor
from functools import cache
from pathlib import Path
from typing import Any

from houseband import config as cfg
from houseband.events import EventLog, Usage
from houseband.score_text import render
from houseband.types import (
    DIMENSION_TITLES,
    DIMENSIONS,
    DIMENSIONS_FOR_MODE,
    Brief,
    Candidate,
    CandidateVerdict,
    DimensionVerdict,
    Mode,
    ScoredDimension,
)

RUBRIC_DIR = Path(__file__).resolve().parent / "rubrics"

# Eight dimensions at once would open eight sockets and mostly wait; four keeps
# the round quick without looking like a burst to the rate limiter.
MAX_CONCURRENT_DIMENSIONS = 4


# ---------------------------------------------------------------------------
# Rubric loading
# ---------------------------------------------------------------------------


@cache
def load_rubric(dimension: str) -> str:
    """Read one dimension's rubric from disk.

    Cached because a round reads the same eight files once per candidate per
    sample, and because a rubric changing mid-round would silently produce
    scores on two different scales.
    """
    path = RUBRIC_DIR / f"{dimension}.md"
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise FileNotFoundError(
            f"No rubric for dimension {dimension!r} at {path}. "
            f"Expected one file per key in DIMENSIONS."
        ) from exc


def missing_rubrics(dimensions: tuple[str, ...] = DIMENSIONS) -> list[str]:
    """Dimensions with no rubric file. Used to fail fast at startup."""
    return [d for d in dimensions if not (RUBRIC_DIR / f"{d}.md").exists()]


def missing_rubrics_for_mode(mode: Mode = "longform") -> list[str]:
    """The same check against the dimension set a given mode actually judges.

    Worth having separately because the two modes do not share their dimension
    sets: a rubric directory that is complete for long-form can be missing both
    starter rubrics, and the failure would otherwise surface as a dead dimension
    partway into a paid run rather than at startup.
    """
    return missing_rubrics(DIMENSIONS_FOR_MODE[mode])


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


def resolve_client(client: Any | None) -> Any:
    """Return the injected client, or build the default one lazily.

    Lazy so that importing this module does not require a credential: tests and
    ``--dry-run`` paths never touch the network, and the import graph should not
    care whether a key exists.
    """
    if client is not None:
        return client
    import anthropic

    return anthropic.Anthropic()


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

_JUDGE_INSTRUCTIONS = """\
You are one member of a blind panel judging short instrumental pieces submitted \
to a composition contest. You judge exactly one dimension, described in the \
rubric below, and you say nothing about any other dimension.

How to score:

* Match the piece to the closest anchor in the rubric's scale. The anchors are \
descriptions, not adjectives: pick the one whose description is true of this \
piece, then adjust by one point if it sits between two anchors.
* Calibration matters more than kindness. A competent but unremarkable piece \
scores 5 or 6. Scores of 9 and 10 are for work you would defend to a \
professional. Scores of 1 and 2 are for work that is broken in the way the \
rubric describes, not merely dull.
* Judge what is in the score, not what you assume was intended. If the evidence \
is ambiguous, say so in the rationale and score the ambiguity down rather than \
guessing generously.
* You are shown a candidate ID and nothing else. You do not know who wrote this \
piece, whether it was written by a person or a program, or how it compares to \
the other submissions. Do not speculate about any of that; it is not evidence \
and reasoning about it is how a panel becomes unfair.

Your output is a score, a rationale of two or three sentences that justifies \
that specific number against the anchors, and a list of findings.

Findings are the part a composer will actually act on, so:

* Anchor every finding. Give bar_start and bar_end, and give the track name \
whenever the problem belongs to one part. Bars are 0-indexed and match the bar \
numbers in the score text. Only leave the anchors empty for a claim that is \
genuinely about the whole piece.
* Make suggested_revision a change someone could make without asking you a \
follow-up question. Name the bars, the part, and the specific alteration. \
"Improve the melody" is not a revision; "raise the third and fourth \
repetitions of the figure in bars 8-15 to start on the fifth" is.
* Attribute each finding to the role responsible: songwriter for melody, \
harmony, key and chords; rhythm for drums, bass, groove and timing; arranger \
for sections, form, instrument entrances and exits, and density; mix for patch \
choice, velocity, panning, and register balance.
* Report the findings that matter. Three well-anchored findings are worth more \
than ten vague ones, and a piece that genuinely has no faults on this dimension \
should return an empty list rather than manufactured criticism.\
"""


def build_system_prompt(dimension: str, brief: Brief, criteria: str) -> list[dict]:
    """Build the cached system prefix for one dimension.

    Ordered by stability so the cache breakpoint covers as much as possible:
    the instructions are identical for every call in every run, the brief and
    criteria are fixed for a run, and the rubric is fixed for a dimension. The
    only thing that varies per candidate is the score text, which lives in the
    messages after this prefix.
    """
    blocks: list[dict] = [
        {"type": "text", "text": _JUDGE_INSTRUCTIONS},
        {
            "type": "text",
            "text": (
                "THE BRIEF EVERY SUBMISSION WAS WRITTEN TO\n\n"
                f"{brief.render()}"
            ),
        },
    ]
    if criteria.strip():
        blocks.append(
            {
                "type": "text",
                "text": (
                    "SHARED CRITERIA FOR THIS ROUND\n\n"
                    "These apply across all dimensions and were derived before any "
                    "submission was seen. Where they conflict with your instincts "
                    "about the genre, follow them.\n\n"
                    f"{criteria.strip()}"
                ),
            }
        )
    blocks.append(
        {
            "type": "text",
            "text": (
                f"YOUR DIMENSION: {DIMENSION_TITLES.get(dimension, dimension)} "
                f"({dimension})\n\n{load_rubric(dimension)}"
            ),
            # Last stable block, so the breakpoint caches instructions + brief +
            # criteria + rubric together. Everything after this varies per
            # candidate and is deliberately outside the cache.
            "cache_control": {"type": "ephemeral"},
        }
    )
    return blocks


def _score_text(candidate: Candidate) -> str:
    """The candidate's score as text, rendered once and reused.

    ``Candidate.score_text`` is populated by the pipeline; falling back to a
    fresh render keeps this function callable on a bare candidate, and keeps a
    missing or unparseable MIDI from taking the round down.
    """
    if candidate.score_text.strip():
        return candidate.score_text
    try:
        return render(candidate.midi_path, candidate.sidecar_path, include_notes=True)
    except Exception as exc:  # pretty_midi raises a variety of parse errors
        return f"(The score could not be rendered: {exc})"


def piano_roll_block(candidate: Candidate) -> dict | None:
    """Base64 image block for the piano roll, or ``None`` if unavailable.

    The picture is not decoration. Repetition, register collision and dynamic
    flatness are all immediately visible in a piano roll and have to be
    reconstructed by careful reading from a note list, so the image materially
    changes what the judge notices. Absence is not an error: audio and image
    rendering are optional dependencies, and a run without them should still
    produce verdicts.
    """
    path = candidate.piano_roll
    if path is None:
        return None
    try:
        data = Path(path).read_bytes()
    except OSError:
        return None
    if not data:
        return None
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/png",
            "data": base64.standard_b64encode(data).decode("ascii"),
        },
    }


def build_messages(candidate: Candidate, dimension: str) -> list[dict]:
    """Build the per-candidate user turn.

    Deliberately mentions only ``candidate_id``: this function is the single
    place where identity could leak to the judge, so it is the single place to
    check that it does not.
    """
    content: list[dict] = []
    image = piano_roll_block(candidate)
    if image is not None:
        content.append(
            {
                "type": "text",
                "text": (
                    f"CANDIDATE {candidate.candidate_id}\n\n"
                    "First, the piano roll. Time runs left to right in bars, pitch "
                    "runs bottom to top, and note intensity encodes velocity."
                ),
            }
        )
        content.append(image)
        content.append(
            {
                "type": "text",
                "text": (
                    "Now the same piece as score text.\n\n"
                    f"{_score_text(candidate)}\n\n"
                    f"Judge this piece on {dimension} only, against the anchored "
                    "scale in your rubric."
                ),
            }
        )
    else:
        content.append(
            {
                "type": "text",
                "text": (
                    f"CANDIDATE {candidate.candidate_id}\n\n"
                    f"{_score_text(candidate)}\n\n"
                    f"Judge this piece on {dimension} only, against the anchored "
                    "scale in your rubric."
                ),
            }
        )
    return [{"role": "user", "content": content}]


# ---------------------------------------------------------------------------
# One dimension
# ---------------------------------------------------------------------------


def _sample_count(dimension: str, samples: int) -> int:
    """How many times to score this dimension.

    An explicit ``samples`` above 1 wins, so a caller can force sampling for an
    experiment. Otherwise the dimensions that drive the learning loop get
    sampled and everything else gets one read, because sampling every dimension
    would triple the round's cost to reduce noise we are not acting on.
    """
    if samples > 1:
        return samples
    if dimension in cfg.MEDIAN_SAMPLED_DIMENSIONS:
        return cfg.MEDIAN_SAMPLES
    return 1


def _neutral(dimension: str, reason: str) -> ScoredDimension:
    """A verdict that says nothing, for when the judge could not be reached.

    Score 5 rather than 0 or 10 because a failed call is not evidence about the
    music, and a 0 would poison both the composite score and anything the coach
    learns from this round. The rationale carries the failure so it shows up in
    the report instead of looking like a real mediocre score.
    """
    return ScoredDimension(
        dimension=dimension,
        score=5,
        rationale=f"Not judged: {reason} Scored 5 as a neutral placeholder.",
        findings=[],
        samples=[],
    )


def judge_dimension(
    candidate: Candidate,
    dimension: str,
    brief: Brief,
    criteria: str,
    client: Any | None = None,
    config: Any | None = None,
    log: EventLog | None = None,
    round: int = 0,
    samples: int = 1,
) -> ScoredDimension:
    """Score one candidate on one dimension, sampling if the policy says to."""
    client = resolve_client(client)
    config = config or cfg.load()
    count = _sample_count(dimension, samples)

    system = build_system_prompt(dimension, brief, criteria)
    messages = build_messages(candidate, dimension)

    if log is not None:
        log.emit(
            "judge.started",
            f"{DIMENSION_TITLES.get(dimension, dimension)} on {candidate.candidate_id}",
            round=round,
            team=candidate.team,
            dimension=dimension,
            candidate_id=candidate.candidate_id,
            samples=count,
        )

    verdicts: list[DimensionVerdict] = []
    failures: list[str] = []
    # Samples run in sequence on purpose: the first writes the cached prefix and
    # the rest read it, which concurrent samples could not do.
    for index in range(count):
        try:
            response = client.messages.parse(
                model=config.model,
                max_tokens=cfg.JUDGE_MAX_TOKENS,
                system=system,
                messages=messages,
                output_format=DimensionVerdict,
            )
        except Exception as exc:
            failures.append(f"{type(exc).__name__}: {exc}")
            if log is not None:
                log.emit(
                    "judge.failed",
                    f"{dimension} sample {index + 1}/{count} failed: {exc}",
                    round=round,
                    team=candidate.team,
                    dimension=dimension,
                    candidate_id=candidate.candidate_id,
                    sample=index + 1,
                    error=f"{type(exc).__name__}: {exc}",
                )
            continue

        parsed = getattr(response, "parsed_output", None)
        if parsed is None:
            failures.append("the model returned no parsed output")
            if log is not None:
                log.emit(
                    "judge.failed",
                    f"{dimension} sample {index + 1}/{count} returned no parsed output",
                    round=round,
                    team=candidate.team,
                    dimension=dimension,
                    candidate_id=candidate.candidate_id,
                    sample=index + 1,
                    usage=Usage.from_response(response),
                )
            continue

        verdicts.append(parsed)
        if log is not None:
            log.emit(
                "judge.verdict",
                f"{dimension} {parsed.score}/10 on {candidate.candidate_id}",
                round=round,
                team=candidate.team,
                dimension=dimension,
                usage=Usage.from_response(response),
                candidate_id=candidate.candidate_id,
                sample=index + 1,
                score=parsed.score,
                findings=len(parsed.findings),
            )

    if not verdicts:
        reason = failures[0] if failures else "no samples were collected."
        return _neutral(dimension, reason if reason.endswith(".") else reason + ".")

    scored = _reduce(dimension, verdicts)
    if failures and len(verdicts) < count:
        scored.rationale += (
            f" ({len(verdicts)} of {count} samples succeeded; "
            f"the rest failed: {failures[0]})"
        )
    return scored


def _reduce(dimension: str, verdicts: list[DimensionVerdict]) -> ScoredDimension:
    """Collapse samples into one verdict, keeping the spread visible.

    The median rather than the mean because a single wild sample should not move
    the score, and because the reported number then belongs to an actual reading
    of the piece rather than to an average of readings. The rationale and
    findings come from the sample nearest the median so that the number and its
    justification cannot contradict each other: with an odd sample count that
    sample *is* the median, and with an even count it is the closer of the two
    the median falls between.
    """
    scores = [v.score for v in verdicts]
    midpoint = statistics.median(scores)
    representative = min(verdicts, key=lambda v: (abs(v.score - midpoint), v.score))
    return ScoredDimension(
        dimension=dimension,
        score=representative.score,
        rationale=representative.rationale,
        findings=representative.findings,
        samples=scores,
    )


# ---------------------------------------------------------------------------
# One candidate
# ---------------------------------------------------------------------------


def judge_candidate(
    candidate: Candidate,
    brief: Brief,
    criteria: str,
    dimensions: tuple[str, ...] | None = None,
    client: Any | None = None,
    config: Any | None = None,
    log: EventLog | None = None,
    round: int = 0,
    mode: Mode = "longform",
) -> CandidateVerdict:
    """Run the whole panel on one candidate.

    Dimensions run concurrently because they are independent by construction:
    eight serial calls, three of them sampled three times, is fourteen
    round-trips of pure waiting.

    ``mode`` selects the dimension set: a starter is not judged on form (a loop
    has none) and is judged on loop usability and headroom instead. An explicit
    ``dimensions`` still wins, for the experiments that want one dimension in
    isolation. The mode is recorded on the verdict so that ``weighted_total``
    applies the right weights later, including after a round-trip through the
    run log.
    """
    client = resolve_client(client)
    config = config or cfg.load()
    dimensions = dimensions or DIMENSIONS_FOR_MODE[mode]

    def one(dimension: str) -> ScoredDimension:
        return judge_dimension(
            candidate,
            dimension,
            brief,
            criteria,
            client=client,
            config=config,
            log=log,
            round=round,
        )

    workers = max(1, min(MAX_CONCURRENT_DIMENSIONS, len(dimensions)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        # map preserves input order, so the verdict's dimension order is stable
        # across runs regardless of which call finished first.
        scored = list(pool.map(one, dimensions))

    return CandidateVerdict(
        candidate_id=candidate.candidate_id,
        team=candidate.team,
        is_reference=candidate.is_reference,
        mode=mode,
        dimensions=scored,
    )


# ---------------------------------------------------------------------------
# The panel
# ---------------------------------------------------------------------------


def run_panel(
    candidates: list[Candidate],
    brief: Brief,
    criteria: str,
    dimensions: tuple[str, ...] | None = None,
    client: Any | None = None,
    config: Any | None = None,
    log: EventLog | None = None,
    round: int = 0,
    mode: Mode = "longform",
) -> dict[str, CandidateVerdict]:
    """Judge every candidate, returning verdicts keyed by ``candidate_id``.

    Candidates are judged one after another rather than all at once. A cache
    entry is only readable once the response that wrote it has started, so
    concurrent candidates would all miss the shared per-dimension prefix and pay
    the write premium eight times over. Serially, the first candidate warms all
    eight prefixes and everyone after it reads them.

    ``mode`` picks the dimension set and is recorded on every verdict. Passing it
    once here rather than per candidate is deliberate: a round in which two
    candidates were judged against different dimension sets would produce
    weighted totals that are not comparable, which is the one thing the panel
    exists to produce.
    """
    client = resolve_client(client)
    config = config or cfg.load()
    dimensions = dimensions or DIMENSIONS_FOR_MODE[mode]

    verdicts: dict[str, CandidateVerdict] = {}
    for candidate in candidates:
        verdicts[candidate.candidate_id] = judge_candidate(
            candidate,
            brief,
            criteria,
            dimensions=dimensions,
            client=client,
            config=config,
            log=log,
            round=round,
            mode=mode,
        )
    return verdicts
