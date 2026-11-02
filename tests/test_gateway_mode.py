"""Gateway mode end to end: config, secrets, the worker over a fake gateway, reset-offset."""

import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import yaml
from pydantic import ValidationError

from jo_watchdog.adapters.gateway import GatewayTelegramClient
from jo_watchdog.app import __main__ as entry
from jo_watchdog.app.config import (
    Config,
    gateway_secret,
    load_config,
    missing_secrets,
)
from jo_watchdog.app.worker import Worker
from jo_watchdog.ledger.db import Ledger, OutboxMessage
from tests.fake_gateway import SECRET, FakeGateway
from tests.fakes import FakeClassifier, FakeNotion
from tests.test_worker import make_config, no_sleep

URL = "http://gateway.test:8780"
ALL = {
    "TELEGRAM_BOT_TOKEN": "t",
    "NOTION_API_KEY": "n",
    "DEEPSEEK_API_KEY": "d",
    "OPENROUTER_API_KEY": "o",
    "WATCHDOG_SECRET": SECRET,
}


def gateway_config(**overrides) -> Config:
    return make_config(**{"telegram_mode": "gateway", "gateway_url": URL, **overrides})


class ConfigTests(unittest.TestCase):
    def test_direct_is_the_default(self):
        config = make_config()
        self.assertEqual(config.telegram_mode, "direct")
        self.assertIsNone(config.gateway_url)

    def test_gateway_mode_needs_a_gateway_url(self):
        with self.assertRaisesRegex(ValidationError, "gateway_url"):
            make_config(telegram_mode="gateway")

    def test_gateway_url_must_be_http(self):
        with self.assertRaisesRegex(ValidationError, "gateway_url"):
            gateway_config(gateway_url="gateway.test:8780")

    def test_unknown_mode_is_rejected(self):
        with self.assertRaises(ValidationError):
            make_config(telegram_mode="webhook")

    def test_example_config_loads_in_gateway_mode(self):
        root = Path(__file__).resolve().parents[1]
        data = yaml.safe_load((root / "config.example.yaml").read_text(encoding="utf-8"))
        data.update(telegram_mode="gateway", gateway_url=URL)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
            self.assertEqual(load_config(path).telegram_mode, "gateway")


class SecretTests(unittest.TestCase):
    def test_gateway_mode_does_not_need_the_telegram_token(self):
        env = {k: v for k, v in ALL.items() if k != "TELEGRAM_BOT_TOKEN"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(missing_secrets("gateway"), [])

    def test_gateway_mode_needs_the_module_secret(self):
        env = {k: v for k, v in ALL.items() if k != "WATCHDOG_SECRET"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(missing_secrets("gateway"), ["WATCHDOG_SECRET"])

    def test_direct_mode_is_unchanged(self):
        env = {k: v for k, v in ALL.items() if k != "WATCHDOG_SECRET"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(missing_secrets(), [])
            self.assertEqual(missing_secrets("direct"), [])
        with mock.patch.dict(os.environ, {"WATCHDOG_SECRET": SECRET}, clear=True):
            self.assertIn("TELEGRAM_BOT_TOKEN", missing_secrets("direct"))

    def test_gateway_secret_reads_the_environment(self):
        with mock.patch.dict(os.environ, ALL, clear=True):
            self.assertEqual(gateway_secret(), SECRET)


class Rig:
    """A real worker and ledger over a fake gateway, like ``make_worker`` but gatewayed."""

    def __init__(self, test: unittest.IsolatedAsyncioTestCase):
        tmp = tempfile.TemporaryDirectory()
        test.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "ledger.db"
        self.gateway = FakeGateway()
        self.client = GatewayTelegramClient(URL, SECRET, transport=self.gateway.transport())
        self.notion = FakeNotion()
        self.ledger = Ledger(self.path)
        test.addCleanup(self.ledger.close)
        self.worker = Worker(
            ledger=self.ledger,
            telegram=self.client,
            notion=self.notion,
            classifier=FakeClassifier(),
            config=gateway_config(),
            sleep=no_sleep,
        )


class WorkerOverGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def test_intake_message_is_captured_and_reacted_through_the_gateway(self):
        rig = Rig(self)
        rig.gateway.post(-100123, 5, 42, "first line")
        await rig.worker.run_once()
        self.assertTrue(rig.ledger.has_message("-100123:42"))
        self.assertEqual(rig.ledger.last_committed_offset(), 1)  # the gateway seq
        self.assertEqual(rig.gateway.reactions[0]["message_id"], 42)

    async def test_a_command_item_does_not_stall_the_cursor(self):
        rig = Rig(self)
        rig.gateway.post(111, None, 1, "/jo")  # a private-chat command, not intake
        rig.gateway.items[0]["kind"] = "command"
        rig.gateway.post(-100123, 5, 42, "first line")
        await rig.worker.poll_once()
        self.assertEqual(rig.ledger.last_committed_offset(), 2)
        self.assertTrue(rig.ledger.has_message("-100123:42"))

    async def test_out_of_scope_items_are_still_filtered(self):
        rig = Rig(self)
        rig.gateway.post(-100777, 5, 1, "elsewhere")
        await rig.worker.poll_once()
        self.assertFalse(rig.ledger.has_message("-100777:1"))

    async def test_ops_post_goes_through_notify_and_stores_the_message_ref(self):
        rig = Rig(self)
        outbox_id = rig.ledger.enqueue_system(OutboxMessage("stale_cache", -100999, 7, "stale"))
        await rig.worker.deliver_outbox()
        (posted,) = rig.gateway.notifications
        self.assertEqual(posted["idempotency_key"], f"watchdog-outbox-{outbox_id}")
        self.assertEqual((posted["to"], posted["topic"]), (-100999, 7))
        (row,) = [r for r in rig.ledger._conn.execute("SELECT * FROM outbox")]
        self.assertEqual(row["state"], "sent")
        self.assertEqual(row["telegram_message_id"], 5530)

    async def test_a_retried_ops_post_reuses_its_key_so_the_gateway_queues_once(self):
        rig = Rig(self)
        rig.ledger.enqueue_system(OutboxMessage("stale_cache", -100999, 7, "stale"))
        # The gateway accepted the post but the answer was lost: the row stays pending.
        real = rig.ledger.outbox_sent
        rig.ledger.outbox_sent = lambda *a, **k: None
        await rig.worker.deliver_outbox()
        rig.ledger.outbox_sent = real
        await rig.worker.deliver_outbox()
        self.assertEqual(len(rig.gateway.notifications), 1)
        self.assertEqual(len(rig.gateway.requests), 2)

    async def test_a_gateway_outage_retries_the_ops_post_later(self):
        rig = Rig(self)
        rig.ledger.enqueue_system(OutboxMessage("stale_cache", -100999, 7, "stale"))
        rig.gateway.fail_next = 1
        await rig.worker.deliver_outbox()
        self.assertEqual(rig.gateway.notifications, [])
        (row,) = [r for r in rig.ledger._conn.execute("SELECT * FROM outbox")]
        self.assertEqual(row["state"], "pending")
        self.assertEqual(row["attempts"], 1)


class ResetOffsetTests(unittest.IsolatedAsyncioTestCase):
    async def test_reread_after_reset_captures_nothing_twice(self):
        rig = Rig(self)
        rig.gateway.post(-100123, 5, 42, "first line")
        rig.gateway.post(-100123, 5, 42, "first line (edit)", edited=True, edit_date=1790565000)
        rig.gateway.post(-100123, 5, 43, "second line")
        await rig.worker.run_once()
        created = len(rig.notion.created)
        messages = rig.ledger._conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        edits = rig.ledger._conn.execute("SELECT COUNT(*) FROM edits").fetchone()[0]
        self.assertEqual(rig.ledger.last_committed_offset(), 3)

        rig.ledger.reset_offset(0)
        self.assertEqual(rig.ledger.next_offset(), 1)
        await rig.worker.run_once()

        self.assertEqual(
            rig.ledger._conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0], messages
        )
        self.assertEqual(
            rig.ledger._conn.execute("SELECT COUNT(*) FROM edits").fetchone()[0], edits
        )
        self.assertEqual(rig.ledger.last_committed_offset(), 3)
        self.assertEqual(len(rig.notion.created), created)

    def test_reset_offset_can_move_backward_unlike_advance(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Ledger(Path(tmp) / "ledger.db")
            try:
                ledger.advance_offset(912345678)
                ledger.advance_offset(5)  # monotonic: ignored
                self.assertEqual(ledger.last_committed_offset(), 912345678)
                ledger.reset_offset(0)
                self.assertEqual(ledger.last_committed_offset(), 0)
                self.assertEqual(ledger.next_offset(), 1)
            finally:
                ledger.close()

    def test_reset_offset_rejects_negative(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Ledger(Path(tmp) / "ledger.db")
            try:
                with self.assertRaises(ValueError):
                    ledger.reset_offset(-1)
            finally:
                ledger.close()


class ResetOffsetCommandTests(unittest.TestCase):
    def _config_file(self, tmp: str) -> str:
        root = Path(__file__).resolve().parents[1]
        data = yaml.safe_load((root / "config.example.yaml").read_text(encoding="utf-8"))
        data["ledger_path"] = str(Path(tmp) / "ledger.db")
        path = Path(tmp) / "config.yaml"
        path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
        return str(path)

    def test_command_resets_the_ledger_offset(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self._config_file(tmp)
            ledger = Ledger(Path(tmp) / "ledger.db")
            ledger.advance_offset(912345678)
            ledger.close()
            out = io.StringIO()
            with redirect_stdout(out):
                code = entry.main(["reset-offset", "--to", "0", "--config", config])
            self.assertEqual(code, 0)
            self.assertIn("912345678", out.getvalue())
            ledger = Ledger(Path(tmp) / "ledger.db")
            try:
                self.assertEqual(ledger.last_committed_offset(), 0)
            finally:
                ledger.close()

    def test_command_requires_to(self):
        with self.assertRaises(SystemExit):
            entry.main(["reset-offset"])


if __name__ == "__main__":
    unittest.main()
