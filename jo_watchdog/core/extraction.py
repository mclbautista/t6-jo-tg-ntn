"""Pure core: the DeepSeek classify/extract contract.

The model returns verbatim raw strings and a bounded verdict, nothing more
(*resolve, don't generate*). This module owns the response schema, the single
user message the model sees, and the system prompt with its version tag. It has
no I/O so the same contract can be replayed against recorded responses.
"""

from __future__ import annotations

import json
import re
from functools import cache
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, field_validator

from jo_watchdog.core.masker import outbound_text

# Changing the prompt text or the few-shot list means a new tag.
PROMPT_TAG = "extract-v1"
FEWSHOT_PATH = Path(__file__).resolve().parent.parent / "prompts" / f"{PROMPT_TAG}.fewshot.json"

Verdict = Literal["jo", "unsure", "not_jo"]
Confidence = Literal["low", "medium", "high"]

RAW_FIELDS = (
    "header_raw",
    "jo_number_raw",
    "project_raw",
    "department_raw",
    "header_date_raw",
    "title",
    "task",
    "purpose",
    "source",
    "watermark",
    "link_settings",
    "mode_of_transfer",
    "platform",
    "blocker",
    "pending",
    "notes",
    "deadline_raw",
    "assignees_raw",
    "cc_raw",
)
EXTRACTED_FIELDS = RAW_FIELDS + ("subtitle",)

_SUBTITLE = re.compile(r"Burned-in: \S.*")


class Extraction(BaseModel):
    """One validated classify/extract response. Unknown keys are a schema violation."""

    model_config = ConfigDict(extra="forbid")

    verdict: Verdict
    confidence: Confidence
    reasons: str
    multiple_jo_suspected: bool

    header_raw: str | None
    jo_number_raw: str | None
    project_raw: str | None
    department_raw: str | None
    header_date_raw: str | None
    title: str | None
    task: str | None
    purpose: str | None
    source: str | None
    watermark: str | None
    link_settings: str | None
    mode_of_transfer: str | None
    platform: str | None
    blocker: str | None
    pending: str | None
    notes: str | None
    deadline_raw: str | None
    assignees_raw: str | None
    cc_raw: str | None
    subtitle: str | None

    @field_validator("reasons")
    @classmethod
    def _reasons_present(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("reasons must not be empty")
        return value

    @field_validator("subtitle")
    @classmethod
    def _subtitle_is_bounded(cls, value: str | None) -> str | None:
        if value is None or value == "None" or _SUBTITLE.fullmatch(value):
            return value
        raise ValueError("subtitle must be 'Burned-in: <language>', 'None', or null")

    @field_validator(*RAW_FIELDS)
    @classmethod
    def _blank_is_null(cls, value: str | None) -> str | None:
        # An empty string carries no fact; normalise it so "blank" has one shape.
        if value is not None and not value.strip():
            return None
        return value


# -- model input --------------------------------------------------------------


def _display_name(user: dict[str, Any] | None) -> str | None:
    if not user:
        return None
    name = " ".join(p for p in (user.get("first_name"), user.get("last_name")) if p)
    return name or user.get("username")


def forward_origin_lines(raw_message: dict[str, Any]) -> list[str] | None:
    """The permitted forward-origin context, or None when the message is not a forward.

    Only origin type, forwarder display name, and original author display name
    or chat/channel title are passed. Dates, ids, and links never are.
    """
    origin = raw_message.get("forward_origin")
    if not isinstance(origin, dict):
        return None
    origin_type = origin.get("type") or "unknown"
    forwarder = _display_name(raw_message.get("from") or raw_message.get("from_user"))
    if origin_type == "user":
        original = _display_name(origin.get("sender_user"))
    elif origin_type == "hidden_user":
        original = origin.get("sender_user_name")
    elif origin_type == "chat":
        original = (origin.get("sender_chat") or {}).get("title")
    elif origin_type == "channel":
        original = (origin.get("chat") or {}).get("title")
    else:
        original = None
    return [
        f"Origin type: {origin_type}",
        f"Forwarded by: {forwarder or 'unknown'}",
        f"Originally from: {original or 'unknown'}",
    ]


def build_user_message(text: str, origin_lines: list[str] | None = None) -> str:
    """The single user message: masked text, then the origin block for forwards only.

    Masking is enforced here, not trusted from the caller, so no caller can send
    an unmasked credential to the model.
    """
    parts = [outbound_text(text).text]
    if origin_lines:
        header = "--- forward origin (context only, not field values) ---"
        block = "\n".join([header, *origin_lines])
        parts.append(outbound_text(block).text)
    return "\n\n".join(parts)


SYSTEM_PROMPT = """\
You read one Telegram message from a post-production intake topic and decide whether it is \
a Job Order (JO). Reply with one JSON object and nothing else.

Keys (every key is required):
- verdict: "jo" for a job order; "not_jo" for chatter, replies, thanks, status updates or \
questions; "unsure" when it might be a job order but you cannot tell.
- confidence: "low", "medium" or "high".
- reasons: one or two short sentences explaining the verdict.
- multiple_jo_suspected: true only if the message seems to contain more than one separate job \
order. When true, extract the fields of the first one only.
- header_raw: the first non-empty line, exactly as written.
- project_raw, department_raw, jo_number_raw: the project code, department code and number \
part of the JO header as written (e.g. "MFG", "TOC", "JO #74"), or null.
- header_date_raw: the date line under the header as written, without a "Date:" label.
- title, task, purpose, source, watermark, link_settings, mode_of_transfer, platform, blocker, \
pending, notes, deadline_raw, assignees_raw, cc_raw: the value written after the matching \
label, copied verbatim (keep hedges such as "if keri" and multi-line lists). "Source", \
"Source Drive" and "Source Material" all go to source. "Assigned" goes to assignees_raw. \
Unlabeled lines after a label belong to that label.
- subtitle: "Burned-in: <language>" only when the message says subtitles are burned in and \
names the language, "None" when the poster wrote "-" or "none" under Subtitle, otherwise null.

Rules:
- Copy text; never rewrite, translate, normalise dates, resolve names, or add anything.
- A field the poster left empty, or did not write, is null. Never invent a placeholder; if the \
poster typed "-", "N/A" or "none", copy it as written.
- Forward-origin context may inform the verdict but never supplies a field value.
"""


@cache
def fewshot_messages() -> list[dict[str, str]]:
    """The committed few-shot turns for ``PROMPT_TAG`` (user/assistant pairs)."""
    with open(FEWSHOT_PATH, encoding="utf-8") as fh:
        examples = json.load(fh)["examples"]
    messages: list[dict[str, str]] = []
    for example in examples:
        messages.append({"role": "user", "content": build_user_message(example["input"])})
        messages.append(
            {"role": "assistant", "content": json.dumps(example["output"], ensure_ascii=False)}
        )
    return messages


def build_messages(masked_text: str, origin_lines: list[str] | None = None) -> list[dict]:
    """The complete chat message list: system prompt, few-shot turns, then the message."""
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        *fewshot_messages(),
        {"role": "user", "content": build_user_message(masked_text, origin_lines)},
    ]


# -- extraction scoring (report only, never a CI gate) -----------------------


def normalize_ws(value: str | None) -> str | None:
    if value is None:
        return None
    collapsed = " ".join(value.split())
    return collapsed or None


def field_matches(expected: dict, actual: dict) -> dict[str, bool]:
    """Per-field exact match after whitespace normalisation."""
    return {
        field: normalize_ws(expected.get(field)) == normalize_ws(actual.get(field))
        for field in EXTRACTED_FIELDS
    }
