"""aiogram-based Telegram adapter: a typed client, not an offset-owning dispatcher.

This module imports aiogram only here — the worker never sees it. The adapter
controls the polling offset explicitly (the worker asks the ledger for
``next_offset`` and this adapter passes it straight to ``get_updates``), so no
dispatcher advances the offset behind our back.
"""

from __future__ import annotations

from jo_watchdog.adapters.protocols import IncomingUpdate


class AiogramTelegramClient:
    """Wraps an aiogram ``Bot`` as a plain long-polling client."""

    def __init__(self, bot):
        # ``bot`` is an aiogram.Bot; kept untyped here so importing this module
        # does not force aiogram on callers that only build the pure core.
        self._bot = bot

    async def get_updates(self, offset: int, timeout: int) -> list[IncomingUpdate]:
        updates = await self._bot.get_updates(
            offset=offset,
            timeout=timeout,
            allowed_updates=["message", "edited_message"],
        )
        result: list[IncomingUpdate] = []
        for update in updates:
            message = update.message or update.edited_message
            if message is None:
                continue
            edited = update.message is None
            raw = update.model_dump(mode="json")
            result.append(
                IncomingUpdate(
                    update_id=update.update_id,
                    chat_id=message.chat.id,
                    message_id=message.message_id,
                    message_thread_id=getattr(message, "message_thread_id", None),
                    text=message.text or message.caption or "",
                    raw=raw,
                    edited=edited,
                    edit_date=(raw.get("edited_message") or {}).get("edit_date")
                    if edited
                    else None,
                )
            )
        return result

    async def set_reaction(self, chat_id: int, message_id: int, emoji: str) -> None:
        # ReactionTypeEmoji is imported lazily to keep aiogram out of the hot path
        # for fake-adapter tests.
        from aiogram.types import ReactionTypeEmoji

        await self._bot.set_message_reaction(
            chat_id=chat_id,
            message_id=message_id,
            reaction=[ReactionTypeEmoji(emoji=emoji)],
        )

    async def send_message(self, chat_id: int, topic_id: int, text: str) -> int:
        from aiogram.types import LinkPreviewOptions

        sent = await self._bot.send_message(
            chat_id=chat_id,
            message_thread_id=topic_id,
            text=text,
            parse_mode="HTML",
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
        return sent.message_id
