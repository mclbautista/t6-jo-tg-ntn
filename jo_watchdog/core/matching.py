"""Pure core: Deliverable match models and the deterministic decision rule.

Jev answers one typed Choice question per candidate with ``match``/``maybe``/``no``
probabilities; code alone decides what becomes a link, an ops hint, or nothing
(#26). No I/O here.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field

# Probabilities for one candidate must sum to 1 within this tolerance.
PROBABILITY_TOLERANCE = 0.01

Verdict = Literal["link", "hint", "none"]
JoOutcome = Literal["linked", "hinted", "none", "skipped"]


class Candidate(BaseModel):
    page_id: str
    name: str


class CandidateDecision(BaseModel):
    page_id: str
    p_match: float
    p_maybe: float
    p_no: float


class MatchResult(BaseModel):
    decisions: list[CandidateDecision]
    requested_model: str
    returned_model: str | None
    input_tokens: int | None
    output_tokens: int | None


class InvalidMatchResult(ValueError):
    """A matcher answer that doesn't fit the candidate set; counts as a failure."""


class CandidateOutcome(BaseModel):
    """What code decided for one candidate, with the probabilities behind it."""

    page_id: str
    name: str
    p_match: float
    p_maybe: float
    p_no: float
    verdict: Verdict
    duplicate_name: bool = False


class MatchDecision(BaseModel):
    """Every candidate's outcome, plus what is written and what is reported."""

    outcomes: list[CandidateOutcome] = Field(default_factory=list)
    link_enabled: bool = False
    linked_page_ids: list[str] = Field(default_factory=list)
    hints: list[CandidateOutcome] = Field(default_factory=list)
    outcome: JoOutcome = "none"


def normalize_name(name: str) -> str:
    """Casefold and collapse whitespace, for duplicate-name detection."""
    return re.sub(r"\s+", " ", name).strip().casefold()


def validate_result(candidates: list[Candidate], result: MatchResult) -> None:
    """Raise ``InvalidMatchResult`` unless there is exactly one sane decision per candidate."""
    expected = [c.page_id for c in candidates]
    got = [d.page_id for d in result.decisions]
    if len(set(got)) != len(got):
        raise InvalidMatchResult("duplicate decision for a candidate")
    missing = set(expected) - set(got)
    unknown = set(got) - set(expected)
    if missing:
        raise InvalidMatchResult(f"missing decision for {len(missing)} candidate(s)")
    if unknown:
        raise InvalidMatchResult(f"decision for {len(unknown)} unknown candidate(s)")
    for d in result.decisions:
        probs = (d.p_match, d.p_maybe, d.p_no)
        if any(p < 0 or p > 1 for p in probs):
            raise InvalidMatchResult("probability outside [0, 1]")
        if not sums_to_one(*probs):
            raise InvalidMatchResult("probabilities do not sum to 1")


def sums_to_one(*probs: float) -> bool:
    """True when ``probs`` sum to 1 within the tolerance (inclusive of float noise)."""
    return abs(sum(probs) - 1.0) <= PROBABILITY_TOLERANCE + 1e-9


def _verdict(d: CandidateDecision, *, t_link: float, t_hint: float) -> Verdict:
    if d.p_match >= max(d.p_maybe, d.p_no) and d.p_match >= t_link:
        return "link"
    if d.p_match + d.p_maybe >= t_hint:
        return "hint"
    return "none"


def decide(
    candidates: list[Candidate],
    result: MatchResult,
    *,
    t_link: float,
    t_hint: float,
    link_enabled: bool,
) -> MatchDecision:
    """Apply the #26 decision rule to a validated matcher result.

    ``link`` needs ``match`` as the top choice at or above ``t_link``; otherwise
    ``hint`` when ``match + maybe`` reaches ``t_hint``; otherwise ``none``.
    Candidates whose names collide after normalization never link: a would-be
    link becomes a hint. With linking disabled (shadow mode) nothing is written
    and every would-be link is reported as a hint, so it can still be reviewed.
    """
    validate_result(candidates, result)
    by_id = {d.page_id: d for d in result.decisions}
    counts: dict[str, int] = {}
    for c in candidates:
        key = normalize_name(c.name)
        counts[key] = counts.get(key, 0) + 1

    outcomes: list[CandidateOutcome] = []
    for c in candidates:
        d = by_id[c.page_id]
        duplicate = counts[normalize_name(c.name)] > 1
        verdict = _verdict(d, t_link=t_link, t_hint=t_hint)
        if verdict == "link" and duplicate:
            verdict = "hint"
        outcomes.append(
            CandidateOutcome(
                page_id=c.page_id,
                name=c.name,
                p_match=d.p_match,
                p_maybe=d.p_maybe,
                p_no=d.p_no,
                verdict=verdict,
                duplicate_name=duplicate,
            )
        )

    links = [o for o in outcomes if o.verdict == "link"]
    linked = [o.page_id for o in links] if link_enabled else []
    hints = [o for o in outcomes if o.verdict == "hint" or (o.verdict == "link" and not linked)]
    return MatchDecision(
        outcomes=outcomes,
        link_enabled=link_enabled,
        linked_page_ids=linked,
        hints=hints,
        outcome="linked" if linked else "hinted" if hints else "none",
    )


def hint_line(hint: CandidateOutcome) -> str:
    """The ops finding line for a hint: probability, never a generated reason."""
    return f"Possible Deliverable: {hint.name} (match probability {hint.p_match:.2f})"


MATCHER_SKIPPED_LINE = "Deliverable match skipped: matcher unavailable"
