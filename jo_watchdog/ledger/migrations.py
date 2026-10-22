"""Numbered, forward-only ledger migrations.

Each migration is a list of SQL statements applied in order inside a single
transaction. ``schema_version`` records which migrations have run; a migration
is never edited once shipped — a schema change is a new, higher-numbered
migration (backward-compatible for one release, per the technical design §15).
"""

from __future__ import annotations

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

# Keyed by migration number, because each build ticket owns its number (#28)
# and they can land in any order. Missing numbers are applied in ascending order.
MIGRATIONS: dict[int, list[str]] = {1: MIGRATION_1, 2: MIGRATION_2, 5: MIGRATION_5}
