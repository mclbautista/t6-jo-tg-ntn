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


def _maskable_spans(line: str) -> list[tuple[int, int]]:
    """Return spans outside URL tokens, which must remain byte-for-byte intact."""
    spans: list[tuple[int, int]] = []
    cursor = 0
    for match in _URL_TOKEN.finditer(line):
        if match.start() > cursor:
            spans.append((cursor, match.start()))
        cursor = match.end()
    if cursor < len(line):
        spans.append((cursor, len(line)))
    return spans


def _mask_segment(segment: str) -> tuple[str, int]:
    """Mask every supported assignment in a URL-free segment."""
    matches = list(_ASSIGNMENT.finditer(segment))
    if not matches:
        return segment, 0

    parts: list[str] = []
    withheld_count = 0
    cursor = 0
    for position, match in enumerate(matches):
        parts.append(segment[cursor : match.end()])
        next_start = matches[position + 1].start() if position + 1 < len(matches) else len(segment)
        value = segment[match.end() : next_start]
        stripped = value.strip()
        if stripped and stripped != WITHHELD:
            leading = len(value) - len(value.lstrip())
            trailing = len(value.rstrip())
            parts.append(value[:leading] + WITHHELD + value[trailing:])
            withheld_count += 1
        else:
            parts.append(value)
        cursor = next_start
    parts.append(segment[cursor:])
    return "".join(parts), withheld_count


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

    parts: list[str] = []
    withheld_count = 0
    cursor = 0
    for start, end in _maskable_spans(line):
        parts.append(line[cursor:start])
        masked, count = _mask_segment(line[start:end])
        parts.append(masked)
        withheld_count += count
        cursor = end
    parts.append(line[cursor:])
    dangling = not line[assignments[-1].end() :].strip()
    return "".join(parts), withheld_count, dangling


def _mask_next_line_value(line: str) -> tuple[str, int]:
    """Mask a dangling assignment's value without altering URL tokens."""
    url_matches = list(_URL_TOKEN.finditer(line))
    if not url_matches:
        value = line.lstrip(" \t")
        if value == WITHHELD:
            return line, 0
        indentation = line[: len(line) - len(value)]
        return indentation + WITHHELD, 1

    parts: list[str] = []
    cursor = 0
    placeholder_added = False
    newly_withheld = False
    for match in url_matches:
        segment = line[cursor : match.start()]
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

    segment = line[cursor:]
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
    return "".join(parts), int(newly_withheld)


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
