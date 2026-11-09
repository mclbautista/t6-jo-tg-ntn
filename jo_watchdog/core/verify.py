"""Pure core: deterministic verification of a capture into binary findings.

Verification annotates and never gates (#23): every finding is report or
don't-report, there are no severity tiers, and nothing here can stop a Notion
write. It makes no LLM call; every input is a fact some earlier stage already
established (extraction, planning, resolution, duplicate lookup, read-back).

Stages that have not landed yet simply leave their facts empty, so a check
whose inputs are missing never fires. In particular the Title/Task/deadline
checks run only when an extraction exists (``verdict`` is set).
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from typing import Any

from pydantic import BaseModel, Field

MANILA = timezone(timedelta(hours=8), "Asia/Manila")

# Report order: the order of the finding table in #23.
FINDING_ORDER = [
    "unsure",
    "multiple_jo",
    "unresolved_person",
    "ambiguous_person",
    "unresolved_project",
    "unknown_department",
    "fuzzy_person",
    "exact_repost",
    "number_reuse",
    "forward_origin",
    "title_blank",
    "task_blank",
    "deadline_past",
    "deadline_unparsed",
    "asap",
    "shift_fallback",
    "header_date_mismatch",
    "deliverable_hint",
    "matcher_skipped",
    "readback_mismatch",
    "duplicate_key",
]

# The only findings Priya has to act on herself (verification ticket).
TAGS_standing_cc = frozenset(
    {
        "multiple_jo",
        "unresolved_person",
        "ambiguous_person",
        "unresolved_project",
        "unknown_department",
    }
)

# Codes that appear at most once per capture. When a stage already recorded
# one (e.g. the planner's `deadline_past`), its copy wins over a recomputed one.
SINGLE_CODES = frozenset(
    {
        "unsure",
        "multiple_jo",
        "unknown_department",
        "exact_repost",
        "number_reuse",
        "forward_origin",
        "title_blank",
        "task_blank",
        "deadline_past",
        "deadline_unparsed",
        "asap",
        "shift_fallback",
        "header_date_mismatch",
        "matcher_skipped",
        "readback_mismatch",
        "duplicate_key",
    }
)

# A poster's own "nothing here" placeholder is not deadline-shaped text.
DEADLINE_PLACEHOLDERS = frozenset({"", "-", "n/a", "na", "none"})


class Finding(BaseModel):
    """One binary finding: a code from ``FINDING_ORDER`` plus its parameters."""

    code: str
    params: dict[str, Any] = Field(default_factory=dict)

    def key(self) -> str:
        return self.model_dump_json()


class PersonMention(BaseModel):
    role: str = "assignee"  # `assignee` | `cc`
    raw: str


class FuzzyMatch(BaseModel):
    raw: str
    contact: str


class EarlierRow(BaseModel):
    url: str
    days: int
    jo_number: str | None = None


class ForwardOrigin(BaseModel):
    """Telegram forward provenance: type, best display name, original link."""

    type: str  # `user` | `hidden_user` | `chat` | `channel`
    name: str = ""
    original_link: str | None = None


class DeliverableHint(BaseModel):
    name: str
    probability: float


class CaptureFacts(BaseModel):
    """Everything verification looks at for one capture."""

    posted_at: datetime | None = None

    # Extraction (#20). `verdict` None means no extraction ran.
    verdict: str | None = None
    reasons: str | None = None
    multiple_jo_suspected: bool = False
    title: str | None = None
    task: str | None = None
    deadline_raw: str | None = None
    header_date_raw: str | None = None

    # Planning (#21).
    deadline: datetime | None = None
    asap: bool = False
    shift_fallback: list[str] | None = None  # names defaulted; [] = no assignee resolved

    # Resolution (#22).
    unresolved_people: list[PersonMention] = Field(default_factory=list)
    ambiguous_people: list[PersonMention] = Field(default_factory=list)
    fuzzy_people: list[FuzzyMatch] = Field(default_factory=list)
    unresolved_projects: list[str] = Field(default_factory=list)
    unknown_department: str | None = None

    # Duplicates and provenance (#24).
    exact_repost: EarlierRow | None = None
    number_reuse: EarlierRow | None = None
    forward_origin: ForwardOrigin | None = None

    # Deliverable matching (#26).
    deliverable_hints: list[DeliverableHint] = Field(default_factory=list)
    matcher_skipped: bool = False

    # Write path (this ticket, #25).
    readback_mismatches: list[str] = Field(default_factory=list)
    duplicate_key_urls: list[str] = Field(default_factory=list)

    # Findings a stage already recorded in the stored plan (#21), merged as-is.
    plan_findings: list[Finding] = Field(default_factory=list)


def _blank(value: str | None) -> bool:
    return value is None or not value.strip()


def _manila_date(moment: datetime) -> date:
    return moment.astimezone(MANILA).date()


_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
_MONTH_DAY = re.compile(
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})"
    r"(?:\s*,?\s*(\d{4}))?\b",
    re.IGNORECASE,
)
_NUMERIC = re.compile(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b")


def _closest_year(month: int, day: int, posted: date) -> date | None:
    """The month/day in whichever year puts it closest to the posting date."""
    candidates = []
    for year in (posted.year - 1, posted.year, posted.year + 1):
        try:
            candidates.append(date(year, month, day))
        except ValueError:
            continue
    if not candidates:
        return None
    return min(candidates, key=lambda d: abs((d - posted).days))


def parse_header_date(raw: str | None, posted: date) -> date | None:
    """Parse a header date such as ``Sept 21 / MONDAY``; None when unparseable.

    Weekday names are ignored. A missing year takes the year closest to the
    posting date, the same rule #21 uses for deadlines.
    """
    if _blank(raw):
        return None
    m = _MONTH_DAY.search(raw)
    if m:
        month, day = _MONTHS[m.group(1).lower()[:3]], int(m.group(2))
        year = m.group(3)
    else:
        m = _NUMERIC.search(raw)
        if not m:
            return None
        month, day, year = int(m.group(1)), int(m.group(2)), m.group(3)
        if year and len(year) == 2:
            year = f"20{year}"
    if year:
        try:
            return date(int(year), month, day)
        except ValueError:
            return None
    return _closest_year(month, day, posted)


def readback_properties(mismatches: list[str]) -> list[str]:
    """Property names from ``verify_readback`` sentences (``<Prop> differs: …``)."""
    return [m.split(" differs", 1)[0] for m in mismatches]


def order_findings(findings: list[Finding]) -> list[Finding]:
    """Drop duplicates (first wins) and sort by the report table order (stable)."""
    seen: set[str] = set()
    unique: list[Finding] = []
    for f in findings:
        key = f.code if f.code in SINGLE_CODES else f.key()
        if key in seen:
            continue
        seen.add(key)
        unique.append(f)
    rank = {code: i for i, code in enumerate(FINDING_ORDER)}
    return sorted(unique, key=lambda f: rank.get(f.code, len(FINDING_ORDER)))


def verify(facts: CaptureFacts, *, header_date_tolerance_days: int = 1) -> list[Finding]:
    """Return the capture's findings in report order. Pure and deterministic."""
    out: list[Finding] = list(facts.plan_findings)
    extracted = facts.verdict is not None

    if facts.verdict == "unsure":
        out.append(Finding(code="unsure", params={"reasons": facts.reasons or ""}))
    if facts.multiple_jo_suspected:
        out.append(Finding(code="multiple_jo", params={"reasons": facts.reasons or ""}))

    for p in facts.unresolved_people:
        out.append(Finding(code="unresolved_person", params={"role": p.role, "raw": p.raw}))
    for p in facts.ambiguous_people:
        out.append(Finding(code="ambiguous_person", params={"role": p.role, "raw": p.raw}))
    for raw in facts.unresolved_projects:
        out.append(Finding(code="unresolved_project", params={"raw": raw}))
    if facts.unknown_department:
        out.append(Finding(code="unknown_department", params={"token": facts.unknown_department}))
    for m in facts.fuzzy_people:
        out.append(Finding(code="fuzzy_person", params={"raw": m.raw, "contact": m.contact}))

    if facts.exact_repost:
        out.append(
            Finding(
                code="exact_repost",
                params={"url": facts.exact_repost.url, "days": facts.exact_repost.days},
            )
        )
    elif facts.number_reuse:
        out.append(
            Finding(
                code="number_reuse",
                params={
                    "jo_number": facts.number_reuse.jo_number or "",
                    "url": facts.number_reuse.url,
                    "days": facts.number_reuse.days,
                },
            )
        )

    origin = facts.forward_origin
    if origin and origin.type != "user":
        out.append(
            Finding(code="forward_origin", params={"origin": origin.type, "name": origin.name})
        )

    if extracted:
        if _blank(facts.title):
            out.append(Finding(code="title_blank"))
        if _blank(facts.task):
            out.append(Finding(code="task_blank"))

    if facts.deadline and facts.posted_at and facts.deadline < facts.posted_at:
        out.append(Finding(code="deadline_past"))
    if (
        extracted
        and facts.deadline is None
        and (facts.deadline_raw or "").strip().casefold() not in DEADLINE_PLACEHOLDERS
    ):
        out.append(Finding(code="deadline_unparsed", params={"raw": facts.deadline_raw.strip()}))

    if facts.asap:
        out.append(Finding(code="asap"))
    if facts.shift_fallback is not None:
        out.append(Finding(code="shift_fallback", params={"names": list(facts.shift_fallback)}))

    if extracted and facts.posted_at and not _blank(facts.header_date_raw):
        posted = _manila_date(facts.posted_at)
        header = parse_header_date(facts.header_date_raw, posted)
        if header and abs((header - posted).days) > header_date_tolerance_days:
            out.append(
                Finding(
                    code="header_date_mismatch",
                    params={"raw": facts.header_date_raw.strip(), "posted": posted.isoformat()},
                )
            )

    for h in facts.deliverable_hints:
        out.append(
            Finding(code="deliverable_hint", params={"name": h.name, "probability": h.probability})
        )
    if facts.matcher_skipped:
        out.append(Finding(code="matcher_skipped"))

    if facts.readback_mismatches:
        out.append(
            Finding(
                code="readback_mismatch",
                params={"properties": readback_properties(facts.readback_mismatches)},
            )
        )
    if facts.duplicate_key_urls:
        out.append(Finding(code="duplicate_key", params={"urls": list(facts.duplicate_key_urls)}))

    return order_findings(out)


def tags_standing_cc(findings: list[Finding]) -> bool:
    return any(f.code in TAGS_standing_cc for f in findings)
