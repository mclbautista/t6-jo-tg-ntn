"""Rollback contract: the prior release can open and read the current ledger.

Migrations are forward-only and backward-compatible for one release, so after
a rollback the previous image (which knows one fewer migration) must still
boot against a ledger the current image already migrated.
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from jo_watchdog.ledger import db as ledger_db
from jo_watchdog.ledger.db import Ledger
from jo_watchdog.ledger.migrations import MIGRATIONS
from jo_watchdog.ledger.states import State


class RollbackTests(unittest.TestCase):
    def test_prior_release_reads_current_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.db"
            current = Ledger(path)
            current.accept_message(-100123, 42, 7, "{}", "PROJ - EDT - JO #1", "PROJ - EDT - JO #1")
            current.close()

            with mock.patch.object(ledger_db, "MIGRATIONS", MIGRATIONS[:-1]):
                prior = Ledger(path)
                try:
                    self.assertEqual(prior.pending(State.RECEIVED), ["-100123:42"])
                    self.assertEqual(prior.last_committed_offset(), 7)
                    versions = [
                        row["version"]
                        for row in prior._conn.execute("SELECT version FROM schema_version")
                    ]
                finally:
                    prior.close()
            # The prior release neither re-applies nor removes the newer migration.
            self.assertEqual(versions, list(range(1, len(MIGRATIONS) + 1)))


if __name__ == "__main__":
    unittest.main()
