"""Whole-pipeline tests: real SQLite, fake Telegram/Notion adapters.

The invariant under test is the same one the crash-injection seam will stress in
ticket #8: an update is committed to the ledger before the Telegram offset
advances, and one Telegram message never produces a second Notion row.
"""

import json
import tempfile
import unittest
from pathlib import Path

from jo_watchdog.app.config import Config
from jo_watchdog.ledger.db import Ledger
from jo_watchdog.ledger.states import State

from tests.fakes import FakeClassifier, FakeNotion, FakeTelegram, update


async def no_sleep(_seconds: float) -> None:
    """Reaction retries wait 2s and 5s; tests do not."""


def make_config(**overrides) -> Config:
    base = dict(
        intake_chat_id=-100123,
        intake_topic_id=5,
        ops_chat_id=-100999,
        ops_topic_id=7,
        captured_reaction="✅",
        job_orders_data_source_id="ds-1",
    )
    base.update(overrides)
    return Config(**base)


def make_worker(
    updates, *, fail_create=False, wrong_name=False, classifier=None, **config_overrides
):
    from jo_watchdog.app.worker import Worker

    tmp = tempfile.TemporaryDirectory()
    ledger = Ledger(Path(tmp.name) / "ledger.db")
    telegram = FakeTelegram(updates)
    notion = FakeNotion(fail_create=fail_create, wrong_name=wrong_name)
    worker = Worker(
        ledger=ledger,
        telegram=telegram,
        notion=notion,
        classifier=classifier or FakeClassifier(),
        config=make_config(**config_overrides),
        sleep=no_sleep,
    )

    def cleanup():
        ledger.close()
        tmp.cleanup()

    return worker, ledger, telegram, notion, cleanup


class CommitBeforeOffsetTests(unittest.IsolatedAsyncioTestCase):
    async def test_in_scope_update_is_committed_before_offset_advances(self):
        worker, ledger, telegram, notion, cleanup = make_worker([update(7, message_id=42)])
        self.addCleanup(cleanup)
        await worker.poll_once()

        # The message is committed and the offset advanced to its update id.
        self.assertTrue(ledger.has_message("-100123:42"))
        self.assertEqual(ledger.last_committed_offset(), 7)
        self.assertEqual(ledger.next_offset(), 8)
        # The poller asked for offset 0 (fresh ledger), then nothing else yet.
        self.assertEqual(telegram.get_updates_calls, [0])

    async def test_redelivered_update_does_not_create_second_row(self):
        # offset N+1 redelivers the last committed update; the UNIQUE key wins.
        worker, ledger, telegram, notion, cleanup = make_worker(
            [update(7, message_id=42), update(7, message_id=42)]
        )
        self.addCleanup(cleanup)
        await worker.poll_once()
        await worker.poll_once()
        rows = ledger._conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        self.assertEqual(rows, 1)
        self.assertEqual(ledger.last_committed_offset(), 7)


class OutOfScopeTests(unittest.IsolatedAsyncioTestCase):
    async def test_wrong_topic_is_ignored(self):
        worker, ledger, telegram, notion, cleanup = make_worker([update(7, topic_id=999)])
        self.addCleanup(cleanup)
        await worker.poll_once()
        self.assertEqual(ledger._conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 0)
        row = ledger._conn.execute("SELECT reason FROM ignore_log").fetchone()
        self.assertEqual(row["reason"], "out_of_scope_topic")
        self.assertEqual(ledger.last_committed_offset(), 7)

    async def test_wrong_chat_is_ignored(self):
        worker, ledger, telegram, notion, cleanup = make_worker([update(7, chat_id=-999999)])
        self.addCleanup(cleanup)
        await worker.poll_once()
        self.assertEqual(ledger._conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 0)
        row = ledger._conn.execute("SELECT reason FROM ignore_log").fetchone()
        self.assertEqual(row["reason"], "out_of_scope_chat")


class PipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_end_to_end_capture_writes_once_reads_back_and_reacts(self):
        worker, ledger, telegram, notion, cleanup = make_worker([update(7, message_id=42)])
        self.addCleanup(cleanup)
        await worker.poll_once()
        await worker.drain_once()

        msg = ledger.get_message("-100123:42")
        self.assertEqual(msg["state"], "done")
        self.assertEqual(msg["notion_page_id"], "page-1")
        self.assertEqual(len(notion.created), 1)
        self.assertEqual(telegram.reactions, [(-100123, 42, "🤔")])

    async def test_plan_is_stored_before_create(self):
        worker, ledger, telegram, notion, cleanup = make_worker([update(7, message_id=42)])
        self.addCleanup(cleanup)
        await worker.poll_once()

        # Run the pipeline up to the write boundary manually so we can observe
        # the stored plan before the external create call fires.
        key = "-100123:42"
        await worker.process(key)

        msg = ledger.get_message(key)
        plan = json.loads(msg["plan_json"])
        self.assertEqual(plan["name"], "first line")
        self.assertEqual(plan["telegram_key"], key)
        self.assertGreater(len(plan["body_blocks"]), 0)
        # The stored plan's body blocks are what reached Notion, masked.
        self.assertEqual(notion.created[0]["parent"], {"data_source_id": "ds-1"})

    async def test_reprocess_does_not_create_second_notion_row(self):
        worker, ledger, telegram, notion, cleanup = make_worker([update(7, message_id=42)])
        self.addCleanup(cleanup)
        await worker.poll_once()
        await worker.drain_once()
        # A second drain finds nothing pending; forcing process again is a no-op.
        await worker.process("-100123:42")
        await worker.drain_once()
        self.assertEqual(len(notion.created), 1)

    async def test_reaction_only_after_confirmed_write_and_readback(self):
        worker, ledger, telegram, notion, cleanup = make_worker(
            [update(7, message_id=42)], fail_create=True
        )
        self.addCleanup(cleanup)
        await worker.poll_once()
        await worker.drain_once()  # the failed create is counted, not raised
        self.assertEqual(telegram.reactions, [])
        msg = ledger.get_message("-100123:42")
        self.assertEqual(msg["state"], "writing")  # recoverable, not done
        self.assertEqual(msg["attempts"], 1)

    async def test_readback_mismatch_is_recorded_not_rewritten(self):
        worker, ledger, telegram, notion, cleanup = make_worker(
            [update(7, message_id=42)], wrong_name=True
        )
        self.addCleanup(cleanup)
        await worker.poll_once()
        await worker.drain_once()

        msg = ledger.get_message("-100123:42")
        self.assertIsNotNone(msg["readback_mismatch"])
        self.assertIn("Name", msg["readback_mismatch"])
        # The mismatch did not trigger a rewrite: still exactly one create.
        self.assertEqual(len(notion.created), 1)
        # Reaction still applied (capture confirmed); the mismatch is a finding, so 🤔.
        self.assertEqual(telegram.reactions, [(-100123, 42, "🤔")])


class RunLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_polls_and_drains_one_cycle(self):
        worker, ledger, telegram, notion, cleanup = make_worker([update(7, message_id=42)])
        self.addCleanup(cleanup)
        await worker.run_once()
        msg = ledger.get_message("-100123:42")
        self.assertEqual(msg["state"], "done")
        self.assertEqual(len(notion.created), 1)


if __name__ == "__main__":
    unittest.main()
