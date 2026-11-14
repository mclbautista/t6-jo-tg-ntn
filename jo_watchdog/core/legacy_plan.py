"""Upgrade a plan stored before #134 to Job Order fields.

Until #134 a stored plan held the Notion create-page properties. Rows in the
ledger from then keep that JSON (a row in `planned` or `writing` is replayed
from it, and finished rows are read for reports and duplicate checks), so it
must keep loading. This is the one place that still knows the old Notion
property names. Remove it once no ledger row holds a plan from before #134.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from jo_watchdog.core.deadline import MANILA
from jo_watchdog.core.job_order import manila_iso

# Plans stored before #99 carried the watchdog block inside ``Notes`` behind this marker.
LEGACY_WATCHDOG_MARKER = "— added by the watchdog —"

_TEXT = {
    "Name": "name",
    "Telegram Key": "telegram_key",
    "JO Number": "number",
    "Title": "title",
    "Subtitle": "subtitle",
    "Task": "task",
    "Purpose": "purpose",
    "Source / Source Drive": "source",
    "Platform": "platform",
    "Link Settings": "link_settings",
    "Watermark": "watermark",
    "Mode of Transfer": "mode_of_transfer",
    "Blocker ": "blocker",
    "Pending": "pending",
    "JO Notes": "notes",
    "Notes": "notes",
    "Watchdog Notes": "watchdog_notes",
}
_OPTIONS = {"Status": "status", "Department": "department"}
_DATES = {"Date": "date", "Deadline": "deadline"}
_RELATIONS = {"Assigned": "assigned_ids", "cc": "cc_ids", "Deliverable": "deliverable_ids"}


def _split_legacy_block(notes: str) -> tuple[str, str] | None:
    """Split pre-#99 ``<poster>\\n\\n<marker>\\n<lines>`` text into (poster, watchdog lines).

    The block is always last, so only the final marker line counts: a marker quoted
    inside the poster's own text is left alone. ``None`` when there is no block.
    """
    token = f"{LEGACY_WATCHDOG_MARKER}\n"
    if notes.startswith(token):
        return "", notes[len(token) :]
    at = notes.rfind(f"\n\n{token}")
    if at == -1:
        return None
    return notes[:at], notes[at + 2 + len(token) :]


def _text(prop: dict[str, Any]) -> str:
    items = prop.get("title") or prop.get("rich_text") or []
    return "".join(item["text"]["content"] for item in items)


def _instant(date: dict[str, Any]) -> str | None:
    """A stored Notion date as ISO text: an instant at its Manila offset, or a plain day."""
    start = date.get("start")
    if not isinstance(start, str):
        return None
    try:
        parsed = datetime.fromisoformat(start)
    except ValueError:
        return None
    if "T" not in start:
        return start
    if parsed.tzinfo is None:
        try:
            zone = ZoneInfo(date["time_zone"]) if date.get("time_zone") else MANILA
        except Exception:
            zone = MANILA
        parsed = parsed.replace(tzinfo=zone)
    return manila_iso(parsed)


def _fields(properties: dict[str, Any]) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    for name, prop in properties.items():
        if name in _TEXT:
            text = _text(prop)
            if text:
                fields[_TEXT[name]] = text
        elif name in _OPTIONS:
            chosen = (prop.get("select") or prop.get("status") or {}).get("name")
            if chosen:
                fields[_OPTIONS[name]] = chosen
        elif name in _DATES:
            instant = _instant(prop.get("date") or {})
            if instant:
                fields[_DATES[name]] = instant
        elif name in _RELATIONS:
            fields[_RELATIONS[name]] = [item["id"] for item in prop.get("relation") or []]
        elif name == "Projects":
            ids = [item["id"] for item in prop.get("relation") or []]
            if ids:
                fields["project_id"] = ids[0]
    notes = fields.get("notes", "")
    if "watchdog_notes" not in fields:
        legacy = _split_legacy_block(notes)
        if legacy is not None:
            poster, block = legacy
            fields.pop("notes", None)
            if poster:
                fields["notes"] = poster
            if block:
                fields["watchdog_notes"] = block
    return fields


def upgrade(data: dict[str, Any]) -> dict[str, Any]:
    """A stored plan from before #134, as a dict that validates as a ``JobOrderPlan``."""
    if "properties" in data:
        fields = _fields(data["properties"])
    else:
        # A pre-#39 skeleton plan: name, key and watchdog-only notes.
        fields = {"name": data["name"], "telegram_key": data["telegram_key"], "status": "Needs Intake Review"}
        notes = data.get("notes") or ""
        legacy = _split_legacy_block(notes)
        if legacy is not None:
            notes = "\n\n".join(part for part in legacy if part)
        if notes:
            fields["watchdog_notes"] = notes
    upgraded = {key: value for key, value in data.items() if key != "properties"}
    upgraded["fields"] = fields
    return upgraded
