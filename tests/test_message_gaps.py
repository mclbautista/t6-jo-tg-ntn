"""Intake-topic message gaps indicate possible loss, not confirmed dropped posts (#101)."""

import logging
import unittest

from tests.fakes import update
from tests.test_worker import make_worker


def gap_alerts(ledger) -> list[dict]:
    rows = ledger._conn.execute(
        "SELECT * FROM outbox WHERE kind = 'message_gap' ORDER BY id"
    ).fetchall()
    return [dict(r) for r in rows]


class GapDetectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_consecutive_ids_raise_no_alert(self):
        worker, ledger, *_, cleanup = make_worker(
            [update(1, message_id=3120), update(2, message_id=3121)]
        )
        self.addCleanup(cleanup)
        await worker.poll_once()
        self.assertEqual(gap_alerts(ledger), [])

    async def test_jump_queues_one_ops_alert_and_logs_a_warning(self):
        worker, ledger, telegram, _, cleanup = make_worker(
            [update(1, message_id=3120), update(2, message_id=3123)]
        )
        self.addCleanup(cleanup)
        with self.assertLogs("jo_watchdog.worker", level=logging.WARNING) as logs:
            await worker.poll_once()

        alerts = gap_alerts(ledger)
        self.assertEqual(len(alerts), 1)
        alert = alerts[0]
        self.assertEqual((alert["chat_id"], alert["topic_id"]), (-100999, 7))
        self.assertIn("3121", alert["text"])
        self.assertIn("3122", alert["text"])
        self.assertIn("https://t.me/c/123/5/3121", alert["text"])
        self.assertIn("ingest", alert["text"])
        self.assertIn("may not have reached", alert["text"])
        self.assertIn("other topics", alert["text"])
        self.assertNotIn("never reached", alert["text"])
        # The alert belongs to the message that revealed the gap.
        row = ledger.get_message("-100123:3123")
        self.assertEqual(alert["message_id"], row["id"])
        self.assertTrue(any("3121" in line and "3122" in line for line in logs.output))
        self.assertTrue(any("possible" in line for line in logs.output))

        await worker.deliver_outbox()
        self.assertEqual(len(telegram.sent), 1)

    async def test_other_topics_in_the_intake_chat_count_as_seen(self):
        worker, ledger, *_, cleanup = make_worker(
            [
                update(1, message_id=3120),
                update(2, message_id=3121, topic_id=99),
                update(3, message_id=3122),
            ]
        )
        self.addCleanup(cleanup)
        await worker.poll_once()
        self.assertEqual(gap_alerts(ledger), [])

    async def test_a_gap_revealed_only_by_an_out_of_scope_topic_does_not_alert(self):
        worker, ledger, *_, cleanup = make_worker(
            [update(1, message_id=3120), update(2, message_id=3125, topic_id=99)]
        )
        self.addCleanup(cleanup)
        await worker.poll_once()
        alerts = gap_alerts(ledger)
        self.assertEqual(alerts, [])

    async def test_first_message_ever_raises_no_alert(self):
        worker, ledger, *_, cleanup = make_worker([update(1, message_id=3123)])
        self.addCleanup(cleanup)
        await worker.poll_once()
        self.assertEqual(gap_alerts(ledger), [])

    async def test_other_chats_are_not_tracked(self):
        worker, ledger, *_, cleanup = make_worker(
            [
                update(1, message_id=3120),
                update(2, message_id=50, chat_id=-555),
                update(3, message_id=90, chat_id=-555),
                update(4, message_id=3121),
            ]
        )
        self.addCleanup(cleanup)
        await worker.poll_once()
        self.assertEqual(gap_alerts(ledger), [])

    async def test_redelivery_does_not_alert_twice(self):
        worker, ledger, telegram, _, cleanup = make_worker(
            [update(1, message_id=3120), update(2, message_id=3123)]
        )
        self.addCleanup(cleanup)
        await worker.poll_once()
        telegram.updates = [update(2, message_id=3123)]
        await worker.poll_once()
        self.assertEqual(len(gap_alerts(ledger)), 1)

    async def test_an_older_id_arriving_late_raises_no_alert(self):
        worker, ledger, *_, cleanup = make_worker(
            [update(1, message_id=3120), update(2, message_id=3119)]
        )
        self.addCleanup(cleanup)
        await worker.poll_once()
        self.assertEqual(gap_alerts(ledger), [])

    async def test_edits_raise_no_alert(self):
        edit = update(2, message_id=3125)
        edit.edited = True
        worker, ledger, *_, cleanup = make_worker([update(1, message_id=3120), edit])
        self.addCleanup(cleanup)
        await worker.poll_once()
        self.assertEqual(gap_alerts(ledger), [])

    async def test_a_long_gap_lists_only_the_first_few_links(self):
        worker, ledger, *_, cleanup = make_worker(
            [update(1, message_id=100), update(2, message_id=200)]
        )
        self.addCleanup(cleanup)
        await worker.poll_once()
        text = gap_alerts(ledger)[0]["text"]
        self.assertIn("101", text)
        self.assertIn("199", text)
        self.assertIn("99 message", text)
        self.assertLessEqual(text.count("https://t.me/"), 5)

    async def test_alerts_can_be_turned_off(self):
        worker, ledger, *_, cleanup = make_worker(
            [update(1, message_id=3120), update(2, message_id=3123)],
            message_gap_alerts=False,
        )
        self.addCleanup(cleanup)
        await worker.poll_once()
        self.assertEqual(gap_alerts(ledger), [])
        self.assertTrue(ledger.has_message("-100123:3123"))


if __name__ == "__main__":
    unittest.main()
