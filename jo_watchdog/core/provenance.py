"""Pure core: Telegram provenance for the Job Order body's source block (#24).

Reads the raw Bot API ``Message`` JSON (what the adapter stores in the ledger)
and turns it into the truthful "posted by / originally from" block. The links
and the ``forward_origin`` finding come from the ops-report code (#23), so a
channel original is linked the same way everywhere and no other origin type
ever gets a link. No I/O.

Date and deadline math always use the intake time (``message.date``, when the
message entered the topic). The forward's original time is context only.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel

from jo_watchdog.core.report import forward_origin_from_raw
from jo_watchdog.core.verify import MANILA

OriginType = Literal["user", "hidden_user", "chat", "channel"]


class TelegramUser(BaseModel):
    id: int | None = None
    first_name: str = ""
    last_name: str = ""
    username: str | None = None

    @property
    def display_name(self) -> str:
        return " ".join(p for p in (self.first_name, self.last_name) if p).strip()


class MessageOrigin(BaseModel):
    """The four Bot API ``MessageOrigin`` variants, flattened to the fields we use."""

    type: OriginType
    date: datetime | None = None  # always sent by Telegram; tolerated if missing
    sender_user: TelegramUser | None = None  # user
    sender_user_name: str | None = None  # hidden_user
    chat_title: str | None = None  # chat (sender_chat) / channel (chat)
    author_signature: str | None = None  # chat / channel


def _to_datetime(value: Any) -> datetime:
    """Bot API dates are unix seconds; accept ISO strings for safety."""
    if isinstance(value, int | float):
        return datetime.fromtimestamp(value, UTC)
    parsed = datetime.fromisoformat(str(value))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _user(data: dict | None) -> TelegramUser | None:
    if not data:
        return None
    return TelegramUser(
        id=data.get("id"),
        first_name=data.get("first_name") or "",
        last_name=data.get("last_name") or "",
        username=data.get("username") or None,
    )


def parse_forward_origin(message: dict) -> MessageOrigin | None:
    """Return the message's forward origin, or ``None`` for a non-forward.

    An origin type this code does not know is treated as no forward rather
    than guessed at.
    """
    origin = message.get("forward_origin")
    if not isinstance(origin, dict):
        return None
    kind = origin.get("type")
    if kind not in ("user", "hidden_user", "chat", "channel"):
        return None
    chat = (origin.get("sender_chat") if kind == "chat" else origin.get("chat")) or {}
    return MessageOrigin(
        type=kind,
        date=_to_datetime(origin["date"]) if origin.get("date") is not None else None,
        sender_user=_user(origin.get("sender_user")) if kind == "user" else None,
        sender_user_name=origin.get("sender_user_name") or None,
        chat_title=chat.get("title") or None,
        author_signature=origin.get("author_signature") or None,
    )


def intake_time(message: dict) -> datetime:
    """When the message entered the intake topic: the base for Date and deadlines."""
    return _to_datetime(message["date"])


def manila(dt: datetime) -> str:
    """Render a datetime in Manila time, e.g. ``Tue, Sep 22 2026 14:05``."""
    local = dt.astimezone(MANILA)
    return f"{local:%a, %b} {local.day} {local:%Y %H:%M}"


def _with_handle(name: str, username: str | None) -> str:
    name = name or "(no name)"
    return f"{name} (@{username})" if username else name


def posted_by(message: dict) -> str:
    """The message's own sender: the forwarder when it is a forward."""
    sender = _user(message.get("from"))
    if sender is not None:
        return _with_handle(sender.display_name, sender.username)
    sender_chat = message.get("sender_chat") or {}
    return sender_chat.get("title") or "(unknown sender)"


def _with_signature(title: str | None, signature: str | None) -> str:
    title = title or "(untitled)"
    return f"{title} — {signature}" if signature else title


def originally_from(origin: MessageOrigin | None, contact_name: str | None = None) -> str | None:
    """The best available original identity, or ``None`` for a non-forward.

    ``contact_name`` is the resolved Contact for a ``user`` origin whose id or
    handle matched; it is shown in brackets and never written to a relation.
    """
    if origin is None:
        return None
    if origin.type == "user":
        user = origin.sender_user or TelegramUser()
        text = _with_handle(user.display_name, user.username)
        return f"{text} [{contact_name}]" if contact_name else text
    if origin.type == "hidden_user":
        return origin.sender_user_name or "(hidden user, no name given)"
    return _with_signature(origin.chat_title, origin.author_signature)


def source_block(message: dict, *, intake_url: str, contact_name: str | None = None) -> str:
    """The body's source block for a captured message (#24 table).

    ``intake_url`` is the message's own link in the intake topic.
    """
    origin = parse_forward_origin(message)
    lines = [
        f"Posted by: {posted_by(message)}",
        f"Posted at: {manila(intake_time(message))}",
        f"Origin: {origin.type if origin else 'none'}",
    ]
    if origin is not None:
        lines.append(f"Originally from: {originally_from(origin, contact_name)}")
        if origin.date is not None:
            lines.append(f"Original time: {manila(origin.date)}")
    lines.append(f"Telegram message: {intake_url}")
    summary = forward_origin_from_raw(message)
    if summary is not None and summary.original_link:
        lines.append(f"Original message: {summary.original_link}")
    return "\n".join(lines)
