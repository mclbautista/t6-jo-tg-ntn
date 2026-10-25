"""Operational failure and shadow-mode smoke scenarios (#62).

Offline only: production worker wiring, real SQLite, scripted adapters, fake clock.
The live run stays manual and explicitly authorized.
"""

import unittest
from unittest import mock

import smoke
from jo_watchdog.app import smoke_scenarios as scenarios
from jo_watchdog.app.smoke_gate import FAIL, PASS, SKIP, Result, verdict
from jo_watchdog.ledger.db import Ledger
from tests.test_worker import make_config


class ScenarioTests(unittest.IsolatedAsyncioTestCase):
    async def passes(self, name):
        run = await scenarios.SCENARIOS[name](make_config())
        self.assertEqual(run.result.status, PASS, run.result.detail)
        return run

    async def test_readback_mismatch_is_reported_without_blocking_the_capture(self):
        await self.passes("readback_mismatch")

    async def test_outbox_retry_honours_retry_after_and_five_total_attempts(self):
        await self.passes("outbox_retry")

    async def test_heartbeat_counts_a_capture_by_when_it_was_written(self):
        run = await self.passes("heartbeat_format")
        self.assertIn("Daily heartbeat", run.result.detail)

    async def test_matcher_failure_does_not_prevent_capture(self):
        await self.passes("matcher_failure")

    async def test_shadow_mode_keeps_the_decision_and_writes_no_relation(self):
        run = await self.passes("shadow_mode")
        self.assertIn("control", run.result.detail)

    async def test_dry_run_performs_no_notion_write_or_reaction(self):
        await self.passes("dry_run_scenario")

    async def test_scenarios_use_the_deployed_retry_schedule(self):
        config = make_config(retry_schedule_seconds=[10, 20])
        run = await scenarios.outbox_retry(config)
        self.assertEqual(run.result.status, PASS, run.result.detail)
        self.assertIn("3 total attempts", run.result.detail)


class RegressionTests(unittest.IsolatedAsyncioTestCase):
    """Each scenario fails when the audited behavior comes back."""

    async def test_dropping_retry_after_fails_the_outbox_scenario(self):
        real = Ledger.outbox_failed_attempt

        def ignoring_retry_after(self, *args, **kwargs):
            kwargs["retry_after"] = None
            return real(self, *args, **kwargs)

        with mock.patch.object(Ledger, "outbox_failed_attempt", ignoring_retry_after):
            run = await scenarios.outbox_retry(make_config())
        self.assertEqual(run.result.status, FAIL)
        self.assertIn("Retry-After", run.result.detail)

    async def test_counting_by_updated_at_fails_the_heartbeat_scenario(self):
        real = Ledger.heartbeat_stats

        def by_updated_at(self, start, end):
            self._conn.execute("UPDATE messages SET written_at = NULL")
            return real(self, start, end)

        with mock.patch.object(Ledger, "heartbeat_stats", by_updated_at):
            run = await scenarios.heartbeat_format(make_config())
        self.assertEqual(run.result.status, FAIL)
        self.assertIn("missing from its window", run.result.detail)

    async def test_a_matcher_failure_that_blocks_capture_fails_its_scenario(self):
        async def blocking(*args, **kwargs):
            raise RuntimeError("matcher took the pipeline down")

        with mock.patch("jo_watchdog.app.worker.run_match_stage", blocking):
            run = await scenarios.matcher_failure(make_config())
        self.assertEqual(run.result.status, FAIL)
        self.assertIn("took the pipeline down", run.result.detail)

    async def test_a_shadow_mode_that_links_fails_its_scenario(self):
        # Linking forced on in the worker's config: shadow mode is no longer shadow.
        original = scenarios.Harness.__init__

        def init(self, config, tmp, **kw):
            kw["deliverable_link_enabled"] = True
            original(self, config, tmp, **kw)

        with mock.patch.object(scenarios.Harness, "__init__", init):
            run = await scenarios.shadow_mode(make_config())
        self.assertEqual(run.result.status, FAIL)
        self.assertIn("Deliverable", run.result.detail)

    async def test_a_dry_run_that_writes_fails_its_scenario(self):
        original = scenarios.Harness.__init__

        def init(self, config, tmp, **kw):
            kw["dry_run"] = False
            original(self, config, tmp, **kw)

        with mock.patch.object(scenarios.Harness, "__init__", init):
            run = await scenarios.dry_run(make_config())
        self.assertEqual(run.result.status, FAIL)


class SmokeIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def test_the_scenario_steps_are_registered(self):
        names = [name for name, _ in smoke.STEPS]
        for name in scenarios.SCENARIOS:
            self.assertIn(name, names)

    async def test_each_step_passes_offline_without_any_secret(self):
        s = smoke.Smoke(make_config(), mock.Mock(fixture_message_id=1, live_telegram=False))
        s.bot = None
        with mock.patch.dict("os.environ", {}, clear=True):
            for name in scenarios.SCENARIOS:
                result = await dict(smoke.STEPS)[name](s)
                self.assertEqual(result.status, PASS, f"{name}: {result.detail}")

    async def test_live_telegram_relays_the_rendered_ops_posts_to_the_ops_topic(self):
        sent = []

        class Bot:
            async def send_message(self, chat_id, text, **kwargs):
                sent.append((chat_id, text, kwargs))

        s = smoke.Smoke(make_config(), mock.Mock(fixture_message_id=1, live_telegram=True))
        s.bot = Bot()
        result = await smoke.step_shadow_mode(s)
        self.assertEqual(result.status, PASS, result.detail)
        self.assertTrue(sent)
        chat_id, text, kwargs = sent[0]
        self.assertEqual(chat_id, s.config.ops_chat_id)
        self.assertEqual(kwargs["message_thread_id"], s.config.ops_topic_id)
        self.assertTrue(text.startswith("🧪 Smoke scenario shadow_mode"))

    def test_a_skipped_scenario_step_cannot_yield_full_verification(self):
        names = [n for n, _ in smoke.STEPS]
        results = {n: Result(PASS) for n in names}
        results["shadow_mode"] = Result(SKIP, "pending")
        self.assertEqual(verdict(results, names, selected_only=False).exit_code, 3)


if __name__ == "__main__":
    unittest.main()
