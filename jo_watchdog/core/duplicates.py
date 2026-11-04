"""Pure core: content-level duplicate keys and repost / number-reuse decisions.

Message identity (``Telegram Key``) is the ledger's job. This module decides
whether two *different* messages look like the same JO (#24). It never merges
or suppresses anything: every new message still gets its own row, and a match
only adds an earlier-row reference to Notes plus an ops finding.

The JO Number integer comes from the header parser (#21): ``#074``, ``074``
and ``74`` all give 74, while the displayed JO Number stays as posted.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from jo_watchdog.core.job_order import JobOrderPlan, plain_text, rich_text
from jo_watchdog.core.plan import WATCHDOG_NOTES_PROPERTY


def jo_key(project_page_id: str | None, number: int | None) -> str | None:
    """``<Project page id>|<jo_number_int>``, only when both resolve.

    JO Numbers are one counter per project, shared by every department (#93),
    so the Department stays on the JO but is not part of the key.
    """
    if not project_page_id or number is None:
        return None
    return f"{project_page_id}|{number}"


def normalize_whitespace(text: str) -> str:
    return " ".join(text.split())


def text_sha256(text: str) -> str:
    """SHA-256 of the whitespace-normalized original text."""
    return hashlib.sha256(normalize_whitespace(text).encode("utf-8")).hexdigest()


class EarlierCapture(BaseModel):
    """An earlier ledger row that a new message may duplicate."""

    telegram_key: str
    text_sha256: str | None
    jo_key: str | None
    page_url: str | None
    posted_at: datetime


class DuplicateMatch(BaseModel):
    kind: Literal["exact_repost", "number_reuse"]
    earlier: EarlierCapture
    days_ago: int


def find_duplicate(
    *,
    telegram_key: str,
    text_sha: str,
    key: str | None,
    posted_at: datetime,
    earlier: list[EarlierCapture],
) -> DuplicateMatch | None:
    """Classify a new message against earlier captured rows.

    Both kinds need a complete ``key`` (D1): without one nothing is detected,
    not even a hash-only exact re-post. Exact re-post: same ``text_sha256`` and
    a Notion page. Number reuse: same ``jo_key`` with different text. An exact
    re-post wins over reuse, and the earliest matching row is referenced. Only
    rows with a Notion page count, since the reference is a link to it.
    """
    if key is None:
        return None
    candidates = sorted(
        (e for e in earlier if e.telegram_key != telegram_key and e.page_url),
        key=lambda e: e.posted_at,
    )
    match: tuple[str, EarlierCapture] | None = None
    for e in candidates:
        if e.text_sha256 == text_sha:
            match = ("exact_repost", e)
            break
    if match is None:
        for e in candidates:
            if e.jo_key == key and e.text_sha256 != text_sha:
                match = ("number_reuse", e)
                break
    if match is None:
        return None
    kind, row = match
    days = max((posted_at - row.posted_at).days, 0)
    return DuplicateMatch(kind=kind, earlier=row, days_ago=days)


def notes_line(match: DuplicateMatch) -> str:
    """The watchdog's Notes line for a match (#21 Notes layout)."""
    label = "exact re-post" if match.kind == "exact_repost" else "possible number reuse"
    return f"Earlier row: {match.earlier.page_url} ({label})"


def append_watchdog_line(watchdog_notes: str, line: str) -> str:
    """Add ``line`` to a Watchdog Notes value, one line per item."""
    return f"{watchdog_notes}\n{line}" if watchdog_notes else line


def add_earlier_row_line(plan: JobOrderPlan, line: str) -> None:
    """Append ``line`` to the plan's Watchdog Notes property (never to Notes)."""
    notes = plain_text(plan.properties.get(WATCHDOG_NOTES_PROPERTY))
    plan.properties[WATCHDOG_NOTES_PROPERTY] = {
        "rich_text": rich_text(append_watchdog_line(notes, line))
    }
