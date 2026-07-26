"""Pairwise comparison, and the tournament that turns it into ratings.

Absolute scores compress. Two pieces that both land on 6 for melody are not
equally good, and the rubric panel has no way to say which it would rather hear.
Asking directly is a much easier question for a judge than assigning a number,
and it produces a ranking the rubric scores cannot.

**Every pair is judged in both presentation orders, and disagreement is a draw.**
This is the correctness property of the module and it is not optional. LLM judges
have a large, well-documented position bias: presented with two comparable
pieces, they favour one slot often enough that a single-order tournament measures
mostly slot preference. Judging A-then-B and B-then-A and only accepting a
winner when both orders agree removes that bias by construction. The cost is
doubled calls and a lot of draws between similar pieces, which is the honest
outcome: if the verdict flips when the order flips, we did not learn anything
about the music.

Comparison prompts use :func:`~houseband.score_text.render_compact` (header,
sections, density, harmony and repetition summaries, no note dump) plus both
piano rolls. Comparison is a judgement about overall impression, and two full
note dumps would multiply the prompt size for detail the judge is not being
asked about.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any

from houseband import config as cfg
from houseband.events import EventLog, Usage
from houseband.judges import elo
from houseband.judges.rubric import piano_roll_block, resolve_client
from houseband.score_text import render_compact
from houseband.types import Brief, Candidate, PairwiseVerdict

MAX_CONCURRENT_COMPARISONS = 4

_COMPARE_INSTRUCTIONS = """\
You are judging a blind head-to-head between two short instrumental pieces \
submitted to the same composition contest, written to the same brief.

You are choosing which piece is better overall, as a piece of music. Weigh, \
roughly in this order: whether it answers the brief, whether it has a melody \
that goes somewhere, whether it has a form with an arc rather than a loop, \
whether the harmony and groove hold up, and whether the arrangement and \
production serve the material. Do not reward complexity for its own sake, and \
do not reward length.

Two rules about how you decide:

* Judge the music, not the presentation. You are given a summary of each score \
and a piano roll, not a full note list. Base your decision on what those show: \
structure, density, register, repetition, harmonic movement, dynamics.
* Say "tie" only when you genuinely cannot separate them. A tie is the right \
answer for two pieces with different but equally successful approaches, and the \
wrong answer for avoiding a decision you could make.

You are shown two candidate IDs and nothing else. You do not know who wrote \
either piece, whether either was written by a person, or how either scored \
elsewhere. Do not speculate; it is not evidence.

Give the winner and two or three sentences of reasoning. Cite bar ranges and \
track names wherever the summaries let you.\
"""


def build_system_prompt(brief: Brief, criteria: str) -> list[dict]:
    """The cached prefix, identical for every comparison in a round."""
    blocks: list[dict] = [
        {"type": "text", "text": _COMPARE_INSTRUCTIONS},
        {
            "type": "text",
            "text": f"THE BRIEF BOTH PIECES WERE WRITTEN TO\n\n{brief.render()}",
        },
    ]
    if criteria.strip():
        blocks.append(
            {
                "type": "text",
                "text": (
                    "SHARED CRITERIA FOR THIS ROUND\n\n"
                    "Derived before any submission was seen. Where they conflict "
                    "with your instincts about the genre, follow them.\n\n"
                    f"{criteria.strip()}"
                ),
            }
        )
    blocks[-1]["cache_control"] = {"type": "ephemeral"}
    return blocks


def _compact_text(candidate: Candidate) -> str:
    """Summary-only score text. A broken MIDI degrades rather than crashes."""
    try:
        return render_compact(candidate.midi_path, candidate.sidecar_path)
    except Exception as exc:  # pretty_midi raises a variety of parse errors
        return f"(The score could not be rendered: {exc})"


def _slot(label: str, candidate: Candidate) -> list[dict]:
    """One candidate's content blocks, labelled by presentation slot."""
    content: list[dict] = [
        {
            "type": "text",
            "text": f"=== CANDIDATE {label} (id {candidate.candidate_id}) ===",
        }
    ]
    image = piano_roll_block(candidate)
    if image is not None:
        content.append(image)
    content.append({"type": "text", "text": _compact_text(candidate)})
    return content


def _judge_order(
    first: Candidate,
    second: Candidate,
    system: list[dict],
    client: Any,
    config: Any,
) -> tuple[PairwiseVerdict, Usage]:
    """One comparison in one presentation order. A is ``first``, B is ``second``."""
    content = _slot("A", first) + _slot("B", second)
    content.append(
        {
            "type": "text",
            "text": (
                "Which of these two pieces is better overall: A, B, or tie? "
                "Decide on the music."
            ),
        }
    )
    response = client.messages.parse(
        model=config.model,
        max_tokens=cfg.JUDGE_MAX_TOKENS,
        system=system,
        messages=[{"role": "user", "content": content}],
        output_format=PairwiseVerdict,
    )
    parsed = getattr(response, "parsed_output", None)
    if parsed is None:
        raise RuntimeError("the model returned no parsed output")
    return parsed, Usage.from_response(response)


def _winner_id(verdict: PairwiseVerdict, first: Candidate, second: Candidate) -> str | None:
    """Translate a slot-relative winner into a candidate id, or ``None`` for a tie."""
    if verdict.winner == "A":
        return first.candidate_id
    if verdict.winner == "B":
        return second.candidate_id
    return None


def compare(
    a: Candidate,
    b: Candidate,
    brief: Brief,
    criteria: str,
    client: Any | None = None,
    config: Any | None = None,
    log: EventLog | None = None,
    round: int = 0,
) -> PairwiseVerdict:
    """Compare two candidates in both orders and return the reconciled verdict.

    The returned verdict is expressed from ``a``'s point of view: ``"A"`` means
    ``a`` won both orders, ``"B"`` means ``b`` won both, and ``"tie"`` means
    either both orders called it a tie or the two orders disagreed.
    """
    client = resolve_client(client)
    config = config or cfg.load()
    system = build_system_prompt(brief, criteria)

    orders = ((a, b), (b, a))
    winners: list[str | None] = []
    failures: list[str] = []
    reasons: list[str] = []

    for first, second in orders:
        label = f"{first.candidate_id} then {second.candidate_id}"
        try:
            verdict, usage = _judge_order(first, second, system, client, config)
        except Exception as exc:
            failures.append(f"{label}: {type(exc).__name__}: {exc}")
            if log is not None:
                log.emit(
                    "judge.failed",
                    f"pairwise {label} failed: {exc}",
                    round=round,
                    order=label,
                    error=f"{type(exc).__name__}: {exc}",
                )
            continue

        winners.append(_winner_id(verdict, first, second))
        reasons.append(f"[{label}] {verdict.reason}")
        if log is not None:
            named = _winner_id(verdict, first, second) or "tie"
            log.emit(
                "pairwise.verdict",
                f"{label}: {named}",
                round=round,
                usage=usage,
                order=label,
                first=first.candidate_id,
                second=second.candidate_id,
                winner=named,
                reason=verdict.reason,
            )

    # A single order is exactly the position-biased signal this module exists to
    # discard, so anything short of two agreeing orders is a draw.
    if len(winners) < len(orders):
        winner: str = "tie"
        note = (
            "Recorded as a draw: not every presentation order produced a verdict "
            f"({'; '.join(failures)})."
        )
    elif winners[0] != winners[1]:
        winner = "tie"
        note = (
            "Recorded as a draw: the two presentation orders disagreed, which is "
            "position bias rather than a preference."
        )
    elif winners[0] is None:
        winner = "tie"
        note = "Both presentation orders called it a tie."
    elif winners[0] == a.candidate_id:
        winner = "A"
        note = "Both presentation orders preferred the same piece."
    else:
        winner = "B"
        note = "Both presentation orders preferred the same piece."

    reconciled = PairwiseVerdict(
        winner=winner,
        reason=" ".join([note, *reasons]).strip(),
    )
    if log is not None:
        log.emit(
            "pairwise.verdict",
            f"{a.candidate_id} vs {b.candidate_id}: {reconciled.winner}",
            round=round,
            order="both",
            a=a.candidate_id,
            b=b.candidate_id,
            winner=reconciled.winner,
            agreed=winner != "tie",
            reason=reconciled.reason,
        )
    return reconciled


def _one_decimal(value: float) -> float:
    """Round for the event log. Elo to more precision than this is false confidence.

    Defined here rather than inlined because ``tournament`` takes a parameter
    called ``round``, which shadows the builtin inside its body.
    """
    return round(value, 1)


def _outcome(verdict: PairwiseVerdict) -> float:
    """Elo outcome from A's point of view."""
    if verdict.winner == "A":
        return 1.0
    if verdict.winner == "B":
        return 0.0
    return 0.5


def tournament(
    candidates: list[Candidate],
    brief: Brief,
    criteria: str,
    client: Any | None = None,
    config: Any | None = None,
    log: EventLog | None = None,
    round: int = 0,
    k: float = elo.DEFAULT_K,
    initial: dict[str, float] | None = None,
) -> dict[str, float]:
    """Round-robin every pair, then rate the results.

    Pairs are enumerated in sorted id order and the first is run alone before the
    rest go concurrent: a cache entry is not readable until the response that
    wrote it has started, so firing every pair at once would miss the shared
    prefix on all of them. One warm-up call buys the cache for the whole
    tournament.
    """
    client = resolve_client(client)
    config = config or cfg.load()

    ordered = sorted(candidates, key=lambda c: c.candidate_id)
    pairs = [
        (ordered[i], ordered[j])
        for i in range(len(ordered))
        for j in range(i + 1, len(ordered))
    ]

    def one(pair: tuple[Candidate, Candidate]) -> tuple[str, str, float]:
        a, b = pair
        verdict = compare(
            a, b, brief, criteria, client=client, config=config, log=log, round=round
        )
        return (a.candidate_id, b.candidate_id, _outcome(verdict))

    results: list[tuple[str, str, float]] = []
    if pairs:
        results.append(one(pairs[0]))
    if len(pairs) > 1:
        workers = max(1, min(MAX_CONCURRENT_COMPARISONS, len(pairs) - 1))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            results.extend(pool.map(one, pairs[1:]))

    pinned = {c.candidate_id for c in candidates if c.is_reference}
    ratings = elo.run_ratings(results, pinned=pinned, initial=initial, k=k)

    if log is not None:
        by_id = {c.candidate_id: c for c in candidates}
        for candidate_id, rating in sorted(ratings.items(), key=lambda kv: -kv[1]):
            candidate = by_id.get(candidate_id)
            log.emit(
                "elo.updated",
                f"{candidate_id} {rating:.0f}",
                round=round,
                team=candidate.team if candidate else None,
                candidate_id=candidate_id,
                elo=_one_decimal(rating),
                pinned=candidate_id in pinned,
                comparisons=sum(1 for a, b, _ in results if candidate_id in (a, b)),
            )
    return ratings
