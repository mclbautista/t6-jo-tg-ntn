"""Pure core: Telegram provenance for the source block and forward findings.

Reads the raw Bot API ``Message`` JSON (what the adapter stores in the ledger)
and turns ``forward_origin`` into the truthful "posted by / originally from"
lines, the intake link, and an original-message link only when Telegram gives
a real message-level reference (#24). No I/O.

Date and deadline math always use the intake time (``message.date``, when the
message entered the topic). The forward's original time is context only.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel

MANILA = ZoneInfo("Asia/Manila")

OriginType = Literal["user", "hidden_user", "chat", "channel"]

# Wording for the ``forward_origin`` finding line (#23 template).
_FINDING_KIND = {"hidden_user": "hidden user", "chat": "group", "channel": "channel"}


class TelegramUser(BaseModel):
    id: int | None = None
    first_name: str = ""
    last_name: str = ""
    username: str | None = None

    @property
    def display_name(self) -> str:
        return " ".join(p for p in (self.first_name, self.last_name) if p).strip()


class ForwardOrigin(BaseModel):
    """The four ``MessageOrigin`` variants, flattened to the fields we use."""

    type: OriginType
    date: datetime
    sender_user: TelegramUser | None = None  # user
    sender_user_name: str | None = None  # hidden_user
    chat_id: int | None = None  # chat (sender_chat) / channel (chat)
    chat_title: str | None = None
    chat_username: str | None = None
    message_id: int | None = None  # channel only
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


def parse_forward_origin(message: dict) -> ForwardOrigin | None:
    """Return the message's forward origin, or ``None`` for a non-forward.

    An origin type this code does not know is treated as no forward rather
    than guessed at.
    """
    origin = message.get("forward_origin")
    if not origin:
        return None
    kind = origin.get("type")
    if kind not in ("user", "hidden_user", "chat", "channel"):
        return None
    chat = origin.get("sender_chat") if kind == "chat" else origin.get("chat")
    chat = chat or {}
    return ForwardOrigin(
        type=kind,
        date=_to_datetime(origin["date"]),
        sender_user=_user(origin.get("sender_user")) if kind == "user" else None,
        sender_user_name=origin.get("sender_user_name") or None,
        chat_id=chat.get("id"),
        chat_title=chat.get("title") or None,
        chat_username=chat.get("username") or None,
        message_id=origin.get("message_id") if kind == "channel" else None,
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


def originally_from(origin: ForwardOrigin | None, contact_name: str | None = None) -> str | None:
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


def _internal_chat_id(chat_id: int) -> str:
    """``-1001234567890`` → ``1234567890`` for ``t.me/c/`` links."""
    text = str(abs(chat_id))
    return text[3:] if text.startswith("100") and len(text) > 3 else text


def intake_link(chat_id: int, topic_id: int | None, message_id: int) -> str:
    internal = _internal_chat_id(chat_id)
    if topic_id is None:
        return f"https://t.me/c/{internal}/{message_id}"
    return f"https://t.me/c/{internal}/{topic_id}/{message_id}"


def original_message_link(origin: ForwardOrigin | None) -> str | None:
    """A link to the forwarded original: channel origins with chat and message id only."""
    if origin is None or origin.type != "channel" or origin.message_id is None:
        return None
    if origin.chat_username:
        return f"https://t.me/{origin.chat_username}/{origin.message_id}"
    if origin.chat_id is not None:
        return f"https://t.me/c/{_internal_chat_id(origin.chat_id)}/{origin.message_id}"
    return None


def source_block(message: dict, *, contact_name: str | None = None) -> str:
    """The body's source block for a captured message (#24 table)."""
    origin = parse_forward_origin(message)
    chat = message.get("chat") or {}
    lines = [
        f"Posted by: {posted_by(message)}",
        f"Posted at: {manila(intake_time(message))}",
        f"Origin: {origin.type if origin else 'none'}",
    ]
    if origin is not None:
        lines.append(f"Originally from: {originally_from(origin, contact_name)}")
        lines.append(f"Original time: {manila(origin.date)}")
    lines.append(
        f"Telegram message: "
        f"{intake_link(chat['id'], message.get('message_thread_id'), message['message_id'])}"
    )
    link = original_message_link(origin)
    if link:
        lines.append(f"Original message: {link}")
    return "\n".join(lines)


def forward_origin_finding(origin: ForwardOrigin | None) -> dict | None:
    """The non-blocking ops finding for a non-``user`` forward, else ``None``."""
    if origin is None or origin.type == "user":
        return None
    return {
        "code": "forward_origin",
        "params": {"kind": _FINDING_KIND[origin.type], "name": originally_from(origin)},
    }
