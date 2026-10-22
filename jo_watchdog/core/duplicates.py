"""Pure core: content-level duplicate keys and repost / number-reuse decisions.

Message identity (``Telegram Key``) is the ledger's job. This module decides
whether two *different* messages look like the same JO (#24). It never merges
or suppresses anything: every new message still gets its own row, and a match
only adds an earlier-row reference to Notes plus an ops finding.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel

_JO_NUMBER = re.compile(r"^\s*#?\s*0*(\d+)\s*$")


def jo_number_int(raw: str | None) -> int | None:
    """``#074``, ``074`` and ``74`` all give ``74``; anything else gives ``None``."""
    if raw is None:
        return None
    match = _JO_NUMBER.match(raw)
    return int(match.group(1)) if match else None


def jo_key(project_page_id: str | None, department: str | None, number: int | None) -> str | None:
    """``<Project page id>|<Department option>|<jo_number_int>``, only when all three resolve."""
    if not project_page_id or not department or number is None:
        return None
    return f"{project_page_id}|{department}|{number}"


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
    received_at: datetime


class DuplicateMatch(BaseModel):
    kind: Literal["exact_repost", "number_reuse"]
    earlier: EarlierCapture
    days_ago: int


def find_duplicate(
    *,
    telegram_key: str,
    text_sha: str,
    key: str | None,
    received_at: datetime,
    earlier: list[EarlierCapture],
) -> DuplicateMatch | None:
    """Classify a new message against earlier captured rows.

    Exact re-post: same ``text_sha256`` and a Notion page. Number reuse: same
    ``jo_key`` with different text (skipped when the key is incomplete). An
    exact re-post wins over reuse, and the earliest matching row is referenced.
    Only rows with a Notion page count, since the reference is a link to it.
    """
    candidates = sorted(
        (e for e in earlier if e.telegram_key != telegram_key and e.page_url),
        key=lambda e: e.received_at,
    )
    match: tuple[str, EarlierCapture] | None = None
    for e in candidates:
        if e.text_sha256 == text_sha:
            match = ("exact_repost", e)
            break
    if match is None and key is not None:
        for e in candidates:
            if e.jo_key == key and e.text_sha256 != text_sha:
                match = ("number_reuse", e)
                break
    if match is None:
        return None
    kind, row = match
    return DuplicateMatch(kind=kind, earlier=row, days_ago=(received_at - row.received_at).days)


def notes_line(match: DuplicateMatch) -> str:
    """The watchdog's Notes line for a match (#21 Notes layout)."""
    label = "exact re-post" if match.kind == "exact_repost" else "possible number reuse"
    return f"Earlier row: {match.earlier.page_url} ({label})"


def duplicate_finding(match: DuplicateMatch, *, jo_number: str | None = None) -> dict:
    """The ops finding (code plus parameters, rendered by #23)."""
    params: dict = {"url": match.earlier.page_url, "days": match.days_ago}
    if match.kind == "number_reuse":
        params["jo_number"] = jo_number
    return {"code": match.kind, "params": params}
