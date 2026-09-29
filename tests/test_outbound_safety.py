"""Contract tests for the credential-safe outbound capture boundary.

These tests deliberately stay separate from the masker corpus tests. They prove
that the walking pipeline cannot persist or emit credential-bearing source on
an outbound path while the owner-protected ledger retains the original.
"""

import json
import logging
import tempfile
import unittest
from pathlib import Path

from jo_watchdog.app.config import Config
from jo_watchdog.app.worker import Worker
from jo_watchdog.core.plan import build_plan
from jo_watchdog.ledger.db import Ledger
from tests.fakes import FakeNotion, FakeTelegram, update

SOURCE = "PW: swordfish-credential\nordinary detail"
WITHHELD = "[password withheld – see Telegram message]"


def make_config() -> Config:
    return Config(
        intake_chat_id=-100123,
        intake_topic_id=5,
        captured_reaction="✅",
        job_orders_data_source_id="ds-1",
    )


class OutboundBoundaryContractTests(unittest.TestCase):
    def test_plan_contains_only_masked_outbound_text(self):
        plan = build_plan(
            name="PW: swordfish-credential",
            telegram_key="-100123:42",
            raw_text=SOURCE,
        )

        serialized = plan.model_dump_json()
        self.assertNotIn("swordfish-credential", serialized)
        self.assertEqual(plan.name, f"PW: {WITHHELD}")
        self.assertEqual("\n".join(plan.body_blocks), f"PW: {WITHHELD}\nordinary detail")


class WalkingCaptureSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_raw_source_stays_in_ledger_but_not_plan_notion_or_logs(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        data_dir = Path(tmp.name) / "data"
        data_dir.mkdir()
        ledger = Ledger(data_dir / "ledger.db")
        self.addCleanup(ledger.close)
        telegram = FakeTelegram([update(7, message_id=42, text=SOURCE)])
        notion = FakeNotion(wrong_name=True)
        worker = Worker(
            ledger=ledger,
            telegram=telegram,
            notion=notion,
            config=make_config(),
        )

        with self.assertLogs("jo_watchdog.worker", level=logging.WARNING) as captured:
            await worker.run_once()

        message = ledger.get_message("-100123:42")
        assert message is not None
        self.assertEqual(message["text"], SOURCE)
        self.assertEqual(message["masked_text"], f"PW: {WITHHELD}\nordinary detail")
        self.assertEqual(message["masked_count"], 1)
        self.assertTrue(message["masker_version"])

        persisted_plan = message["plan_json"]
        notion_payload = json.dumps(notion.created, ensure_ascii=False)
        structured_logs = "\n".join(captured.output)
        for outbound in (persisted_plan, notion_payload, structured_logs):
            self.assertNotIn("swordfish-credential", outbound)
        self.assertIn(WITHHELD, persisted_plan)
        self.assertIn(WITHHELD, notion_payload)
        self.assertIn(WITHHELD, structured_logs)

    async def test_ignore_log_masks_first_line_before_persistence(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        data_dir = Path(tmp.name) / "data"
        data_dir.mkdir()
        ledger = Ledger(data_dir / "ledger.db")
        self.addCleanup(ledger.close)
        telegram = FakeTelegram([update(7, message_id=42, topic_id=999, text=SOURCE)])
        worker = Worker(
            ledger=ledger,
            telegram=telegram,
            notion=FakeNotion(),
            config=make_config(),
        )

        await worker.poll_once()

        row = ledger._conn.execute("SELECT first_line FROM ignore_log").fetchone()
        assert row is not None
        self.assertEqual(row["first_line"], f"PW: {WITHHELD}")
        self.assertNotIn("swordfish-credential", row["first_line"])


if __name__ == "__main__":
    unittest.main()
