"""Dry-run rollback control: stop at `planned`, show the masked plan, write nothing."""

import unittest

from tests.fakes import update
from tests.test_worker import make_worker

SECRET_TEXT = "PROJ - EDT - JO #12\nlink https://drive.example/x password: hunter22"


class DryRunTests(unittest.IsolatedAsyncioTestCase):
    async def test_dry_run_stops_at_planned_with_no_write_or_reaction(self):
        worker, ledger, telegram, notion, cleanup = make_worker(
            [update(7, message_id=42, text=SECRET_TEXT)], dry_run=True
        )
        self.addCleanup(cleanup)
        with self.assertLogs("jo_watchdog.worker", level="INFO") as logs:
            await worker.run_once()
            await worker.drain_once()

        msg = ledger.get_message("-100123:42")
        self.assertEqual(msg["state"], "planned")
        self.assertIsNotNone(msg["plan_json"])
        self.assertEqual(notion.created, [])
        self.assertEqual(telegram.reactions, [])

        # Reported exactly once, masked.
        reports = [line for line in logs.output if "dry_run_plan" in line]
        self.assertEqual(len(reports), 1)
        self.assertIn("Name: PROJ - EDT - JO #12", reports[0])
        self.assertNotIn("hunter22", reports[0])

    async def test_turning_dry_run_off_resumes_the_stored_plan(self):
        worker, ledger, telegram, notion, cleanup = make_worker(
            [update(7, message_id=42)], dry_run=True
        )
        self.addCleanup(cleanup)
        await worker.run_once()
        stored = ledger.get_message("-100123:42")["plan_json"]

        worker.config = worker.config.model_copy(update={"dry_run": False})
        await worker.drain_once()

        msg = ledger.get_message("-100123:42")
        self.assertEqual(msg["state"], "done")
        self.assertEqual(msg["plan_json"], stored)
        self.assertEqual(len(notion.created), 1)
        self.assertEqual(telegram.reactions, [(-100123, 42, "✅")])


if __name__ == "__main__":
    unittest.main()
