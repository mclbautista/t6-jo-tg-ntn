"""The aiogram adapter turns real ``Update`` objects into ``IncomingUpdate``s."""

import unittest

from aiogram.types import Update

from jo_watchdog.adapters.telegram import AiogramTelegramClient


def _update(**message) -> Update:
    return Update.model_validate(
        {
            "update_id": 41,
            "message": {
                "message_id": 7,
                "date": 1761700000,
                "chat": {"id": -100123, "type": "supergroup"},
                "text": "JO https://example.com",
                **message,
            },
        }
    )


class _Bot:
    def __init__(self, updates):
        self.updates = updates

    async def get_updates(self, **_):
        return self.updates


class GetUpdatesTests(unittest.IsolatedAsyncioTestCase):
    async def test_message_with_link_preview_options_serializes(self):
        # Telegram sends only the preview fields it set; aiogram fills the rest
        # with ``Default`` sentinels that pydantic cannot dump to JSON.
        update = _update(link_preview_options={"url": "https://example.com"})
        (incoming,) = await AiogramTelegramClient(_Bot([update])).get_updates(0, 0)
        self.assertEqual(incoming.update_id, 41)
        self.assertEqual(
            incoming.raw["message"]["link_preview_options"], {"url": "https://example.com"}
        )
