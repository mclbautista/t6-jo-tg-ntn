# Ledger schema (versioned contract)

The ledger is a plain SQLite file in WAL mode. It is the sole authority for
whether a Telegram message has been captured, and owns the committed Telegram
polling offset. Other tools may open it read-only against this contract.

The `schema_version` table records which numbered, forward-only migrations have
been applied. Never edit a shipped migration — a schema change is a new,
higher-numbered migration that stays backward-compatible for one release.

## Tables

### `schema_version`

| column     | type    | notes                          |
|------------|---------|--------------------------------|
| version    | INTEGER | PK; the migration number       |
| applied_at | TEXT    | ISO-8601 UTC timestamp         |

### `messages` (v2)

One row per accepted Telegram message, keyed by `telegram_key`.

| column             | type    | notes                                              |
|--------------------|---------|----------------------------------------------------|
| id                 | INTEGER | PK (autoinc)                                       |
| chat_id            | INTEGER | Telegram chat id                                   |
| message_id         | INTEGER | Telegram message id                                |
| telegram_key       | TEXT    | UNIQUE; `chat_id:message_id`                       |
| update_id          | INTEGER | Telegram update id (offset tracking)               |
| raw_source         | TEXT    | the raw update JSON, kept unmasked (local only)    |
| text               | TEXT    | message text/caption (source of body blocks)       |
| first_line         | TEXT    | first non-empty line (Name fallback)               |
| masked_text        | TEXT    | outbound-safe representation of `text`             |
| masker_version     | TEXT    | deterministic masker version used for this row     |
| masked_count       | INTEGER | number of credential assignments withheld          |
| state              | TEXT    | one of `received`,`planned`,`writing`,`written`,`done` |
| plan_json          | TEXT    | the exact stored plan (Name, Telegram Key, blocks) |
| notion_page_id     | TEXT    | Notion page id after a successful create           |
| readback_mismatch  | TEXT    | mismatch detail from read-back comparison          |
| created_at         | TEXT    | ISO-8601 UTC                                       |
| updated_at         | TEXT    | ISO-8601 UTC                                       |

### `offsets`

Single row (`id = 1`); the highest committed Telegram update id.

| column                | type    | notes                    |
|-----------------------|---------|--------------------------|
| id                    | INTEGER | PK, always 1             |
| last_committed_offset | INTEGER | committed offset (max)   |

### `ignore_log`

One row per out-of-scope update (wrong chat or topic, or no text later).

| column     | type    | notes                          |
|------------|---------|--------------------------------|
| id         | INTEGER | PK (autoinc)                   |
| chat_id    | INTEGER |                                |
| message_id | INTEGER |                                |
| update_id  | INTEGER |                                |
| reason     | TEXT    | e.g. `out_of_scope_chat`       |
| first_line | TEXT    | masked-ready first line        |
| created_at | TEXT    | ISO-8601 UTC                   |

## Pipeline states

`received` → `planned` → `writing` → `written` → `done`

Version 2 adds `masked_text`, `masker_version`, and `masked_count`; opening a
version-1 ledger backfills those fields and rewrites any stored plan through the
same outbound boundary. Ticket #3 adds `extracted` (DeepSeek output) between
`received` and `planned` via migration 3. `writing` is the crash-window state:
before the Notion create call a row moves to `writing`; on recovery the watchdog queries Notion by
`Telegram Key` to adopt or create, so one message never yields two rows.
