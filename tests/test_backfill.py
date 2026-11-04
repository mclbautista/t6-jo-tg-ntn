"""Manual backfill of intake messages that never reached the watchdog (#101).

``ingest`` queues the message in the ledger exactly as the poller would, without
moving the polling offset, so the running worker captures it through the normal
pipeline (classify, plan, resolve, duplicate check, Notion write, read-back).
"""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import yaml

from jo_watchdog.app import __main__ as entry
from jo_watchdog.core.backfill import BackfillError, from_payload, from_text
from jo_watchdog.ledger.db import Ledger
from tests.fakes import POSTED_AT_UNIX, update
from tests.test_worker import make_worker

KEY = "-100123:3121"


def telegram_update(message_id=3121, *, topic_id=5, chat_id=-100123, text="JO-TOC #12 do it"):
    return {
        "update_id": 601013703,
        "message": {
            "message_id": message_id,
            "chat": {"id": chat_id, "type": "supergroup"},
            "message_thread_id": topic_id,
            "is_topic_message": True,
            "from": {"id": 929048561, "is_bot": False, "username": "hmmkayy"},
            "date": POSTED_AT_UNIX,
            "text": text,
            "entities": [{"type": "mention", "offset": 0, "length": 3}],
        },
    }


class PayloadTests(unittest.TestCase):
    def test_a_telegram_update_is_taken_as_is(self):
        backfill = from_payload(telegram_update(), by="ops", at="2026-11-04T09:00:00Z")
        self.assertEqual((backfill.chat_id, backfill.message_id), (-100123, 3121))
        self.assertEqual(backfill.topic_id, 5)
        self.assertEqual(backfill.text, "JO-TOC #12 do it")
        self.assertEqual(backfill.raw["message"]["entities"][0]["type"], "mention")
        self.assertEqual(
            backfill.raw["jo_watchdog_backfill"], {"by": "ops", "at": "2026-11-04T09:00:00Z"}
        )

    def test_a_bare_message_and_a_gateway_item_are_wrapped(self):
        message = telegram_update()["message"]
        gateway_item = {"seq": 26, "chat": -100123, "raw": telegram_update()}
        for payload in (message, gateway_item):
            with self.subTest(payload=sorted(payload)):
                backfill = from_payload(payload, by="ops", at="t")
                self.assertEqual(backfill.message_id, 3121)
                self.assertEqual(backfill.raw["message"]["text"], "JO-TOC #12 do it")

    def test_a_caption_counts_as_text(self):
        payload = telegram_update()
        payload["message"]["caption"] = payload["message"].pop("text")
        self.assertEqual(from_payload(payload, by="o", at="t").text, "JO-TOC #12 do it")

    def test_edits_and_shapeless_payloads_are_refused(self):
        edit = {"update_id": 1, "edited_message": telegram_update()["message"]}
        for payload in (edit, {"hello": "world"}, {"message": {"text": "no ids"}}):
            with self.subTest(payload=payload), self.assertRaises(BackfillError):
                from_payload(payload, by="o", at="t")

    def test_text_builds_an_intake_message(self):
        backfill = from_text(
            "JO-TOC #12 do it",
            message_id=3121,
            chat_id=-100123,
            topic_id=5,
            from_user="@hmmkayy",
            date=POSTED_AT_UNIX,
            by="ops",
            at="t",
        )
        message = backfill.raw["message"]
        self.assertEqual(message["chat"]["id"], -100123)
        self.assertEqual(message["message_thread_id"], 5)
        self.assertEqual(message["from"], {"username": "hmmkayy", "is_bot": False})
        self.assertEqual(message["date"], POSTED_AT_UNIX)
        numeric = from_text(
            "x",
            message_id=1,
            chat_id=-1,
            topic_id=5,
            from_user="929048561",
            date=None,
            by="o",
            at="t",
        )
        self.assertEqual(numeric.raw["message"]["from"], {"id": 929048561, "is_bot": False})
        self.assertNotIn("date", numeric.raw["message"])


class LedgerIngestTests(unittest.IsolatedAsyncioTestCase):
    async def test_ingest_queues_a_received_row_without_moving_the_offset(self):
        worker, ledger, *_, cleanup = make_worker([update(26, message_id=3123)])
        self.addCleanup(cleanup)
        await worker.poll_once()
        key = ledger.ingest_message(
            -100123, 3121, json.dumps(telegram_update()), "JO-TOC #12 do it", "JO-TOC #12 do it"
        )
        self.assertEqual(key, KEY)
        self.assertEqual(ledger.get_message(key)["state"], "received")
        self.assertEqual(ledger.last_committed_offset(), 26)

    async def test_an_ingested_message_is_captured_once_by_the_normal_pipeline(self):
        worker, ledger, telegram, notion, cleanup = make_worker([])
        self.addCleanup(cleanup)
        ledger.ingest_message(
            -100123, 3121, json.dumps(telegram_update()), "JO-TOC #12 do it", "JO-TOC #12 do it"
        )
        await worker.run_once()

        msg = ledger.get_message(KEY)
        self.assertEqual(msg["state"], "done")
        self.assertEqual(msg["notion_page_id"], "page-1")
        self.assertEqual(len(notion.created), 1)
        self.assertEqual(telegram.reactions[0][:2], (-100123, 3121))

        # A later delivery of the same message (a repaired gateway) is a redelivery.
        telegram.updates = [update(27, message_id=3121, text="JO-TOC #12 do it")]
        await worker.run_once()
        self.assertEqual(len(notion.created), 1)
        self.assertEqual(ledger.get_message(KEY)["notion_page_id"], "page-1")


class IngestCommandTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        root = Path(__file__).resolve().parents[1]
        data = yaml.safe_load((root / "config.example.yaml").read_text(encoding="utf-8"))
        data.update(
            ledger_path=str(self.dir / "ledger.db"), intake_chat_id=-100123, intake_topic_id=5
        )
        self.config = self.dir / "config.yaml"
        self.config.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")

    def _run(self, *args: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = entry.main(["ingest", *args, "--config", str(self.config)])
        return code, out.getvalue(), err.getvalue()

    def _row(self, key=KEY):
        ledger = Ledger(self.dir / "ledger.db")
        try:
            return ledger.get_message(key)
        finally:
            ledger.close()

    def _json_file(self, payload) -> str:
        path = self.dir / "update.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return str(path)

    def test_json_file_queues_the_message(self):
        code, out, _ = self._run("--json", self._json_file(telegram_update()))
        self.assertEqual(code, 0)
        self.assertIn(KEY, out)
        row = self._row()
        self.assertEqual(row["state"], "received")
        self.assertEqual(row["posted_at"], "2026-09-28T11:00:00+08:00")
        self.assertIn("jo_watchdog_backfill", json.loads(row["raw_source"]))

    def test_json_from_stdin(self):
        stdin = io.StringIO(json.dumps(telegram_update()))
        with mock.patch("sys.stdin", stdin):
            code, _, _ = self._run("--json", "-")
        self.assertEqual(code, 0)
        self.assertIsNotNone(self._row())

    def test_text_form_defaults_to_the_intake_chat_and_topic(self):
        code, _, _ = self._run(
            "--text",
            "JO-TOC #12 do it",
            "--message-id",
            "3121",
            "--from-user",
            "@hmmkayy",
            "--date",
            "2026-11-04T11:39:00",
        )
        self.assertEqual(code, 0)
        row = self._row()
        self.assertEqual(row["text"], "JO-TOC #12 do it")
        # A date without an offset is Manila time.
        self.assertEqual(row["posted_at"], "2026-11-04T11:39:00+08:00")

    def test_a_message_outside_the_intake_topic_is_refused(self):
        code, _, err = self._run("--json", self._json_file(telegram_update(topic_id=99)))
        self.assertEqual(code, 2)
        self.assertIn("intake", err)
        self.assertIsNone(self._row())

    def test_a_message_already_in_the_ledger_is_not_queued_twice(self):
        path = self._json_file(telegram_update())
        self.assertEqual(self._run("--json", path)[0], 0)
        code, _, err = self._run("--json", path)
        self.assertEqual(code, 1)
        self.assertIn("already", err)

    def test_empty_text_is_refused(self):
        code, _, _ = self._run("--json", self._json_file(telegram_update(text="  ")))
        self.assertEqual(code, 2)
        self.assertIsNone(self._row())

    def test_exactly_one_source_is_required(self):
        with self.assertRaises(SystemExit):
            self._run()
        with self.assertRaises(SystemExit):
            self._run("--text", "x")  # no --message-id
        with self.assertRaises(SystemExit):
            self._run("--json", "-", "--text", "x", "--message-id", "1")

    def test_wait_reports_the_capture_once_the_worker_is_done(self):
        path = self._json_file(telegram_update())

        def capture_meanwhile(_seconds):
            ledger = Ledger(self.dir / "ledger.db")
            try:
                ledger._conn.execute(
                    "UPDATE messages SET state = 'done', notion_page_id = 'abc123'"
                    " WHERE telegram_key = ?",
                    (KEY,),
                )
            finally:
                ledger.close()

        with mock.patch.object(entry.time, "sleep", capture_meanwhile):
            code, out, _ = self._run("--json", path, "--wait", "30")
        self.assertEqual(code, 0)
        self.assertIn("done", out)
        self.assertIn("abc123", out)

    def test_wait_times_out_with_the_row_still_queued(self):
        path = self._json_file(telegram_update())
        with (
            mock.patch.object(entry.time, "sleep", lambda _s: None),
            mock.patch.object(entry.time, "monotonic", side_effect=[0, 0, 100]),
        ):
            code, out, _ = self._run("--json", path, "--wait", "30")
        self.assertEqual(code, 0)
        self.assertIn("still received", out)


if __name__ == "__main__":
    unittest.main()
