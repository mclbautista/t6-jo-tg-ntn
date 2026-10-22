"""Durable ops outbox: migration 5, atomic enqueue, at-least-once delivery (#23).

Real SQLite, fake Telegram and Notion.
"""

import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from jo_watchdog.app.caches import StaleCache
from jo_watchdog.app.config import Config
from jo_watchdog.app.worker import Worker
from jo_watchdog.core.findings import Finding
from jo_watchdog.core.job_order import plan_job_order
from jo_watchdog.ledger.db import OUTBOX_RETRY_SECONDS, Ledger, OutboxMessage
from jo_watchdog.ledger.states import State
from tests.fakes import FakeClassifier, FakeNotion, FakeTelegram, ok_result, update

KEY = "-100123:42"
WITHHELD = "[password withheld – see Telegram message]"


async def no_sleep(_seconds: float) -> None:
    """Reaction retries wait 2s and 5s; tests do not."""


def make_config(**overrides) -> Config:
    base = dict(
        intake_chat_id=-100123,
        intake_topic_id=5,
        ops_chat_id=-100999,
        ops_topic_id=7,
        leizette_telegram_handle="leizette",
        captured_reaction="✅",
        job_orders_data_source_id="ds-1",
    )
    base.update(overrides)
    return Config(**base)


def channel_forward(update_id=1, message_id=42, text="MG - TOC - JO #41\nTask: edit"):
    u = update(update_id, message_id=message_id, text=text)
    u.raw["message"]["date"] = 1790000000
    u.raw["message"]["forward_origin"] = {
        "type": "channel",
        "chat": {"id": -1005551234, "title": "Studio", "username": "studio"},
        "message_id": 88,
    }
    return u


class Harness:
    def __init__(
        self, test, updates=(), *, telegram=None, notion=None, classifier=None, **config
    ):
        self.tmp = tempfile.TemporaryDirectory()
        test.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "ledger.db"
        self.telegram = telegram or FakeTelegram(list(updates))
        self.notion = notion or FakeNotion()
        # A clean `jo` extraction, so only what a test sets up is flagged.
        self.classifier = classifier or FakeClassifier(
            ok_result(title="Teaser", task="Edit the teaser")
        )
        self.config = make_config(**config)
        self.open()
        test.addCleanup(lambda: self.ledger.close())

    def open(self):
        self.ledger = Ledger(self.path)
        self.worker = Worker(
            ledger=self.ledger,
            telegram=self.telegram,
            notion=self.notion,
            classifier=self.classifier,
            config=self.config,
            sleep=no_sleep,
        )

    def reopen(self):
        """Simulate a process restart on the same ledger file."""
        self.ledger.close()
        self.open()

    def outbox(self):
        return [dict(r) for r in self.ledger._conn.execute("SELECT * FROM outbox ORDER BY id")]


class MigrationFiveTests(unittest.TestCase):
    def test_schema(self):
        h = Harness(self)
        conn = h.ledger._conn
        cols = {r[1] for r in conn.execute("PRAGMA table_info(outbox)")}
        self.assertEqual(
            cols,
            {
                "id", "message_id", "kind", "chat_id", "topic_id", "text", "state",
                "attempts", "next_attempt_at", "last_error", "telegram_message_id",
                "created_at", "sent_at",
            },
        )
        msg_cols = {r[1] for r in conn.execute("PRAGMA table_info(messages)")}
        self.assertTrue({"findings_json", "reaction_state"} <= msg_cols)
        indexes = {r[1] for r in conn.execute("PRAGMA index_list(outbox)")}
        self.assertIn("outbox_state_next_attempt", indexes)
        versions = [r[0] for r in conn.execute("SELECT version FROM schema_version")]
        self.assertIn(5, versions)

    def test_migration_is_applied_once_across_reopen(self):
        h = Harness(self)
        h.reopen()
        count = h.ledger._conn.execute(
            "SELECT COUNT(*) FROM schema_version WHERE version = 5"
        ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_outbox_state_is_constrained(self):
        h = Harness(self)
        with self.assertRaises(sqlite3.IntegrityError):
            h.ledger._conn.execute(
                "INSERT INTO outbox (kind, chat_id, topic_id, text, state, created_at)"
                " VALUES ('heartbeat', 1, 1, 't', 'bogus', 'now')"
            )


class CaptureReportTests(unittest.IsolatedAsyncioTestCase):
    async def test_flagged_capture_queues_and_posts_one_combined_report(self):
        h = Harness(self, [channel_forward()])
        await h.worker.poll_once()
        await h.worker.drain_once()

        msg = h.ledger.get_message(KEY)
        self.assertEqual(msg["state"], State.DONE.value)
        findings = json.loads(msg["findings_json"])
        self.assertEqual([f["code"] for f in findings], ["forward_origin"])
        [row] = h.outbox()
        self.assertEqual(row["state"], "pending")
        self.assertEqual(row["kind"], "capture_report")
        self.assertEqual(row["message_id"], msg["id"])
        self.assertEqual((row["chat_id"], row["topic_id"]), (-100999, 7))

        await h.worker.deliver_outbox()
        [(chat, topic, text)] = h.telegram.sent
        self.assertEqual((chat, topic), (-100999, 7))
        self.assertEqual(
            text,
            "⚠️ Flagged on capture\n"
            "<code>MG - TOC - JO #41</code>\n"
            "\n"
            "Forwarded from a channel: Studio\n"
            "\n"
            '<a href="https://notion.example/page-1">Notion row</a>\n'
            '<a href="https://t.me/c/123/5/42">Telegram message</a>\n'
            '<a href="https://t.me/studio/88">Original message</a>',
        )
        self.assertNotIn("@leizette", text)  # forward origin tags no one
        self.assertNotIn("Task: edit", text)  # the raw message is never included
        [row] = h.outbox()
        self.assertEqual(row["state"], "sent")
        self.assertEqual(row["telegram_message_id"], 9001)
        self.assertEqual(row["attempts"], 1)

    async def test_clean_capture_posts_nothing(self):
        h = Harness(self, [update(1, message_id=42)])
        await h.worker.run_once()
        self.assertEqual(h.outbox(), [])
        self.assertEqual(h.telegram.sent, [])
        self.assertEqual(json.loads(h.ledger.get_message(KEY)["findings_json"]), [])

    async def test_readback_mismatch_is_reported_untagged(self):
        h = Harness(self, [update(1, message_id=42)], notion=FakeNotion(wrong_name=True))
        await h.worker.run_once()
        [(_, _, text)] = h.telegram.sent
        self.assertIn("Notion row differs from the plan: Name", text)
        self.assertNotIn("@", text)

    async def test_hidden_user_forward_has_no_original_link(self):
        u = update(1, message_id=42)
        u.raw["message"]["forward_origin"] = {"type": "hidden_user", "sender_user_name": "Che"}
        h = Harness(self, [u])
        await h.worker.run_once()
        [(_, _, text)] = h.telegram.sent
        self.assertIn("Forwarded from a hidden user: Che", text)
        self.assertNotIn("Original message", text)

    async def test_report_text_is_masked(self):
        u = channel_forward(text="PW: swordfish\nTask: edit")
        h = Harness(self, [u])
        await h.worker.run_once()
        [row] = h.outbox()
        self.assertNotIn("swordfish", row["text"])
        self.assertIn(f"<code>PW: {WITHHELD}</code>", row["text"])
        self.assertNotIn("swordfish", h.telegram.sent[0][2])

    async def test_plan_findings_tag_leizette(self):
        # #21 stores planner findings on the plan; they join the same report.
        def plan(*args, **kwargs):
            planned = plan_job_order(*args, **kwargs)
            planned.findings.append(Finding(code="unknown_department", params={"token": "XYZ"}))
            return planned

        h = Harness(self, [update(1, message_id=42)])
        with mock.patch("jo_watchdog.app.worker.plan_job_order", plan):
            await h.worker.run_once()
        [(_, _, text)] = h.telegram.sent
        self.assertIn('Department "XYZ" isn\'t a Notion option', text)
        self.assertTrue(text.endswith("\n\n@leizette"))


class ExtractionFactsTests(unittest.IsolatedAsyncioTestCase):
    async def test_unsure_blank_task_and_header_date_reach_the_report(self):
        classifier = FakeClassifier(
            ok_result(
                "unsure",
                reasons="No JO header.",
                title="Teaser",
                task=None,
                header_date_raw="Sept 25 / FRIDAY",  # posted Sep 28 in Manila
            )
        )
        h = Harness(self, [update(1, message_id=42)], classifier=classifier)
        await h.worker.run_once()
        [(_, _, text)] = h.telegram.sent
        self.assertIn("Classifier unsure: No JO header.\nTask is blank\n", text)
        self.assertIn(
            'Header date ("Sept 25 / FRIDAY") doesn\'t match the actual post date (Mon, Sep 28)',
            text,
        )
        self.assertNotIn("@", text)

    async def test_multiple_jo_tags_leizette(self):
        classifier = FakeClassifier(
            ok_result(title="T", task="X", multiple_jo_suspected=True, reasons="Two headers.")
        )
        h = Harness(self, [update(1, message_id=42)], classifier=classifier)
        await h.worker.run_once()
        [(_, _, text)] = h.telegram.sent
        self.assertIn(
            "Looks like more than one JO in this message, split by hand: Two headers.", text
        )
        self.assertTrue(text.endswith("\n\n@leizette"))


class StaleCacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_stale_cache_event_is_queued_and_posted(self):
        class StaleCaches:
            ready = True

            async def refresh_if_due(self):
                return [
                    StaleCache(
                        kind="people",
                        fetched_at="2026-10-20T23:00:00Z",
                        hours_stale=30.0,
                        error="Notion 503",
                    )
                ]

        h = Harness(self)
        h.worker.caches = StaleCaches()
        await h.worker.run_once()
        [(chat, topic, text)] = h.telegram.sent
        self.assertEqual((chat, topic), (-100999, 7))
        self.assertTrue(
            text.startswith("⚠️ Notion cache is stale\nPeople: last refreshed Oct 21 07:00 (")
        )
        self.assertTrue(text.endswith("Last error: Notion 503"))
        self.assertEqual(h.outbox()[0]["kind"], "stale_cache")


class AtomicityTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_outbox_insert_rolls_back_written(self):
        h = Harness(self, [channel_forward()])
        h.ledger._conn.execute(
            "CREATE TRIGGER boom BEFORE INSERT ON outbox BEGIN SELECT RAISE(ABORT, 'disk'); END"
        )
        await h.worker.poll_once()
        with self.assertRaises(sqlite3.IntegrityError):
            await h.worker.drain_once()
        msg = h.ledger.get_message(KEY)
        # Neither half committed: still `writing`, no findings, no outbox row. The
        # created page's id was kept (#25), so recovery retries only the read-back.
        self.assertEqual(msg["state"], State.WRITING.value)
        self.assertIsNone(msg["findings_json"])
        self.assertEqual(msg["notion_page_id"], "page-1")
        self.assertEqual(h.outbox(), [])

    def test_mark_written_requires_writing_state(self):
        h = Harness(self)
        h.ledger.accept_message(-100123, 42, 1, "{}", "t", "t")
        report = OutboxMessage("capture_report", -100999, 7, "x")
        self.assertFalse(h.ledger.mark_written(KEY, "p", findings=[], reports=[report]))
        self.assertEqual(h.outbox(), [])

    def test_unknown_kind_is_rejected(self):
        h = Harness(self)
        with self.assertRaises(ValueError):
            h.ledger.enqueue_system(OutboxMessage("chatter", 1, 1, "x"))


class AtLeastOnceTests(unittest.IsolatedAsyncioTestCase):
    async def test_crash_after_written_before_delivery_still_posts(self):
        h = Harness(self, [channel_forward()])
        await h.worker.poll_once()
        await h.worker.drain_once()
        h.reopen()  # crash before deliver_outbox ran
        await h.worker.deliver_outbox()
        self.assertEqual(len(h.telegram.sent), 1)
        self.assertEqual(h.outbox()[0]["state"], "sent")

    async def test_crash_between_send_and_mark_sent_duplicates_not_omits(self):
        h = Harness(self, [channel_forward()])
        await h.worker.poll_once()
        await h.worker.drain_once()

        def crash(*a, **kw):
            raise SystemExit("killed after send")

        h.ledger.outbox_sent = crash
        with self.assertRaises(SystemExit):
            await h.worker.deliver_outbox()
        self.assertEqual(h.outbox()[0]["state"], "pending")

        h.reopen()
        await h.worker.deliver_outbox()
        # A traceable duplicate, never a missing report.
        self.assertEqual(len(h.telegram.sent), 2)
        self.assertEqual(h.telegram.sent[0], h.telegram.sent[1])
        row = h.outbox()[0]
        self.assertEqual((row["state"], row["telegram_message_id"]), ("sent", 9002))

    async def test_send_failures_back_off_then_give_up(self):
        telegram = FakeTelegram([channel_forward()], fail_send=99)
        h = Harness(self, telegram=telegram)
        await h.worker.poll_once()
        await h.worker.drain_once()
        await h.worker.deliver_outbox()
        row = h.outbox()[0]
        self.assertEqual((row["state"], row["attempts"]), ("pending", 1))
        self.assertIsNotNone(row["next_attempt_at"])
        self.assertIn("telegram send failed", row["last_error"])

        # Not due yet: the next cycle does not retry immediately.
        await h.worker.deliver_outbox()
        self.assertEqual(h.outbox()[0]["attempts"], 1)

        clock = time.time()
        for _ in range(len(OUTBOX_RETRY_SECONDS) - 1):
            h.ledger.outbox_failed_attempt(row["id"], "still down", now=clock)
        row = h.outbox()[0]
        self.assertEqual((row["state"], row["attempts"]), ("failed", 5))

    async def test_retry_succeeds_when_due(self):
        telegram = FakeTelegram([channel_forward()], fail_send=1)
        h = Harness(self, telegram=telegram)
        await h.worker.run_once()
        self.assertEqual(h.telegram.sent, [])
        h.ledger._conn.execute("UPDATE outbox SET next_attempt_at = '2000-01-01T00:00:00Z'")
        await h.worker.deliver_outbox()
        row = h.outbox()[0]
        self.assertEqual((row["state"], row["attempts"]), ("sent", 2))
        self.assertIsNone(row["last_error"])


    async def test_failing_capture_does_not_starve_delivery(self):
        h = Harness(self, [update(1, message_id=42)], notion=FakeNotion(fail_create=True))
        h.ledger.enqueue_system(OutboxMessage("stale_cache", -100999, 7, "stale"))
        await h.worker.run_once()
        self.assertEqual(h.telegram.sent, [(-100999, 7, "stale")])
        self.assertEqual(h.ledger.get_message(KEY)["attempts"], 1)


class ReactionIndependenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_reaction_failure_keeps_report_and_finishes_capture(self):
        telegram = FakeTelegram([channel_forward()], fail_reaction=True)
        h = Harness(self, telegram=telegram)
        await h.worker.run_once()
        msg = h.ledger.get_message(KEY)
        self.assertEqual(msg["state"], State.DONE.value)
        self.assertEqual(msg["reaction_state"], "failed")
        self.assertEqual(len(h.telegram.sent), 1)
        self.assertEqual(h.outbox()[0]["state"], "sent")

    async def test_reaction_success_is_recorded(self):
        h = Harness(self, [update(1, message_id=42)])
        await h.worker.run_once()
        self.assertEqual(h.ledger.get_message(KEY)["reaction_state"], "set")
        self.assertEqual(h.telegram.reactions, [(-100123, 42, "✅")])

    async def test_send_failure_does_not_touch_reaction(self):
        telegram = FakeTelegram([channel_forward()], fail_send=1)
        h = Harness(self, telegram=telegram)
        await h.worker.run_once()
        self.assertEqual(h.ledger.get_message(KEY)["reaction_state"], "set")
        self.assertEqual(h.telegram.reactions, [(-100123, 42, "✅")])


class SystemMessageQueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_system_message_without_ledger_message(self):
        h = Harness(self)
        oid = h.ledger.enqueue_system(OutboxMessage("stale_cache", -100999, 7, "stale"))
        await h.worker.deliver_outbox()
        self.assertEqual(h.telegram.sent, [(-100999, 7, "stale")])
        row = h.outbox()[0]
        self.assertEqual((row["id"], row["message_id"]), (oid, None))

    async def test_system_message_linked_to_message(self):
        h = Harness(self)
        h.ledger.accept_message(-100123, 42, 1, "{}", "t", "t")
        h.ledger.enqueue_system(OutboxMessage("edit_after_capture", -100999, 7, "e"), key=KEY)
        self.assertEqual(h.outbox()[0]["message_id"], h.ledger.get_message(KEY)["id"])


class ConfigTests(unittest.TestCase):
    def test_ops_ids_are_required(self):
        with self.assertRaises(ValueError):
            make_config(ops_topic_id=0)

    def test_defaults(self):
        c = make_config()
        self.assertEqual(c.header_date_tolerance_days, 1)
        self.assertIsNone(make_config(leizette_telegram_handle=None).leizette_telegram_handle)


if __name__ == "__main__":
    unittest.main()
