"""Deterministic credential masking and the shared outbound-text boundary.

V1 deliberately does not guess: unlabeled credentials and labels outside the
supported set are known false negatives. URL tokens are preserved verbatim,
including embedded access keys, because changing them would break source links.
"""

from __future__ import annotations

import re
from typing import NamedTuple

MASKER_VERSION = "1.0.0"
WITHHELD = "[password withheld – see Telegram message]"

# The longest labels come first so ``pass`` cannot consume ``passcode``.
_ASSIGNMENT = re.compile(r"\b(password|passcode|pass|pin|pw)[:=]", re.IGNORECASE)
_URL_TOKEN = re.compile(r"(?:https?|ftp)://\S*|www\.\S*", re.IGNORECASE)


def _mask_value(value: str) -> tuple[str, int]:
    """Mask a value's non-URL text while preserving every URL token."""
    url_matches = list(_URL_TOKEN.finditer(value))
    if not url_matches:
        stripped = value.strip()
        if not stripped or stripped == WITHHELD:
            return value, 0
        leading = len(value) - len(value.lstrip())
        trailing = len(value.rstrip())
        return value[:leading] + WITHHELD + value[trailing:], 1

    parts: list[str] = []
    cursor = 0
    placeholder_added = False
    newly_withheld = False
    for match in url_matches:
        segment = value[cursor : match.start()]
        if segment.strip() == WITHHELD and not placeholder_added:
            parts.append(segment)
            placeholder_added = True
        elif segment.strip() and not placeholder_added:
            leading = len(segment) - len(segment.lstrip())
            trailing = len(segment.rstrip())
            parts.append(segment[:leading] + WITHHELD + segment[trailing:])
            placeholder_added = True
            newly_withheld = True
        elif segment.strip():
            leading = len(segment) - len(segment.lstrip())
            trailing = len(segment.rstrip())
            parts.append(segment[:leading] + segment[trailing:])
        else:
            parts.append(segment)
        parts.append(match.group())
        cursor = match.end()

    segment = value[cursor:]
    if segment.strip() == WITHHELD and not placeholder_added:
        parts.append(segment)
        placeholder_added = True
    elif segment.strip() and not placeholder_added:
        leading = len(segment) - len(segment.lstrip())
        trailing = len(segment.rstrip())
        parts.append(segment[:leading] + WITHHELD + segment[trailing:])
        newly_withheld = True
    elif segment.strip():
        leading = len(segment) - len(segment.lstrip())
        trailing = len(segment.rstrip())
        parts.append(segment[:leading] + segment[trailing:])
    else:
        parts.append(segment)
    return "".join(parts), int(newly_withheld)


def _line_result(line: str) -> tuple[str, int, bool]:
    """Return masked line, count, and whether its last assignment is dangling."""
    url_spans = [(match.start(), match.end()) for match in _URL_TOKEN.finditer(line)]
    assignments = [
        match
        for match in _ASSIGNMENT.finditer(line)
        if not any(start <= match.start() < end for start, end in url_spans)
    ]
    if not assignments:
        return line, 0, False

    parts = [line[: assignments[0].start()]]
    withheld_count = 0
    for position, match in enumerate(assignments):
        value_end = (
            assignments[position + 1].start()
            if position + 1 < len(assignments)
            else len(line)
        )
        parts.append(line[match.start() : match.end()])
        masked_value, count = _mask_value(line[match.end() : value_end])
        parts.append(masked_value)
        withheld_count += count
    dangling = not line[assignments[-1].end() :].strip()
    return "".join(parts), withheld_count, dangling


def _mask_next_line_value(line: str) -> tuple[str, int]:
    """Mask a dangling assignment's value without altering URL tokens."""
    return _mask_value(line)


class MaskedText(NamedTuple):
    """Text approved to leave the protected ledger, with replay metadata."""

    text: str
    withheld_count: int
    masker_version: str


def outbound_text(text: str) -> MaskedText:
    """Return the one safe representation for outbound source-derived text."""
    lines = text.splitlines(keepends=True)
    withheld_count = 0
    index = 0

    while index < len(lines):
        content = lines[index].rstrip("\r\n")
        newline = lines[index][len(content) :]
        masked, count, dangling = _line_result(content)
        lines[index] = masked + newline
        withheld_count += count

        # A separator at end-of-line consumes the next non-blank line while
        # preserving any URL tokens and embedded keys byte-for-byte.
        if dangling:
            next_index = index + 1
            while next_index < len(lines) and not lines[next_index].strip():
                next_index += 1
            if next_index < len(lines):
                next_content = lines[next_index].rstrip("\r\n")
                next_newline = lines[next_index][len(next_content) :]
                masked_value, value_count = _mask_next_line_value(next_content)
                lines[next_index] = masked_value + next_newline
                withheld_count += value_count
        index += 1

    return MaskedText("".join(lines), withheld_count, MASKER_VERSION)


def mask_result(text: str) -> tuple[str, int]:
    """Return masked text and the number of newly withheld assignments."""
    result = outbound_text(text)
    return result.text, result.withheld_count


def mask_text(text: str) -> str:
    """Return the outbound-safe representation of ``text``."""
    return outbound_text(text).text


def mask_blocks(blocks: list[str]) -> list[str]:
    """Apply :func:`mask_text` to each block."""
    return [mask_text(b) for b in blocks]
