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
* the captured reaction independent of everything else, and never set before
  the row is confirmed written.

``JevCrashInjectionTests`` repeats this with the Deliverable source and the Jev
matcher wired in, enumerating a clean run per scenario (matcher success, matcher
retry, candidate-query retry, matcher exhausted) and a second crash at every
call the uncertain-writing reconciliation makes (#56).
"""

import inspect
import unittest

from jo_watchdog.adapters.jev import MatcherError
from jo_watchdog.app.caches import FORMAT_VERSION, CacheManager
from jo_watchdog.core.matching import Candidate, CandidateDecision, MatchResult
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
        "get_message",
        "has_message",
        "due",
        "pending",
        "stage_result",
        "next_offset",
        "last_committed_offset",
        "poller_alive_at",
        "heartbeat_exists",
        "has_heartbeats",
        "heartbeat_stats",
        "edit_seen",
        "has_pending_edits",
        "edits_for",
        "earlier_captures",
        "due_outbox",
        "exhausted_keys",
        "latest_cache_snapshot",
        "cache_snapshot_count",
    }
)
ADAPTER_CALLS = {
    "telegram": ("get_updates", "set_reaction", "send_message"),
    "notion": ("create_page", "retrieve_page", "find_pages_by_telegram_key"),
    "classifier": ("classify",),
    "matcher": ("match",),
    "deliverables": ("query_project_deliverables",),
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

    def install_ledger(self, pipeline: Pipeline) -> None:
        ledger = pipeline.ledger
        for name in dir(ledger):
            if name.startswith("_") or name == "close" or not callable(getattr(ledger, name)):
                continue
            self.wrap(ledger, name, f"ledger.{name}", point=name not in READS)

    def install(self, pipeline: Pipeline) -> None:
        self.install_ledger(pipeline)
        for owner, names in ADAPTER_CALLS.items():
            target = getattr(pipeline, owner, None)  # the Jev seams exist only when wired
            for name in names if target is not None else ():
                self.wrap(target, name, f"{owner}.{name}")


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


async def recover(pipeline: Pipeline, seams: Seams, cycles: int = 8, *, crash_at=None) -> bool:
    """Restart the process; with ``crash_at`` (relative to this restart) kill it again.

    The services outlive the process; only the process (ledger connection and
    worker) is new. Revive the wrapped adapters instead of unwrapping them.
    """
    seams.dead, seams.crash_at, seams.counts = False, crash_at, {}
    pipeline.start()
    seams.install_ledger(pipeline)
    try:
        for _ in range(cycles):
            pipeline.clock.advance(seconds=40)
            await pipeline.worker.run_once()
    except Crash:
        return True
    return False


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
            "ledger.accept_message",
            "ledger.record_extraction",
            "ledger.plan_message",
            "ledger.transition",
            "ledger.mark_writing",
            "ledger.store_page_id",
            "ledger.mark_written",
            "ledger.set_reaction_state",
            "ledger.mark_done",
            "ledger.outbox_sent",
            "telegram.get_updates",
            "telegram.set_reaction",
            "telegram.send_message",
            "classifier.classify",
            "notion.create_page",
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
        self.assertGreaterEqual(len(points), 40)
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
            # A reaction is never visible before the row is confirmed written.
            if pipeline.telegram.reactions:
                self.assertIn(survivor.get_message(KEY)["state"], ("written", "done"))
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


# -- Jev and reconciliation (#56) -------------------------------------------

PROJECTS = [{"page_id": "proj-mg", "name": "Moonglow", "code": "MG"}]
PEOPLE = [{"page_id": "c-leizette", "name": "Leizette"}]
FTR = Candidate(page_id="d-ftr", name="Moonglow FTR SCR")
PROMO = Candidate(page_id="d-promo", name="Moonglow Promo")
LINKABLE = {"d-ftr": (0.95, 0.04, 0.01), "d-promo": (0.02, 0.08, 0.90)}
SWAPPED = {"d-ftr": (0.01, 0.04, 0.95), "d-promo": (0.95, 0.04, 0.01)}


class ScriptedDeliverables:
    """Candidate source answering a script of candidate lists or errors (the last repeats)."""

    def __init__(self, *script):
        self.script = list(script) or [[FTR, PROMO]]
        self.calls = 0

    async def query_project_deliverables(self, data_source_id, project_page_id):
        self.calls += 1
        step = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(step, Exception):
            raise step
        return list(step)


class ScriptedMatcher:
    """Jev stand-in: probability maps or errors, in order; the last one repeats."""

    def __init__(self, *script):
        self.script = list(script) or [LINKABLE]
        self.calls = 0

    async def match(self, state, project_name, candidates):
        self.calls += 1
        step = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(step, Exception):
            raise step
        return MatchResult(
            decisions=[
                CandidateDecision(page_id=pid, p_match=m, p_maybe=y, p_no=n)
                for pid, (m, y, n) in step.items()
            ],
            requested_model="~typesafe/jev-latest",
            returned_model="typesafe/jev-1.14-20261014",
            input_tokens=200,
            output_tokens=30,
        )


class QuietCaches(CacheManager):
    """Caches loaded from the ledger snapshots only; no Notion refresh to crash into."""

    async def refresh_if_due(self):
        return []


class JevPipeline(Pipeline):
    """The crash pipeline with live-looking caches, a Deliverable source and Jev."""

    def __init__(self, test, *, deliverables, matcher):
        self.deliverables, self.matcher = deliverables, matcher
        super().__init__(
            test,
            telegram=ReplayTelegram([forwarded_jo()]),
            notion=FakeNotion(),
            classifier=FakeClassifier(ok_result(title="Teaser", task="Edit the teaser")),
            deliverable_link_enabled=True,
        )

    def seed_caches(self, projects=PROJECTS) -> None:
        fetched_at = self.clock().strftime("%Y-%m-%dT%H:%M:%SZ")
        for kind, payload in (
            ("people", PEOPLE),
            ("projects", projects),
            ("departments", ["TOC"]),
        ):
            self.ledger.save_cache_snapshot(kind, FORMAT_VERSION, payload, len(payload), fetched_at)

    def make_worker(self, ledger, **overrides):
        caches = QuietCaches(ledger=ledger, notion=None, config=self.config, clock=self.clock)
        if ledger.latest_cache_snapshot("projects") is None:
            self.ledger = ledger
            self.seed_caches()
        caches.load_persisted()
        return super().make_worker(
            ledger,
            caches=caches,
            matcher=self.matcher,
            deliverables=self.deliverables,
            **overrides,
        )

    def stage(self, name: str):
        return self.ledger.stage_result(KEY, name)

    def jev_calls(self) -> list[dict]:
        rows = self.ledger._conn.execute(
            "SELECT outcome FROM llm_calls WHERE provider = 'jev' ORDER BY id"
        )
        return [dict(r) for r in rows]


# Every external-call seam each Jev scenario must hit, so a refactor that stops
# reaching one fails loudly instead of silently shrinking the injected set.
JEV_SEAMS = {
    "deliverables.query_project_deliverables",
    "matcher.match",
    "ledger.plan_message",
    "ledger.mark_writing",
    "notion.create_page",
    "ledger.store_page_id",
    "notion.retrieve_page",
    "ledger.mark_written",
    "telegram.set_reaction",
    "ledger.set_reaction_state",
    "telegram.send_message",
    "ledger.outbox_sent",
    "classifier.classify",
    "telegram.get_updates",
    "ledger.accept_message",
}
RECONCILIATION_SEAMS = {
    "notion.find_pages_by_telegram_key",
    "notion.retrieve_page",
    "ledger.store_page_id",
    "ledger.mark_written",
    "telegram.set_reaction",
    "ledger.mark_done",
}


def scenarios():
    """name -> (deliverables script, matcher script, expected jev llm_calls outcomes count)."""
    return {
        "matcher_ok": ([[FTR, PROMO]], [LINKABLE], 1),
        "matcher_retry": ([[FTR, PROMO]], [MatcherError("timeout", "slow"), LINKABLE], 2),
        "query_retry": ([RuntimeError("notion 503"), [FTR, PROMO]], [LINKABLE], 1),
        "matcher_exhausted": (
            [[FTR, PROMO]],
            [MatcherError("timeout", "a"), MatcherError("timeout", "b")],
            2,
        ),
    }


class JevCrashInjectionTests(unittest.IsolatedAsyncioTestCase):
    def build(self, name: str) -> JevPipeline:
        source_script, matcher_script, _ = scenarios()[name]
        return JevPipeline(
            self,
            deliverables=ScriptedDeliverables(*source_script),
            matcher=ScriptedMatcher(*matcher_script),
        )

    async def clean(self, name: str) -> tuple[JevPipeline, dict[str, int]]:
        pipeline = self.build(name)
        seams = Seams()
        self.assertFalse(await run_until_crash(pipeline, seams))
        self.assertEqual(pipeline.message()["state"], "done")
        return pipeline, dict(seams.counts)

    @staticmethod
    def points(counts: dict[str, int], name: str | None = None):
        """Every before/after seam of a run; a call scripted to raise has no "after"."""
        raising = set()
        if name is not None:
            source_script, matcher_script, _ = scenarios()[name]
            for label, script in (
                ("deliverables.query_project_deliverables", source_script),
                ("matcher.match", matcher_script),
            ):
                raising |= {
                    (label, nth, "after")
                    for nth, step in enumerate(script, 1)
                    if isinstance(step, Exception)
                }
        return [
            (label, nth, phase)
            for label, total in sorted(counts.items())
            for nth in range(1, total + 1)
            for phase in ("before", "after")
            if (label, nth, phase) not in raising
        ]

    def assert_one_correct_capture(self, pipeline: JevPipeline, reference: dict, name: str) -> None:
        row = pipeline.message()
        self.assertEqual((row["state"], row["reaction_state"]), ("done", "set"))
        self.assertEqual(len(pipeline.notion.pages), 1)
        self.assertEqual(len(pipeline.notion.created), 1)
        self.assertEqual(row["plan_json"], reference["plan_json"])
        created = pipeline.notion.created[0]["properties"]
        self.assertEqual(created.get("Deliverable"), reference["deliverable"])
        reports = [t for _, _, t in pipeline.telegram.sent if "Flagged on capture" in t]
        self.assertGreaterEqual(len(reports), 1)
        self.assertEqual([r["state"] for r in pipeline.outbox("capture_report")], ["sent"])
        self.assertEqual(pipeline.outbox("outage_alert"), [])
        self.assertIsNone(row["exhausted_at"])
        # The Jev records commit with the plan, never partially: one per attempt of the
        # stage that committed. A restart may meet a service that has since recovered,
        # so a replayed stage can need fewer attempts than the scripted failures.
        recorded = len(pipeline.jev_calls())
        self.assertGreaterEqual(recorded, 1)
        self.assertLessEqual(recorded, scenarios()[name][2])

    def reference(self, pipeline: JevPipeline) -> dict:
        created = pipeline.notion.created[0]["properties"]
        return {
            "plan_json": pipeline.message()["plan_json"],
            "deliverable": created.get("Deliverable"),
        }

    async def test_each_scenario_reaches_the_jev_and_recovery_seams(self):
        for name in scenarios():
            with self.subTest(scenario=name):
                _, counts = await self.clean(name)
                self.assertLessEqual(JEV_SEAMS, set(counts))
        _, counts = await self.clean("matcher_retry")
        self.assertEqual(counts["matcher.match"], 2)
        _, counts = await self.clean("query_retry")
        self.assertEqual(counts["deliverables.query_project_deliverables"], 2)
        _, counts = await self.clean("matcher_exhausted")
        self.assertEqual(counts["matcher.match"], 2)

    async def test_the_clean_runs_link_or_skip_as_scripted(self):
        pipeline, _ = await self.clean("matcher_ok")
        self.assertEqual(
            pipeline.notion.created[0]["properties"]["Deliverable"], {"relation": [{"id": "d-ftr"}]}
        )
        pipeline, _ = await self.clean("matcher_exhausted")
        self.assertNotIn("Deliverable", pipeline.notion.created[0]["properties"])
        self.assertEqual(pipeline.stage("match")["outcome"], "skipped")
        for name, expected in (("matcher_retry", 2), ("query_retry", 1), ("matcher_exhausted", 2)):
            pipeline, _ = await self.clean(name)
            self.assertEqual(len(pipeline.jev_calls()), expected, name)

    async def test_a_crash_before_or_after_every_call_converges_for_every_scenario(self):
        for name in scenarios():
            clean, counts = await self.clean(name)
            reference = self.reference(clean)
            points = self.points(counts, name)
            self.assertGreaterEqual(len(points), 40)
            for point in points:
                with self.subTest(scenario=name, point=point):
                    await self.assert_converges(name, point, reference)

    async def assert_converges(self, name: str, point, reference: dict) -> None:
        pipeline = self.build(name)
        seams = Seams(crash_at=point)
        self.assertTrue(
            await run_until_crash(pipeline, seams), "the injected crash was never reached"
        )
        survivor = Ledger(pipeline.path)
        try:
            row = survivor.get_message(KEY) or {}
            if survivor.last_committed_offset() >= 7:
                self.assertTrue(survivor.has_message(KEY))
            if pipeline.telegram.reactions:  # reactions only after confirmed capture
                self.assertIn(row["state"], ("written", "done"))
            planned = row.get("plan_json")
        finally:
            survivor.close()
        calls_before = (pipeline.matcher.calls, pipeline.deliverables.calls)
        await recover(pipeline, seams)
        self.assert_one_correct_capture(pipeline, reference, name)
        if planned:
            # A stored plan is replayed: neither Jev nor the Deliverable query repeats.
            self.assertEqual((pipeline.matcher.calls, pipeline.deliverables.calls), calls_before)

    async def test_a_second_crash_at_every_reconciliation_call_still_writes_one_row(self):
        clean, _ = await self.clean("matcher_ok")
        reference = self.reference(clean)
        first_crashes = {
            "create_landed_without_page_id": ("notion.create_page", 1, "after"),
            "create_never_sent": ("notion.create_page", 1, "before"),
            "writing_marked_create_not_started": ("ledger.mark_writing", 1, "after"),
        }
        for label, first in first_crashes.items():
            # The recovery run itself, uninterrupted, says which calls to attack.
            probe = self.build("matcher_ok")
            seams = Seams(crash_at=first)
            self.assertTrue(await run_until_crash(probe, seams))
            self.assertFalse(await recover(probe, seams))
            recovery_counts = dict(seams.counts)
            self.assertIn("notion.find_pages_by_telegram_key", recovery_counts, label)
            self.assertLessEqual(
                RECONCILIATION_SEAMS & {"notion.find_pages_by_telegram_key"}, set(recovery_counts)
            )
            for point in self.points(recovery_counts):
                with self.subTest(first=label, second=point):
                    pipeline = self.build("matcher_ok")
                    seams = Seams(crash_at=first)
                    self.assertTrue(await run_until_crash(pipeline, seams))
                    self.assertTrue(await recover(pipeline, seams, crash_at=point))
                    self.assertFalse(await recover(pipeline, seams))
                    self.assert_one_correct_capture(pipeline, reference, "matcher_ok")
                    self.assertEqual(pipeline.notion.create_calls, 1)

    async def test_the_reconciliation_adopts_the_row_a_landed_create_left_behind(self):
        pipeline = self.build("matcher_ok")
        seams = Seams(crash_at=("notion.create_page", 1, "after"))
        self.assertTrue(await run_until_crash(pipeline, seams))
        self.assertEqual(peek(pipeline)["state"], "writing")
        self.assertIsNone(peek(pipeline)["notion_page_id"])
        self.assertFalse(await recover(pipeline, seams))
        self.assertEqual(pipeline.notion.find_calls, 1)
        self.assertEqual(pipeline.notion.create_calls, 1)
        self.assertEqual(pipeline.message()["notion_page_id"], "page-1")

    async def test_a_failed_reconciliation_query_retries_and_never_creates_a_second_row(self):
        pipeline = self.build("matcher_ok")
        seams = Seams(crash_at=("notion.create_page", 1, "after"))
        self.assertTrue(await run_until_crash(pipeline, seams))
        pipeline.notion.fail_find = 2
        self.assertFalse(await recover(pipeline, seams, cycles=12))
        self.assertEqual(pipeline.notion.find_calls, 3)
        self.assertEqual(pipeline.notion.create_calls, 1)
        self.assertEqual(len(pipeline.notion.pages), 1)
        self.assertEqual(pipeline.message()["state"], "done")

    async def test_the_stored_plan_survives_cache_matcher_and_candidate_changes(self):
        clean, _ = await self.clean("matcher_ok")
        reference = self.reference(clean)
        pipeline = self.build("matcher_ok")
        seams = Seams(crash_at=("ledger.mark_writing", 1, "before"))
        self.assertTrue(await run_until_crash(pipeline, seams))
        stored = peek(pipeline)
        self.assertEqual(
            (stored["state"], stored["plan_json"]), ("planned", reference["plan_json"])
        )
        # Everything upstream now answers differently, including the persisted caches.
        pipeline.matcher.script = [SWAPPED]
        pipeline.deliverables.script = [[PROMO]]
        survivor = Ledger(pipeline.path)
        fetched_at = pipeline.clock().strftime("%Y-%m-%dT%H:%M:%SZ")
        renamed = [{"page_id": "proj-mg", "name": "Renamed", "code": "MG"}]
        survivor.save_cache_snapshot("projects", FORMAT_VERSION, renamed, 1, fetched_at)
        survivor.close()
        calls = (pipeline.matcher.calls, pipeline.deliverables.calls)
        self.assertFalse(await recover(pipeline, seams))
        self.assert_one_correct_capture(pipeline, reference, "matcher_ok")
        self.assertEqual((pipeline.matcher.calls, pipeline.deliverables.calls), calls)
        self.assertEqual(
            pipeline.notion.created[0]["properties"]["Deliverable"], {"relation": [{"id": "d-ftr"}]}
        )
        self.assertEqual(len(pipeline.classifier.calls), 1)

    async def test_a_reaction_never_precedes_the_confirmed_write(self):
        # The write is confirmed by read-back; fail it and the reaction must wait.
        pipeline = self.build("matcher_ok")
        pipeline.notion.fail_retrieve = 1
        await pipeline.worker.run_once()
        self.assertEqual(pipeline.telegram.reactions, [])
        self.assertNotEqual(pipeline.message()["state"], "done")
        for _ in range(8):
            pipeline.clock.advance(seconds=40)
            await pipeline.worker.run_once()
        self.assertEqual(pipeline.message()["state"], "done")
        self.assertEqual(len(pipeline.telegram.reactions), 1)
        self.assertEqual(pipeline.notion.create_calls, 1)


if __name__ == "__main__":
    unittest.main()
