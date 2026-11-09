"""Heartbeat backlinks in gateway mode use the real Telegram ID, never the gateway ref (#109)."""

import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

from jo_watchdog.adapters.gateway import GatewayTelegramClient
from jo_watchdog.app.worker import Worker
from jo_watchdog.ledger.db import Ledger, OutboxMessage
from jo_watchdog.ledger.migrations import MIGRATIONS
from tests.fake_gateway import SECRET, FakeGateway
from tests.fakes import FakeClassifier, FakeNotion
from tests.test_gateway_mode import URL, gateway_config
from tests.test_worker import no_sleep

DAY1 = datetime(2026, 11, 3, 8, 0, tzinfo=UTC)


class Rig:
    """Worker + ledger over the fake gateway, with a clock the test moves."""

    def __init__(self, test: unittest.IsolatedAsyncioTestCase, path: Path | None = None):
        if path is None:
            tmp = tempfile.TemporaryDirectory()
            test.addCleanup(tmp.cleanup)
            path = Path(tmp.name) / "ledger.db"
        self.path = path
        self.now = DAY1
        self.gateway = FakeGateway()
        self.notion = FakeNotion()
        self.start(test)

    def start(self, test: unittest.IsolatedAsyncioTestCase) -> None:
        """(Re)open the ledger and worker, like a process restart."""
        self.ledger = Ledger(self.path)
        test.addCleanup(self.ledger.close)
        self.worker = Worker(
            ledger=self.ledger,
            telegram=GatewayTelegramClient(URL, SECRET, transport=self.gateway.transport()),
            notion=self.notion,
            classifier=FakeClassifier(),
            config=gateway_config(),
            sleep=no_sleep,
            clock=lambda: self.now,
        )

    async def next_day(self) -> None:
        self.now += timedelta(days=1)
        await self.worker.run_once()

    def outbox(self) -> list[dict]:
        return [dict(r) for r in self.ledger._conn.execute("SELECT * FROM outbox ORDER BY id")]

    def heartbeat_texts(self) -> list[str]:
        return [r["text"] for r in self.outbox() if r["kind"] == "heartbeat"]


class GatewayBacklinkTests(unittest.IsolatedAsyncioTestCase):
    async def test_backlink_uses_the_real_telegram_id_not_the_gateway_ref(self):
        rig = Rig(self)
        await rig.worker.run_once()  # a new ledger skips its first window
        await rig.next_day()  # heartbeat 1 queued at the gateway as ref 5530
        (row,) = rig.outbox()
        self.assertEqual((row["state"], row["gateway_ref"]), ("accepted", 5530))
        self.assertIsNone(row["telegram_message_id"])

        rig.gateway.set_status(5530, "sent", telegram_message_id=7001)
        await rig.next_day()  # resolves 5530 -> 7001, then heartbeat 2 links it
        first, second = rig.outbox()
        self.assertEqual((first["state"], first["telegram_message_id"]), ("sent", 7001))
        self.assertIn("/7001", second["text"])
        self.assertNotIn("/5530", second["text"])

    async def test_a_queued_post_stays_accepted_and_is_never_linked(self):
        rig = Rig(self)
        await rig.worker.run_once()
        await rig.next_day()
        await rig.next_day()  # the gateway still says queued
        first, second = rig.outbox()
        self.assertEqual(first["state"], "accepted")
        self.assertIsNone(first["telegram_message_id"])
        self.assertNotIn("t.me", second["text"])
        self.assertNotIn("5530", second["text"])

        rig.gateway.set_status(5530, "sent", telegram_message_id=7001)
        await rig.worker.run_once()
        self.assertEqual(rig.outbox()[0]["state"], "sent")
        self.assertEqual(rig.outbox()[0]["telegram_message_id"], 7001)

    async def test_an_unresolved_previous_heartbeat_falls_back_to_the_newest_confirmed_one(self):
        rig = Rig(self)
        await rig.worker.run_once()
        await rig.next_day()  # heartbeat 1 -> ref 5530
        rig.gateway.set_status(5530, "sent", telegram_message_id=7001)
        await rig.next_day()  # heartbeat 2 -> ref 5531, still queued
        await rig.next_day()  # heartbeat 3 falls back to heartbeat 1
        third = rig.outbox()[2]["text"]
        self.assertIn("/7001", third)
        self.assertNotIn("/5530", third)
        self.assertNotIn("/5531", third)

    async def test_a_restart_between_enqueue_and_resolution_leaves_the_row_accepted(self):
        rig = Rig(self)
        await rig.worker.run_once()
        await rig.next_day()
        rig.ledger.close()
        rig.start(self)
        self.assertEqual(rig.outbox()[0]["state"], "accepted")

        rig.gateway.set_status(5530, "sent", telegram_message_id=7001)
        await rig.worker.run_once()
        self.assertEqual(rig.outbox()[0]["state"], "sent")
        self.assertEqual(rig.outbox()[0]["telegram_message_id"], 7001)

    async def test_resolution_is_bounded_per_cycle(self):
        rig = Rig(self)
        for n in range(30):
            rig.ledger.enqueue_system(OutboxMessage("stale_cache", -100999, 7, f"s{n}"))
        await rig.worker.deliver_outbox()
        await rig.worker.deliver_outbox()  # 20 per cycle: the rest follow
        rig.gateway.default_status = ("sent", 9000)
        rig.gateway.requests.clear()
        await rig.worker.resolve_accepted()
        self.assertLessEqual(len(rig.gateway.requests), 20)

    async def test_a_retried_send_still_queues_once(self):
        rig = Rig(self)
        await rig.worker.run_once()
        await rig.next_day()
        self.assertEqual(len(rig.gateway.notifications), 1)
        self.assertEqual(
            rig.gateway.notifications[0]["idempotency_key"],
            f"watchdog-outbox-{rig.outbox()[0]['id']}",
        )


class MigrationTests(unittest.TestCase):
    def test_existing_sent_rows_are_not_treated_as_resolved(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.db"
            before_10 = {v: m for v, m in MIGRATIONS.items() if v < 10}
            with mock.patch.dict(MIGRATIONS, before_10, clear=True):
                old = Ledger(path)  # a ledger as the previous release left it
            old._conn.execute(
                "INSERT INTO outbox (kind, chat_id, topic_id, text, state, attempts, created_at,"
                " telegram_message_id) VALUES"
                " ('heartbeat', -100999, 7, 't', 'sent', 1, 'now', 5530)"
            )
            old._conn.execute(
                "INSERT INTO heartbeats VALUES"
                " ('2026-11-03T00:00:00Z', '2026-11-04T00:00:00Z', 1, 'now')"
            )
            old.close()

            ledger = Ledger(path)  # applies migration 10
            self.addCleanup(ledger.close)
            self.assertIsNone(ledger.previous_heartbeat(datetime(2026, 11, 9, tzinfo=UTC)))
            row = ledger._conn.execute("SELECT * FROM outbox").fetchone()
            self.assertEqual((row["state"], row["telegram_message_id"]), ("sent", 5530))
            self.assertIsNone(row["gateway_ref"])


if __name__ == "__main__":
    unittest.main()
