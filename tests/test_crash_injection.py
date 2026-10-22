"""Whole-pipeline crash injection (#25).

A scenario runs one forwarded Job Order through the real worker, real SQLite and
fake Telegram/Notion/DeepSeek adapters. A first, clean run counts every call the
pipeline makes across the ledger and the three adapters; then the scenario is
replayed once per call, "killing the process" immediately before and immediately
after it. After the restart (a new ledger connection and worker on the same
file, the fake services keeping what they already accepted) the pipeline must
converge with:

* exactly one Notion row for the Telegram Key,
* the stored plan unchanged,
* the offset never ahead of a message that was not durably queued,
* the captured report delivered at least once (duplication is acceptable),
* the captured reaction independent of everything else.
"""

import inspect
import unittest

from jo_watchdog.ledger.db import Ledger
from tests.clockwork import KEY, Crash, Pipeline
from tests.fakes import FakeClassifier, FakeNotion, FakeTelegram, ok_result, update

CHANNEL = {
    "type": "channel",
    "chat": {"id": -1005551234, "title": "Studio", "username": "studio"},
    "message_id": 88,
}

# Pure reads cannot change what survives a crash; they only honour a dead process.
READS = frozenset(
    {
        "get_message", "has_message", "due", "pending", "stage_result", "next_offset",
        "last_committed_offset", "poller_alive_at", "heartbeat_exists", "has_heartbeats",
        "heartbeat_stats", "edit_seen", "has_pending_edits", "edits_for", "earlier_captures",
        "due_outbox", "exhausted_keys", "latest_cache_snapshot", "cache_snapshot_count",
    }
)
ADAPTER_CALLS = {
    "telegram": ("get_updates", "set_reaction", "send_message"),
    "notion": ("create_page", "retrieve_page", "find_pages_by_telegram_key"),
    "classifier": ("classify",),
}


class ReplayTelegram(FakeTelegram):
    """Redelivers every update at or after the offset, as Telegram does until acknowledged."""

    def __init__(self, updates):
        super().__init__(updates)
        self.all_updates = list(updates)

    async def get_updates(self, offset, timeout):
        self.get_updates_calls.append(offset)
        return [u for u in self.all_updates if u.update_id >= offset]


class Seams:
    """Counts calls and kills the 'process' at one chosen call, before or after it."""

    def __init__(self, crash_at: tuple[str, int, str] | None = None):
        self.crash_at = crash_at  # (label, nth call, "before" | "after")
        self.counts: dict[str, int] = {}
        self.dead = False

    def wrap(self, obj, name: str, label: str, *, point: bool = True):
        real = getattr(obj, name)

        def check_before(nth):
            if self.dead:
                raise Crash
            if point and self.crash_at == (label, nth, "before"):
                self.dead = True
                raise Crash

        def check_after(nth):
            if point and self.crash_at == (label, nth, "after"):
                self.dead = True
                raise Crash

        def count():
            if not point:
                return 0
            self.counts[label] = self.counts.get(label, 0) + 1
            return self.counts[label]

        if inspect.iscoroutinefunction(real):

            async def wrapped(*args, **kwargs):
                nth = count()
                check_before(nth)
                result = await real(*args, **kwargs)
                check_after(nth)
                return result

        else:

            def wrapped(*args, **kwargs):
                nth = count()
                check_before(nth)
                result = real(*args, **kwargs)
                check_after(nth)
                return result

        setattr(obj, name, wrapped)

    def install(self, pipeline: Pipeline) -> None:
        ledger = pipeline.ledger
        for name in dir(ledger):
            if name.startswith("_") or name == "close" or not callable(getattr(ledger, name)):
                continue
            self.wrap(ledger, name, f"ledger.{name}", point=name not in READS)
        for owner, names in ADAPTER_CALLS.items():
            for name in names:
                self.wrap(getattr(pipeline, owner), name, f"{owner}.{name}")


def forwarded_jo():
    u = update(7, message_id=42, text="MG - TOC - JO #41\nTask: edit the teaser")
    u.raw["message"]["forward_origin"] = CHANNEL  # a non-user origin, so a report is queued
    return u


def make_pipeline(test) -> Pipeline:
    return Pipeline(
        test,
        telegram=ReplayTelegram([forwarded_jo()]),
        notion=FakeNotion(),
        classifier=FakeClassifier(ok_result(title="Teaser", task="Edit the teaser")),
    )


def peek(pipeline: Pipeline) -> dict:
    """The message row as the dead process left it, read through a fresh connection."""
    survivor = Ledger(pipeline.path)
    try:
        return survivor.get_message(KEY)
    finally:
        survivor.close()


async def run_until_crash(pipeline: Pipeline, seams: Seams) -> bool:
    seams.install(pipeline)
    try:
        await pipeline.worker.run_once()
    except Crash:
        return True
    return False


async def recover(pipeline: Pipeline, seams: Seams, cycles: int = 8) -> None:
    # The services outlive the process; only the process (ledger connection and
    # worker) is new. Revive the wrapped adapters instead of unwrapping them.
    seams.dead, seams.crash_at = False, None
    pipeline.start()
    for _ in range(cycles):
        pipeline.clock.advance(seconds=40)
        await pipeline.worker.run_once()


class CrashInjectionTests(unittest.IsolatedAsyncioTestCase):
    async def reference_plan(self) -> tuple[str, dict[str, int]]:
        """The plan a clean run stores, and how many times each seam was hit."""
        pipeline = make_pipeline(self)
        seams = Seams()
        crashed = await run_until_crash(pipeline, seams)
        self.assertFalse(crashed)
        self.assertEqual(pipeline.message()["state"], "done")
        return pipeline.message()["plan_json"], dict(seams.counts)

    async def test_the_clean_run_touches_every_boundary_the_suite_injects_at(self):
        _, counts = await self.reference_plan()
        for label in (
            "ledger.accept_message", "ledger.record_extraction", "ledger.plan_message",
            "ledger.transition", "ledger.mark_writing", "ledger.store_page_id",
            "ledger.mark_written", "ledger.set_reaction_state", "ledger.mark_done",
            "ledger.outbox_sent", "telegram.get_updates", "telegram.set_reaction",
            "telegram.send_message", "classifier.classify", "notion.create_page",
            "notion.retrieve_page",
        ):
            self.assertIn(label, counts)

    async def test_a_crash_before_or_after_every_call_converges_to_one_correct_row(self):
        plan, counts = await self.reference_plan()
        points = [
            (label, nth, phase)
            for label, total in sorted(counts.items())
            for nth in range(1, total + 1)
            for phase in ("before", "after")
        ]
        self.assertGreater(len(points), 40)
        for point in points:
            with self.subTest(point=point):
                await self.assert_converges(point, plan)

    async def assert_converges(self, point, reference_plan: str) -> None:
        pipeline = make_pipeline(self)
        seams = Seams(crash_at=point)
        crashed = await run_until_crash(pipeline, seams)
        self.assertTrue(crashed, "the injected crash was never reached")

        # Commit-before-offset: the offset never passes a message that is not queued.
        survivor = Ledger(pipeline.path)
        try:
            if survivor.last_committed_offset() >= 7:
                self.assertTrue(survivor.has_message(KEY))
            stored_before = (survivor.get_message(KEY) or {}).get("plan_json")
        finally:
            survivor.close()

        await recover(pipeline, seams)

        row = pipeline.message()
        self.assertEqual(row["state"], "done")
        self.assertEqual(row["reaction_state"], "set")
        count = pipeline.ledger._conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        self.assertEqual(count, 1)
        # One Notion row per Telegram Key, however the crash fell.
        self.assertEqual(len(pipeline.notion.pages), 1)
        # The stored plan is replayed, never recomputed.
        self.assertEqual(row["plan_json"], reference_plan)
        if stored_before:
            self.assertEqual(row["plan_json"], stored_before)
        # The report may repeat after a crash but is never lost.
        reports = [t for _, _, t in pipeline.telegram.sent if "Flagged on capture" in t]
        self.assertGreaterEqual(len(reports), 1)
        self.assertEqual([r["state"] for r in pipeline.outbox("capture_report")], ["sent"])
        # A crash is not a failure: no attempt was spent and nothing was given up.
        self.assertEqual(pipeline.outbox("outage_alert"), [])
        self.assertIsNone(row["exhausted_at"])
        self.assertTrue(pipeline.telegram.reactions)

    async def test_the_plan_is_replayed_after_the_caches_or_classifier_change(self):
        plan, _ = await self.reference_plan()
        pipeline = make_pipeline(self)
        seams = Seams(crash_at=("ledger.mark_writing", 1, "before"))
        self.assertTrue(await run_until_crash(pipeline, seams))
        stored = peek(pipeline)
        self.assertEqual((stored["state"], stored["plan_json"]), ("planned", plan))
        # Everything upstream now answers differently; the stored plan must not move.
        pipeline.classifier.results = [ok_result(title="Different", task="Other")]
        await recover(pipeline, seams)
        self.assertEqual(pipeline.message()["plan_json"], plan)
        self.assertEqual(len(pipeline.notion.created), 1)
        self.assertEqual(len(pipeline.classifier.calls), 1)  # never re-extracted

    async def test_a_crash_between_create_and_page_id_never_double_writes(self):
        pipeline = make_pipeline(self)
        seams = Seams(crash_at=("notion.create_page", 1, "after"))
        self.assertTrue(await run_until_crash(pipeline, seams))
        self.assertEqual(len(pipeline.notion.pages), 1)
        self.assertIsNone(peek(pipeline)["notion_page_id"])
        await recover(pipeline, seams)
        self.assertEqual(pipeline.notion.create_calls, 1)
        self.assertEqual(pipeline.notion.find_calls, 1)
        self.assertEqual(pipeline.message()["notion_page_id"], "page-1")

    async def test_a_dead_reaction_never_blocks_the_report_or_the_capture(self):
        pipeline = make_pipeline(self)
        seams = Seams(crash_at=("telegram.set_reaction", 1, "before"))
        self.assertTrue(await run_until_crash(pipeline, seams))
        self.assertEqual(peek(pipeline)["state"], "written")
        await recover(pipeline, seams)
        self.assertEqual(pipeline.message()["state"], "done")
        self.assertEqual(len(pipeline.outbox("capture_report")), 1)


if __name__ == "__main__":
    unittest.main()
