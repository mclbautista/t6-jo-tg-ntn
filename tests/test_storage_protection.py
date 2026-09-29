"""Filesystem and persistent-log safety contracts for protected source data."""

import importlib
import io
import json
import logging
import stat
import tempfile
import unittest
from logging.handlers import RotatingFileHandler
from pathlib import Path

from jo_watchdog.ledger.db import Ledger

SOURCE = "PW: swordfish-credential"
WITHHELD = "[password withheld – see Telegram message]"


@unittest.skipUnless(hasattr(stat, "S_IMODE"), "platform does not expose POSIX mode bits")
class OwnerOnlyStorageTests(unittest.TestCase):
    def test_ledger_directory_database_and_companions_are_owner_only(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "data" / "ledger.db"
        ledger = Ledger(path)
        self.addCleanup(ledger.close)

        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        companions = [Path(f"{path}-wal"), Path(f"{path}-shm")]
        self.assertTrue(any(companion.exists() for companion in companions))
        for companion in companions:
            if companion.exists():
                self.assertEqual(stat.S_IMODE(companion.stat().st_mode), 0o600)

    def test_persistent_json_log_is_rotated_masked_and_owner_only(self):
        """Define the logging setup contract required by the live design.

        ``configure_logging`` installs JSON-lines stdout and a rotating file
        handler. Its filtering is the same shared masker boundary used by the
        capture pipeline, including %-style logging arguments.
        """
        logging_config = importlib.import_module("jo_watchdog.app.logging_config")
        configure_logging = logging_config.configure_logging

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        data_dir = Path(tmp.name) / "data"
        log_path = data_dir / "jo-watchdog.jsonl"
        stream = io.StringIO()
        root = logging.getLogger()
        previous_handlers = root.handlers[:]
        previous_level = root.level

        try:
            configure_logging(
                log_path=log_path,
                stream=stream,
                max_bytes=256,
                backup_count=2,
            )
            logging.getLogger("jo_watchdog.contract").warning(
                "message_text=%s", SOURCE
            )
            for handler in root.handlers:
                handler.flush()

            self.assertTrue(
                any(isinstance(handler, RotatingFileHandler) for handler in root.handlers)
            )
            self.assertEqual(stat.S_IMODE(data_dir.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(log_path.stat().st_mode), 0o600)

            stdout_line = stream.getvalue().strip().splitlines()[-1]
            file_line = log_path.read_text(encoding="utf-8").strip().splitlines()[-1]
            for line in (stdout_line, file_line):
                record = json.loads(line)
                rendered = json.dumps(record, ensure_ascii=False)
                self.assertNotIn("swordfish-credential", rendered)
                self.assertIn(WITHHELD, rendered)
        finally:
            for handler in root.handlers:
                if handler not in previous_handlers:
                    handler.close()
            root.handlers[:] = previous_handlers
            root.setLevel(previous_level)


if __name__ == "__main__":
    unittest.main()
