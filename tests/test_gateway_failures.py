"""Failed, unknown and stuck ops posts in gateway mode (#113).

Worker + ledger over the in-memory fake gateway, asserting on outbox state, alerts and the
rendered backlink. Refs differ from Telegram IDs, as in ``test_backlinks``.
"""

import unittest
from datetime import timedelta

from tests.fake_gateway import telegram_id_for
from tests.test_backlinks import FIRST_REF, Clocked, link_to


class Failing(Clocked):
    def alerts(self) -> list[dict]:
        rows = self.ledger._conn.execute(
            "SELECT * FROM outbox WHERE kind = 'outage_alert' ORDER BY id"
        ).fetchall()
        return [dict(r) for r in rows]

    def notifications_for_heartbeats(self) -> int:
        beats = [
            n for n in self.gateway.notifications if n["text"].startswith("📋 Daily heartbeat")
        ]
        return len(beats)


class FailedPostTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_failed_answer_marks_the_row_failed_with_the_gateway_error(self):
        rig = Failing(self)
        await rig.worker.run_once()
        rig.gateway.set_notify_state(FIRST_REF, "failed")
        await rig.next_day()  # queued now; the gateway's answer is read next cycle
        await rig.worker.run_once()
        (row,) = rig.heartbeats()
        self.assertEqual(row["state"], "failed")
        self.assertEqual(row["last_error"], rig.gateway.notify_error)
        self.assertIsNone(row["telegram_message_id"])

    async def test_a_failed_post_is_never_queued_again(self):
        rig = Failing(self)
        await rig.worker.run_once()
        rig.gateway.set_notify_state(FIRST_REF, "failed")
        await rig.next_day()
        for _ in range(3):
            await rig.worker.run_once()
        self.assertEqual(rig.notifications_for_heartbeats(), 1)

    async def test_an_unknown_ref_marks_the_row_failed_and_is_never_re_sent(self):
        rig = Failing(self)
        await rig.worker.run_once()
        rig.gateway.set_notify_state(FIRST_REF, "unknown")
        await rig.next_day()
        for _ in range(3):
            await rig.worker.run_once()
        (row,) = rig.heartbeats()
        self.assertEqual(row["state"], "failed")
        self.assertIn("does not know", row["last_error"])
        self.assertEqual(rig.notifications_for_heartbeats(), 1)

    async def test_a_failed_heartbeat_raises_exactly_one_outage_alert(self):
        rig = Failing(self)
        await rig.worker.run_once()
        rig.gateway.set_notify_state(FIRST_REF, "failed")
        await rig.next_day()
        for _ in range(4):
            await rig.worker.run_once()
        (alert,) = rig.alerts()
        self.assertIn("heartbeat was not delivered", alert["text"])
        self.assertIn(rig.gateway.notify_error, alert["text"])

    async def test_the_alert_is_queued_once_even_after_a_restart(self):
        rig = Failing(self)
        await rig.worker.run_once()
        rig.gateway.set_notify_state(FIRST_REF, "unknown")
        await rig.next_day()
        await rig.worker.run_once()
        await rig.worker.run_once()
        self.assertEqual(len(rig.alerts()), 1)

    async def test_the_next_heartbeat_falls_back_to_the_newest_confirmed_one(self):
        rig = Failing(self)
        await rig.worker.run_once()
        await rig.next_day()  # heartbeat 1 (ref 5530) sent
        rig.gateway.set_notify_state(FIRST_REF + 1, "failed")
        await rig.next_day()  # heartbeat 2 fails
        await rig.next_day()  # heartbeat 3 links heartbeat 1
        _, second, third = rig.heartbeats()
        self.assertEqual(second["state"], "failed")
        self.assertEqual(rig.backlink(third), link_to(FIRST_REF))


class StuckPostTests(unittest.IsolatedAsyncioTestCase):
    async def _stuck_rig(self):
        rig = Failing(self)
        rig.gateway.notify_state = "queued"
        await rig.worker.run_once()
        await rig.next_day()
        (row,) = rig.heartbeats()
        # The ledger stamps real time; make the row one hour older than the worker's clock.
        queued = rig.now.strftime("%Y-%m-%dT%H:%M:%SZ")
        rig.ledger._conn.execute(
            "UPDATE outbox SET created_at = ? WHERE id = ?", (queued, row["id"])
        )
        return rig, row["id"]

    async def test_a_post_unresolved_for_an_hour_logs_a_warning_once_and_is_not_re_sent(self):
        rig, _ = await self._stuck_rig()
        with self.assertNoLogs("jo_watchdog.worker", "WARNING"):
            await rig.worker.run_once()  # queued for 0 minutes: no warning
        rig.now += timedelta(hours=1)
        with self.assertLogs("jo_watchdog.worker", "WARNING") as logs:
            await rig.worker.run_once()
            await rig.worker.run_once()
        self.assertEqual(sum("still unresolved" in m for m in logs.output), 1)
        (row,) = rig.heartbeats()
        self.assertEqual(row["state"], "accepted")
        self.assertEqual(rig.notifications_for_heartbeats(), 1)

    async def test_a_stuck_post_is_never_used_for_a_backlink(self):
        rig, _ = await self._stuck_rig()
        await rig.next_day()  # a day later: heartbeat 2, heartbeat 1 is stuck
        _, second = rig.heartbeats()
        self.assertIsNone(rig.backlink(second))

    async def test_a_stuck_post_that_the_gateway_later_sends_still_resolves(self):
        rig, _ = await self._stuck_rig()
        rig.now += timedelta(hours=2)
        await rig.worker.run_once()  # warns
        rig.gateway.set_notify_state(FIRST_REF, "sent")
        await rig.worker.run_once()
        (row,) = rig.heartbeats()
        self.assertEqual(row["state"], "sent")
        self.assertEqual(row["telegram_message_id"], telegram_id_for(FIRST_REF))


if __name__ == "__main__":
    unittest.main()
