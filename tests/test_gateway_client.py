"""``GatewayTelegramClient`` speaks the gateway's module API through a fake gateway."""

import unittest

import httpx

from jo_watchdog.adapters.gateway import GatewayTelegramClient
from jo_watchdog.adapters.protocols import GatewayRef, TelegramClient
from tests.fake_gateway import SECRET, FakeGateway

URL = "http://gateway.test:8780"


def client(gateway: FakeGateway, secret: str = SECRET) -> GatewayTelegramClient:
    return GatewayTelegramClient(URL, secret, transport=gateway.transport())


class GetUpdatesTests(unittest.IsolatedAsyncioTestCase):
    async def test_satisfies_the_telegram_client_protocol(self):
        self.assertIsInstance(client(FakeGateway()), TelegramClient)

    async def test_maps_items_with_seq_as_update_id(self):
        gateway = FakeGateway()
        gateway.post(-100123, 5, 42, "MG - TOC - JO #41")
        gateway.post(-100123, None, 43, "no topic")
        first, second = await client(gateway).get_updates(offset=1, timeout=0)
        self.assertEqual(
            (first.update_id, first.chat_id, first.message_id, first.message_thread_id),
            (1, -100123, 42, 5),
        )
        self.assertEqual(first.text, "MG - TOC - JO #41")
        self.assertEqual(first.raw["message"]["text"], "MG - TOC - JO #41")
        self.assertFalse(first.edited)
        self.assertEqual(second.update_id, 2)
        self.assertIsNone(second.message_thread_id)

    async def test_offset_is_the_first_seq_wanted(self):
        gateway = FakeGateway()
        for n in range(3):
            gateway.post(-100123, 5, n + 1, "x")
        updates = await client(gateway).get_updates(offset=3, timeout=0)
        self.assertEqual([u.update_id for u in updates], [3])
        (request,) = gateway.requests
        self.assertEqual(request.url.params["after"], "2")

    async def test_fresh_ledger_offset_zero_reads_from_the_start(self):
        gateway = FakeGateway()
        gateway.post(-100123, 5, 1, "x")
        updates = await client(gateway).get_updates(offset=0, timeout=0)
        self.assertEqual([u.update_id for u in updates], [1])
        self.assertEqual(gateway.requests[0].url.params["after"], "0")

    async def test_sends_the_bearer_secret_and_long_poll_timeout(self):
        gateway = FakeGateway()
        await client(gateway).get_updates(offset=1, timeout=50)
        request = gateway.requests[0]
        self.assertEqual(request.headers["authorization"], f"Bearer {SECRET}")
        self.assertEqual(request.url.params["timeout"], "50")

    async def test_edited_items_carry_edit_date(self):
        gateway = FakeGateway()
        gateway.post(-100123, 5, 42, "fixed", edited=True, edit_date=1790565000)
        (u,) = await client(gateway).get_updates(offset=1, timeout=0)
        self.assertTrue(u.edited)
        self.assertEqual(u.edit_date, 1790565000)

    async def test_command_items_are_passed_on_so_the_cursor_can_pass_them(self):
        gateway = FakeGateway()
        gateway.post(-100123, 5, 1, "x")
        gateway.items[0]["kind"] = "command"
        (u,) = await client(gateway).get_updates(offset=1, timeout=0)
        self.assertEqual(u.update_id, 1)

    async def test_wrong_secret_raises(self):
        gateway = FakeGateway()
        with self.assertRaises(httpx.HTTPStatusError):
            await client(gateway, secret="x" * 32).get_updates(offset=1, timeout=0)

    async def test_server_error_raises(self):
        gateway = FakeGateway()
        gateway.fail_next = 1
        with self.assertRaises(httpx.HTTPStatusError):
            await client(gateway).get_updates(offset=1, timeout=0)


class SendTests(unittest.IsolatedAsyncioTestCase):
    async def test_send_message_posts_notify_and_returns_a_gateway_ref(self):
        gateway = FakeGateway()
        ref = await client(gateway).send_message(
            -100999, 7, "<b>hi</b>", idempotency_key="watchdog-outbox-3"
        )
        self.assertEqual(ref, GatewayRef(ref=5530))  # queued: a ref, not a Telegram ID
        self.assertEqual(
            gateway.notifications,
            [
                {
                    "to": -100999,
                    "topic": 7,
                    "text": "<b>hi</b>",
                    "format": "html",
                    "idempotency_key": "watchdog-outbox-3",
                }
            ],
        )

    async def test_same_key_queues_once(self):
        gateway = FakeGateway()
        c = client(gateway)
        a = await c.send_message(-100999, 7, "hi", idempotency_key="watchdog-outbox-3")
        b = await c.send_message(-100999, 7, "hi", idempotency_key="watchdog-outbox-3")
        self.assertEqual(a, b)
        self.assertEqual(len(gateway.notifications), 1)

    async def test_send_failure_raises(self):
        gateway = FakeGateway()
        gateway.fail_next = 1
        with self.assertRaises(httpx.HTTPStatusError):
            await client(gateway).send_message(-100999, 7, "hi", idempotency_key="k")

    async def test_set_reaction_posts_react(self):
        gateway = FakeGateway()
        await client(gateway).set_reaction(-100123, 42, "👍")
        self.assertEqual(
            gateway.reactions, [{"chat": -100123, "message_id": 42, "emoji": "👍"}]
        )


if __name__ == "__main__":
    unittest.main()
