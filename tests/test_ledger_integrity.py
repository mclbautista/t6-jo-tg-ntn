"""Startup integrity check and the online ``backup`` command (#131)."""

import contextlib
import io
import sqlite3
import stat
import tempfile
import unittest
import unittest.mock
from pathlib import Path

import yaml

from jo_watchdog.app.__main__ import main
from jo_watchdog.ledger.db import Ledger, LedgerCorrupt, backup


def ingest(ledger: Ledger, message_id: int) -> None:
    ledger.ingest_message(-100123, message_id, "{}", f"JO {message_id}", f"JO {message_id}")


def message_count(path: Path) -> int:
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    finally:
        conn.close()


class LedgerFiles(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.db = self.dir / "data" / "ledger.db"
        self.config = self.dir / "config.yaml"
        self.config.write_text(
            yaml.safe_dump(
                {
                    "intake_chat_id": -100123,
                    "intake_topic_id": 5,
                    "ops_chat_id": -100999,
                    "ops_topic_id": 9,
                    "job_orders_data_source_id": "ds-1",
                    "ledger_path": str(self.db),
                }
            ),
            encoding="utf-8",
        )

    def cli(self, *argv: str) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = main([*argv, "--config", str(self.config)])
        return code, out.getvalue()


class StartupIntegrityTests(LedgerFiles):
    def corrupt(self) -> None:
        """Overwrite every page after the first, as a torn write from outside would."""
        ledger = Ledger(self.db)
        for message_id in range(1, 40):
            ingest(ledger, message_id)
        ledger.close()
        raw = bytearray(self.db.read_bytes())
        page = int.from_bytes(raw[16:18], "big")
        raw[page:] = b"\xa5" * (len(raw) - page)
        self.db.write_bytes(raw)

    def test_a_healthy_ledger_opens(self):
        Ledger(self.db).close()
        Ledger(self.db).close()

    def test_a_corrupt_ledger_is_refused_before_any_migration(self):
        self.corrupt()
        before = self.db.read_bytes()
        with self.assertRaises(LedgerCorrupt) as caught:
            Ledger(self.db)
        self.assertIn(str(self.db), str(caught.exception))
        self.assertIn("integrity check", str(caught.exception))
        self.assertEqual(self.db.read_bytes(), before)

    def test_a_file_that_is_not_a_database_is_refused(self):
        self.db.parent.mkdir(parents=True)
        self.db.write_bytes(b"not a database, " * 400)
        with self.assertRaises(LedgerCorrupt):
            Ledger(self.db)

    def test_run_exits_non_zero_with_the_reason_and_no_traceback(self):
        self.corrupt()
        env = {
            "TELEGRAM_BOT_TOKEN": "t",
            "NOTION_API_KEY": "n",
            "DEEPSEEK_API_KEY": "d",
            "OPENROUTER_API_KEY": "o",
        }
        with (
            unittest.mock.patch.dict("os.environ", env),
            unittest.mock.patch("jo_watchdog.app.__main__.require_live_keys"),
            unittest.mock.patch("jo_watchdog.app.__main__.configure_logging"),
        ):
            code, output = self.cli("run")
        self.assertEqual(code, 1)
        self.assertIn("integrity check", output)
        self.assertNotIn("Traceback", output)


class BackupTests(LedgerFiles):
    def setUp(self):
        super().setUp()
        self.ledger = Ledger(self.db)
        self.addCleanup(self.ledger.close)
        for message_id in range(1, 6):
            ingest(self.ledger, message_id)
        self.dest = self.dir / "out" / "ledger-backup.db"
        self.dest.parent.mkdir()

    def test_backup_of_a_live_ledger_is_a_complete_standalone_copy(self):
        # The owning connection is still open and its rows are still only in the WAL.
        self.assertTrue(Path(f"{self.db}-wal").stat().st_size > 0)
        code, output = self.cli("backup", str(self.dest))
        self.assertEqual(code, 0, output)
        self.assertEqual(message_count(self.dest), 5)
        self.assertEqual(stat.S_IMODE(self.dest.stat().st_mode), 0o600)
        self.assertEqual(sorted(p.name for p in self.dest.parent.iterdir()), [self.dest.name])
        conn = sqlite3.connect(self.dest)
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "delete")
        # The live ledger keeps working.
        ingest(self.ledger, 6)
        self.assertEqual(message_count(self.dest), 5)

    def test_backup_never_overwrites_an_existing_file(self):
        self.dest.write_text("keep me")
        code, output = self.cli("backup", str(self.dest))
        self.assertEqual(code, 2)
        self.assertIn("already exists", output)
        self.assertEqual(self.dest.read_text(), "keep me")

    def test_backup_needs_an_existing_ledger_and_a_destination(self):
        with self.assertRaises(SystemExit):
            self.cli("backup")
        missing = self.dir / "nope" / "ledger.db"
        with self.assertRaises(FileNotFoundError):
            backup(missing, self.dest)
        self.assertFalse(missing.exists())
        self.assertFalse(self.dest.exists())


if __name__ == "__main__":
    unittest.main()
