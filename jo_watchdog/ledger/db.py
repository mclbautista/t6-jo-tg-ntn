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
from collections.abc import Callable, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import NamedTuple

from jo_watchdog.core.duplicates import text_sha256
from jo_watchdog.core.masker import outbound_text
from jo_watchdog.core.plan import build_plan
from jo_watchdog.ledger.migrations import MIGRATIONS, REBUILDS_TABLES
from jo_watchdog.ledger.states import State, can_transition

CACHE_SNAPSHOTS_KEPT = 10

# Ops-post retry delays (#25, D3): attempt one is immediate; the delays between
# attempts are 30s, 2m, 5m, 10m, so there are five attempts in total.
OUTBOX_RETRY_SECONDS = (30, 120, 300, 600)

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


def fmt_utc(when: datetime) -> str:
    """A timezone-aware datetime as the ledger's UTC timestamp string."""
    return when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class FailureResult(NamedTuple):
    """What one failed attempt did to a message."""

    attempts: int
    exhausted: bool
    next_attempt_at: str | None


def telegram_key(chat_id: int, message_id: int) -> str:
    return f"{chat_id}:{message_id}"


def _forward_origin_type(raw_source: str) -> str | None:
    """``forward_origin.type`` from a stored update, or None for a non-forward."""
    try:
        raw = json.loads(raw_source)
    except (TypeError, ValueError):
        return None
    message = raw.get("message") if isinstance(raw, dict) else None
    origin = message.get("forward_origin") if isinstance(message, dict) else None
    return origin.get("type") if isinstance(origin, dict) else None


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
                "  posted_at, text_sha256, forward_origin_type, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                    text_sha256(text),
                    _forward_origin_type(raw_source),
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
        row = self._conn.execute("SELECT * FROM messages WHERE telegram_key = ?", (key,)).fetchone()
        return dict(row) if row else None

    def pending(self, state: State | str) -> list[str]:
        rows = self._conn.execute(
            "SELECT telegram_key FROM messages WHERE state = ? ORDER BY id",
            (State(state).value,),
        ).fetchall()
        return [r["telegram_key"] for r in rows]

    def due(
        self, state: State | str, now: str | None = None, *, settle_before: str | None = None
    ) -> list[str]:
        """Keys in ``state`` whose retry time, if any, has come, oldest first.

        Exhausted messages are never due until ``retry`` clears them. With
        ``settle_before``, a message that has no scheduled retry (it was left
        mid-flight by a crash) is due only once it was last touched at or
        before that time.
        """
        now = now or _now()
        settle = settle_before or now
        rows = self._conn.execute(
            "SELECT telegram_key FROM messages WHERE state = ? AND exhausted_at IS NULL"
            " AND ((next_attempt_at IS NOT NULL AND next_attempt_at <= ?)"
            "      OR (next_attempt_at IS NULL AND updated_at <= ?)) ORDER BY id",
            (State(state).value, now, settle),
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

    def record_classifier_failure(
        self,
        key: str,
        *,
        call: dict,
        schedule: Sequence[int],
        now: datetime,
        retry_after: float | None = None,
        on_exhausted: Callable[[str, int], OutboxMessage] | None = None,
    ) -> FailureResult:
        """Count one failed classify attempt and schedule the next; the row stays queued."""
        error = f"{call['outcome']}: {call.get('error') or ''}".strip()
        return self.record_failure(
            key,
            error=error,
            schedule=schedule,
            now=now,
            retry_after=retry_after,
            state=State.RECEIVED,
            call=call,
            on_exhausted=on_exhausted,
        )

    # -- retries and exhaustion (#25) ---------------------------------------

    def record_failure(
        self,
        key: str,
        *,
        error: str,
        schedule: Sequence[int],
        now: datetime,
        retry_after: float | None = None,
        state: State | None = None,
        call: dict | None = None,
        on_exhausted: Callable[[str, int], OutboxMessage] | None = None,
    ) -> FailureResult:
        """Count one failed attempt, then schedule the next or give up.

        ``schedule`` holds the delays between attempts, so there are
        ``len(schedule) + 1`` attempts in all. The delay after the Nth failure
        is ``schedule[N-1]``, stretched to a provider's ``Retry-After`` when
        that is longer. The failure of the last attempt sets ``exhausted_at`` and
        queues the one outage alert (``on_exhausted(masked_error, attempts)``) in
        the same transaction. The row keeps its state and stored plan; nothing is deleted.
        """
        masked = outbound_text(error).text
        stamp = fmt_utc(now)
        with self._transaction():
            row = self._conn.execute(
                "SELECT id, attempts, state FROM messages WHERE telegram_key = ?", (key,)
            ).fetchone()
            if row is None or (state is not None and row["state"] != state.value):
                raise LookupError(f"{key} is not in state {state.value if state else 'any'}")
            if call is not None:
                self._insert_llm_call(row["id"], call, stamp)
            attempts = row["attempts"] + 1
            exhausted = attempts > len(schedule)
            if exhausted:
                next_at = None
            else:
                delay = max(schedule[attempts - 1], retry_after or 0)
                next_at = fmt_utc(now + timedelta(seconds=delay))
            self._conn.execute(
                "UPDATE messages SET attempts = ?, next_attempt_at = ?, last_error = ?,"
                " exhausted_at = ?, updated_at = ? WHERE id = ?",
                (attempts, next_at, masked, stamp if exhausted else None, _now(), row["id"]),
            )
            if exhausted and on_exhausted is not None:
                alert = on_exhausted(masked, attempts)
                if alert.kind not in OUTBOX_KINDS:
                    raise ValueError(f"unknown outbox kind {alert.kind!r}")
                self._insert_outbox(alert, message_row_id=row["id"], now=stamp)
        self._protect_files()
        return FailureResult(attempts, exhausted, next_at)

    def retry(self, key: str, *, now: datetime) -> bool:
        """Make one exhausted message eligible again; the stored plan is untouched."""
        return self._retry("telegram_key = ?", (key,), now) == 1

    def retry_all_exhausted(self, *, now: datetime) -> int:
        """Make every exhausted message eligible again; returns how many."""
        return self._retry("1 = 1", (), now)

    def _retry(self, where: str, params: tuple, now: datetime) -> int:
        cur = self._conn.execute(
            "UPDATE messages SET attempts = 0, exhausted_at = NULL, next_attempt_at = ?,"
            f" updated_at = ? WHERE exhausted_at IS NOT NULL AND {where}",
            (fmt_utc(now), _now(), *params),
        )
        return cur.rowcount

    def exhausted_keys(self) -> list[str]:
        rows = self._conn.execute(
            "SELECT telegram_key FROM messages WHERE exhausted_at IS NOT NULL ORDER BY id"
        ).fetchall()
        return [r["telegram_key"] for r in rows]

    def store_page_id(self, key: str, notion_page_id: str) -> bool:
        """Remember the created page while the row is still ``writing``.

        A later read-back failure then retries only the retrieve. Progress, so
        the attempt counter restarts.
        """
        cur = self._conn.execute(
            "UPDATE messages SET notion_page_id = ?, attempts = 0, next_attempt_at = NULL,"
            " last_error = NULL, updated_at = ? WHERE telegram_key = ? AND state = ?",
            (notion_page_id, _now(), key, State.WRITING.value),
        )
        return cur.rowcount == 1

    def record_stage_result(self, key: str, stage: str, result: dict) -> None:
        """Store (or replace) one stage's result for a message."""
        now = _now()
        self._conn.execute(
            "INSERT INTO stage_results (message_id, stage, result_json, created_at)"
            " SELECT id, ?, ?, ? FROM messages WHERE telegram_key = ?"
            " ON CONFLICT(message_id, stage) DO UPDATE SET result_json = excluded.result_json",
            (stage, json.dumps(result, ensure_ascii=False), now, key),
        )

    # -- liveness and heartbeat (#25) ---------------------------------------

    def stamp_alive(self, now: datetime) -> None:
        """Record that the poller is making progress (read by the deadman and health check)."""
        stamp = fmt_utc(now)
        self._conn.execute(
            "INSERT INTO runtime_state (key, value, updated_at) VALUES ('poller_alive_at', ?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value,"
            " updated_at = excluded.updated_at",
            (stamp, stamp),
        )

    def poller_alive_at(self) -> datetime | None:
        row = self._conn.execute(
            "SELECT value FROM runtime_state WHERE key = 'poller_alive_at'"
        ).fetchone()
        return datetime.fromisoformat(row["value"]) if row else None

    def heartbeat_exists(self, window_start: datetime) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM heartbeats WHERE window_start = ?", (fmt_utc(window_start),)
        ).fetchone()
        return row is not None

    def has_heartbeats(self) -> bool:
        return self._conn.execute("SELECT 1 FROM heartbeats LIMIT 1").fetchone() is not None

    def skip_heartbeat(self, window_start: datetime, window_end: datetime) -> None:
        """Mark a window as handled without a post (a brand-new ledger's first window)."""
        self._conn.execute(
            "INSERT OR IGNORE INTO heartbeats (window_start, window_end, outbox_id, created_at)"
            " VALUES (?, ?, NULL, ?)",
            (fmt_utc(window_start), fmt_utc(window_end), _now()),
        )

    def enqueue_heartbeat(
        self, window_start: datetime, window_end: datetime, report: OutboxMessage
    ) -> bool:
        """Queue the heartbeat for a window exactly once, even across restarts."""
        if report.kind != "heartbeat":
            raise ValueError("not a heartbeat message")
        start = fmt_utc(window_start)
        now = _now()
        with self._transaction():
            if self._conn.execute(
                "SELECT 1 FROM heartbeats WHERE window_start = ?", (start,)
            ).fetchone():
                return False
            outbox_id = self._insert_outbox(report, message_row_id=None, now=now)
            self._conn.execute(
                "INSERT INTO heartbeats (window_start, window_end, outbox_id, created_at)"
                " VALUES (?, ?, ?, ?)",
                (start, fmt_utc(window_end), outbox_id, now),
            )
        self._protect_files()
        return True

    def _latest_model(self, provider: str, *, before: str, since: str = "") -> str | None:
        """The newest ``returned_model`` a provider sent in ``[since, before)``."""
        row = self._conn.execute(
            "SELECT returned_model FROM llm_calls WHERE provider = ?"
            " AND returned_model IS NOT NULL AND created_at < ? AND created_at >= ?"
            " ORDER BY created_at DESC, id DESC LIMIT 1",
            (provider, before, since),
        ).fetchone()
        return row[0] if row else None

    def heartbeat_stats(self, window_start: datetime, window_end: datetime) -> dict:
        """Everything the heartbeat reports, straight from ledger rows (no counters).

        A capture belongs to the window holding its immutable ``written_at``
        (set once by ``mark_written``), so a reaction or ``done`` landing after
        the boundary never moves it. Rows written before migration 8 have no
        ``written_at``; they fall back to ``updated_at`` at query time, which is
        never stored as a capture time.
        """
        start, end = fmt_utc(window_start), fmt_utc(window_end)
        one = lambda sql, *args: self._conn.execute(sql, args).fetchone()[0]  # noqa: E731
        captured = (
            "state IN ('written', 'done')"
            " AND COALESCE(written_at, updated_at) >= ? AND COALESCE(written_at, updated_at) < ?"
        )
        ignored = "FROM ignore_log WHERE reason = ? AND created_at >= ? AND created_at < ?"
        deliverables = {
            outcome: one(
                "SELECT COUNT(*) FROM stage_results WHERE stage = 'match'"
                " AND created_at >= ? AND created_at < ?"
                " AND json_extract(result_json, '$.outcome') = ?",
                start,
                end,
                outcome,
            )
            for outcome in ("linked", "hinted", "none", "skipped")
        }
        models = {}
        for (provider,) in self._conn.execute(
            "SELECT DISTINCT provider FROM llm_calls WHERE returned_model IS NOT NULL"
            " AND created_at >= ? AND created_at < ?",
            (start, end),
        ).fetchall():
            current = self._latest_model(provider, before=end, since=start)
            previous = self._latest_model(provider, before=start)
            if current and previous and current != previous:
                models[provider] = (previous, current)
        return {
            "captured": one(f"SELECT COUNT(*) FROM messages WHERE {captured}", start, end),
            "flagged": one(
                f"SELECT COUNT(*) FROM messages WHERE {captured}"
                " AND findings_json IS NOT NULL AND findings_json != '[]'",
                start,
                end,
            ),
            "ignored": one(f"SELECT COUNT(*) {ignored}", "not_jo", start, end),
            "no_text": one(f"SELECT COUNT(*) {ignored}", "no_text", start, end),
            "ignored_first_lines": [
                r[0]
                for r in self._conn.execute(
                    f"SELECT first_line {ignored} ORDER BY id DESC LIMIT 5", ("not_jo", start, end)
                )
            ],
            "passwords_withheld": one(
                "SELECT COUNT(*) FROM messages WHERE masked_count > 0"
                " AND created_at >= ? AND created_at < ?",
                start,
                end,
            ),
            "reaction_failed": one(
                "SELECT COUNT(*) FROM messages WHERE reaction_state = 'failed'"
                " AND updated_at >= ? AND updated_at < ?",
                start,
                end,
            ),
            "deliverables": deliverables,
            "stuck": one("SELECT COUNT(*) FROM messages WHERE exhausted_at IS NOT NULL"),
            "pending_retry": one(
                "SELECT COUNT(*) FROM messages WHERE attempts > 0 AND exhausted_at IS NULL"
            ),
            "ops_failed": one("SELECT COUNT(*) FROM outbox WHERE state = 'failed'"),
            "models": models,
        }

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
            "UPDATE messages SET state = ?, attempts = 0, next_attempt_at = NULL,"
            " last_error = NULL, updated_at = ? WHERE telegram_key = ? AND state = ?",
            (State(to_state).value, _now(), key, State(from_state).value),
        )
        return cur.rowcount == 1

    def plan_message(
        self,
        key: str,
        plan: dict,
        *,
        jo_key: str | None = None,
        stage_results: dict[str, dict] | None = None,
        llm_calls: list[dict] | None = None,
    ) -> bool:
        """Move ``extracted`` → ``planned`` and store the plan in one transaction.

        A crash can therefore never leave a `planned` row without its plan. The
        planning-time stage results (the #26 match) and their LLM calls commit
        with it, so a replayed plan never repeats the call.
        """
        if not can_transition(State.EXTRACTED, State.PLANNED):
            return False
        with self._transaction():
            cur = self._conn.execute(
                "UPDATE messages SET state = ?, plan_json = ?, jo_key = ?, attempts = 0,"
                " next_attempt_at = NULL, last_error = NULL, updated_at = ?"
                " WHERE telegram_key = ? AND state = ?",
                (
                    State.PLANNED.value,
                    json.dumps(plan, ensure_ascii=False),
                    jo_key,
                    _now(),
                    key,
                    State.EXTRACTED.value,
                ),
            )
            if cur.rowcount == 1 and (stage_results or llm_calls):
                now = _now()
                row_id = self._conn.execute(
                    "SELECT id FROM messages WHERE telegram_key = ?", (key,)
                ).fetchone()["id"]
                for stage, result in (stage_results or {}).items():
                    self._conn.execute(
                        "INSERT INTO stage_results (message_id, stage, result_json, created_at)"
                        " VALUES (?, ?, ?, ?) ON CONFLICT(message_id, stage)"
                        " DO UPDATE SET result_json = excluded.result_json",
                        (row_id, stage, json.dumps(result, ensure_ascii=False), now),
                    )
                for call in llm_calls or []:
                    self._insert_llm_call(row_id, call, now)
        return cur.rowcount == 1

    def store_plan(self, key: str, plan: dict, *, jo_key: str | None = None) -> None:
        """Store the exact plan, and the content-duplicate key it was planned with."""
        self._conn.execute(
            "UPDATE messages SET plan_json = ?, jo_key = ?, updated_at = ? WHERE telegram_key = ?",
            (json.dumps(plan, ensure_ascii=False), jo_key, _now(), key),
        )

    def earlier_captures(self, key: str, *, text_sha: str, jo_key: str | None) -> list[dict]:
        """Other rows with a Notion page that share this text or JO key, oldest first."""
        rows = self._conn.execute(
            "SELECT telegram_key, text_sha256, jo_key, notion_page_id, posted_at, created_at"
            " FROM messages WHERE notion_page_id IS NOT NULL AND telegram_key != ?"
            " AND (text_sha256 = ? OR (? IS NOT NULL AND jo_key = ?)) ORDER BY id",
            (key, text_sha, jo_key, jo_key),
        ).fetchall()
        return [dict(r) for r in rows]

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
                " reaction_state = 'pending', attempts = 0, next_attempt_at = NULL,"
                " last_error = NULL, written_at = ?, updated_at = ?"
                " WHERE telegram_key = ? AND state = ?",
                (
                    State.WRITTEN.value,
                    notion_page_id,
                    json.dumps(findings or [], ensure_ascii=False),
                    now,
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
            # Edits that arrived mid-pipeline are reported with this capture.
            self._conn.execute(
                "UPDATE edits SET outcome = 'reported'"
                " WHERE telegram_key = ? AND outcome = 'pending_capture'",
                (key,),
            )
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

    # -- edits (#24) -------------------------------------------------------

    def edit_seen(self, update_id: int) -> bool:
        """True when this edit update was already recorded (offset N+1 redelivery)."""
        row = self._conn.execute("SELECT 1 FROM edits WHERE update_id = ?", (update_id,)).fetchone()
        return row is not None

    def has_pending_edits(self, key: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM edits WHERE telegram_key = ? AND outcome = 'pending_capture'",
            (key,),
        ).fetchone()
        return row is not None

    def edits_for(self, key: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM edits WHERE telegram_key = ? ORDER BY id", (key,)
        ).fetchall()
        return [dict(r) for r in rows]

    def record_edit(
        self,
        key: str,
        update_id: int,
        *,
        edited_at: str,
        outcome: str,
        report: OutboxMessage | None = None,
    ) -> None:
        """Record an edit, its ops post if any, and advance the offset atomically.

        The Notion row and the stored plan are never touched.
        """
        if outcome not in ("reported", "pending_capture", "ignored_message"):
            raise ValueError(f"unknown edit outcome {outcome!r}")
        if report is not None and report.kind not in OUTBOX_KINDS:
            raise ValueError(f"unknown outbox kind {report.kind!r}")
        now = _now()
        with self._transaction():
            self._conn.execute(
                "INSERT INTO edits (telegram_key, update_id, edited_at, outcome, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (key, update_id, edited_at, outcome, now),
            )
            if report is not None:
                row = self._conn.execute(
                    "SELECT id FROM messages WHERE telegram_key = ?", (key,)
                ).fetchone()
                self._insert_outbox(report, message_row_id=row["id"] if row else None, now=now)
            self._advance_offset(update_id)
        self._protect_files()

    # -- ops outbox --------------------------------------------------------

    def _insert_outbox(self, report: OutboxMessage, *, message_row_id: int | None, now: str) -> int:
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
        retry_after: float | None = None,
    ) -> str:
        """Count one failed post; schedule the next or give up. Returns the new state."""
        row = self._conn.execute(
            "SELECT attempts FROM outbox WHERE id = ?", (outbox_id,)
        ).fetchone()
        attempts = row["attempts"] + 1
        clock = time.time() if now is None else now
        if attempts > len(retry_seconds):
            state, next_at = "failed", None
        else:
            state = "pending"
            delay = max(retry_seconds[attempts - 1], retry_after or 0)
            next_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(clock + delay))
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
