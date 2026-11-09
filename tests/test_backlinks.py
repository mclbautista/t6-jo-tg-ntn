"""Heartbeat backlinks in gateway mode use the real Telegram message ID, never the ref (#63, #109).

Worker + ledger over the in-memory fake gateway, asserting on the rendered backlink and the
outbox rows. The fake gateway's Telegram IDs differ from its refs, so a ref stored as an ID
shows up as a wrong link.
"""

import re
import sqlite3
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from jo_watchdog.core.report import intake_link
from jo_watchdog.ledger import migrations
from jo_watchdog.ledger.db import Ledger
from tests.fake_gateway import telegram_id_for
from tests.test_gateway_mode import Rig

FIRST_REF = 5530
OPS = (-100999, 7)


class Clocked(Rig):
    def __init__(self, test):
        super().__init__(test)
        self.now = datetime(2026, 11, 2, 23, 30, tzinfo=UTC)  # 07:30 Manila, Nov 3
        self.worker.clock = lambda: self.now

    async def next_day(self):
        self.now += timedelta(days=1)
        await self.worker.run_once()

    def heartbeats(self) -> list[sqlite3.Row]:
        return self.ledger._conn.execute(
            "SELECT * FROM outbox WHERE kind = 'heartbeat' ORDER BY id"
        ).fetchall()

    def backlink(self, row) -> str | None:
        found = re.search(r'href="([^"]+)"', row["text"])
        return found.group(1) if found else None


def link_to(ref: int) -> str:
    return intake_link(OPS[0], OPS[1], telegram_id_for(ref))


class BacklinkTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_next_heartbeat_links_the_real_telegram_id_not_the_gateway_ref(self):
        rig = Clocked(self)
        await rig.worker.run_once()  # a new ledger skips its first window
        await rig.next_day()  # heartbeat 1: no previous
        await rig.next_day()  # heartbeat 2 links heartbeat 1
        first, second = rig.heartbeats()
        self.assertEqual(first["gateway_ref"], FIRST_REF)
        self.assertNotEqual(first["telegram_message_id"], first["gateway_ref"])
        self.assertEqual(rig.backlink(second), link_to(FIRST_REF))
        self.assertFalse(rig.backlink(second).endswith(f"/{FIRST_REF}"))

    async def test_a_queued_post_stays_accepted_and_is_never_linked_until_sent(self):
        rig = Clocked(self)
        rig.gateway.notify_state = "queued"
        await rig.worker.run_once()
        await rig.next_day()  # heartbeat 1, queued at the gateway
        (first,) = rig.heartbeats()
        self.assertEqual(first["state"], "accepted")
        self.assertIsNone(first["telegram_message_id"])

        await rig.next_day()  # heartbeat 2 must not link the queued post
        _, second = rig.heartbeats()
        self.assertIsNone(rig.backlink(second))

        rig.gateway.set_notify_state(FIRST_REF, "sent")
        await rig.worker.run_once()  # a later cycle resolves it
        first, _ = rig.heartbeats()
        self.assertEqual(first["state"], "sent")
        self.assertEqual(first["telegram_message_id"], telegram_id_for(FIRST_REF))

    async def test_an_unresolved_predecessor_falls_back_to_the_newest_confirmed_heartbeat(self):
        rig = Clocked(self)
        await rig.worker.run_once()
        await rig.next_day()  # heartbeat 1 (ref 5530), sent
        rig.gateway.set_notify_state(FIRST_REF + 1, "queued")
        await rig.next_day()  # heartbeat 2 (ref 5531), stays queued
        await rig.next_day()  # heartbeat 3 skips the unresolved one
        _, second, third = rig.heartbeats()
        self.assertEqual(second["state"], "accepted")
        self.assertEqual(rig.backlink(third), link_to(FIRST_REF))

    async def test_a_restart_between_enqueue_and_resolution_resolves_next_cycle(self):
        rig = Clocked(self)
        rig.gateway.notify_state = "queued"
        await rig.worker.run_once()
        await rig.next_day()
        rig.ledger.close()  # restart: a new ledger and worker over the same file
        rig.ledger = Ledger(rig.path, gateway_mode=True)
        self.addCleanup(rig.ledger.close)
        rig.worker.ledger = rig.ledger
        (row,) = rig.heartbeats()
        self.assertEqual(row["state"], "accepted")
        rig.gateway.notify_state = "sent"
        await rig.worker.run_once()
        (row,) = rig.heartbeats()
        self.assertEqual(row["state"], "sent")
        self.assertEqual(row["telegram_message_id"], telegram_id_for(FIRST_REF))

    async def test_the_first_heartbeat_of_a_new_ledger_is_still_skipped(self):
        rig = Clocked(self)
        await rig.worker.run_once()
        self.assertEqual(rig.heartbeats(), [])
        self.assertEqual(rig.gateway.notifications, [])

    async def test_a_lookup_error_leaves_the_row_accepted_for_the_next_cycle(self):
        rig = Clocked(self)
        await rig.worker.run_once()
        await rig.next_day()  # heartbeat 1 accepted then resolved
        rig.gateway.notify_state = "queued"
        await rig.next_day()  # heartbeat 2 queued
        rig.gateway.fail_next = 1  # the status lookup gets a 503
        await rig.worker.resolve_accepted()
        self.assertEqual(rig.heartbeats()[1]["state"], "accepted")


class MigrationTests(unittest.TestCase):
    def _ledger_before_migration_10(self, tmp: str, telegram_id: int) -> Path:
        """A ledger at migration 9 holding one sent heartbeat row."""
        path = Path(tmp) / "ledger.db"
        ten = migrations.MIGRATIONS.pop(10)  # db.py shares this dict
        try:
            ledger = Ledger(path)
            ledger._conn.execute(
                "INSERT INTO outbox (kind, chat_id, topic_id, text, state,"
                " telegram_message_id, created_at)"
                " VALUES ('heartbeat', -100999, 7, 't', 'sent', ?, '2026-11-03T00:00:00Z')",
                (telegram_id,),
            )
            ledger.close()
        finally:
            migrations.MIGRATIONS[10] = ten
        return path

    def test_pre_existing_gateway_rows_are_never_treated_as_resolved(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._ledger_before_migration_10(tmp, 5530)
            ledger = Ledger(path, gateway_mode=True)
            try:
                (row,) = ledger._conn.execute("SELECT * FROM outbox").fetchall()
                self.assertEqual(row["gateway_ref"], 5530)
                self.assertIsNone(row["telegram_message_id"])
                self.assertEqual(row["state"], "sent")
            finally:
                ledger.close()

    def test_direct_mode_rows_keep_their_real_telegram_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._ledger_before_migration_10(tmp, 466)
            ledger = Ledger(path)
            try:
                (row,) = ledger._conn.execute("SELECT * FROM outbox").fetchall()
                self.assertEqual(row["telegram_message_id"], 466)
                self.assertIsNone(row["gateway_ref"])
            finally:
                ledger.close()


if __name__ == "__main__":
    unittest.main()
