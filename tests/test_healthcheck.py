"""Container health check reads the poller's liveness stamp read-only."""

import sqlite3
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml

from jo_watchdog.app import healthcheck
from jo_watchdog.app.__main__ import main

NOW = datetime(2026, 10, 22, 0, 0, tzinfo=UTC)


def stamp(path: Path, value: str | None) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE runtime_state (key TEXT PRIMARY KEY, value TEXT NOT NULL,"
        " updated_at TEXT NOT NULL)"
    )
    if value is not None:
        conn.execute(
            "INSERT INTO runtime_state VALUES ('poller_alive_at', ?, ?)", (value, value)
        )
    conn.commit()
    conn.close()


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


class HealthcheckTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.db = self.dir / "ledger.db"

    def test_fresh_stamp_is_healthy(self):
        stamp(self.db, iso(NOW - timedelta(seconds=119)))
        healthy, _ = healthcheck.check(self.db, now=NOW)
        self.assertTrue(healthy)

    def test_stale_stamp_is_unhealthy(self):
        stamp(self.db, iso(NOW - timedelta(seconds=121)))
        healthy, reason = healthcheck.check(self.db, now=NOW)
        self.assertFalse(healthy)
        self.assertIn("121s", reason)

    def test_missing_ledger_table_or_stamp_is_unhealthy(self):
        self.assertFalse(healthcheck.check(self.db, now=NOW)[0])
        sqlite3.connect(self.db).close()
        self.assertFalse(healthcheck.check(self.db, now=NOW)[0])
        self.db.unlink()
        stamp(self.db, None)
        self.assertFalse(healthcheck.check(self.db, now=NOW)[0])

    def test_check_never_creates_the_ledger(self):
        healthcheck.check(self.db, now=NOW)
        self.assertFalse(self.db.exists())

    def test_cli_exit_codes(self):
        config = self.dir / "config.yaml"
        config.write_text(
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
        self.assertEqual(main(["healthcheck", "--config", str(config)]), 1)
        stamp(self.db, iso(datetime.now(UTC)))
        self.assertEqual(main(["healthcheck", "--config", str(config)]), 0)
        self.assertEqual(main(["healthcheck", "--config", str(self.dir / "nope.yaml")]), 1)


if __name__ == "__main__":
    unittest.main()
