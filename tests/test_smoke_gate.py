"""Smoke verification (#58): production wiring, per-message proof, and honest results.

Offline only: real SQLite, fake Telegram/Notion/Jev. The live run stays manual.
"""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import smoke
from jo_watchdog.app import __main__ as entry
from jo_watchdog.app.smoke_gate import (
    ATTESTED,
    FAIL,
    MANUAL,
    PASS,
    SKIP,
    Result,
    attest,
    load_attestations,
    verdict,
    verify_capture,
)
from jo_watchdog.app.wiring import Adapters, build_worker, missing_wiring
from jo_watchdog.ledger.db import Ledger
from tests.fakes import FakeClassifier, FakeNotion, FakeTelegram, update
from tests.test_match_stage import CANDS, LINKABLE, FakeDeliverables, FakeMatcher
from tests.test_worker import make_config, make_worker, no_sleep

KEY = "-100123:42"



NOTION_FIXTURES = Path(__file__).parent / "fixtures" / "notion"


def _fixture(name):
    return json.loads((NOTION_FIXTURES / name).read_text(encoding="utf-8"))


class CachedNotion(FakeNotion):
    """FakeNotion that also serves the recorded cache refreshes the production worker makes."""

    async def query_all(self, data_source_id):
        if data_source_id == make_config().projects_data_source_id:
            return _fixture("projects_query.json")["results"]
        return _fixture("contacts_query_page1.json")["results"]

    async def retrieve_data_source(self, data_source_id):
        return _fixture("job_orders_data_source.json")


def adapters(telegram=None, notion=None, matcher=None) -> Adapters:
    return Adapters(
        telegram=telegram or FakeTelegram(),
        notion=notion or FakeNotion(),
        classifier=FakeClassifier(),
        matcher=matcher,
    )


class WiringTests(unittest.IsolatedAsyncioTestCase):
    def _ledger(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        ledger = Ledger(Path(tmp.name) / "ledger.db")
        self.addCleanup(ledger.close)
        return ledger

    def test_build_worker_wires_caches_matcher_and_deliverable_source(self):
        notion = FakeNotion()
        worker = build_worker(
            make_config(), self._ledger(), adapters(notion=notion, matcher=FakeMatcher(LINKABLE))
        )
        self.assertEqual(missing_wiring(worker), [])
        self.assertIs(worker.deliverables, notion)
        self.assertIs(worker.caches.notion, notion)

    def test_a_worker_without_the_matcher_is_reported_as_unwired(self):
        worker = build_worker(make_config(), self._ledger(), adapters(matcher=None))
        self.assertEqual(missing_wiring(worker), ["matcher"])

    async def test_the_service_entry_point_builds_its_worker_the_same_way(self):
        built = adapters(matcher=FakeMatcher())
        ran = []

        class Stub:
            async def run(self):
                ran.append(True)

        async def close():
            ran.append("closed")

        built.closers.append(close)
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(entry, "load_config", return_value=make_config(
                ledger_path=str(Path(tmp) / "ledger.db"))),
            mock.patch.object(entry, "require_live_keys"),
            mock.patch.object(entry, "missing_secrets", return_value=[]),
            mock.patch.object(entry, "configure_logging"),
            mock.patch.object(entry, "build_adapters", return_value=built) as build_a,
            mock.patch.object(entry, "build_worker", return_value=Stub()) as build_w,
        ):
            await entry.run("config.yaml")
        build_a.assert_called_once()
        self.assertIs(build_w.call_args.args[2], built)
        self.assertEqual(ran, [True, "closed"])


class SyntheticCaptureTests(unittest.IsolatedAsyncioTestCase):
    """``smoke.synthetic_capture`` drives the shared wiring on an isolated ledger."""

    def _args(self, **kw):
        return SimpleNamespace(
            fixture_message_id=42, live_telegram=False, live_message_id=None, **kw
        )

    async def _capture(self, *, dry_run, notion=None, matcher=None):
        s = smoke.Smoke(make_config(), self._args())
        s.bot = None
        built = adapters(
            notion=notion or CachedNotion(), matcher=matcher or FakeMatcher(LINKABLE)
        )
        built.notion.query_project_deliverables = FakeDeliverables(CANDS).query_project_deliverables
        with (
            mock.patch.object(smoke, "missing_secrets", return_value=[]),
            mock.patch.object(smoke, "build_adapters", return_value=built),
        ):
            return await smoke.synthetic_capture(s, "smoke text", dry_run=dry_run)

    async def test_capture_is_proved_from_the_named_messages_own_facts(self):
        result, telegram = await self._capture(dry_run=False)
        self.assertEqual(result.status, PASS, result.detail)
        self.assertIn(KEY, result.detail)
        self.assertEqual(telegram.reactions[0][:2], (-100123, 42))

    async def test_dry_run_holds_at_planned_with_one_delivered_plan(self):
        notion = CachedNotion()
        result, telegram = await self._capture(dry_run=True, notion=notion)
        self.assertEqual(result.status, PASS, result.detail)
        self.assertEqual(notion.created, [])
        self.assertEqual(telegram.reactions, [])

    async def test_missing_secrets_skip_rather_than_pass(self):
        s = smoke.Smoke(make_config(), self._args())
        with mock.patch.object(smoke, "missing_secrets", return_value=["NOTION_API_KEY"]):
            result, _ = await smoke.synthetic_capture(s, "x", dry_run=False)
        self.assertEqual(result.status, SKIP)

    async def test_an_unwired_worker_fails_the_smoke(self):
        s = smoke.Smoke(make_config(), self._args())
        built = adapters()  # no matcher
        with (
            mock.patch.object(smoke, "missing_secrets", return_value=[]),
            mock.patch.object(smoke, "build_adapters", return_value=built),
        ):
            result, _ = await smoke.synthetic_capture(s, "x", dry_run=False)
        self.assertEqual(result.status, FAIL)
        self.assertIn("matcher", result.detail)


class VerifyCaptureTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, updates, **telegram_kw):
        worker, ledger, telegram, notion, cleanup = make_worker(updates)
        self.addCleanup(cleanup)
        worker.telegram.fail_reaction = telegram_kw.get("fail_reaction", False)
        worker.telegram.fail_send = telegram_kw.get("fail_send", 0)
        worker.sleep = no_sleep
        await worker.run_once()
        return worker, ledger, telegram, notion

    async def test_a_failed_reaction_is_not_a_pass(self):
        # The audited behavior: capture "passed" though the reaction never landed.
        _, ledger, _, notion = await self._run([update(7, message_id=42)], fail_reaction=True)
        self.assertEqual(ledger.get_message(KEY)["state"], "done")
        result = await verify_capture(ledger, KEY, notion=notion)
        self.assertEqual(result.status, FAIL)
        self.assertIn("reaction_state", result.detail)

    async def test_a_stuck_write_reports_the_masked_provider_error(self):
        _, ledger, _, notion = await self._run([update(7, message_id=42)])
        ledger._conn.execute(
            "UPDATE messages SET state = 'writing', last_error = ? WHERE telegram_key = ?",
            ("notion_write: NotionError: Notion 400 validation_error: time zone offsets", KEY),
        )
        result = await verify_capture(ledger, KEY, notion=notion)
        self.assertEqual(result.status, FAIL)
        self.assertIn("stopped at writing", result.detail)
        self.assertIn("400 validation_error", result.detail)

    async def test_an_undelivered_outbox_is_not_a_pass(self):
        _, ledger, _, notion = await self._run([update(7, message_id=42)], fail_send=99)
        result = await verify_capture(ledger, KEY, notion=notion)
        self.assertEqual(result.status, FAIL)
        self.assertIn("not delivered", result.detail)

    async def test_the_named_message_is_checked_not_the_last_row(self):
        worker, ledger, telegram, notion = await self._run(
            [update(7, message_id=42), update(8, message_id=43)]
        )
        self.assertEqual((await verify_capture(ledger, KEY, notion=notion)).status, PASS)
        ledger.set_reaction_state(KEY, "failed")  # only the first message is damaged
        self.assertEqual(ledger.get_message("-100123:43")["reaction_state"], "set")
        self.assertEqual((await verify_capture(ledger, KEY, notion=notion)).status, FAIL)
        self.assertEqual((await verify_capture(ledger, "-100123:99", notion=notion)).status, FAIL)

    async def test_a_page_with_another_telegram_key_fails(self):
        _, ledger, _, notion = await self._run([update(7, message_id=42)])
        page = notion.pages["page-1"]
        page["properties"]["Telegram Key"]["rich_text"] = [{"plain_text": "-100123:999"}]
        self.assertEqual((await verify_capture(ledger, KEY, notion=notion)).status, FAIL)

    async def test_a_missing_recorded_reaction_fails(self):
        _, ledger, _, notion = await self._run([update(7, message_id=42)])
        result = await verify_capture(ledger, KEY, notion=notion, recorded_reactions=[])
        self.assertEqual(result.status, FAIL)


class GateTests(unittest.TestCase):
    NAMES = ["a", "b", "live_telegram", "unknown_select"]

    def test_all_required_checks_skipped_is_not_a_success(self):
        results = {n: Result(SKIP, "x") for n in self.NAMES}
        final = verdict(results, self.NAMES, selected_only=False)
        self.assertEqual(final.label, "INCOMPLETE")
        self.assertNotEqual(final.exit_code, 0)

    def test_one_skipped_required_check_blocks_full_verification(self):
        results = {"a": Result(PASS), "b": Result(SKIP, "no key")}
        final = verdict(results, self.NAMES, selected_only=False)
        self.assertEqual((final.label, final.exit_code), ("INCOMPLETE", 3))
        self.assertIn("b (SKIP)", final.reasons[0])

    def test_a_check_that_never_ran_blocks_full_verification(self):
        final = verdict({"a": Result(PASS)}, self.NAMES, selected_only=False)
        self.assertIn("b (not run)", final.reasons[0])

    def test_optional_probes_may_be_skipped(self):
        results = {"a": Result(PASS), "b": Result(PASS), "live_telegram": Result(SKIP)}
        self.assertEqual(verdict(results, self.NAMES, selected_only=False).label, "FULL")

    def test_a_failure_wins_and_exits_nonzero(self):
        results = {"a": Result(PASS), "b": Result(FAIL, "reaction")}
        final = verdict(results, self.NAMES, selected_only=False)
        self.assertEqual((final.label, final.exit_code), ("FAILED", 1))

    def test_a_subset_run_is_never_full(self):
        final = verdict({"a": Result(PASS)}, self.NAMES, selected_only=True)
        self.assertEqual(final.label, "PARTIAL")
        self.assertNotEqual(final.exit_code, 0)

    def test_a_manual_result_needs_an_attestation(self):
        results = {"a": Result(PASS), "b": Result(MANUAL, "review")}
        self.assertEqual(verdict(results, self.NAMES, selected_only=False).label, "INCOMPLETE")


class AttestationTests(unittest.TestCase):
    ENTRY = {
        "step": "outbox_retry",
        "actor": "Cyril",
        "at": "2026-10-25T10:00:00+08:00",
        "scope": "ops topic, one failed post retried",
        "evidence": "screenshot ops-2026-10-25.png",
    }

    def _file(self, entries):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "att.json"
        path.write_text(json.dumps(entries))
        return path

    def test_an_attestation_settles_a_skip_and_keeps_who_when_what_and_proof(self):
        loaded = load_attestations(self._file([self.ENTRY]), {"outbox_retry"})
        result = attest(Result(SKIP, "not automated"), loaded["outbox_retry"])
        self.assertEqual(result.status, ATTESTED)
        for part in ("Cyril", "2026-10-25T10:00:00+08:00", "one failed post", "ops-2026"):
            self.assertIn(part, result.detail)

    def test_attestation_cannot_override_a_failure_or_a_pass(self):
        att = load_attestations(self._file([self.ENTRY]), {"outbox_retry"})["outbox_retry"]
        self.assertEqual(attest(Result(FAIL, "x"), att).status, FAIL)
        self.assertEqual(attest(Result(PASS, "x"), att).status, PASS)

    def test_incomplete_or_unknown_attestations_are_rejected(self):
        for field in ("actor", "at", "scope", "evidence"):
            with self.subTest(field=field):
                entry = {**self.ENTRY, field: " "}
                with self.assertRaises(ValueError):
                    load_attestations(self._file([entry]), {"outbox_retry"})
        with self.assertRaises(ValueError):
            load_attestations(self._file([{**self.ENTRY, "at": "yesterday"}]), {"outbox_retry"})
        with self.assertRaises(ValueError):
            load_attestations(self._file([self.ENTRY]), {"other_step"})


class RunTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, steps, **args):
        namespace = SimpleNamespace(
            config="c.yaml", only=None, attestations=None, fixture_message_id=1, **args
        )
        with (
            mock.patch.object(smoke, "STEPS", steps),
            mock.patch.object(smoke, "load_config", return_value=make_config()),
            mock.patch.object(smoke.Smoke, "open", new=mock.AsyncMock()),
            mock.patch.object(smoke.Smoke, "close", new=mock.AsyncMock()),
        ):
            return await smoke.run(namespace)

    async def test_a_run_where_every_required_step_skips_exits_nonzero(self):
        async def skipped(_):
            return smoke.skip("no keys")

        code = await self._run([("a", skipped), ("b", skipped)])
        self.assertEqual(code, 3)

    async def test_a_run_where_every_required_step_passes_exits_zero(self):
        async def passed(_):
            return smoke.ok()

        self.assertEqual(await self._run([("a", passed), ("b", passed)]), 0)


if __name__ == "__main__":
    unittest.main()


class StepRegistryTests(unittest.TestCase):
    def test_no_step_is_a_pending_placeholder(self):
        """A merge once restored placeholders that SKIPped the #62 scenarios (blocking FULL)."""
        for name, step in smoke.STEPS:
            self.assertNotEqual(step.__qualname__.split(".")[0], "pending", name)
