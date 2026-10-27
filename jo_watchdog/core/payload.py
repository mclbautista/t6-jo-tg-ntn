"""Pure core: exact Notion payload construction and read-back comparison."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from zoneinfo import ZoneInfo

from jo_watchdog.core.masker import mask_text
from jo_watchdog.core.plan import BLOCK_LIMIT, Plan

# The only Status the walking skeleton ever writes (job-order field mapping).
STATUS_NEEDS_INTAKE_REVIEW = "Needs Intake Review"


def _text(content: str) -> dict:
    return {"text": {"content": content}}


def build_notion_payload(
    plan: Plan,
    *,
    data_source_id: str,
    status: str = STATUS_NEEDS_INTAKE_REVIEW,
) -> dict:
    """Build the exact Notion create-page payload from a stored plan.

    The body blocks are masked here, at the exit boundary, so the masked text
    (not the raw source) is what reaches Notion. Property names are the exact
    live schema names (``Name``, ``Telegram Key``, ``Status``).
    """
    children = [
        {
            "object": "block",
            "type": "paragraph",
            "paragraph": {"rich_text": [{"type": "text", **_text(mask_text(block))}]},
        }
        for block in plan.body_blocks
    ]
    properties = {
        "Name": {"title": [_text(plan.name)]},
        "Telegram Key": {"rich_text": [_text(plan.telegram_key)]},
        "Status": {"status": {"name": status}},
    }
    if plan.notes:
        properties["Notes"] = {
            "rich_text": [
                _text(plan.notes[i : i + BLOCK_LIMIT])
                for i in range(0, len(plan.notes), BLOCK_LIMIT)
            ]
        }
    return {
        "parent": {"data_source_id": data_source_id},
        "properties": properties,
        "children": children,
    }


def page_url_for(page_id: str) -> str:
    """A working Notion URL for a page id, for rows whose create-time URL was not kept."""
    return f"https://www.notion.so/{page_id.replace('-', '')}"


DATE_PROPERTIES = ("Date", "Deadline")
_MANILA = ZoneInfo("Asia/Manila")


def _minute(value: object, time_zone: str | None = None) -> datetime | None:
    """A Notion date start as an instant truncated to the minute (Notion drops seconds)."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        try:
            zone = ZoneInfo(time_zone) if time_zone else _MANILA
        except Exception:
            zone = _MANILA
        parsed = parsed.replace(tzinfo=zone)
    return parsed.replace(second=0, microsecond=0)


def _date_mismatch(name: str, planned: dict | None, read: dict | None) -> str | None:
    planned_start = (planned or {}).get("start")
    if planned_start is None:
        return None
    want = _minute(planned_start, (planned or {}).get("time_zone"))
    got = _minute((read or {}).get("start"), (read or {}).get("time_zone"))
    if want is not None and got is not None and want == got:
        return None
    shown = (read or {}).get("start")
    return f"{name} differs: planned {planned_start!r}, read {shown!r}"


def verify_readback(
    plan: Plan,
    *,
    retrieved_name: str,
    retrieved_telegram_key: str,
    retrieved_dates: Mapping[str, dict | None] | None = None,
) -> list[str]:
    """Compare a retrieved Notion page against the stored plan.

    Returns a list of human-readable mismatch descriptions (empty when the
    written page matches the plan). Mismatches are recorded, never rewritten.
    Dates are compared by instant, to the minute, so an offset shift is caught
    while Notion's dropped seconds are not. Pass ``retrieved_dates`` to check them.
    """
    mismatches: list[str] = []
    if retrieved_name != plan.name:
        mismatches.append(f"Name differs: planned {plan.name!r}, read {retrieved_name!r}")
    if retrieved_telegram_key != plan.telegram_key:
        mismatches.append(
            f"Telegram Key differs: planned {plan.telegram_key!r}, read {retrieved_telegram_key!r}"
        )
    if retrieved_dates is not None:
        properties = getattr(plan, "properties", {}) or {}
        for name in DATE_PROPERTIES:
            planned = (properties.get(name) or {}).get("date")
            problem = _date_mismatch(name, planned, retrieved_dates.get(name))
            if problem:
                mismatches.append(problem)
    return mismatches


def extract_readback_fields(page: dict) -> dict:
    """Extract ``name``, ``telegram_key`` and the ``dates`` from a Notion page object.

    Handles the real Notion property shapes (``title`` / ``rich_text`` arrays of
    text objects) so the read-back comparison runs against the same data the
    API returns.
    """

    def _plain(item: dict) -> str:
        return item.get("plain_text") or item.get("text", {}).get("content", "") or ""

    props = page.get("properties", {})
    name = ""
    title = props.get("Name", {}).get("title") or []
    if title:
        name = _plain(title[0])
    telegram_key = ""
    rich = props.get("Telegram Key", {}).get("rich_text") or []
    if rich:
        telegram_key = _plain(rich[0])
    dates = {name: (props.get(name) or {}).get("date") for name in DATE_PROPERTIES}
    return {"name": name, "telegram_key": telegram_key, "dates": dates}
