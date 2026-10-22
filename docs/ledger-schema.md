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

### `messages` (v3)

One row per accepted Telegram message, keyed by `telegram_key`. Migration 3
rebuilt this table (SQLite cannot alter a `CHECK`) inside one transaction:
create `messages_new`, copy every existing column, drop, rename, re-create the
table's indexes. It runs with foreign-key enforcement off and checks foreign
keys before commit, so tables that reference `messages(id)` stay valid.

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
| state              | TEXT    | one of `received`,`extracted`,`planned`,`writing`,`written`,`done`,`ignored` |
| plan_json          | TEXT    | the exact stored plan (Name, Telegram Key, blocks) |
| notion_page_id     | TEXT    | Notion page id after a successful create           |
| readback_mismatch  | TEXT    | mismatch detail from read-back comparison          |
| findings_json      | TEXT    | v5: final findings (`[{code, params}]`), plan plus read-back |
| reaction_state     | TEXT    | v5: captured reaction, `pending` \| `set` \| `failed` |
| created_at         | TEXT    | ISO-8601 UTC                                       |
| updated_at         | TEXT    | ISO-8601 UTC                                       |
| masked_text        | TEXT    | outbound-safe representation of `text` (v2)        |
| masker_version     | TEXT    | deterministic masker version used for this row (v2)|
| masked_count       | INTEGER | number of credential assignments withheld (v2)     |
| posted_at          | TEXT    | Telegram message date, ISO-8601 with `+08:00` (v3; backfilled from `raw_source`) |
| attempts           | INTEGER | NOT NULL DEFAULT 0; failed attempts at the current stage, reset on every successful state change (v3) |
| next_attempt_at    | TEXT    | ISO-8601 UTC; when a failed stage may run again (v3) |
| last_error         | TEXT    | masked `<failure class>: <detail>` of the last failed attempt (v3) |
| verdict            | TEXT    | DeepSeek verdict: `jo`, `unsure` or `not_jo` (v3)  |
| ignore_reason      | TEXT    | `not_jo` or `no_text` for `ignored` rows (v3)      |

### `stage_results` (v3)

One validated result per message and stage, so a stage never recomputes.

| column      | type    | notes                                                  |
|-------------|---------|--------------------------------------------------------|
| id          | INTEGER | PK                                                     |
| message_id  | INTEGER | NOT NULL, REFERENCES `messages(id)`                    |
| stage       | TEXT    | NOT NULL; `extract` (#20), `match` (#26)               |
| result_json | TEXT    | NOT NULL; see below                                    |
| created_at  | TEXT    | NOT NULL, ISO-8601 UTC                                 |

`UNIQUE(message_id, stage)`. An `extract` result holds `extraction` (the
validated DeepSeek response), `findings` (`unsure` and `multiple_jo`, each with
the model's `reasons`, for #23 to report) and `trace` (`requested_model`,
`returned_model`, `prompt_tag`, `input_tokens`, `output_tokens`, `latency_ms`,
`masker_version`).

### `llm_calls` (v3)

One row per model call, successful or not.

| column          | type    | notes                                                  |
|-----------------|---------|--------------------------------------------------------|
| id              | INTEGER | PK                                                     |
| message_id      | INTEGER | REFERENCES `messages(id)`                              |
| provider        | TEXT    | NOT NULL; `deepseek` or `jev`                          |
| purpose         | TEXT    | NOT NULL; `classify_extract` or `deliverable_match`    |
| requested_model | TEXT    | NOT NULL; the configured alias (unpinned)              |
| returned_model  | TEXT    | the model id the provider reported                     |
| prompt_tag      | TEXT    | NOT NULL; e.g. `extract-v1`                            |
| masker_version  | TEXT    | NOT NULL                                               |
| input_tokens    | INTEGER |                                                        |
| output_tokens   | INTEGER |                                                        |
| latency_ms      | INTEGER |                                                        |
| outcome         | TEXT    | NOT NULL; `ok`, `invalid_json`, `schema_violation`, `empty_content`, `timeout`, `api_error` |
| error           | TEXT    | masked error detail                                    |
| detail_json     | TEXT    | Jev candidates and probabilities (#26)                 |
| created_at      | TEXT    | NOT NULL, ISO-8601 UTC; indexed for the heartbeat      |

### `offsets`

Single row (`id = 1`); the highest committed Telegram update id.

| column                | type    | notes                    |
|-----------------------|---------|--------------------------|
| id                    | INTEGER | PK, always 1             |
| last_committed_offset | INTEGER | committed offset (max)   |

### `ignore_log`

One row per out-of-scope update (wrong chat or topic), and one per message that
moves to `ignored` (`not_jo` or `no_text`, with the masked first line).

| column     | type    | notes                          |
|------------|---------|--------------------------------|
| id         | INTEGER | PK (autoinc)                   |
| chat_id    | INTEGER |                                |
| message_id | INTEGER |                                |
| update_id  | INTEGER |                                |
| reason     | TEXT    | e.g. `out_of_scope_chat`       |
| first_line | TEXT    | masked-ready first line        |
| created_at | TEXT    | ISO-8601 UTC                   |

### `cache_snapshot` (v4, #22)

One row per successful, non-empty refresh of a Notion lookup cache. The newest
row per `kind` is the live snapshot and is loaded at startup before the first
live refresh. A failed refresh writes nothing. After each insert only the last
10 rows per kind are kept.

| column         | type    | notes                                                   |
|----------------|---------|---------------------------------------------------------|
| id             | INTEGER | PK                                                      |
| kind           | TEXT    | `people`, `projects` or `departments`                   |
| format_version | INTEGER | payload shape version; a mismatching snapshot is ignored |
| payload_json   | TEXT    | JSON array (shapes below)                               |
| item_count     | INTEGER | number of items in the payload                          |
| fetched_at     | TEXT    | ISO-8601 UTC time of the refresh                        |

Index `idx_cache_snapshot_kind_fetched` on `(kind, fetched_at)`.

Payload items, format version 1:

- `people`: `{page_id, name, telegram_user_id, telegram_handle, aliases, end_of_shift}` from Contacts `Name`, `TelegramUserID`, `TelegramHandle`, `Aliases`, `End of Shift` (raw select value).
- `projects`: `{page_id, name, code}` from Projects `Name` and `Project Code`.
- `departments`: the Job Orders `Department` select option names, as strings.
### `outbox` (v5)

The durable ops-topic outbox. A capture's report is inserted as `pending` in the
same transaction that moves its message to `written`, so a crash can never
separate a capture from its findings. Delivery is at-least-once: a crash after
Telegram accepts a post but before the row is marked `sent` re-posts it.

| column              | type    | notes                                                  |
|---------------------|---------|--------------------------------------------------------|
| id                  | INTEGER | PK (autoinc); logged with every send, for tracing      |
| message_id          | INTEGER | `messages(id)`; null for system messages               |
| kind                | TEXT    | `capture_report`, `edit_after_capture`, `stale_cache`, `outage_alert`, `heartbeat`, `dry_run_plan` |
| chat_id             | INTEGER | ops chat id                                            |
| topic_id            | INTEGER | ops forum topic id                                     |
| text                | TEXT    | rendered Telegram HTML; every source field masked      |
| state               | TEXT    | `pending` \| `sent` \| `failed`                         |
| attempts            | INTEGER | send attempts so far (default 0)                       |
| next_attempt_at     | TEXT    | ISO-8601 UTC; null = due now                           |
| last_error          | TEXT    | masked error of the last failed attempt                |
| telegram_message_id | INTEGER | id of the posted ops message once `sent`               |
| created_at          | TEXT    | ISO-8601 UTC                                           |
| sent_at             | TEXT    | ISO-8601 UTC                                           |

Index `outbox_state_next_attempt` on `(state, next_attempt_at)`. A failed post
retries after 30 s, 2 min, 5 min and 10 min; the fifth failure sets `failed`.

## Pipeline states

`received` → `extracted` → `planned` → `writing` → `written` → `done`, and
`received` → `ignored` (terminal).

Version 2 adds `masked_text`, `masker_version`, and `masked_count`; opening a
version-1 ledger backfills those fields and rewrites any stored plan through the
same outbound boundary. Version 3 adds `extracted` (validated DeepSeek output)
and `ignored` (`not_jo` and `no_text`). A failed classify call leaves the row in
`received` with `attempts` incremented and `next_attempt_at` set; there is no
partial-row fallback. `writing` is the crash-window state:
before the Notion create call a row moves to `writing`; on recovery the watchdog queries Notion by
`Telegram Key` to adopt or create, so one message never yields two rows.

Migrations are keyed by number and applied in ascending order of the numbers
not yet recorded in `schema_version`.

## Planned migrations

Migrations 6–7 are specified column by column in the build tickets and land with them:

| Migration | Ticket | Adds |
|---|---|---|
| 6 | [#24](issues/24-forwards-edits-reposts-and-jo-number-reuse.md) | `jo_key`, `text_sha256`, `forward_origin_type`, `edits` |
| 7 | [#25](issues/25-crash-safe-reliability-and-heartbeat.md) | `exhausted_at`, `runtime_state`, `heartbeats` |

Move each into the Tables section above when it ships.
