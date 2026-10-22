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

# Keyed by migration number so build tickets can land in any order; missing
# numbers are applied in ascending order. Migration 3 belongs to #20.
MIGRATIONS: dict[int, list[str]] = {1: MIGRATION_1, 2: MIGRATION_2, 4: MIGRATION_4}
