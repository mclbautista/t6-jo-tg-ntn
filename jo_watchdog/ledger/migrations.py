"""Numbered, forward-only ledger migrations.

Each migration is a list of SQL statements applied in order inside a single
transaction, or a callable that receives the connection inside that transaction
(for rebuilds whose column list depends on what is already there).
``schema_version`` records which migrations have run; a migration is never
edited once shipped — a schema change is a new, higher-numbered
migration (backward-compatible for one release, per the technical design §15).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable

MIGRATION_1 = [
    """
    CREATE TABLE schema_version (
        version INTEGER PRIMARY KEY,
        applied_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER NOT NULL,
        message_id INTEGER NOT NULL,
        telegram_key TEXT NOT NULL UNIQUE,
        update_id INTEGER NOT NULL,
        raw_source TEXT NOT NULL,
        text TEXT NOT NULL,
        first_line TEXT NOT NULL,
        state TEXT NOT NULL CHECK (
            state IN ('received', 'planned', 'writing', 'written', 'done')
        ),
        plan_json TEXT,
        notion_page_id TEXT,
        readback_mismatch TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE offsets (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        last_committed_offset INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE ignore_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER NOT NULL,
        message_id INTEGER NOT NULL,
        update_id INTEGER NOT NULL,
        reason TEXT NOT NULL,
        first_line TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
]

MIGRATION_2 = [
    "ALTER TABLE messages ADD COLUMN masked_text TEXT",
    "ALTER TABLE messages ADD COLUMN masker_version TEXT",
    "ALTER TABLE messages ADD COLUMN masked_count INTEGER",
]

# -- migration 3 (#20): extracted/ignored states, retry columns, LLM traces ----

_MESSAGES_V3 = """
    CREATE TABLE messages_new (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER NOT NULL,
        message_id INTEGER NOT NULL,
        telegram_key TEXT NOT NULL UNIQUE,
        update_id INTEGER NOT NULL,
        raw_source TEXT NOT NULL,
        text TEXT NOT NULL,
        first_line TEXT NOT NULL,
        state TEXT NOT NULL CHECK (
            state IN ('received', 'extracted', 'planned', 'writing', 'written', 'done', 'ignored')
        ),
        plan_json TEXT,
        notion_page_id TEXT,
        readback_mismatch TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        masked_text TEXT,
        masker_version TEXT,
        masked_count INTEGER,
        posted_at TEXT,
        attempts INTEGER NOT NULL DEFAULT 0,
        next_attempt_at TEXT,
        last_error TEXT,
        verdict TEXT,
        ignore_reason TEXT
    )
"""

# Telegram's message date (unix seconds) as Manila-local ISO-8601 with +08:00.
_POSTED_AT_FROM_RAW = """
    CASE WHEN json_valid(raw_source)
              AND json_type(raw_source, '$.message.date') = 'integer'
         THEN strftime('%Y-%m-%dT%H:%M:%S', json_extract(raw_source, '$.message.date'),
                       'unixepoch', '+8 hours') || '+08:00'
    END
"""

_V3_NEW_COLUMNS = {
    "posted_at",
    "attempts",
    "next_attempt_at",
    "last_error",
    "verdict",
    "ignore_reason",
}


def _migration_3(conn: sqlite3.Connection) -> None:
    """Rebuild ``messages`` (SQLite cannot alter a CHECK) and add trace tables.

    Every column already present is carried over, including columns a later
    migration may have added first on a development ledger, and the table's
    explicit indexes are re-created. The caller runs this with foreign-key
    enforcement off and checks foreign keys before commit.
    """
    existing = [
        (row[1], row[2], row[3], row[4])  # name, type, notnull, default
        for row in conn.execute("PRAGMA table_info(messages)")
    ]
    indexes = [
        row[0]
        for row in conn.execute(
            "SELECT sql FROM sqlite_master"
            " WHERE type = 'index' AND tbl_name = 'messages' AND sql IS NOT NULL"
        )
    ]
    conn.execute(_MESSAGES_V3)
    v3_columns = {row[1] for row in conn.execute("PRAGMA table_info(messages_new)")}
    for name, col_type, notnull, default in existing:
        if name in v3_columns:
            continue
        ddl = f'ALTER TABLE messages_new ADD COLUMN "{name}" {col_type}'
        if default is not None:
            ddl += f" DEFAULT {default}"
        if notnull and default is not None:
            ddl += " NOT NULL"
        conn.execute(ddl)

    carried = [name for name, *_ in existing if name not in _V3_NEW_COLUMNS]
    columns = ", ".join(f'"{name}"' for name in carried)
    conn.execute(
        f"INSERT INTO messages_new ({columns}, posted_at)"
        f" SELECT {columns}, {_POSTED_AT_FROM_RAW} FROM messages"
    )
    conn.execute("DROP TABLE messages")
    conn.execute("ALTER TABLE messages_new RENAME TO messages")
    for sql in indexes:
        conn.execute(sql)

    conn.execute(
        """
        CREATE TABLE stage_results (
            id INTEGER PRIMARY KEY,
            message_id INTEGER NOT NULL REFERENCES messages(id),
            stage TEXT NOT NULL,
            result_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE (message_id, stage)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE llm_calls (
            id INTEGER PRIMARY KEY,
            message_id INTEGER REFERENCES messages(id),
            provider TEXT NOT NULL,
            purpose TEXT NOT NULL,
            requested_model TEXT NOT NULL,
            returned_model TEXT,
            prompt_tag TEXT NOT NULL,
            masker_version TEXT NOT NULL,
            input_tokens INTEGER,
            output_tokens INTEGER,
            latency_ms INTEGER,
            outcome TEXT NOT NULL,
            error TEXT,
            detail_json TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX llm_calls_created_at ON llm_calls (created_at)")


MIGRATION_3 = _migration_3

# Persisted Notion cache snapshots (#22). The latest row per kind is live.
MIGRATION_4 = [
    """
    CREATE TABLE cache_snapshot (
        id INTEGER PRIMARY KEY,
        kind TEXT NOT NULL CHECK (kind IN ('people', 'projects', 'departments')),
        format_version INTEGER NOT NULL,
        payload_json TEXT NOT NULL,
        item_count INTEGER NOT NULL,
        fetched_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX idx_cache_snapshot_kind_fetched ON cache_snapshot (kind, fetched_at)",
]

# Migration 5 (#23): the durable ops outbox, plus the final findings and the
# captured-reaction state on each message.
MIGRATION_5 = [
    """
    CREATE TABLE outbox (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        message_id INTEGER REFERENCES messages(id),
        kind TEXT NOT NULL,
        chat_id INTEGER NOT NULL,
        topic_id INTEGER NOT NULL,
        text TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN ('pending', 'sent', 'failed')),
        attempts INTEGER NOT NULL DEFAULT 0,
        next_attempt_at TEXT,
        last_error TEXT,
        telegram_message_id INTEGER,
        created_at TEXT NOT NULL,
        sent_at TEXT
    )
    """,
    "CREATE INDEX outbox_state_next_attempt ON outbox (state, next_attempt_at)",
    "ALTER TABLE messages ADD COLUMN findings_json TEXT",
    "ALTER TABLE messages ADD COLUMN reaction_state TEXT",
]

Migration = list[str] | Callable[[sqlite3.Connection], None]

# Keyed by number and applied in ascending order of the numbers not yet recorded.
MIGRATIONS: dict[int, Migration] = {
    1: MIGRATION_1,
    2: MIGRATION_2,
    3: MIGRATION_3,
    4: MIGRATION_4,
    5: MIGRATION_5,
}

# Migrations that rebuild a referenced table run with foreign-key enforcement off.
REBUILDS_TABLES: frozenset[int] = frozenset({3})
