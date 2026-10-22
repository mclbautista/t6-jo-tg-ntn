"""Pure core: deterministic deadline resolution in Asia/Manila time.

A hand-rolled phrase parser that fails closed: it never fills a missing
component implicitly. The resolver takes the resolved assignees' End of Shift
values as an argument, so it is testable without Notion (#22 supplies them).
Rules and the corpus table live in issue #21 (docs/issues/21-*.md).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import date, datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

from jo_watchdog.core.findings import (
    ASAP,
    DEADLINE_PAST,
    DEADLINE_UNPARSED,
    SHIFT_FALLBACK,
    Finding,
)

MANILA = ZoneInfo("Asia/Manila")
END_OF_DAY = time(23, 59)
FLEXIBLE = "Flexible / Variable"

DeadlineSource = Literal["stated", "shift", "fallback", "blank"]

_PLACEHOLDERS = {"-", "n/a", "na", "none"}

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}  # fmt: skip
_DATE = re.compile(
    r"\b(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?"
    r"|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?\s+(\d{1,2})\b",
    re.IGNORECASE,
)
# `5AM`, `5:00 AM`, `7pm`, `530PM` (= 17:30), `12:00 AM` (= 00:00).
_TIME = re.compile(r"\b(\d{1,2})(?::?(\d{2}))?\s*(am|pm)\b", re.IGNORECASE)
_WEEKDAY = re.compile(
    r"\b(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday"
    r"|mon|tues?|wed|thu(?:rs?)?|fri|sat|sun)\b",
    re.IGNORECASE,
)
_SHIFT = re.compile(r"\b(end of shift|within shift|asap)\b", re.IGNORECASE)
_DAY_END = re.compile(r"\b(tonight|today|within the day)\b", re.IGNORECASE)
_BARE_PM = re.compile(r"\bpm\b", re.IGNORECASE)

_PARENTHETICAL = re.compile(r"\([^)]*\)?")
_LEADING_HEDGE = re.compile(r"^\s*(?:if|kung)\s+\S+", re.IGNORECASE)
_HEDGE = re.compile(r"\b(?:if|kung)\b|\sor\s", re.IGNORECASE)
_FILLER = re.compile(r"\b(?:rin|po|na)\b", re.IGNORECASE)
_SHIFT_OPTION = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*(AM|PM)\s*$", re.IGNORECASE)


class DeadlineResult(BaseModel):
    deadline: datetime | None = None
    source: DeadlineSource = "blank"
    findings: list[Finding] = Field(default_factory=list)


def strip_hedges(raw: str) -> str:
    """Remove hedges and filler words; the caller keeps the original for Notes.

    A hedge is a parenthetical, or text starting at ``if``, ``kung`` or `` or ``.
    A leading hedge (``If keri rin po today``) drops only its marker and the
    word after it, so the phrase it qualifies survives.
    """
    text = _PARENTHETICAL.sub(" ", raw)
    text = _LEADING_HEDGE.sub(" ", text, count=1)
    cut = _HEDGE.search(text)
    if cut:
        text = text[: cut.start()]
    text = _FILLER.sub(" ", text)
    return " ".join(text.split())


def _closest_year(month: int, day: int, posted: date) -> date | None:
    candidates = []
    for year in (posted.year - 1, posted.year, posted.year + 1):
        try:
            candidates.append(date(year, month, day))
        except ValueError:
            continue
    if not candidates:
        return None
    return min(candidates, key=lambda d: abs((d - posted).days))


def _parse_time(match: re.Match[str]) -> time | None:
    hour, minute = int(match.group(1)), int(match.group(2) or 0)
    if not 1 <= hour <= 12 or minute > 59:
        return None
    if match.group(3).lower() == "am":
        hour = 0 if hour == 12 else hour
    else:
        hour = 12 if hour == 12 else hour + 12
    return time(hour, minute)


def parse_shift_hour(value: str | time | None) -> time | None:
    """Parse a Contacts ``End of Shift`` option (``7:00 PM``); None if flexible/blank.

    An already-parsed ``time`` (from the resolution slice) passes through.
    """
    if isinstance(value, time):
        return value
    if not value or value.strip() == FLEXIBLE:
        return None
    match = _SHIFT_OPTION.match(value)
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if not 1 <= hour <= 12 or minute > 59:
        return None
    if match.group(3).upper() == "AM":
        hour = 0 if hour == 12 else hour
    else:
        hour = 12 if hour == 12 else hour + 12
    return time(hour, minute)


def _at(day: date, at: time) -> datetime:
    return datetime.combine(day, at, tzinfo=MANILA)


def _resolve_shift(
    posted_at: datetime,
    stated: date | None,
    assignee_shifts: Sequence[tuple[str, str | time | None]],
) -> tuple[datetime, DeadlineSource, list[Finding]]:
    """Rule 7: latest End of Shift across assignees, else the 23:59 fallback."""
    candidates: list[datetime] = []
    defaulted: list[str] = []
    for name, value in assignee_shifts:
        hour = parse_shift_hour(value)
        if hour is None:
            defaulted.append(name)
            continue
        if stated is None:
            occurrence = _at(posted_at.date(), hour)
            if occurrence < posted_at:
                occurrence += timedelta(days=1)
        else:
            # A shift ending in the morning ends the night that starts on `stated`.
            day = stated + timedelta(days=1) if hour < time(12) else stated
            occurrence = _at(day, hour)
        candidates.append(occurrence)

    if candidates:
        return max(candidates), "shift", []

    params: dict = {"defaulted": defaulted} if defaulted else {"reason": "no assignee resolved"}
    fallback = _at(stated or posted_at.date(), END_OF_DAY)
    return fallback, "fallback", [Finding(code=SHIFT_FALLBACK, params=params)]


def resolve_deadline(
    deadline_raw: str | None,
    posted_at: datetime,
    assignee_shifts: Sequence[tuple[str, str | time | None]] = (),
) -> DeadlineResult:
    """Resolve a posted deadline phrase to an Asia/Manila datetime.

    ``posted_at`` is the intake posting (or forwarding) time. ``assignee_shifts``
    holds ``(display name, End of Shift)`` for each resolved assignee; an empty
    sequence means no assignee resolved.
    """
    posted_at = posted_at.astimezone(MANILA)
    original = " ".join((deadline_raw or "").replace("\xa0", " ").split())
    if not original or original.lower() in _PLACEHOLDERS:
        return DeadlineResult()

    text = strip_hedges(original)

    stated: date | None = None
    date_match = _DATE.search(text)
    if date_match:
        month = _MONTHS[date_match.group(1).lower()[:3]]
        stated = _closest_year(month, int(date_match.group(2)), posted_at.date())
        if stated is None:
            return _unparsed(original)
        text = text[: date_match.start()] + " " + text[date_match.end() :]
    text = _WEEKDAY.sub(" ", text)

    at: time | None = None
    time_match = _TIME.search(text)
    if time_match:
        at = _parse_time(time_match)
        if at is None:
            return _unparsed(original)
        text = text[: time_match.start()] + " " + text[time_match.end() :]

    shift_match = _SHIFT.search(text)
    day_end = _DAY_END.search(text) or (at is None and _BARE_PM.search(text))

    findings: list[Finding] = []
    if shift_match and shift_match.group(1).lower() == "asap":
        findings.append(Finding(code=ASAP))

    if at is not None:
        deadline, source = _at(stated or posted_at.date(), at), "stated"
    elif shift_match:
        deadline, source, shift_findings = _resolve_shift(posted_at, stated, assignee_shifts)
        findings.extend(shift_findings)
    elif day_end or stated is not None:
        # Rules 4 and 6: a date followed by a no-clock phrase is still 23:59.
        deadline, source = _at(stated or posted_at.date(), END_OF_DAY), "stated"
    else:
        return _unparsed(original)

    if stated is None and re.sub(r"[\s/,.\-]+", "", _leftover(text)):
        # Without a date, unrecognised words could change the day (`tomorrow`).
        return _unparsed(original)

    if deadline < posted_at:
        findings.append(Finding(code=DEADLINE_PAST, params={"deadline": deadline.isoformat()}))
    return DeadlineResult(deadline=deadline, source=source, findings=findings)


def _leftover(text: str) -> str:
    for pattern in (_SHIFT, _DAY_END, _BARE_PM):
        text = pattern.sub(" ", text)
    return text


def _unparsed(original: str) -> DeadlineResult:
    return DeadlineResult(findings=[Finding(code=DEADLINE_UNPARSED, params={"raw": original})])
