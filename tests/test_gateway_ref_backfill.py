"""``backfill-gateway-refs``: reconcile outbox rows stored before #109 (#114).

Before #109 a gateway ``message_ref`` was stored as the Telegram message ID; migration 10
moved it to ``gateway_ref`` and left such rows ``sent`` with no ID. The command asks the
gateway what each ref became. Real ledger over the in-memory fake gateway; made-up chat IDs.
"""

import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

import yaml

from jo_watchdog.adapters.gateway import GatewayTelegramClient
from jo_watchdog.adapters.protocols import NotifyStatus
from jo_watchdog.app import __main__ as entry
from jo_watchdog.ledger.db import Ledger
from tests.fake_gateway import SECRET, FakeGateway, telegram_id_for
from tests.test_gateway_mode import URL

OPS = (-100999, 7)
SECRET_TEXT = "private heartbeat text https://t.me/c/999/12"


class BackfillRig:
    def __init__(self, test: unittest.TestCase):
        tmp = tempfile.TemporaryDirectory()
        test.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.gateway = FakeGateway()
        root = Path(__file__).resolve().parents[1]
        data = yaml.safe_load((root / "config.example.yaml").read_text(encoding="utf-8"))
        data.update(
            ledger_path=str(self.dir / "ledger.db"), telegram_mode="gateway", gateway_url=URL
        )
        self.config = str(self.dir / "config.yaml")
        Path(self.config).write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
        self.ledger = Ledger(self.dir / "ledger.db", gateway_mode=True)
        test.addCleanup(self.ledger.close)
        self._next_ref = 5530
        # The gateway only knows refs it issued.
        self.gateway._issued.update(range(5530, 5560))

    def legacy_row(self, *, kind="heartbeat", state="sent", ref=None, telegram_id=None) -> int:
        """A row as migration 10 leaves a pre-#109 gateway-mode post: sent, ref kept, no ID."""
        ref = ref if ref is not None else self._next_ref
        self._next_ref = max(self._next_ref, ref) + 1
        cur = self.ledger._conn.execute(
            "INSERT INTO outbox (kind, chat_id, topic_id, text, state, gateway_ref,"
            " telegram_message_id, created_at, sent_at) VALUES (?, ?, ?, ?, ?, ?, ?,"
            " '2026-11-01T00:00:00Z', '2026-11-01T00:00:01Z')",
            (kind, OPS[0], OPS[1], SECRET_TEXT, state, ref, telegram_id),
        )
        return cur.lastrowid

    def row(self, outbox_id: int):
        query = "SELECT * FROM outbox WHERE id = ?"
        return self.ledger._conn.execute(query, (outbox_id,)).fetchone()

    def dump(self) -> list[tuple]:
        return [tuple(r) for r in self.ledger._conn.execute("SELECT * FROM outbox ORDER BY id")]

    def run(self, *args: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()

        def client(url, secret):
            return GatewayTelegramClient(url, secret, transport=self.gateway.transport())

        with (
            mock.patch.dict(os.environ, {"WATCHDOG_SECRET": SECRET}),
            mock.patch.object(entry, "GatewayTelegramClient", client),
            redirect_stdout(out),
            redirect_stderr(err),
        ):
            code = entry.main(["backfill-gateway-refs", "--config", self.config, *args])
        return code, out.getvalue(), err.getvalue()


class DryRunTests(unittest.TestCase):
    def test_prints_a_redacted_table_and_writes_nothing(self):
        rig = BackfillRig(self)
        rig.legacy_row(ref=5530)  # sent
        rig.legacy_row(ref=5531)
        rig.gateway.set_notify_state(5531, "queued")
        before = rig.dump()
        code, out, _ = rig.run()
        self.assertEqual(code, 0)
        self.assertIn("5530", out)
        self.assertIn(str(telegram_id_for(5530)), out)
        self.assertIn("sent", out)
        self.assertIn("queued", out)
        self.assertIn("dry run", out.lower())
        self.assertNotIn("private heartbeat", out)
        self.assertNotIn("t.me", out)
        self.assertNotIn(str(OPS[0]), out)
        self.assertEqual(rig.dump(), before)


class DryRunOutcomeTests(unittest.TestCase):
    def test_failed_unknown_and_incomplete_answers_are_reported_and_not_written(self):
        rig = BackfillRig(self)
        for ref in (5530, 5531, 5532):
            rig.legacy_row(ref=ref)
        rig.gateway.set_notify_state(5530, "failed")
        rig.gateway.set_notify_state(5531, "unknown")
        rig.gateway.set_notify_state(5532, "sent")
        before = rig.dump()

        async def answer(_client, ref):
            state = {5530: "failed", 5531: "unknown", 5532: "sent"}[ref]
            return NotifyStatus(state=state, telegram_message_id=None)  # sent with no ID

        with mock.patch.object(GatewayTelegramClient, "notify_status", answer):
            code, out, _ = rig.run()
        self.assertEqual(code, 1)  # a sent answer with no ID is an error, not a resolution
        for word in ("failed", "unknown", "error"):
            self.assertIn(word, out)
        self.assertEqual(rig.dump(), before)


class ApplyTests(unittest.TestCase):
    def test_a_sent_ref_gets_its_real_telegram_id(self):
        rig = BackfillRig(self)
        row_id = rig.legacy_row(ref=5530)
        code, _, _ = rig.run("--apply")
        self.assertEqual(code, 0)
        row = rig.row(row_id)
        self.assertEqual(row["state"], "sent")
        self.assertEqual(row["telegram_message_id"], telegram_id_for(5530))
        self.assertEqual(row["gateway_ref"], 5530)

    def test_the_resolved_row_feeds_the_next_backlink(self):
        rig = BackfillRig(self)
        row_id = rig.legacy_row(ref=5530)
        rig.ledger._conn.execute(
            "INSERT INTO heartbeats (window_start, window_end, outbox_id, created_at)"
            " VALUES ('2026-11-01T23:30:00Z', '2026-11-02T23:30:00Z', ?, '2026-11-02T23:30:00Z')",
            (row_id,),
        )
        later = datetime(2026, 11, 4, 23, 30, tzinfo=UTC)
        self.assertIsNone(rig.ledger.previous_heartbeat(later))
        rig.run("--apply")
        found = rig.ledger.previous_heartbeat(later)
        self.assertEqual(found["telegram_message_id"], telegram_id_for(5530))

    def test_failed_and_unknown_refs_are_cleared_and_never_back_link(self):
        rig = BackfillRig(self)
        failed = rig.legacy_row(ref=5530)
        unknown = rig.legacy_row(ref=5531)
        rig.gateway.set_notify_state(5530, "failed")
        rig.gateway.set_notify_state(5531, "unknown")
        code, out, _ = rig.run("--apply")
        self.assertEqual(code, 0)
        for row_id in (failed, unknown):
            row = rig.row(row_id)
            self.assertIsNone(row["telegram_message_id"])
            self.assertNotEqual(row["state"], "sent")
        self.assertEqual(rig.row(failed)["last_error"], "chat not found")
        self.assertEqual(rig.row(failed)["state"], "failed")
        self.assertNotIn("chat not found", out)  # outcomes only

    def test_a_queued_ref_is_left_untouched(self):
        rig = BackfillRig(self)
        row_id = rig.legacy_row(ref=5530)
        rig.gateway.set_notify_state(5530, "queued")
        before = tuple(rig.row(row_id))
        rig.run("--apply")
        self.assertEqual(tuple(rig.row(row_id)), before)

    def test_a_queued_ref_is_reported_again_but_never_counted_as_fixable(self):
        rig = BackfillRig(self)
        rig.legacy_row(ref=5530)
        rig.gateway.set_notify_state(5530, "queued")
        rig.run("--apply")
        code, out, _ = rig.run("--apply")
        self.assertEqual(code, 0)
        self.assertIn("nothing left to fix", out.lower())
        self.assertIn("1 still waiting", out)

    def test_a_second_run_changes_nothing_and_reports_nothing_to_fix(self):
        rig = BackfillRig(self)
        rig.legacy_row(ref=5530)
        rig.legacy_row(ref=5531)
        rig.legacy_row(ref=5532)
        rig.gateway.set_notify_state(5531, "failed")
        rig.gateway.set_notify_state(5532, "unknown")
        rig.run("--apply")
        after_first = rig.dump()
        for args in ((), ("--apply",)):
            code, out, _ = rig.run(*args)
            self.assertEqual(code, 0)
            self.assertIn("nothing left to fix", out.lower())
            self.assertEqual(rig.dump(), after_first)

    def test_rows_already_resolved_or_from_direct_mode_are_never_touched(self):
        rig = BackfillRig(self)
        worker_resolved = rig.legacy_row(ref=5530, telegram_id=466)  # gateway row, resolved
        direct = rig.legacy_row(ref=5531)
        rig.ledger._conn.execute(
            "UPDATE outbox SET gateway_ref = NULL, telegram_message_id = 77 WHERE id = ?",
            (direct,),
        )
        accepted = rig.legacy_row(ref=5532, state="accepted")  # the worker's to resolve
        before = rig.dump()
        code, out, _ = rig.run("--apply")
        self.assertEqual(code, 0)
        self.assertEqual(rig.dump(), before)
        self.assertIn("nothing left to fix", out.lower())
        self.assertEqual(rig.row(worker_resolved)["telegram_message_id"], 466)
        self.assertEqual(rig.row(accepted)["state"], "accepted")

    def test_a_lookup_error_leaves_that_row_alone_and_exits_non_zero(self):
        rig = BackfillRig(self)
        row_id = rig.legacy_row(ref=5530)
        rig.gateway.fail_next = 1
        before = tuple(rig.row(row_id))
        code, out, _ = rig.run("--apply")
        self.assertEqual(code, 1)
        self.assertIn("error", out)
        self.assertEqual(tuple(rig.row(row_id)), before)


class GuardTests(unittest.TestCase):
    def test_refuses_to_run_in_direct_mode(self):
        rig = BackfillRig(self)
        data = yaml.safe_load(Path(rig.config).read_text(encoding="utf-8"))
        data.update(telegram_mode="direct", gateway_url=None)
        Path(rig.config).write_text(yaml.safe_dump(data), encoding="utf-8")
        code, _, err = rig.run()
        self.assertEqual(code, 2)
        self.assertIn("gateway", err)

    def test_needs_the_module_secret(self):
        rig = BackfillRig(self)
        out, err = io.StringIO(), io.StringIO()
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            redirect_stdout(out),
            redirect_stderr(err),
        ):
            code = entry.main(["backfill-gateway-refs", "--config", rig.config])
        self.assertEqual(code, 2)
        self.assertIn("WATCHDOG_SECRET", err.getvalue())


if __name__ == "__main__":
    unittest.main()
