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
from collections.abc import Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import NamedTuple

from jo_watchdog.core.masker import outbound_text
from jo_watchdog.core.plan import build_plan
from jo_watchdog.ledger.migrations import MIGRATIONS, REBUILDS_TABLES
from jo_watchdog.ledger.states import State, can_transition

CACHE_SNAPSHOTS_KEPT = 10

# Ops-post retry delays: five attempts at 30s, 2m, 5m, 10m, 15m (#25).
OUTBOX_RETRY_SECONDS = (30, 120, 300, 600, 900)

OUTBOX_KINDS = frozenset(
    {
        "capture_report",
        "edit_after_capture",
        "stale_cache",
        "outage_alert",
        "heartbeat",
        "dry_run_plan",
    }
)


class OutboxMessage(NamedTuple):
    """One rendered, masked ops post waiting to be inserted into ``outbox``."""

    kind: str
    chat_id: int
    topic_id: int
    text: str


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
        for version in sorted(MIGRATIONS):
            if version in applied:
                continue
            migration = MIGRATIONS[version]
            rebuild = version in REBUILDS_TABLES
            if rebuild:
                # Foreign-key enforcement can only change outside a transaction.
                self._conn.execute("PRAGMA foreign_keys=OFF")
            try:
                with self._transaction():
                    if callable(migration):
                        migration(self._conn)
                    else:
                        for statement in migration:
                            self._conn.execute(statement)
                    if rebuild and self._conn.execute("PRAGMA foreign_key_check").fetchone():
                        raise sqlite3.IntegrityError(
                            f"migration {version} left dangling foreign keys"
                        )
                    self._conn.execute(
                        "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                        (version, _now()),
                    )
            finally:
                if rebuild:
                    self._conn.execute("PRAGMA foreign_keys=ON")

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
        posted_at: str | None = None,
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
                "  posted_at, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                    posted_at,
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

    def due(self, state: State | str, now: str | None = None) -> list[str]:
        """Keys in ``state`` whose retry time, if any, has come."""
        rows = self._conn.execute(
            "SELECT telegram_key FROM messages WHERE state = ?"
            " AND (next_attempt_at IS NULL OR next_attempt_at <= ?) ORDER BY id",
            (State(state).value, now or _now()),
        ).fetchall()
        return [r["telegram_key"] for r in rows]

    def stage_result(self, key: str, stage: str) -> dict | None:
        row = self._conn.execute(
            "SELECT s.result_json FROM stage_results s JOIN messages m ON m.id = s.message_id"
            " WHERE m.telegram_key = ? AND s.stage = ?",
            (key, stage),
        ).fetchone()
        return json.loads(row["result_json"]) if row else None

    # -- classification ----------------------------------------------------

    def _insert_llm_call(self, message_row_id: int, call: dict, now: str) -> None:
        self._conn.execute(
            "INSERT INTO llm_calls"
            " (message_id, provider, purpose, requested_model, returned_model, prompt_tag,"
            "  masker_version, input_tokens, output_tokens, latency_ms, outcome, error,"
            "  detail_json, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                message_row_id,
                call["provider"],
                call["purpose"],
                call["requested_model"],
                call.get("returned_model"),
                call["prompt_tag"],
                call["masker_version"],
                call.get("input_tokens"),
                call.get("output_tokens"),
                call.get("latency_ms"),
                call["outcome"],
                outbound_text(call["error"]).text if call.get("error") else None,
                call.get("detail_json"),
                now,
            ),
        )

    def _move(self, key: str, from_state: State, to_state: State, now: str) -> dict:
        if not can_transition(from_state, to_state):
            raise ValueError(f"illegal transition {from_state.value} -> {to_state.value}")
        row = self._conn.execute(
            "SELECT * FROM messages WHERE telegram_key = ? AND state = ?",
            (key, from_state.value),
        ).fetchone()
        if row is None:
            raise LookupError(f"{key} is not in state {from_state.value}")
        self._conn.execute(
            "UPDATE messages SET state = ?, attempts = 0, next_attempt_at = NULL,"
            " last_error = NULL, updated_at = ? WHERE id = ?",
            (to_state.value, now, row["id"]),
        )
        return dict(row)

    def _insert_stage_result(self, message_row_id: int, stage: str, result: dict, now: str):
        self._conn.execute(
            "INSERT INTO stage_results (message_id, stage, result_json, created_at)"
            " VALUES (?, ?, ?, ?)",
            (message_row_id, stage, json.dumps(result, ensure_ascii=False), now),
        )

    def _log_ignored(self, row: dict, reason: str, now: str) -> None:
        self._conn.execute(
            "UPDATE messages SET ignore_reason = ? WHERE id = ?", (reason, row["id"])
        )
        self._conn.execute(
            "INSERT INTO ignore_log"
            " (chat_id, message_id, update_id, reason, first_line, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                row["chat_id"],
                row["message_id"],
                row["update_id"],
                reason,
                outbound_text(row["first_line"]).text,
                now,
            ),
        )

    def record_extraction(self, key: str, *, call: dict, result: dict) -> State:
        """Store a validated extraction, its trace, and the verdict's next state atomically.

        ``jo`` and ``unsure`` move to ``extracted``; ``not_jo`` moves to ``ignored``
        and gets an ``ignore_log`` row with the masked first line.
        """
        verdict = result["extraction"]["verdict"]
        to_state = State.IGNORED if verdict == "not_jo" else State.EXTRACTED
        now = _now()
        with self._transaction():
            row = self._move(key, State.RECEIVED, to_state, now)
            self._conn.execute(
                "UPDATE messages SET verdict = ? WHERE id = ?", (verdict, row["id"])
            )
            self._insert_llm_call(row["id"], call, now)
            self._insert_stage_result(row["id"], "extract", result, now)
            if to_state is State.IGNORED:
                self._log_ignored(row, "not_jo", now)
        return to_state

    def record_no_text(self, key: str) -> None:
        """A message with no text or caption skips the model and is ignored as ``no_text``."""
        now = _now()
        with self._transaction():
            row = self._move(key, State.RECEIVED, State.IGNORED, now)
            self._log_ignored(row, "no_text", now)

    def record_classifier_failure(self, key: str, *, call: dict, retry_at: str) -> None:
        """Count one failed classify attempt and schedule the next; the row stays queued."""
        now = _now()
        error = outbound_text(f"{call['outcome']}: {call.get('error') or ''}".strip()).text
        with self._transaction():
            row = self._conn.execute(
                "SELECT id FROM messages WHERE telegram_key = ? AND state = ?",
                (key, State.RECEIVED.value),
            ).fetchone()
            if row is None:
                raise LookupError(f"{key} is not in state received")
            self._insert_llm_call(row["id"], call, now)
            self._conn.execute(
                "UPDATE messages SET attempts = attempts + 1, next_attempt_at = ?,"
                " last_error = ?, updated_at = ? WHERE id = ?",
                (retry_at, error, now, row["id"]),
            )

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

    def mark_written(
        self,
        key: str,
        notion_page_id: str,
        *,
        findings: list[dict] | None = None,
        reports: Sequence[OutboxMessage] = (),
    ) -> bool:
        """Move ``writing`` → ``written`` with its findings and ops posts, atomically.

        The state change, the final findings, a pending captured reaction and
        every pending outbox row commit in one transaction (#23): a crash can
        never leave a written row whose report was not queued.
        """
        if not can_transition(State.WRITING, State.WRITTEN):
            return False
        for report in reports:
            if report.kind not in OUTBOX_KINDS:
                raise ValueError(f"unknown outbox kind {report.kind!r}")
        now = _now()
        with self._transaction():
            cur = self._conn.execute(
                "UPDATE messages SET state = ?, notion_page_id = ?, findings_json = ?,"
                " reaction_state = 'pending', updated_at = ?"
                " WHERE telegram_key = ? AND state = ?",
                (
                    State.WRITTEN.value,
                    notion_page_id,
                    json.dumps(findings or [], ensure_ascii=False),
                    now,
                    key,
                    State.WRITING.value,
                ),
            )
            if cur.rowcount != 1:
                return False
            row_id = self._conn.execute(
                "SELECT id FROM messages WHERE telegram_key = ?", (key,)
            ).fetchone()["id"]
            for report in reports:
                self._insert_outbox(report, message_row_id=row_id, now=now)
        self._protect_files()
        return True

    def set_reaction_state(self, key: str, state: str) -> None:
        """Record the captured reaction outcome; independent of the outbox."""
        if state not in ("pending", "set", "failed"):
            raise ValueError(f"unknown reaction state {state!r}")
        self._conn.execute(
            "UPDATE messages SET reaction_state = ?, updated_at = ? WHERE telegram_key = ?",
            (state, _now(), key),
        )

    def record_mismatch(self, key: str, detail: str) -> None:
        self._conn.execute(
            "UPDATE messages SET readback_mismatch = ?, updated_at = ?"
            " WHERE telegram_key = ?",
            (detail, _now(), key),
        )

    def mark_done(self, key: str) -> bool:
        return self.transition(key, State.WRITTEN, State.DONE)

    # -- ops outbox --------------------------------------------------------

    def _insert_outbox(
        self, report: OutboxMessage, *, message_row_id: int | None, now: str
    ) -> int:
        cur = self._conn.execute(
            "INSERT INTO outbox (message_id, kind, chat_id, topic_id, text, state,"
            " attempts, created_at) VALUES (?, ?, ?, ?, ?, 'pending', 0, ?)",
            (message_row_id, report.kind, report.chat_id, report.topic_id, report.text, now),
        )
        return cur.lastrowid

    def enqueue_system(self, report: OutboxMessage, *, key: str | None = None) -> int:
        """Queue a system message (edit after capture, stale cache, alerts).

        ``key`` links it to a ledger message when there is one.
        """
        if report.kind not in OUTBOX_KINDS:
            raise ValueError(f"unknown outbox kind {report.kind!r}")
        with self._transaction():
            row_id = None
            if key is not None:
                row = self._conn.execute(
                    "SELECT id FROM messages WHERE telegram_key = ?", (key,)
                ).fetchone()
                row_id = row["id"] if row else None
            outbox_id = self._insert_outbox(report, message_row_id=row_id, now=_now())
        self._protect_files()
        return outbox_id

    def due_outbox(self, *, now: str | None = None, limit: int = 20) -> list[dict]:
        """Pending outbox rows whose next attempt is due, oldest first."""
        rows = self._conn.execute(
            "SELECT * FROM outbox WHERE state = 'pending'"
            " AND (next_attempt_at IS NULL OR next_attempt_at <= ?)"
            " ORDER BY id LIMIT ?",
            (now or _now(), limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def outbox_sent(self, outbox_id: int, telegram_message_id: int | None) -> None:
        self._conn.execute(
            "UPDATE outbox SET state = 'sent', attempts = attempts + 1,"
            " telegram_message_id = ?, sent_at = ?, last_error = NULL"
            " WHERE id = ? AND state = 'pending'",
            (telegram_message_id, _now(), outbox_id),
        )

    def outbox_failed_attempt(
        self,
        outbox_id: int,
        error: str,
        *,
        retry_seconds: Sequence[int] = OUTBOX_RETRY_SECONDS,
        now: float | None = None,
    ) -> str:
        """Count one failed post; schedule the next or give up. Returns the new state."""
        row = self._conn.execute(
            "SELECT attempts FROM outbox WHERE id = ?", (outbox_id,)
        ).fetchone()
        attempts = row["attempts"] + 1
        clock = time.time() if now is None else now
        if attempts >= len(retry_seconds):
            state, next_at = "failed", None
        else:
            state = "pending"
            next_at = time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime(clock + retry_seconds[attempts - 1])
            )
        self._conn.execute(
            "UPDATE outbox SET state = ?, attempts = ?, next_attempt_at = ?, last_error = ?"
            " WHERE id = ?",
            (state, attempts, next_at, outbound_text(error).text, outbox_id),
        )
        return state

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
