"""Pure core: Job Order header parsing, canonical Name, and JO Number.

The header is the first non-empty line (``header_raw``). It parses when it
splits on ``-`` (spaces optional) into a project token, a department token and
a number part; otherwise Name falls back to the posted first line.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping

from pydantic import BaseModel

# `JO #74`, `JO No. 15`, `#0155`, `JOB ORDER 27`, `JO01`. A dash between the
# label and the digits (`JOB ORDER - 033`) is accepted because the header split
# keeps everything after the department token as the number part.
_NUMBER = re.compile(
    r"(?:JOB\s+ORDER|JO)?\s*-?\s*(?:No\.?|#)?\s*(\d+)",
    re.IGNORECASE,
)
_SEPARATOR = re.compile(r"\s*-\s*")
# A `JO CODE:` label is not part of the project token (`JO CODE: INA-TOC-30`).
_LABEL = re.compile(r"^\s*JO\s+CODE\s*:\s*", re.IGNORECASE)


class Header(BaseModel):
    """A fully parsed header: all three tokens are present."""

    project: str
    department: str
    number: str  # digits as posted, leading zeros kept

    @property
    def jo_number(self) -> str:
        return f"#{self.number}"

    @property
    def jo_number_int(self) -> int:
        return int(self.number)


def parse_number(text: str | None) -> str | None:
    """Return the digits of a JO number part as posted, or None."""
    if not text:
        return None
    match = _NUMBER.fullmatch(text.strip())
    return match.group(1) if match else None


def normalize_department(token: str, aliases: Mapping[str, str]) -> str:
    """Look the token up in the alias map (case-insensitively), then uppercase."""
    token = token.strip()
    folded = {k.strip().upper(): v for k, v in aliases.items()}
    return folded.get(token.upper(), token).strip().upper()


def parse_header(header_raw: str | None, aliases: Mapping[str, str]) -> Header | None:
    """Parse a header into project, department and number, or None.

    A stray leading backslash (a Telegram markdown-escape artifact, ``\\LS``) is
    dropped from the project token; everything else is kept as posted.
    """
    if not header_raw:
        return None
    line = _LABEL.sub("", header_raw.strip())
    parts = _SEPARATOR.split(line, maxsplit=2)
    if len(parts) != 3:
        return None
    project, department, number_part = (p.strip() for p in parts)
    project = project.lstrip("\\").strip()
    digits = parse_number(number_part)
    if not project or not department or digits is None:
        return None
    return Header(
        project=project,
        department=normalize_department(department, aliases),
        number=digits,
    )


def canonical_name(
    header: Header | None,
    fallback: str,
    valid_departments: Collection[str] | None = None,
) -> str:
    """``<project> - <DEPT> - JO #<digits>``, or ``fallback`` when it can't be built.

    ``valid_departments`` is the live option set from the resolution slice
    (#22). ``None`` means validation has not run yet, so the token is accepted.
    """
    if header is None:
        return fallback
    if valid_departments is not None and header.department not in valid_departments:
        return fallback
    return f"{header.project} - {header.department} - JO #{header.number}"
