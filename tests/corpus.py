"""Test-only stand-ins for the redacted corpus: extraction fields and posting time.

The corpus has no Telegram metadata and #20's model extraction is not under
test here, so a ``Label: value`` reader supplies the raw fields and the posted
date line (at 10:00 Manila) stands in for the intake posting time.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from t6.domain.studio_time import MANILA

FIXTURES = Path(__file__).parent / "fixtures"
CORPUS_YEAR = 2026  # the weekday names in the corpus match 2026
POSTED_HOUR = 10

_LABELS = {
    "title": "title",
    "task": "task",
    "purpose": "purpose",
    "source": "source",
    "source drive": "source",
    "watermark": "watermark",
    "mode of transfer": "mode_of_transfer",
    "link settings": "link_settings",
    "platform": "platform",
    "blocker": "blocker",
    "pending": "pending",
    "note": "notes",
    "notes": "notes",
    "deadline": "deadline_raw",
}
_LINE = re.compile(r"^\s*([A-Za-z ]+?)\s*:\s*(.*)$")
_DATE_LINE = re.compile(r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep)[a-z]*\.?\s+(\d{1,2})\b", re.I)
_MONTHS = {
    m: i
    for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep"], start=1)
}


def load_corpus() -> list[str]:
    return json.loads((FIXTURES / "redacted_corpus.json").read_text(encoding="utf-8"))


def extraction_for(text: str) -> SimpleNamespace:
    fields = {name: None for name in set(_LABELS.values())}
    fields.update(header_raw=None, jo_number_raw=None, department_raw=None, subtitle=None)
    lines = [line for line in text.split("\n") if line.strip()]
    fields["header_raw"] = lines[0].strip()
    for line in lines[1:]:
        match = _LINE.match(line)
        if match and match.group(1).strip().lower() in _LABELS:
            field = _LABELS[match.group(1).strip().lower()]
            if fields[field] is None:
                fields[field] = match.group(2).strip() or None
    return SimpleNamespace(**fields)


def posted_at_for(text: str) -> datetime:
    for line in [line for line in text.split("\n") if line.strip()][:3]:
        match = _DATE_LINE.search(line)
        if match:
            month = _MONTHS[match.group(1).lower()[:3]]
            return datetime(CORPUS_YEAR, month, int(match.group(2)), POSTED_HOUR, tzinfo=MANILA)
    raise ValueError(f"no posted date in {text[:40]!r}")
