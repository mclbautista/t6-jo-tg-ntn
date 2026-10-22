"""The SQLite ledger: sole authority for capture and offset advancement.

The ledger owns two invariants the whole watchdog leans on:

1. **Commit-before-offset** — a message and its raw source are written to
   ``messages`` in the same transaction that advances the committed Telegram
   offset. If either half fails, neither is visible, so the offset can never
   advance past an update that was not durably queued.
2. **Own your dedupe key** — ``telegram_key`` is ``chat_id:message_id`` and is
   UNIQUE, so redelivering an update cannot create a second row.

It runs in WAL mode and is a plain, inspectable SQLite file.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from jo_watchdog.core.masker import outbound_text
from jo_watchdog.core.plan import build_plan
from jo_watchdog.ledger.migrations import MIGRATIONS
from jo_watchdog.ledger.states import State, can_transition

CACHE_SNAPSHOTS_KEPT = 10


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def telegram_key(chat_id: int, message_id: int) -> str:
    return f"{chat_id}:{message_id}"


class Ledger:
    """A single connection to the ledger database."""

    def __init__(self, path: str | Path, *, apply_migrations: bool = True):
        db_path = Path(path)
        db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        db_path.parent.chmod(0o700)
        self._path = str(db_path)
        previous_umask = os.umask(0o077)
        try:
            self._conn = sqlite3.connect(self._path, isolation_level=None)
        finally:
            os.umask(previous_umask)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        if apply_migrations:
            self._migrate()
            self._backfill_mask_metadata()
        self._protect_files()

    def _protect_files(self) -> None:
        """Keep the database and SQLite companion files readable by the owner only."""
        for path in (Path(self._path), Path(f"{self._path}-wal"), Path(f"{self._path}-shm")):
            if path.exists():
                path.chmod(0o600)

    # -- schema ------------------------------------------------------------

    def _migrate(self) -> None:
        applied = {
            row["version"]
            for row in self._conn.execute("SELECT version FROM schema_version")
        } if self._table_exists("schema_version") else set()
        for i, migration in sorted(MIGRATIONS.items()):
            if i in applied:
                continue
            with self._transaction():
                for statement in migration:
                    self._conn.execute(statement)
                self._conn.execute(
                    "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                    (i, _now()),
                )

    def _table_exists(self, name: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        return row is not None

    def _backfill_mask_metadata(self) -> None:
        """Make pre-v1 rows reproducible and remove raw text from stored plans."""
        rows = self._conn.execute(
            "SELECT id, telegram_key, text, plan_json FROM messages"
            " WHERE masked_text IS NULL OR masker_version IS NULL OR masked_count IS NULL"
        ).fetchall()
        if not rows:
            return
        with self._transaction():
            for row in rows:
                masked = outbound_text(row["text"])
                plan_json = row["plan_json"]
                if plan_json is not None:
                    old_plan = json.loads(plan_json)
                    plan = build_plan(old_plan["name"], row["telegram_key"], row["text"])
                    plan_json = plan.model_dump_json()
                self._conn.execute(
                    "UPDATE messages SET masked_text = ?, masker_version = ?,"
                    " masked_count = ?, plan_json = ? WHERE id = ?",
                    (
                        masked.text,
                        masked.masker_version,
                        masked.withheld_count,
                        plan_json,
                        row["id"],
                    ),
                )

    @contextmanager
    def _transaction(self):
        """An explicit ``BEGIN IMMEDIATE`` transaction that rolls back on error.

        The connection runs in autocommit mode (``isolation_level=None``), so
        ``with self._conn:`` would NOT roll back — an explicit transaction is
        the only way to keep the commit-before-offset invariant atomic.
        """
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        else:
            self._conn.execute("COMMIT")

    # -- offset ------------------------------------------------------------

    def _offset_row(self):
        return self._conn.execute(
            "SELECT last_committed_offset FROM offsets WHERE id = 1"
        ).fetchone()

    def last_committed_offset(self) -> int:
        """The highest committed update id, or 0 if nothing has committed."""
        row = self._offset_row()
        return row["last_committed_offset"] if row else 0

    def next_offset(self) -> int:
        """The offset to pass to ``getUpdates``.

        Telegram's ``offset`` is "first update id to return". On a fresh ledger
        this is 0 (return everything); after committing up to ``N`` it is
        ``N + 1``, which re-delivers the last update and relies on the UNIQUE
        ``telegram_key`` for idempotency.
        """
        row = self._offset_row()
        return 0 if row is None else row["last_committed_offset"] + 1

    def _advance_offset(self, update_id: int) -> None:
        """Move the committed offset forward, never backward (upsert + monotonic)."""
        self._conn.execute(
            "INSERT INTO offsets (id, last_committed_offset) VALUES (1, ?)"
            " ON CONFLICT(id) DO UPDATE SET last_committed_offset = excluded.last_committed_offset"
            " WHERE offsets.last_committed_offset < excluded.last_committed_offset",
            (update_id,),
        )

    def advance_offset(self, update_id: int) -> None:
        """Advance the committed offset to at least ``update_id`` (monotonic)."""
        with self._transaction():
            self._advance_offset(update_id)

    # -- intake ------------------------------------------------------------

    def accept_message(
        self,
        chat_id: int,
        message_id: int,
        update_id: int,
        raw_source: str,
        text: str,
        first_line: str,
    ) -> str:
        """Durably queue an in-scope update and advance the offset atomically.

        Returns the telegram key. Raises ``sqlite3.IntegrityError`` on a
        duplicate key, in which case nothing was committed.
        """
        key = telegram_key(chat_id, message_id)
        now = _now()
        masked = outbound_text(text)
        with self._transaction():
            self._conn.execute(
                "INSERT INTO messages"
                " (chat_id, message_id, telegram_key, update_id, raw_source, text,"
                "  first_line, masked_text, masker_version, masked_count, state,"
                "  created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    chat_id,
                    message_id,
                    key,
                    update_id,
                    raw_source,
                    text,
                    first_line,
                    masked.text,
                    masked.masker_version,
                    masked.withheld_count,
                    State.RECEIVED.value,
                    now,
                    now,
                ),
            )
            self._advance_offset(update_id)
        self._protect_files()
        return key

    def has_message(self, key: str) -> bool:
        return self.get_message(key) is not None

    def get_message(self, key: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM messages WHERE telegram_key = ?", (key,)
        ).fetchone()
        return dict(row) if row else None

    def pending(self, state: State | str) -> list[str]:
        rows = self._conn.execute(
            "SELECT telegram_key FROM messages WHERE state = ? ORDER BY id",
            (State(state).value,),
        ).fetchall()
        return [r["telegram_key"] for r in rows]

    # -- ignore log --------------------------------------------------------

    def ignore(
        self,
        chat_id: int,
        message_id: int,
        update_id: int,
        reason: str,
        first_line: str,
    ) -> None:
        """Record out-of-scope traffic and advance the offset past it."""
        safe_first_line = outbound_text(first_line).text
        with self._transaction():
            self._conn.execute(
                "INSERT INTO ignore_log"
                " (chat_id, message_id, update_id, reason, first_line, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (chat_id, message_id, update_id, reason, safe_first_line, _now()),
            )
            self._advance_offset(update_id)
        self._protect_files()

    # -- state machine -----------------------------------------------------

    def transition(self, key: str, from_state: State | str, to_state: State | str) -> bool:
        """Move a message from ``from_state`` to ``to_state`` if legal.

        Returns True when the transition was applied, False when the move is
        illegal or the row does not exist / is not in ``from_state``.
        """
        if not can_transition(from_state, to_state):
            return False
        cur = self._conn.execute(
            "UPDATE messages SET state = ?, updated_at = ?"
            " WHERE telegram_key = ? AND state = ?",
            (State(to_state).value, _now(), key, State(from_state).value),
        )
        return cur.rowcount == 1

    def store_plan(self, key: str, plan: dict) -> None:
        self._conn.execute(
            "UPDATE messages SET plan_json = ?, updated_at = ? WHERE telegram_key = ?",
            (json.dumps(plan, ensure_ascii=False), _now(), key),
        )

    def mark_writing(self, key: str) -> bool:
        return self.transition(key, State.PLANNED, State.WRITING)

    def mark_written(self, key: str, notion_page_id: str) -> bool:
        ok = self.transition(key, State.WRITING, State.WRITTEN)
        if ok:
            self._conn.execute(
                "UPDATE messages SET notion_page_id = ?, updated_at = ?"
                " WHERE telegram_key = ?",
                (notion_page_id, _now(), key),
            )
        return ok

    def record_mismatch(self, key: str, detail: str) -> None:
        self._conn.execute(
            "UPDATE messages SET readback_mismatch = ?, updated_at = ?"
            " WHERE telegram_key = ?",
            (detail, _now(), key),
        )

    def mark_done(self, key: str) -> bool:
        return self.transition(key, State.WRITTEN, State.DONE)

    # -- lifecycle ---------------------------------------------------------

    # -- cache snapshots -----------------------------------------------------

    def save_cache_snapshot(
        self,
        kind: str,
        format_version: int,
        payload: list | dict,
        item_count: int,
        fetched_at: str,
        *,
        keep: int = CACHE_SNAPSHOTS_KEPT,
    ) -> None:
        """Insert a successful refresh, then prune that kind to the last ``keep`` rows."""
        with self._transaction():
            self._conn.execute(
                "INSERT INTO cache_snapshot"
                " (kind, format_version, payload_json, item_count, fetched_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (kind, format_version, json.dumps(payload), item_count, fetched_at),
            )
            self._conn.execute(
                "DELETE FROM cache_snapshot WHERE kind = ? AND id NOT IN ("
                " SELECT id FROM cache_snapshot WHERE kind = ?"
                " ORDER BY fetched_at DESC, id DESC LIMIT ?)",
                (kind, kind, keep),
            )

    def latest_cache_snapshot(self, kind: str) -> dict | None:
        """The live snapshot for ``kind``: its newest row, payload decoded."""
        row = self._conn.execute(
            "SELECT * FROM cache_snapshot WHERE kind = ? ORDER BY fetched_at DESC, id DESC LIMIT 1",
            (kind,),
        ).fetchone()
        if row is None:
            return None
        snapshot = dict(row)
        snapshot["payload"] = json.loads(snapshot.pop("payload_json"))
        return snapshot

    def cache_snapshot_count(self, kind: str) -> int:
        return self._conn.execute(
            "SELECT COUNT(*) FROM cache_snapshot WHERE kind = ?", (kind,)
        ).fetchone()[0]

    def close(self) -> None:
        self._conn.close()
