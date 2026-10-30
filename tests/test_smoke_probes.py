"""Telegram origin and reaction smoke probes (#60), offline.

The fixtures are the documented Bot API shapes (``telegram_updates.json``), so these
tests prove the checks and the missing-fixture gating only. They are not live evidence.
"""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import smoke
from jo_watchdog.app.smoke_gate import ATTESTED, FAIL, MANUAL, PASS, SKIP, Attestation, attest
from jo_watchdog.app.smoke_probes import (
    ORIGIN_TYPES,
    evaluate_channel_link,
    evaluate_forward_origins,
    evaluate_owner_handle,
    evaluate_reactions,
    load_forward_fixtures,
)
from tests.test_worker import make_config

UPDATES = json.loads(
    (Path(__file__).parent / "fixtures" / "telegram_updates.json").read_text(encoding="utf-8")
)["updates"]
FULL = {
    "user": UPDATES["forward_user"],
    "hidden_user": UPDATES["forward_hidden_user"],
    "chat": UPDATES["forward_chat"],
    "channel": UPDATES["forward_channel_public"],
}


def emoji(*names):
    return [SimpleNamespace(type="emoji", emoji=n) for n in names]


class ForwardOriginTests(unittest.TestCase):
    def test_all_four_origins_pass_together(self):
        self.assertEqual(evaluate_forward_origins(FULL).status, PASS)

    def test_a_missing_fixture_is_a_gap_not_a_pass(self):
        for kind in ORIGIN_TYPES:
            with self.subTest(kind=kind):
                result = evaluate_forward_origins({k: v for k, v in FULL.items() if k != kind})
                self.assertEqual(result.status, SKIP)
                self.assertIn(kind, result.detail)

    def test_no_fixtures_at_all_is_a_gap(self):
        self.assertEqual(evaluate_forward_origins(None).status, SKIP)
        self.assertEqual(evaluate_forward_origins({"user": None}).status, SKIP)

    def test_a_fixture_of_the_wrong_origin_fails(self):
        self.assertEqual(evaluate_forward_origins({**FULL, "chat": FULL["user"]}).status, FAIL)

    def test_a_failure_outranks_a_gap(self):
        result = evaluate_forward_origins({"chat": FULL["user"]})
        self.assertEqual(result.status, FAIL)

    def test_a_channel_forward_without_ids_is_not_a_real_fixture(self):
        bad = {**FULL, "channel": UPDATES["forward_channel_missing_ids"]}
        self.assertEqual(evaluate_forward_origins(bad).status, FAIL)

    def test_a_bare_message_is_accepted_as_well_as_an_update(self):
        self.assertIn("update_id", FULL["channel"])
        bare = FULL["channel"]["message"]
        self.assertEqual(evaluate_forward_origins({**FULL, "channel": bare}).status, PASS)


class ChannelLinkTests(unittest.TestCase):
    def test_a_public_channel_links_by_username(self):
        result = evaluate_channel_link(FULL)
        self.assertEqual(result.status, PASS)
        self.assertIn("https://t.me/brandupdates/42", result.detail)

    def test_a_private_channel_links_by_internal_id(self):
        result = evaluate_channel_link({"channel": UPDATES["forward_channel_private"]})
        self.assertIn("https://t.me/c/5555555555/43", result.detail)

    def test_no_channel_fixture_is_a_gap(self):
        self.assertEqual(evaluate_channel_link({"user": FULL["user"]}).status, SKIP)

    def test_the_link_is_checked_against_the_raw_ids(self):
        # The audited gap: a placeholder passed. A wrong link must now fail.
        with mock.patch(
            "jo_watchdog.app.smoke_probes.forward_origin_from_raw",
            return_value=SimpleNamespace(type="channel", original_link="https://t.me/x/1"),
        ):
            self.assertEqual(evaluate_channel_link(FULL).status, FAIL)

    def test_an_unsupported_origin_that_grows_a_link_fails(self):
        with mock.patch(
            "jo_watchdog.app.smoke_probes.forward_origin_from_raw",
            return_value=SimpleNamespace(type="user", original_link="https://t.me/x/1"),
        ):
            self.assertEqual(evaluate_forward_origins(FULL).status, FAIL)


class FixtureFileTests(unittest.TestCase):
    def _file(self, data):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "fw.json"
        path.write_text(json.dumps(data))
        return path

    def test_loads_an_object_keyed_by_origin(self):
        self.assertEqual(load_forward_fixtures(self._file(FULL)), FULL)

    def test_rejects_unknown_keys_and_non_objects(self):
        with self.assertRaises(ValueError):
            load_forward_fixtures(self._file({"story": {}}))
        with self.assertRaises(ValueError):
            load_forward_fixtures(self._file([]))


class ReactionTests(unittest.TestCase):
    def test_a_configured_reaction_in_the_list_passes(self):
        result, shown = evaluate_reactions(emoji("👍", "🔥"), "🔥")
        self.assertEqual(result.status, PASS)
        self.assertEqual(shown, "👍 🔥")

    def test_an_unavailable_configured_reaction_is_rejected(self):
        result, _ = evaluate_reactions(emoji("👍"), "🔥")
        self.assertEqual(result.status, FAIL)

    def test_an_empty_list_means_reactions_are_off(self):
        result, shown = evaluate_reactions([], "👍")
        self.assertEqual(result.status, FAIL)
        self.assertEqual(shown, "(none)")

    def test_a_custom_emoji_does_not_satisfy_a_standard_one(self):
        custom = [SimpleNamespace(type="custom_emoji", custom_emoji_id="1")]
        self.assertEqual(evaluate_reactions(custom, "👍")[0].status, FAIL)

    def test_an_absent_list_allows_standard_emoji_only(self):
        self.assertEqual(evaluate_reactions(None, "👍")[0].status, PASS)
        self.assertEqual(evaluate_reactions(None, "❤️")[0].status, PASS)  # variation selector
        result, shown = evaluate_reactions(None, "🫠")
        self.assertEqual(result.status, FAIL)
        self.assertIn("absent", shown)


class OwnerHandleTests(unittest.TestCase):
    ATT = Attestation(
        "owner_handle", "Cyril", "2026-10-25T10:00:00+08:00",
        "@cyril_owner and leizette_h are the real handles", "screenshot of both profiles",
    )

    def test_unattested_is_manual_and_names_the_handles(self):
        result = evaluate_owner_handle("@cyril_owner", "@leizette_h", None)
        self.assertEqual(result.status, MANUAL)
        self.assertIn("@cyril_owner", result.detail)

    def test_an_attestation_naming_both_handles_settles_it(self):
        result = evaluate_owner_handle("@cyril_owner", "@leizette_h", self.ATT)
        self.assertEqual(attest(result, self.ATT).status, ATTESTED)

    def test_an_attestation_missing_a_handle_fails(self):
        result = evaluate_owner_handle("@cyril_owner", "@someone_else", self.ATT)
        self.assertEqual(result.status, FAIL)
        self.assertEqual(attest(result, self.ATT).status, FAIL)


class SmokeStepTests(unittest.IsolatedAsyncioTestCase):
    def _smoke(self, **kw):
        args = SimpleNamespace(
            fixture_message_id=1, forward_fixtures=None, reaction_message_id=None, reaction="captured", **kw
        )
        return smoke.Smoke(make_config(), args)

    async def test_origin_steps_gate_on_a_fixture_file(self):
        s = self._smoke()
        self.assertEqual((await smoke.step_forward_origins(s)).status, SKIP)
        self.assertEqual((await smoke.step_channel_link(s)).status, SKIP)

    async def test_origin_steps_read_the_supplied_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "fw.json"
            path.write_text(json.dumps(FULL))
            s = self._smoke()
            s.args.forward_fixtures = str(path)
            self.assertEqual((await smoke.step_forward_origins(s)).status, PASS)
            self.assertEqual((await smoke.step_channel_link(s)).status, PASS)

    async def test_reactions_step_uses_get_chat_list(self):
        s = self._smoke()
        s.bot = mock.AsyncMock()
        s.bot.get_chat.return_value = SimpleNamespace(available_reactions=emoji("👍"))
        s.config = make_config(captured_reaction="🔥")
        self.assertEqual((await smoke.step_reactions(s)).status, FAIL)

    async def test_reactions_step_checks_the_flagged_reaction_too(self):
        s = self._smoke()
        s.bot = mock.AsyncMock()
        s.config = make_config(captured_reaction="👍")
        s.bot.get_chat.return_value = SimpleNamespace(available_reactions=emoji("👍"))
        result = await smoke.step_reactions(s)  # default flagged_reaction 🤔 is missing
        self.assertEqual(result.status, FAIL)
        self.assertIn("flagged_reaction", result.detail)
        s.bot.get_chat.return_value = SimpleNamespace(available_reactions=emoji("👍", "🤔"))
        self.assertEqual((await smoke.step_reactions(s)).status, PASS)

    async def test_reaction_apply_needs_a_message_id_and_applies_via_production_client(self):
        s = self._smoke()
        self.assertEqual((await smoke.step_reaction_apply(s)).status, SKIP)
        s.args.reaction_message_id = 77
        telegram = mock.AsyncMock()
        built = mock.Mock(telegram=telegram, close=mock.AsyncMock())
        with (
            mock.patch.object(smoke, "missing_secrets", return_value=[]),
            mock.patch.object(smoke, "build_adapters", return_value=built),
        ):
            result = await smoke.step_reaction_apply(s)
        self.assertEqual(result.status, PASS)
        telegram.set_reaction.assert_awaited_once_with(
            s.config.intake_chat_id, 77, s.config.captured_reaction
        )

    async def test_a_rejected_reaction_fails_the_apply_step(self):
        s = self._smoke()
        s.args.reaction_message_id = 77
        telegram = mock.AsyncMock()
        telegram.set_reaction.side_effect = RuntimeError("REACTION_INVALID")
        built = mock.Mock(telegram=telegram, close=mock.AsyncMock())
        with (
            mock.patch.object(smoke, "missing_secrets", return_value=[]),
            mock.patch.object(smoke, "build_adapters", return_value=built),
        ):
            self.assertEqual((await smoke.step_reaction_apply(s)).status, FAIL)

    def test_no_origin_or_reaction_step_is_still_a_placeholder(self):
        steps = dict(smoke.STEPS)
        self.assertIs(steps["forward_origins"], smoke.step_forward_origins)
        self.assertIs(steps["channel_link"], smoke.step_channel_link)
        self.assertIn("reaction_apply", steps)


if __name__ == "__main__":
    unittest.main()
