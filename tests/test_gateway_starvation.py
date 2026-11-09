"""Accepted gateway posts are all polled, and the stuck clock starts at acceptance (#121).

More accepted rows than one polling batch, over the fake gateway: later rows must be
resolved even while earlier rows stay queued or their lookups error, and none is re-sent.
"""

import unittest
from datetime import timedelta

from tests.fake_gateway import telegram_id_for
from tests.test_backlinks import Clocked

BATCH = 20
TOTAL = BATCH + 5
BASE_REF = 9000


def stamp(when) -> str:
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


class Starving(Clocked):
    def add_accepted(self, count: int = TOTAL, *, created_at=None, accepted_at=None) -> list[int]:
        """Insert ``count`` accepted ops posts, refs ``BASE_REF + n``; return their outbox ids."""
        ids = []
        for n in range(count):
            ref = BASE_REF + n
            self.gateway._issued.add(ref)
            cur = self.ledger._conn.execute(
                "INSERT INTO outbox (kind, chat_id, topic_id, text, state, attempts,"
                " created_at, gateway_ref, accepted_at)"
                " VALUES ('stale_cache', 1, 1, 't', 'accepted', 1, ?, ?, ?)",
                (stamp(created_at or self.now), ref, stamp(accepted_at or self.now)),
            )
            ids.append(cur.lastrowid)
        return ids

    def row(self, outbox_id: int) -> dict:
        found = self.ledger._conn.execute("SELECT * FROM outbox WHERE id = ?", (outbox_id,))
        return dict(found.fetchone())

    def queue_first_batch(self) -> None:
        for n in range(BATCH):
            self.gateway.set_notify_state(BASE_REF + n, "queued")


class StarvationTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_later_row_is_resolved_while_a_full_batch_stays_queued(self):
        rig = Starving(self)
        ids = rig.add_accepted()
        rig.queue_first_batch()
        for _ in range(3):  # bounded: every row is asked within a few cycles
            await rig.worker.resolve_accepted()
        for n in range(BATCH, TOTAL):
            row = rig.row(ids[n])
            self.assertEqual(row["state"], "sent")
            self.assertEqual(row["telegram_message_id"], telegram_id_for(BASE_REF + n))
        self.assertTrue(all(rig.row(i)["state"] == "accepted" for i in ids[:BATCH]))
        self.assertEqual(rig.gateway.notifications, [])  # nothing re-sent

    async def test_later_failed_and_unknown_rows_fail_without_a_resend(self):
        rig = Starving(self)
        ids = rig.add_accepted()
        rig.queue_first_batch()
        rig.gateway.set_notify_state(BASE_REF + BATCH, "failed")
        rig.gateway.set_notify_state(BASE_REF + BATCH + 1, "unknown")
        for _ in range(3):
            await rig.worker.resolve_accepted()
        failed = rig.row(ids[BATCH])
        unknown = rig.row(ids[BATCH + 1])
        self.assertEqual((failed["state"], unknown["state"]), ("failed", "failed"))
        self.assertEqual(failed["last_error"], rig.gateway.notify_error)
        self.assertEqual(rig.gateway.notifications, [])

    async def test_later_rows_resolve_while_earlier_lookups_keep_erroring(self):
        rig = Starving(self)
        ids = rig.add_accepted()
        real = rig.client.notify_status

        async def flaky(ref):
            if ref < BASE_REF + BATCH:
                raise RuntimeError("gateway down for this one")
            return await real(ref)

        rig.client.notify_status = flaky
        for _ in range(3):
            await rig.worker.resolve_accepted()
        self.assertTrue(all(rig.row(i)["state"] == "sent" for i in ids[BATCH:]))
        self.assertTrue(all(rig.row(i)["state"] == "accepted" for i in ids[:BATCH]))

    async def test_queued_rows_are_still_resolved_later_and_never_resent(self):
        rig = Starving(self)
        ids = rig.add_accepted()
        rig.queue_first_batch()
        for _ in range(3):
            await rig.worker.resolve_accepted()
        for n in range(BATCH):
            rig.gateway.set_notify_state(BASE_REF + n, "sent")
        for _ in range(3):
            await rig.worker.resolve_accepted()
        self.assertTrue(all(rig.row(i)["state"] == "sent" for i in ids))
        self.assertEqual(rig.gateway.notifications, [])


class StuckClockTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_hour_counts_from_acceptance_not_from_when_it_was_queued(self):
        rig = Starving(self)
        rig.add_accepted(1, created_at=rig.now - timedelta(hours=5), accepted_at=rig.now)
        rig.gateway.set_notify_state(BASE_REF, "queued")
        with self.assertNoLogs("jo_watchdog.worker", "WARNING"):
            await rig.worker.resolve_accepted()  # accepted just now, though created hours ago
        rig.now += timedelta(hours=1)
        with self.assertLogs("jo_watchdog.worker", "WARNING") as logs:
            await rig.worker.resolve_accepted()
        self.assertTrue(any("still unresolved" in m for m in logs.output))

    async def test_a_persistent_lookup_error_still_warns_after_an_hour(self):
        rig = Starving(self)
        (outbox_id,) = rig.add_accepted(1)

        async def broken(ref):
            raise RuntimeError("lookup down")

        rig.client.notify_status = broken
        with self.assertLogs("jo_watchdog.worker", "WARNING") as early:
            await rig.worker.resolve_accepted()
        self.assertFalse(any("still unresolved" in m for m in early.output))
        rig.now += timedelta(hours=1)
        with self.assertLogs("jo_watchdog.worker", "WARNING") as logs:
            await rig.worker.resolve_accepted()
            await rig.worker.resolve_accepted()
        self.assertEqual(sum("still unresolved" in m for m in logs.output), 1)
        row = rig.row(outbox_id)
        self.assertEqual(row["state"], "accepted")  # kept, never re-sent
        self.assertEqual(rig.gateway.notifications, [])

    async def test_a_new_acceptance_is_stamped_with_the_worker_clock(self):
        rig = Starving(self)
        await rig.worker.run_once()
        await rig.next_day()
        (row,) = rig.heartbeats()
        self.assertEqual(row["accepted_at"], stamp(rig.now))


if __name__ == "__main__":
    unittest.main()
