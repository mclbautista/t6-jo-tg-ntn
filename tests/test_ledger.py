"""Tests for the SQLite ledger: WAL, versioned schema, states, atomic accept."""

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from jo_watchdog.ledger.db import Ledger
from jo_watchdog.ledger.states import State


def make_ledger(path):
    return Ledger(str(path))


class SchemaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = Ledger(Path(self.tmp.name) / "ledger.db")

    def tearDown(self):
        self.ledger.close()
        self.tmp.cleanup()

    def test_opens_in_wal_mode(self):
        row = self.ledger._conn.execute("PRAGMA journal_mode").fetchone()
        self.assertEqual(row[0], "wal")

    def test_schema_version_table_exists_and_is_versioned(self):
        rows = self.ledger._conn.execute(
            "SELECT version FROM schema_version ORDER BY version"
        ).fetchall()
        self.assertTrue(len(rows) >= 1)
        self.assertEqual(rows[0][0], 1)

    def test_messages_table_enforces_pipeline_states(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.ledger._conn.execute(
                "INSERT INTO messages (chat_id, message_id, telegram_key, update_id,"
                " raw_source, text, first_line, state, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (-100, 1, "-100:1", 1, "{}", "x", "x", "bogus_state", "t", "t"),
            )

    def test_all_initial_states_are_allowed(self):
        for state in ("received", "planned", "writing", "written", "done"):
            self.ledger._conn.execute(
                "INSERT INTO messages (chat_id, message_id, telegram_key, update_id,"
                " raw_source, text, first_line, state, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (-100, len(state), f"-100:{state}", 1, "{}", "x", "x", state, "t", "t"),
            )

    def test_offsets_table_single_row(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.ledger._conn.execute(
                "INSERT INTO offsets (id, last_committed_offset) VALUES (2, 5)"
            )


class AcceptMessageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = Ledger(Path(self.tmp.name) / "ledger.db")

    def tearDown(self):
        self.ledger.close()
        self.tmp.cleanup()

    def test_accept_commits_message_and_advances_offset(self):
        self.assertEqual(self.ledger.next_offset(), 0)
        self.ledger.accept_message(
            chat_id=-100123,
            message_id=42,
            update_id=7,
            raw_source='{"raw": true}',
            text="first line",
            first_line="first line",
        )
        self.assertTrue(self.ledger.has_message("-100123:42"))
        self.assertEqual(self.ledger.last_committed_offset(), 7)
        self.assertEqual(self.ledger.next_offset(), 8)

    def test_accept_is_atomic_commit_before_offset(self):
        # Force the offset step to fail: the message insert must roll back too,
        # so the offset can never advance past an uncommitted message.
        class FailingLedger(Ledger):
            def _advance_offset(self, update_id):
                raise RuntimeError("boom")

        fl = FailingLedger(Path(self.tmp.name) / "fail.db")
        with self.assertRaises(RuntimeError):
            fl.accept_message(-100123, 42, 7, "{}", "x", "x")
        self.assertFalse(fl.has_message("-100123:42"))
        self.assertEqual(fl.last_committed_offset(), 0)
        fl.close()

    def test_duplicate_telegram_key_is_rejected(self):
        self.ledger.accept_message(-100123, 42, 7, "{}", "x", "x")
        with self.assertRaises(sqlite3.IntegrityError):
            self.ledger.accept_message(-100123, 42, 8, "{}", "x", "x")
        # Offset must not have advanced past the failed duplicate.
        self.assertEqual(self.ledger.last_committed_offset(), 7)

    def test_advance_offset_only_moves_forward(self):
        self.ledger.accept_message(-100123, 42, 9, "{}", "x", "x")
        self.ledger.accept_message(-100123, 43, 5, "{}", "y", "y")  # lower update_id
        self.assertEqual(self.ledger.last_committed_offset(), 9)


class IgnoreLogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = Ledger(Path(self.tmp.name) / "ledger.db")

    def tearDown(self):
        self.ledger.close()
        self.tmp.cleanup()

    def test_ignore_records_and_advances_offset(self):
        self.ledger.ignore(-100999, 5, 3, "out_of_scope_topic", "chatter")
        row = self.ledger._conn.execute(
            "SELECT reason, first_line FROM ignore_log"
        ).fetchone()
        self.assertEqual((row["reason"], row["first_line"]), ("out_of_scope_topic", "chatter"))
        self.assertEqual(self.ledger.last_committed_offset(), 3)

    def test_ignore_leaves_no_message_row(self):
        self.ledger.ignore(-100999, 5, 3, "no_text", "")
        self.assertEqual(
            self.ledger._conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 0
        )


class StateMachineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = Ledger(Path(self.tmp.name) / "ledger.db")
        self.ledger.accept_message(-100123, 42, 7, "{}", "first line", "first line")

    def tearDown(self):
        self.ledger.close()
        self.tmp.cleanup()

    def test_valid_transition_chain(self):
        key = "-100123:42"
        self.assertTrue(self.ledger.transition(key, State.RECEIVED, State.PLANNED))
        self.assertTrue(self.ledger.transition(key, State.PLANNED, State.WRITING))
        self.assertTrue(self.ledger.transition(key, State.WRITING, State.WRITTEN))
        self.assertTrue(self.ledger.transition(key, State.WRITTEN, State.DONE))
        msg = self.ledger.get_message(key)
        self.assertEqual(msg["state"], "done")

    def test_invalid_transition_is_rejected(self):
        key = "-100123:42"
        self.assertFalse(self.ledger.transition(key, State.RECEIVED, State.WRITTEN))
        self.assertEqual(self.ledger.get_message(key)["state"], "received")

    def test_store_plan_and_page_id(self):
        key = "-100123:42"
        self.ledger.transition(key, State.RECEIVED, State.PLANNED)
        self.ledger.store_plan(key, {"name": "N", "telegram_key": key, "body_blocks": ["b"]})
        msg = self.ledger.get_message(key)
        self.assertEqual(json.loads(msg["plan_json"])["name"], "N")

    def test_record_mismatch(self):
        key = "-100123:42"
        self.ledger.transition(key, State.RECEIVED, State.PLANNED)
        self.ledger.transition(key, State.PLANNED, State.WRITING)
        self.ledger.mark_written(key, "page-1")
        self.ledger.record_mismatch(key, "Name differs")
        msg = self.ledger.get_message(key)
        self.assertEqual(msg["readback_mismatch"], "Name differs")

    def test_pending_lists_received_rows(self):
        self.ledger.accept_message(-100123, 43, 8, "{}", "second", "second")
        self.assertEqual(
            sorted(self.ledger.pending(State.RECEIVED)), ["-100123:42", "-100123:43"]
        )


if __name__ == "__main__":
    unittest.main()
