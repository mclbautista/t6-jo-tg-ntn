"""Pure core: exact Notion payload construction and read-back comparison."""

from __future__ import annotations

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


def verify_readback(
    plan: Plan, *, retrieved_name: str, retrieved_telegram_key: str
) -> list[str]:
    """Compare a retrieved Notion page against the stored plan.

    Returns a list of human-readable mismatch descriptions (empty when the
    written page matches the plan). Mismatches are recorded, never rewritten.
    """
    mismatches: list[str] = []
    if retrieved_name != plan.name:
        mismatches.append(f"Name differs: planned {plan.name!r}, read {retrieved_name!r}")
    if retrieved_telegram_key != plan.telegram_key:
        mismatches.append(
            f"Telegram Key differs: planned {plan.telegram_key!r}, read {retrieved_telegram_key!r}"
        )
    return mismatches


def extract_readback_fields(page: dict) -> dict:
    """Extract ``name`` and ``telegram_key`` from a Notion page object.

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
    return {"name": name, "telegram_key": telegram_key}
