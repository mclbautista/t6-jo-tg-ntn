"""Corpus scoring for the fuzzy, link and hint thresholds (#61).

Offline and read-only: it scores the redacted corpus's assignee mentions against
the Contacts snapshot (fuzzy) and recorded Jev probabilities (link/hint), then
retains the numbers in a results file for the owner to review. It never edits
config, never enables linking, and never recommends a looser threshold than the
current one: ambiguity protection only gets stricter, and only by the owner's
decision.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from jo_watchdog.core.matching import CandidateDecision, _verdict
from jo_watchdog.core.resolve import (
    MentionResult,
    PeopleIndex,
    Person,
    ResolveSettings,
    fuzzy_ranking,
    normalize_name,
    resolve_mention,
    split_mentions,
)

SCHEMA_VERSION = 1
FUZZY_SCORE_GRID = (80.0, 85.0, 88.0, 90.0, 92.0, 95.0, 98.0)
FUZZY_LEAD_GRID = (0.0, 4.0, 8.0, 12.0, 16.0)
LINK_GRID = (0.70, 0.80, 0.85, 0.90, 0.95, 0.98)
HINT_GRID = (0.30, 0.40, 0.50, 0.60, 0.70)

_ASSIGNED = re.compile(r"^\s*Assigned(?:\s+to)?\s*:\s*(.*)$", re.IGNORECASE | re.MULTILINE)
# Resolves anything that reaches the fuzzy stage, to tell which mentions get there.
_PROBE = ResolveSettings(fuzzy_min_score=-1, fuzzy_min_lead=-1000)


@dataclass(frozen=True)
class FuzzyCase:
    """One assignee mention that falls through to guarded fuzzy matching."""

    mention: str
    top_name: str
    top_score: float
    runner_up_name: str | None
    runner_up_score: float

    @property
    def lead(self) -> float:
        return self.top_score - self.runner_up_score


@dataclass(frozen=True)
class MatchSample:
    """One recorded Jev answer and the verdict the owner expects for it."""

    name: str
    p_match: float
    p_maybe: float
    p_no: float
    expected: str  # link | hint | none


def assignee_mentions(corpus: Iterable[str]) -> list[str]:
    """Every mention from each Job Order's first ``Assigned`` line, in corpus order."""
    mentions: list[str] = []
    for text in corpus:
        found = _ASSIGNED.search(text)
        if found:
            mentions.extend(split_mentions(found.group(1)))
    return mentions


def fuzzy_cases(
    mentions: Iterable[str], people: Sequence[Person], settings: ResolveSettings
) -> list[FuzzyCase]:
    """The mentions that reach the fuzzy stage, with their top and runner-up scores."""
    index = PeopleIndex(people, settings.honorifics)
    cases: list[FuzzyCase] = []
    for raw in mentions:
        probe: MentionResult = resolve_mention(raw, "assignee", index, _PROBE)
        if probe.method != "fuzzy":
            continue
        ranked = fuzzy_ranking(normalize_name(raw, settings.honorifics), index)
        (top_id, top), rest = ranked[0], ranked[1:]
        runner = rest[0] if rest else None
        cases.append(
            FuzzyCase(
                mention=raw,
                top_name=index.people[top_id].name,
                top_score=top,
                runner_up_name=index.people[runner[0]].name if runner else None,
                runner_up_score=runner[1] if runner else 0.0,
            )
        )
    return cases


def fuzzy_outcome(case: FuzzyCase, min_score: float, min_lead: float) -> str:
    """``resolved``, or why it is held: ``below_score`` or ``ambiguous`` (lead too small)."""
    if case.top_score < min_score:
        return "below_score"
    if case.lead < min_lead:
        return "ambiguous"
    return "resolved"


def sweep_fuzzy(cases: Sequence[FuzzyCase]) -> list[dict]:
    rows = []
    for score in FUZZY_SCORE_GRID:
        for lead in FUZZY_LEAD_GRID:
            counts = {"resolved": 0, "below_score": 0, "ambiguous": 0}
            for case in cases:
                counts[fuzzy_outcome(case, score, lead)] += 1
            rows.append({"fuzzy_min_score": score, "fuzzy_min_lead": lead, **counts})
    return rows


def sweep_match(samples: Sequence[MatchSample]) -> list[dict]:
    rows = []
    for t_link in LINK_GRID:
        for t_hint in HINT_GRID:
            if t_hint > t_link:
                continue
            false_links = missed_links = wrong_hints = 0
            for s in samples:
                decision = CandidateDecision(
                    page_id=s.name, p_match=s.p_match, p_maybe=s.p_maybe, p_no=s.p_no
                )
                got = _verdict(decision, t_link=t_link, t_hint=t_hint)
                false_links += got == "link" and s.expected != "link"
                missed_links += s.expected == "link" and got != "link"
                wrong_hints += got != s.expected and not (got == "link" or s.expected == "link")
            rows.append(
                {
                    "t_link": t_link,
                    "t_hint": t_hint,
                    "false_links": int(false_links),
                    "missed_links": int(missed_links),
                    "wrong_hints": int(wrong_hints),
                }
            )
    return rows


def match_samples(pairs: Sequence[dict]) -> list[MatchSample]:
    """Samples from the recorded regression pairs (``tests/fixtures/jev/regression_pairs.json``)."""
    samples = []
    for pair in pairs:
        body = pair["response"]["body"]["answers"]
        for i, candidate in enumerate(pair["candidates"]):
            p = body[f"c{i}"]["probabilities"]
            samples.append(
                MatchSample(
                    name=pair["name"],
                    p_match=p["match"],
                    p_maybe=p["maybe"],
                    p_no=p["no"],
                    expected=pair["expected_verdicts"][candidate["page_id"]],
                )
            )
    return samples


def _digest(results: dict) -> str:
    canonical = json.dumps(results, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def build_report(
    *,
    corpus_size: int,
    cases: Sequence[FuzzyCase],
    samples: Sequence[MatchSample],
    current: dict[str, float],
    sample_source: str,
    previous: dict | None = None,
) -> dict:
    """The retained results: sweeps, the thresholds in force, and the review state.

    ``adopted`` is always the thresholds currently in config. Looser fuzzy points
    that would resolve more mentions are listed under ``refused_loosening``: they
    weaken ambiguity protection, so only the owner can adopt them by editing config.
    """
    fuzzy = sweep_fuzzy(cases)
    match = sweep_match(samples)
    at_current = (current["fuzzy_min_score"], current["fuzzy_min_lead"])
    base = next(
        (r for r in fuzzy if (r["fuzzy_min_score"], r["fuzzy_min_lead"]) == at_current), None
    )
    refused = [
        r
        for r in fuzzy
        if r["fuzzy_min_score"] <= current["fuzzy_min_score"]
        and r["fuzzy_min_lead"] <= current["fuzzy_min_lead"]
        and (r["fuzzy_min_score"], r["fuzzy_min_lead"]) != at_current
        and base is not None
        and r["resolved"] > base["resolved"]
    ]
    results = {
        "corpus_messages": corpus_size,
        "fuzzy_cases": [
            {
                "mention": c.mention,
                "top": c.top_name,
                "top_score": round(c.top_score, 1),
                "runner_up": c.runner_up_name,
                "runner_up_score": round(c.runner_up_score, 1),
                "lead": round(c.lead, 1),
            }
            for c in cases
        ],
        "fuzzy_sweep": fuzzy,
        "match_sample_source": sample_source,
        "match_samples": len(samples),
        "match_sweep": match,
        "refused_loosening": refused,
    }
    digest = _digest(results)
    review = {"status": "pending", "reviewed_by": None, "reviewed_at": None, "evidence": None}
    prior = (previous or {}).get("owner_review")
    if (
        prior
        and prior.get("status") == "reviewed"
        and (previous or {}).get("results_digest") == digest
        and (previous or {}).get("adopted") == current
    ):
        review = prior
    return {
        "schema_version": SCHEMA_VERSION,
        "adopted": current,
        "link_enabled": False,
        "recommendation": "keep the adopted thresholds; "
        "nothing here enables linking or loosens ambiguity protection",
        "results": results,
        "results_digest": digest,
        "owner_review": review,
    }


def write_report(path: Path, report: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def read_report(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
