"""Turn a missed intake message into the update the poller would have stored (#101).

Ops backfill a message that never reached the watchdog (the gateway dropped two on
2026-11-04) with ``python -m jo_watchdog.app ingest``. The payload is normalized to a
Telegram update shaped like the ones the poller stores, so the worker's normal
pipeline reads its text, entities, posting date and forward origin unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Key added to the stored update so a backfilled row says who entered it and when.
PROVENANCE_KEY = "jo_watchdog_backfill"


class BackfillError(ValueError):
    """The payload is not one intake message the pipeline can capture."""


@dataclass(frozen=True)
class Backfill:
    chat_id: int
    message_id: int
    topic_id: int | None
    text: str
    raw: dict[str, Any]  # a Telegram update: ``{"message": {...}, PROVENANCE_KEY: {...}}``


def from_payload(payload: Any, *, by: str, at: str) -> Backfill:
    """A Telegram update, a bare Telegram message, or a gateway item (``{"raw": update}``)."""
    if not isinstance(payload, dict):
        raise BackfillError("the JSON must be an object")
    if isinstance(payload.get("raw"), dict):
        payload = payload["raw"]
    if "edited_message" in payload:
        raise BackfillError("an edit is not a capture; backfill the original message")
    if "message" in payload:
        update = dict(payload)
    elif "message_id" in payload and "chat" in payload:
        update = {"message": payload}
    else:
        raise BackfillError("expected a Telegram update, a Telegram message or a gateway item")
    message = update["message"]
    if not isinstance(message, dict):
        raise BackfillError("message must be an object")
    chat = message.get("chat")
    message_id = message.get("message_id")
    if not isinstance(chat, dict) or not isinstance(chat.get("id"), int):
        raise BackfillError("message.chat.id is missing")
    if not isinstance(message_id, int):
        raise BackfillError("message.message_id is missing")
    update[PROVENANCE_KEY] = {"by": by, "at": at}
    return Backfill(
        chat_id=chat["id"],
        message_id=message_id,
        topic_id=message.get("message_thread_id"),
        text=message.get("text") or message.get("caption") or "",
        raw=update,
    )


def from_text(
    text: str,
    *,
    message_id: int,
    chat_id: int,
    topic_id: int,
    from_user: str | None,
    date: int | None,
    by: str,
    at: str,
) -> Backfill:
    """An intake message rebuilt from its text, when the original update is not at hand.

    Entities (``text_mention``) and forward origin are lost, so a JSON payload is
    preferred whenever one exists.
    """
    message: dict[str, Any] = {
        "message_id": message_id,
        "chat": {"id": chat_id, "type": "supergroup"},
        "message_thread_id": topic_id,
        "is_topic_message": True,
        "text": text,
    }
    if from_user:
        handle = from_user.strip().lstrip("@")
        message["from"] = (
            {"id": int(handle), "is_bot": False}
            if handle.isdigit()
            else {"username": handle, "is_bot": False}
        )
    if date is not None:
        message["date"] = date
    return from_payload({"message": message}, by=by, at=at)


def scope_problem(backfill: Backfill, *, intake_chat_id: int, intake_topic_id: int) -> str | None:
    """Why the message must not be captured, or None when it is a capturable intake post."""
    if backfill.chat_id != intake_chat_id:
        return f"chat {backfill.chat_id} is not the intake chat {intake_chat_id}"
    if backfill.topic_id != intake_topic_id:
        return f"topic {backfill.topic_id} is not the intake topic {intake_topic_id}"
    if not backfill.text.strip():
        return "the message has no text to capture"
    return None
