"""Pure core: the minimal exact Notion plan for a captured message.

This module has no I/O and no third-party imports beyond Pydantic, so it stays
wrappable by an external orchestrator later (see the technical design, §1).

The walking skeleton's plan is deliberately minimal: ``Name`` (title), the
``Telegram Key`` text property, and the raw-text body blocks that will carry the
source message. Every later ticket widens this model.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field

from jo_watchdog.core.masker import outbound_text

# Notion text items cap at 2000 characters (write-path research, §2).
BLOCK_LIMIT = 2000

# Poster notes go to ``Notes``; everything the watchdog adds goes to ``Watchdog Notes`` (#99).
NOTES_PROPERTY = "JO Notes"
WATCHDOG_NOTES_PROPERTY = "Watchdog Notes"


class Plan(BaseModel):
    """The exact payload to store locally before the external Notion create call."""

    name: str
    telegram_key: str
    body_blocks: list[str] = Field(default_factory=list)
    # Watchdog-only text (the earlier-row reference, #24), written to Watchdog Notes (#99);
    # empty writes no Watchdog Notes.
    notes: str = ""
    # The duplicate decision made at plan time (#24), replayed unchanged:
    # ``{"code": "exact_repost" | "number_reuse", "url", "days", "jo_number"}``.
    duplicate: dict | None = None


def split_blocks(text: str, limit: int = BLOCK_LIMIT) -> list[str]:
    """Split ``text`` into blocks of at most ``limit`` characters.

    Prefers newline boundaries within the limit so a block ends on a line break
    where possible; a single line longer than the limit is hard-split.
    """
    if limit <= 0:
        raise ValueError("limit must be positive")
    if not text:
        return []

    blocks: list[str] = []
    current: list[str] = []
    current_len = 0

    for line in text.split("\n"):
        # A lone line longer than the limit is hard-split (no newline to save it).
        while len(line) > limit:
            blocks.append(line[:limit])
            line = line[limit:]
        if current_len + len(line) + (1 if current else 0) > limit:
            blocks.append("\n".join(current))
            current = []
            current_len = 0
        current.append(line)
        current_len += len(line) + (1 if current_len else 0)

    if current:
        blocks.append("\n".join(current))

    return blocks


def first_line_of(text: str) -> str:
    """Return the first non-empty line of ``text`` (the Name fallback)."""
    for line in text.split("\n"):
        if line.strip():
            return line.strip()
    return ""


_TAG_LINE = re.compile(r"\\?\[[^\[\]]+\\?\]")


def header_line_of(text: str) -> str:
    """Return the first non-empty line that is not a bracketed tag (``[ADVANCED]``).

    Tag lines may precede the header; the header is the first line after them.
    Falls back to the first line when every line is a tag.
    """
    for line in text.split("\n"):
        if line.strip() and not _TAG_LINE.fullmatch(line.strip()):
            return line.strip()
    return first_line_of(text)


def build_plan(name: str, telegram_key: str, raw_text: str, *, source_block: str = "") -> Plan:
    """Build a plan after source-derived text crosses the outbound boundary.

    ``source_block`` (the provenance lines, #24) leads the body when given.
    """
    safe_name = outbound_text(name).text
    safe_source = outbound_text(raw_text).text
    return Plan(
        name=safe_name,
        telegram_key=telegram_key,
        body_blocks=[
            *split_blocks(outbound_text(source_block).text),
            *split_blocks(safe_source),
        ],
    )
