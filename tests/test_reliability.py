"""Retries, exhaustion, manual retry, write recovery, deadman and heartbeat (#25).

Real SQLite, fake Telegram/Notion/DeepSeek adapters and a fake clock.
"""

import asyncio
import json
import sqlite3
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from zoneinfo import ZoneInfo

from jo_watchdog.app import __main__, healthcheck
from jo_watchdog.app.worker import DeadmanExit
from jo_watchdog.core.heartbeat import heartbeat_window
from jo_watchdog.core.resolve import DepartmentResolution, PeopleResolution, ProjectResolution
from jo_watchdog.ledger.db import Ledger, OutboxMessage, fmt_utc
from jo_watchdog.ledger.migrations import MIGRATIONS
from tests.clockwork import KEY, OWNER, Crash, FakeClock, Pipeline
from tests.fakes import FakeClassifier, FakeNotion, FakeTelegram, failed_result, ok_result, update

CHANNEL = {
    "type": "channel",
    "chat": {"id": -1005551234, "title": "Studio", "username": "studio"},
    "message_id": 88,
}


def jo(update_id=1, message_id=42, text="MG - TOC - JO #41\nTask: edit"):
    return update(update_id, message_id=message_id, text=text)


class RateLimited(Exception):
    def __init__(self, retry_after):
        super().__init__("rate limited")
        self.retry_after = retry_after


class RetryScheduleTests(unittest.IsolatedAsyncioTestCase):
    async def test_notion_write_retries_at_30s_2m_5m_10m_then_gives_up_once(self):
        notion = FakeNotion(fail_create=True)
        p = Pipeline(self, [jo()], notion=notion)
        start = p.clock()
        attempt_times = []
        for delay in (30, 120, 300, 600):
            await p.worker.run_once()
            row = p.message()
            attempt_times.append(p.clock())
            self.assertEqual(row["state"], "writing")
            self.assertEqual(row["next_attempt_at"], fmt_utc(p.clock() + timedelta(seconds=delay)))
            calls = notion.create_calls
            p.clock.advance(seconds=delay - 1)
            await p.worker.run_once()  # not yet due
            self.assertEqual(notion.create_calls, calls)
            p.clock.advance(seconds=1)
        self.assertEqual(
            [round((t - start).total_seconds()) for t in attempt_times], [0, 30, 150, 450]
        )

        await p.worker.run_once()  # the fifth failure
        row = p.message()
        self.assertEqual(row["attempts"], 5)
        self.assertEqual(row["exhausted_at"], fmt_utc(p.clock()))
        self.assertIsNone(row["next_attempt_at"])
        self.assertEqual(row["state"], "writing")  # retained, plan intact
        self.assertIsNotNone(row["plan_json"])

        alerts = p.outbox("outage_alert")
        self.assertEqual(len(alerts), 1)
        text = alerts[0]["text"]
        self.assertTrue(text.startswith("🔴 Notion write failed after 5 attempts\n"))
        self.assertIn("Message still queued, not lost", text)
        self.assertIn("(attempt 5 of 5, given up at 09:17)", text)  # 17.5 minutes in, Manila time
        self.assertIn("<code>retry -100123:42</code>", text)
        self.assertTrue(text.endswith(f"\n\n@{OWNER}"))
        self.assertEqual(p.telegram.sent[-1][2], text)

        # Exhausted work is not retried and not re-alerted, however long it waits.
        calls = notion.create_calls
        p.clock.advance(hours=6)
        await p.cycles(3)
        self.assertEqual(notion.create_calls, calls)
        self.assertEqual(len(p.outbox("outage_alert")), 1)
        self.assertEqual(notion.pages, {})  # no partial Job Order anywhere

    async def test_classifier_uses_the_same_schedule_and_alert(self):
        classifier = FakeClassifier(failed_result("timeout"))
        p = Pipeline(self, [jo()], classifier=classifier)
        for delay in (30, 120, 300, 600):
            await p.worker.run_once()
            self.assertEqual(
                p.message()["next_attempt_at"], fmt_utc(p.clock() + timedelta(seconds=delay))
            )
            p.clock.advance(seconds=delay)
        await p.worker.run_once()
        row = p.message()
        self.assertEqual((row["state"], row["attempts"]), ("received", 5))
        self.assertIsNotNone(row["exhausted_at"])
        self.assertEqual(len(classifier.calls), 5)
        (alert,) = p.outbox("outage_alert")
        self.assertIn("🔴 Classification failed after 5 attempts", alert["text"])
        self.assertEqual(p.ledger._conn.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0], 5)

    async def test_retry_after_stretches_a_short_delay(self):
        notion = FakeNotion(fail_create=True, create_error=RateLimited(600))
        p = Pipeline(self, [jo()], notion=notion)
        await p.worker.run_once()
        self.assertEqual(
            p.message()["next_attempt_at"], fmt_utc(p.clock() + timedelta(seconds=600))
        )

    async def test_classifier_retry_after_stretches_only_a_short_delay(self):
        for retry_after, expected in ((900, 900), (10, 30)):
            with self.subTest(retry_after=retry_after):
                result = failed_result("api_error").model_copy(
                    update={"retry_after": retry_after}
                )
                p = Pipeline(self, [jo()], classifier=FakeClassifier(result))
                await p.worker.run_once()
                self.assertEqual(
                    p.message()["next_attempt_at"],
                    fmt_utc(p.clock() + timedelta(seconds=expected)),
                )

    async def test_classifier_retry_after_applies_to_every_attempt(self):
        result = failed_result("api_error").model_copy(update={"retry_after": 400})
        p = Pipeline(self, [jo()], classifier=FakeClassifier(result))
        waits = []
        for _ in range(4):
            await p.worker.run_once()
            row = p.message()
            self.assertIsNone(row["exhausted_at"])
            due = row["next_attempt_at"]
            due_at = datetime.strptime(due, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
            waits.append(round((due_at - p.clock()).total_seconds()))
            p.clock.advance(seconds=waits[-1])
        self.assertEqual(waits, [400, 400, 400, 600])
        await p.worker.run_once()
        self.assertIsNotNone(p.message()["exhausted_at"])

    async def test_ops_post_retry_after_stretches_the_outbox_delay(self):
        class Limited(FakeTelegram):
            async def send_message(self, chat_id, topic_id, text):
                raise RateLimited(600)

        p = Pipeline(self, telegram=Limited())
        p.ledger.enqueue_system(OutboxMessage("stale_cache", -100999, 7, "stale"))
        await p.worker.run_once()
        (row,) = p.outbox("stale_cache")
        self.assertEqual(
            row["next_attempt_at"], fmt_utc(p.clock() + timedelta(seconds=600))
        )

    async def test_ops_posts_make_five_attempts_with_the_same_delays(self):
        p = Pipeline(self, telegram=FakeTelegram(fail_send=99))
        p.ledger.enqueue_system(OutboxMessage("stale_cache", -100999, 7, "stale"))
        start = p.clock()
        attempted_at = []
        for delay in (30, 120, 300, 600):
            await p.worker.run_once()
            (row,) = p.outbox("stale_cache")
            attempted_at.append(round((p.clock() - start).total_seconds()))
            self.assertEqual(row["state"], "pending")
            self.assertEqual(
                row["next_attempt_at"], fmt_utc(p.clock() + timedelta(seconds=delay))
            )
            p.clock.advance(seconds=delay)
        self.assertEqual(attempted_at, [0, 30, 150, 450])
        await p.worker.run_once()
        (row,) = p.outbox("stale_cache")
        self.assertEqual((row["state"], row["attempts"]), ("failed", 5))
        self.assertIsNone(row["next_attempt_at"])

    async def test_a_successful_state_change_resets_the_attempt_counter(self):
        notion = FakeNotion(fail_retrieve=2)
        p = Pipeline(self, [jo()], notion=notion)
        await p.worker.run_once()  # create works, read-back fails
        row = p.message()
        self.assertEqual((row["state"], row["attempts"]), ("writing", 1))
        self.assertEqual(row["notion_page_id"], "page-1")  # kept at once, still `writing`
        p.clock.advance(seconds=30)
        await p.worker.run_once()
        self.assertEqual(p.message()["attempts"], 2)
        p.clock.advance(seconds=120)
        await p.worker.run_once()
        row = p.message()
        self.assertEqual((row["state"], row["attempts"]), ("done", 0))

    async def test_read_back_failure_retries_only_the_retrieve(self):
        notion = FakeNotion(fail_retrieve=1)
        p = Pipeline(self, [jo()], notion=notion)
        await p.worker.run_once()
        p.clock.advance(seconds=30)
        await p.worker.run_once()
        self.assertEqual(p.message()["state"], "done")
        self.assertEqual(notion.create_calls, 1)  # never a second create
        self.assertEqual(notion.find_calls, 0)  # nor a Telegram Key query
        self.assertEqual(len(notion.pages), 1)

    async def test_normal_success_never_queries_by_telegram_key(self):
        notion = FakeNotion()
        p = Pipeline(self, [jo()], notion=notion)
        await p.cycles(2)
        self.assertEqual(p.message()["state"], "done")
        self.assertEqual(notion.find_calls, 0)

    async def test_read_back_exhaustion_names_its_step(self):
        notion = FakeNotion(fail_retrieve=99)
        p = Pipeline(self, [jo()], notion=notion)
        for delay in (30, 120, 300, 600, 600):
            await p.worker.run_once()
            p.clock.advance(seconds=delay)
        (alert,) = p.outbox("outage_alert")
        self.assertIn("🔴 Notion read-back failed after 5 attempts", alert["text"])
        self.assertEqual(p.message()["notion_page_id"], "page-1")

    async def test_reconciliation_failure_names_its_step(self):
        class CrashAfterCreate(FakeNotion):
            async def create_page(self, payload):
                await super().create_page(payload)
                raise Crash

        notion = CrashAfterCreate(fail_find=99)
        p = Pipeline(self, [jo()], notion=notion)
        with self.assertRaises(Crash):
            await p.worker.run_once()
        p.start()
        for delay in (30, 120, 300, 600, 600):
            p.clock.advance(seconds=delay)
            await p.worker.run_once()
        (alert,) = p.outbox("outage_alert")
        self.assertIn("🔴 Notion reconciliation failed after 5 attempts", alert["text"])

    async def test_ops_posts_use_their_own_counter_and_count_as_failed(self):
        telegram = FakeTelegram([jo()], fail_send=99)
        p = Pipeline(self, telegram=telegram)
        p.ledger.enqueue_system(OutboxMessage("stale_cache", -100999, 7, "stale"))
        for delay in (30, 120, 300, 600, 600):
            await p.worker.run_once()
            p.clock.advance(seconds=delay)
        (row,) = p.outbox("stale_cache")
        self.assertEqual((row["state"], row["attempts"]), ("failed", 5))
        self.assertEqual(p.message()["attempts"], 0)  # the message is a separate counter
        self.assertEqual(p.message()["state"], "done")

    async def test_empty_cache_hold_spends_no_attempt(self):
        caches = SimpleNamespace(ready=False)

        async def refresh_if_due():
            return []

        caches.refresh_if_due = refresh_if_due
        p = Pipeline(self, [jo()], caches=caches)
        for _ in range(5):
            await p.worker.run_once()
            p.clock.advance(hours=1)
        row = p.message()
        self.assertEqual(
            (row["state"], row["attempts"], row["exhausted_at"]), ("extracted", 0, None)
        )
        self.assertEqual(p.outbox("outage_alert"), [])
        caches.ready = True
        caches.settings = SimpleNamespace(department_aliases={})
        caches.contact_for_telegram_user = lambda *a: None
        caches.departments = ["TOC"]
        caches.resolve_people = lambda *a: PeopleResolution()
        caches.resolve_project = lambda raw: ProjectResolution(page_id="proj")
        caches.resolve_department = lambda raw: DepartmentResolution(department="TOC")
        await p.worker.run_once()
        self.assertEqual(p.message()["state"], "done")


class ReactionTests(unittest.IsolatedAsyncioTestCase):
    async def test_reaction_retries_inline_then_counts_without_an_alert(self):
        p = Pipeline(self, telegram=FakeTelegram([jo()], fail_reaction=True))
        await p.worker.run_once()
        row = p.message()
        self.assertEqual((row["state"], row["reaction_state"]), ("done", "failed"))
        self.assertEqual(p.sleeps, [2, 5])
        self.assertEqual(p.outbox("outage_alert"), [])

    async def test_a_late_reaction_success_is_recorded(self):
        class Flaky(FakeTelegram):
            async def set_reaction(self, chat_id, message_id, emoji):
                self.tries = getattr(self, "tries", 0) + 1
                if self.tries < 3:
                    raise RuntimeError("busy")
                await super().set_reaction(chat_id, message_id, emoji)

        p = Pipeline(self, telegram=Flaky([jo()]))
        await p.worker.run_once()
        self.assertEqual(p.message()["reaction_state"], "set")


class ManualRetryTests(unittest.IsolatedAsyncioTestCase):
    async def exhausted(self, **kwargs):
        notion = FakeNotion(fail_create=True)
        p = Pipeline(self, [jo(1, 42), jo(2, 43)], notion=notion, **kwargs)
        for delay in (30, 120, 300, 600, 600):
            await p.worker.run_once()
            p.clock.advance(seconds=delay)
        self.assertEqual(len(p.ledger.exhausted_keys()), 2)
        return p, notion

    async def test_retry_makes_one_message_eligible_without_changing_its_plan(self):
        p, notion = await self.exhausted()
        plan = p.message()["plan_json"]
        notion.fail_create = False
        self.assertTrue(p.ledger.retry(KEY, now=p.clock()))
        row = p.message()
        self.assertEqual((row["attempts"], row["exhausted_at"]), (0, None))
        self.assertEqual(row["next_attempt_at"], fmt_utc(p.clock()))
        await p.worker.run_once()
        self.assertEqual(p.message()["state"], "done")
        self.assertEqual(p.message()["plan_json"], plan)
        self.assertEqual(p.message("-100123:43")["state"], "writing")  # still waiting
        self.assertEqual(len(notion.pages), 1)

    async def test_retry_all_exhausted(self):
        p, notion = await self.exhausted()
        notion.fail_create = False
        self.assertEqual(p.ledger.retry_all_exhausted(now=p.clock()), 2)
        await p.worker.run_once()
        self.assertEqual(len(notion.pages), 2)
        self.assertEqual(p.ledger.exhausted_keys(), [])
        self.assertEqual(p.message()["state"], "done")
        self.assertEqual(p.message("-100123:43")["state"], "done")

    async def test_retry_ignores_a_message_that_is_not_exhausted(self):
        p = Pipeline(self, [jo()])
        await p.worker.run_once()
        self.assertFalse(p.ledger.retry(KEY, now=p.clock()))
        self.assertFalse(p.ledger.retry("-1:1", now=p.clock()))

    async def test_a_message_can_exhaust_and_alert_again_after_a_retry(self):
        p, _ = await self.exhausted()
        p.ledger.retry(KEY, now=p.clock())
        for delay in (30, 120, 300, 600, 600):
            await p.worker.run_once()
            p.clock.advance(seconds=delay)
        self.assertEqual(len(p.outbox("outage_alert")), 3)  # 2 first time, 1 again


class RetryCommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.ledger_path = self.dir / "ledger.db"
        self.config = self.dir / "config.yaml"
        self.config.write_text(
            "intake_chat_id: -100123\nintake_topic_id: 5\nops_chat_id: -100999\n"
            "ops_topic_id: 7\njob_orders_data_source_id: ds-1\n"
            f"ledger_path: {self.ledger_path}\n"
        )
        ledger = Ledger(self.ledger_path)
        for message_id in (42, 43):
            ledger.accept_message(-100123, message_id, message_id, "{}", "t", "t")
            ledger._conn.execute(
                "UPDATE messages SET exhausted_at = '2026-10-22T01:00:00Z', attempts = 5"
                " WHERE message_id = ?",
                (message_id,),
            )
        ledger.close()

    def exhausted(self):
        ledger = Ledger(self.ledger_path)
        try:
            return ledger.exhausted_keys()
        finally:
            ledger.close()

    def test_retry_one_key(self):
        self.assertEqual(__main__.main(["retry", KEY, "--config", str(self.config)]), 0)
        self.assertEqual(self.exhausted(), ["-100123:43"])

    def test_retry_all_exhausted(self):
        argv = ["retry", "--all-exhausted", "--config", str(self.config)]
        self.assertEqual(__main__.main(argv), 0)
        self.assertEqual(self.exhausted(), [])

    def test_retry_needs_exactly_one_target(self):
        self.assertEqual(__main__.main(["retry", "--config", str(self.config)]), 2)
        self.assertEqual(
            __main__.main(["retry", KEY, "--all-exhausted", "--config", str(self.config)]), 2
        )

    def test_retry_of_an_unknown_key_reports_failure(self):
        self.assertEqual(__main__.main(["retry", "-1:1", "--config", str(self.config)]), 1)


class CrashAfterCreateNotion(FakeNotion):
    async def create_page(self, payload):
        await super().create_page(payload)
        raise Crash


class WritingRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def crashed_after_create(self, notion=None):
        notion = notion or CrashAfterCreateNotion()
        p = Pipeline(self, [jo()], notion=notion)
        with self.assertRaises(Crash):
            await p.worker.run_once()
        row = p.message()
        self.assertEqual(row["state"], "writing")
        self.assertIsNone(row["notion_page_id"])  # the ledger never heard of the page
        self.assertEqual(len(notion.pages), 1)
        p.start()  # restart
        return p, notion

    async def test_recovery_waits_for_the_first_retry_delay_then_adopts_the_row(self):
        p, notion = await self.crashed_after_create()
        await p.worker.run_once()
        self.assertEqual(notion.find_calls, 0)  # too soon: the create may still be landing
        p.clock.advance(seconds=29)
        await p.worker.run_once()
        self.assertEqual(notion.find_calls, 0)
        p.clock.advance(seconds=1)
        await p.worker.run_once()
        self.assertEqual(notion.find_calls, 1)
        row = p.message()
        self.assertEqual((row["state"], row["notion_page_id"]), ("done", "page-1"))
        self.assertEqual(len(notion.pages), 1)
        self.assertEqual(notion.create_calls, 1)  # adopted, not created again

    async def test_recovery_creates_only_when_no_row_exists(self):
        class CrashBeforeCreate(FakeNotion):
            async def create_page(self, payload):
                self.create_calls += 1
                raise Crash

        notion = CrashBeforeCreate()
        p = Pipeline(self, [jo()], notion=notion)
        with self.assertRaises(Crash):
            await p.worker.run_once()
        self.assertEqual(notion.pages, {})
        p.start()
        notion.__class__ = FakeNotion  # the dependency is healthy again
        p.clock.advance(seconds=30)
        await p.worker.run_once()
        self.assertEqual(notion.find_calls, 1)
        self.assertEqual(len(notion.pages), 1)
        self.assertEqual(p.message()["state"], "done")

    async def test_multiple_matches_are_reported_and_never_merged_or_deleted(self):
        p, notion = await self.crashed_after_create()
        duplicate = json.loads(json.dumps(notion.pages["page-1"]))
        duplicate["id"], duplicate["url"] = "page-9", "https://notion.example/page-9"
        notion.pages["page-9"] = duplicate
        p.clock.advance(seconds=30)
        await p.worker.run_once()
        row = p.message()
        self.assertEqual((row["state"], row["notion_page_id"]), ("done", "page-1"))
        self.assertEqual(sorted(notion.pages), ["page-1", "page-9"])  # nothing removed
        (report,) = p.outbox("capture_report")
        self.assertIn("More than one Notion row has this Telegram Key", report["text"])
        self.assertIn("notion.example/page-1", report["text"])
        self.assertIn("notion.example/page-9", report["text"])


class LivenessTests(unittest.IsolatedAsyncioTestCase):
    async def test_every_cycle_stamps_liveness(self):
        p = Pipeline(self)
        await p.worker.run_once()
        self.assertEqual(p.ledger.poller_alive_at(), p.clock())
        p.clock.advance(seconds=40)
        await p.worker.run_once()
        self.assertEqual(p.ledger.poller_alive_at(), p.clock())
        ok, _ = healthcheck.check(p.path, now=p.clock())
        self.assertTrue(ok)
        ok, _ = healthcheck.check(p.path, now=p.clock() + timedelta(minutes=3))
        self.assertFalse(ok)

    async def test_failing_poll_cycles_do_not_stamp_liveness(self):
        class Broken(FakeTelegram):
            async def get_updates(self, offset, timeout):
                raise RuntimeError("cannot serialize update")

        p = Pipeline(self, telegram=Broken())
        p.worker._beat()
        started = p.clock()
        for _ in range(5):
            p.clock.advance(seconds=40)
            with self.assertRaises(RuntimeError):
                await p.worker.run_once()
        self.assertEqual(p.ledger.poller_alive_at(), started)
        ok, _ = healthcheck.check(p.path, now=p.clock())
        self.assertFalse(ok)

    async def test_deadman_fires_after_ten_stale_minutes_and_not_before(self):
        p = Pipeline(self)
        await p.worker.run_once()
        p.clock.advance(minutes=10)
        p.worker.check_deadman()  # exactly ten minutes is still alive
        p.clock.advance(seconds=1)
        with self.assertRaises(DeadmanExit):
            p.worker.check_deadman()

    async def test_run_exits_when_the_poller_hangs(self):
        class Hung(FakeTelegram):
            async def get_updates(self, offset, timeout):
                await asyncio.Event().wait()

        p = Pipeline(self, telegram=Hung())
        task = asyncio.create_task(p.worker.run())
        await asyncio.sleep(0.05)
        self.assertFalse(task.done())
        p.clock.advance(minutes=11)
        with self.assertRaises(DeadmanExit):
            await asyncio.wait_for(task, 2)

    def test_main_exits_non_zero_on_deadman(self):
        async def boom(_path):
            raise DeadmanExit("stale")

        with mock.patch.object(__main__, "run", boom):
            self.assertEqual(__main__.main(["--config", "x.yaml"]), 1)


class HeartbeatWindowTests(unittest.TestCase):
    def window(self, now):
        start, end = heartbeat_window(now, "07:00", ZoneInfo("Asia/Manila"))
        return fmt_utc(start), fmt_utc(end)

    def test_window_ends_at_the_latest_manila_0700(self):
        clock = FakeClock()
        # 09:00 Manila: the window is Oct 21 07:00 -> Oct 22 07:00 Manila.
        self.assertEqual(self.window(clock()), ("2026-10-20T23:00:00Z", "2026-10-21T23:00:00Z"))

    def test_before_0700_still_covers_the_previous_day(self):
        clock = FakeClock()
        clock.advance(hours=-3)  # 06:00 Manila
        self.assertEqual(self.window(clock()), ("2026-10-19T23:00:00Z", "2026-10-20T23:00:00Z"))

    def test_exactly_0700_belongs_to_the_window_that_just_ended(self):
        clock = FakeClock()
        clock.advance(hours=-2)  # 07:00 Manila
        self.assertEqual(self.window(clock()), ("2026-10-20T23:00:00Z", "2026-10-21T23:00:00Z"))


class HeartbeatTests(unittest.IsolatedAsyncioTestCase):
    def day_one(self):
        forward = jo(2, 12, "MG - TOC - JO #42\nTask: cut")
        forward.raw["message"]["forward_origin"] = CHANNEL
        updates = [
            jo(1, 11),
            forward,
            update(3, message_id=13, text="lunch at noon?"),
            update(4, message_id=14, text=""),
            update(5, message_id=15, text="MG - TOC - JO #43\nPassword: hunter22\nTask: x"),
        ]
        classifier = FakeClassifier(
            ok_result(title="A", task="a"),
            ok_result(title="B", task="b"),
            ok_result("not_jo"),
            ok_result(title="C", task="c"),
        )
        return updates, classifier

    async def test_heartbeat_renders_the_previous_24_hours_from_the_ledger(self):
        updates, classifier = self.day_one()
        p = Pipeline(self, updates, classifier=classifier)
        await p.worker.run_once()
        self.assertEqual(p.outbox("heartbeat"), [])  # a brand-new ledger has nothing to report
        p.clock.advance(hours=24)  # 09:00 Manila the next day
        await p.worker.run_once()
        (row,) = p.outbox("heartbeat")
        self.assertEqual(row["state"], "sent")
        self.assertEqual(
            row["text"],
            "\n".join(
                [
                    "📋 Daily heartbeat — Oct 22 07:00 → Oct 23 07:00",
                    "",
                    "Captured: 3",
                    "Flagged: 1",
                    "Ignored (not a JO): 1",
                    "No-text forwards: 1",
                    "Passwords withheld: 1",
                    "Reaction failed: 0",
                    "",
                    "Deliverables linked: 0",
                    "Deliverables hinted: 0",
                    "Deliverables none: 0",
                    "Deliverables skipped: 0",
                    "",
                    "Queue: 0 stuck, 0 pending retry, 0 ops posts failed",
                    "",
                    "Ignored, first lines:",
                    '"lunch at noon?"',
                ]
            ),
        )

    async def test_heartbeat_is_sent_once_per_window_even_across_restarts(self):
        p = Pipeline(self)
        await p.worker.run_once()
        p.clock.advance(hours=24)
        await p.cycles(3)
        p.start()
        await p.cycles(2)
        self.assertEqual(len(p.outbox("heartbeat")), 1)
        p.clock.advance(hours=24)
        await p.worker.run_once()
        self.assertEqual(len(p.outbox("heartbeat")), 2)

    async def test_heartbeat_comes_from_ledger_rows_not_counters(self):
        updates, classifier = self.day_one()
        p = Pipeline(self, updates, classifier=classifier)
        await p.worker.run_once()
        start, end = heartbeat_window(
            p.clock() + timedelta(hours=24), "07:00", ZoneInfo("Asia/Manila")
        )
        before = p.ledger.heartbeat_stats(start, end)
        p.ledger._conn.execute(
            "UPDATE messages SET reaction_state = 'failed' WHERE message_id = 11"
        )
        after = p.ledger.heartbeat_stats(start, end)
        self.assertEqual(after["reaction_failed"], before["reaction_failed"] + 1)

    async def test_stuck_pending_and_failed_ops_posts_show_in_the_queue_line(self):
        notion = FakeNotion(fail_create=True)
        p = Pipeline(self, [jo(1, 42), jo(2, 43)], notion=notion)
        await p.worker.run_once()
        p.ledger._conn.execute(
            "UPDATE messages SET exhausted_at = '2026-10-22T01:00:00Z' WHERE message_id = 43"
        )
        p.ledger._conn.execute(
            "INSERT INTO outbox (kind, chat_id, topic_id, text, state, attempts, created_at)"
            " VALUES ('stale_cache', 1, 1, 'x', 'failed', 5, '2026-10-22T01:00:00Z')"
        )
        stats = p.ledger.heartbeat_stats(
            p.clock() - timedelta(days=1), p.clock() + timedelta(days=1)
        )
        self.assertEqual(
            (stats["stuck"], stats["pending_retry"], stats["ops_failed"]), (1, 1, 1)
        )

    async def test_model_change_line_only_when_the_returned_model_changed(self):
        def result(model):
            r = ok_result(title="T", task="t")
            r.returned_model = model
            return r

        classifier = FakeClassifier(result("deepseek-chat-v9"))
        p = Pipeline(self, [jo(1, 11)], classifier=classifier)
        await p.worker.run_once()
        p.clock.advance(hours=24)
        telegram_updates = [jo(2, 12)]
        classifier.results = [result("deepseek-chat-v9")]
        p.telegram.updates = telegram_updates
        await p.worker.run_once()  # heartbeat for day one, plus day two's message
        self.assertNotIn("Model changed", p.outbox("heartbeat")[0]["text"])

        p.clock.advance(hours=24)
        p.telegram.updates = [jo(3, 13)]
        classifier.results = [result("deepseek-chat-v10")]
        await p.worker.run_once()  # heartbeat for day two (still v9), day three's message
        self.assertNotIn("Model changed", p.outbox("heartbeat")[1]["text"])

        p.clock.advance(hours=24)
        await p.worker.run_once()  # heartbeat for day three: v9 -> v10
        self.assertIn(
            "Model changed: deepseek-chat-v9 → deepseek-chat-v10", p.outbox("heartbeat")[2]["text"]
        )

    async def test_no_cost_total_is_reported_but_tokens_stay_queryable(self):
        p = Pipeline(self, [jo()])
        await p.worker.run_once()
        p.clock.advance(hours=24)
        await p.worker.run_once()
        text = p.outbox("heartbeat")[0]["text"]
        self.assertNotIn("token", text.lower())
        self.assertNotIn("cost", text.lower())
        (tokens,) = p.ledger._conn.execute(
            "SELECT input_tokens + output_tokens FROM llm_calls"
        ).fetchone()
        self.assertEqual(tokens, 1500)


class MigrationSevenTests(unittest.TestCase):
    def test_migration_7_adds_the_specified_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Ledger(Path(tmp) / "ledger.db")
            self.addCleanup(ledger.close)
            self.assertIn(7, MIGRATIONS)
            columns = {r[1]: r[2] for r in ledger._conn.execute("PRAGMA table_info(messages)")}
            self.assertEqual(columns["exhausted_at"], "TEXT")
            runtime = {
                r[1]: (r[2], r[5]) for r in ledger._conn.execute("PRAGMA table_info(runtime_state)")
            }
            self.assertEqual(
                runtime, {"key": ("TEXT", 1), "value": ("TEXT", 0), "updated_at": ("TEXT", 0)}
            )
            heartbeats = [r[1] for r in ledger._conn.execute("PRAGMA table_info(heartbeats)")]
            self.assertEqual(heartbeats, ["window_start", "window_end", "outbox_id", "created_at"])
            with self.assertRaises(sqlite3.IntegrityError):
                ledger._conn.execute(
                    "INSERT INTO heartbeats VALUES ('w', 'x', 999, 'now')"
                )  # outbox_id must reference a real outbox row


class CaptureWindowTests(unittest.IsolatedAsyncioTestCase):
    """#54: a capture stays in the window it was written in (real SQLite, fake clock)."""

    def at_0659_59(self):
        # 06:59:59 Manila on Oct 22 is 22:59:59 UTC on Oct 21.
        return FakeClock(datetime(2026, 10, 21, 22, 59, 59, tzinfo=UTC))

    def windows(self, p):
        zone = ZoneInfo("Asia/Manila")
        before = heartbeat_window(datetime(2026, 10, 21, 23, 0, 1, tzinfo=UTC), "07:00", zone)
        after = heartbeat_window(datetime(2026, 10, 22, 23, 0, 1, tzinfo=UTC), "07:00", zone)
        return p.ledger.heartbeat_stats(*before), p.ledger.heartbeat_stats(*after)

    def assert_only_in_first(self, p):
        first, second = self.windows(p)
        self.assertEqual((first["captured"], second["captured"]), (1, 0))

    async def test_reaction_retries_crossing_0700_do_not_move_the_capture(self):
        clock = self.at_0659_59()
        p = Pipeline(self, [jo()], clock=clock)
        p.telegram.fail_reaction = True

        async def sleep(seconds):
            clock.advance(seconds=seconds)

        p.worker = p.make_worker(p.ledger, sleep=sleep)
        await p.worker.run_once()
        row = p.message()
        self.assertEqual((row["state"], row["reaction_state"]), ("done", "failed"))
        self.assertGreater(row["updated_at"], row["written_at"])  # the reaction came later
        self.assertEqual(row["written_at"], "2026-10-21T22:59:59Z")
        self.assert_only_in_first(p)

    async def test_flagged_follows_the_same_window(self):
        clock = self.at_0659_59()
        p = Pipeline(self, [jo(text="MG - TOC - JO #41\nPassword: hunter22\nTask: x")], clock=clock)
        await p.worker.run_once()
        clock.advance(minutes=5)
        p.ledger.set_reaction_state(KEY, "set")
        first, second = self.windows(p)
        self.assertEqual((first["flagged"], second["flagged"]), (1, 0))

    async def test_restart_and_delayed_recovery_keep_the_capture_in_its_window(self):
        class CrashOnReaction(FakeTelegram):
            async def set_reaction(self, *args):
                raise Crash

        clock = self.at_0659_59()
        p = Pipeline(self, [jo()], clock=clock, telegram=CrashOnReaction([jo()]))
        with self.assertRaises(Crash):
            await p.worker.run_once()
        self.assertEqual(p.message()["state"], "written")

        clock.advance(hours=3)  # the restart is long after 07:00
        p.telegram = FakeTelegram()
        p.start()
        await p.worker.run_once()
        row = p.message()
        self.assertEqual((row["state"], row["reaction_state"]), ("done", "set"))
        self.assertEqual(row["written_at"], "2026-10-21T22:59:59Z")
        self.assertGreater(row["updated_at"], "2026-10-22T00:00:00Z")
        self.assert_only_in_first(p)

    async def test_written_at_is_set_once_by_mark_written(self):
        p = Pipeline(self, [jo()], clock=self.at_0659_59())
        self.assertIsNone(p.ledger._conn.execute("SELECT written_at FROM messages").fetchone())
        await p.worker.run_once()
        stamp = p.message()["written_at"]
        p.clock.advance(hours=5)
        p.ledger.record_mismatch(KEY, "late")
        p.ledger.set_reaction_state(KEY, "failed")
        self.assertEqual(p.message()["written_at"], stamp)

    async def test_the_heartbeat_labels_and_counts_the_preceding_24_hours_at_0700_manila(self):
        clock = self.at_0659_59()
        p = Pipeline(self, [jo()], clock=clock)
        await p.worker.run_once()  # 06:59:59; the brand-new ledger skips its first window
        self.assertEqual(p.outbox("heartbeat"), [])
        clock.advance(seconds=1)  # 07:00:00 Manila closes the window holding the capture
        await p.worker.run_once()
        (row,) = p.outbox("heartbeat")
        self.assertIn("Daily heartbeat — Oct 21 07:00 → Oct 22 07:00", row["text"])
        self.assertIn("Captured: 1", row["text"])

    async def test_legacy_rows_without_written_at_fall_back_to_updated_at(self):
        p = Pipeline(self, [jo()], clock=self.at_0659_59())
        await p.worker.run_once()
        p.ledger._conn.execute("UPDATE messages SET written_at = NULL")
        first, second = self.windows(p)
        self.assertEqual((first["captured"], second["captured"]), (1, 0))
        # Nothing invents a capture time for the legacy row.
        self.assertIsNone(p.message()["written_at"])


class MigrationEightTests(unittest.TestCase):
    def test_upgrade_adds_a_nullable_written_at_and_leaves_legacy_rows_null(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.db"
            prior = {v: m for v, m in MIGRATIONS.items() if v < 8}
            with mock.patch("jo_watchdog.ledger.db.MIGRATIONS", prior):
                old = Ledger(path)
                old.accept_message(-100123, 42, 7, "{}", "PROJ - EDT - JO #1", "PROJ - EDT - JO #1")
                old._conn.execute("UPDATE messages SET state = 'done'")
                old.close()
            ledger = Ledger(path)
            self.addCleanup(ledger.close)
            info = ledger._conn.execute("PRAGMA table_info(messages)")
            columns = {r[1]: (r[2], r[3]) for r in info}
            self.assertEqual(columns["written_at"], ("TEXT", 0))
            (value,) = ledger._conn.execute("SELECT written_at FROM messages").fetchone()
            self.assertIsNone(value)


if __name__ == "__main__":
    unittest.main()
