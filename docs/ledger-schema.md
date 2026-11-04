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
| update_id          | INTEGER | Telegram update id (offset tracking); 0 when backfilled by `ingest` (#101) |
| raw_source         | TEXT    | the raw update JSON, kept unmasked (local only)    |
| text               | TEXT    | message text/caption (source of body blocks)       |
| first_line         | TEXT    | first non-empty line (Name fallback)               |
| state              | TEXT    | one of `received`,`extracted`,`planned`,`writing`,`written`,`done`,`ignored` |
| plan_json          | TEXT    | the exact stored plan (Name, Telegram Key, blocks) |
| notion_page_id     | TEXT    | Notion page id after a successful create           |
| readback_mismatch  | TEXT    | mismatch detail from read-back comparison          |
| findings_json      | TEXT    | v5: final findings (`[{code, params}]`), plan plus read-back |
| reaction_state     | TEXT    | v5: capture reaction (👍, or 🤔 when findings were stored), `pending` \| `set` \| `failed` |
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
| exhausted_at       | TEXT    | v7: ISO-8601 UTC when the retry schedule ran out; NULL otherwise. An exhausted row keeps its state and plan, is never picked up again, and is made eligible by `retry` |
| written_at         | TEXT    | v8 (#54): ISO-8601 UTC, set once in the `mark_written` transaction and never updated; NULL for rows written before v8 |
| jo_key             | TEXT    | v6, reshaped in v9 (#93): `<Project page id>\|<jo_number_int>`, set at planning only when both resolve; NULL otherwise. JO Numbers are one counter per project, so the Department is not part of it |
| text_sha256        | TEXT    | v6: SHA-256 of the whitespace-normalized original `text`; set on intake (backfilled) |
| forward_origin_type| TEXT    | v6: `user`, `hidden_user`, `chat` or `channel`; NULL for a non-forward (backfilled from `raw_source`) |

Indexes `messages_jo_key` on `jo_key` and `messages_text_sha256` on
`text_sha256` (v6). An exact re-post is an earlier row with the same
`text_sha256` and a `notion_page_id`; number reuse is an earlier row with the
same `jo_key` and different text. Neither ever merges or suppresses a row.

Migration 9 (#93) rewrites v6 keys stored as
`<Project page id>|<Department option>|<jo_number_int>` to the two-part form
by keeping the parts before the first `|` and after the last, so captures
made before it still match. It changes data only, so the previous release
still opens the ledger; keys that release writes keep the old form and are
not rewritten again.

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
| reason     | TEXT    | e.g. `out_of_scope_chat`, `edit_unknown_message` (v6) |
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
| kind                | TEXT    | `capture_report`, `edit_after_capture`, `stale_cache`, `outage_alert`, `heartbeat`, `dry_run_plan`, `message_gap` |
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

### `edits` (v6, #24)

One row per in-scope `edited_message` update for a message in `messages`. An
edit never changes the Notion row or the stored plan; a correction needs a new
Telegram message. The row, any ops post, and the offset advance commit in one
transaction. Edits to messages not in `messages` go to `ignore_log` with reason
`edit_unknown_message` instead.

| column       | type    | notes                                                    |
|--------------|---------|----------------------------------------------------------|
| id           | INTEGER | PK                                                       |
| telegram_key | TEXT    | the edited message's key                                 |
| update_id    | INTEGER | Telegram update id; a redelivered update is not re-recorded |
| edited_at    | TEXT    | ISO-8601 UTC from Telegram's `edit_date`                 |
| outcome      | TEXT    | `reported` (message `written`/`done`: `edit_after_capture` posted), `pending_capture` (still in the pipeline), `ignored_message` (message `ignored`) |
| created_at   | TEXT    | ISO-8601 UTC                                             |

A `pending_capture` edit becomes `reported` in the transaction that moves its
message to `written`, which also queues one `edit_after_capture` post however
many edits arrived. Index `edits_telegram_key` on `telegram_key`.

### `runtime_state` (v7, #25)

Small key/value facts about the running process.

| column     | type | notes                                  |
|------------|------|----------------------------------------|
| key        | TEXT | PK; currently only `poller_alive_at`   |
| value      | TEXT | ISO-8601 UTC                           |
| updated_at | TEXT | ISO-8601 UTC                           |

`poller_alive_at` is stamped at the start of every poll cycle, after the poll,
and after each message the drain processes. The deadman exits the process
non-zero when it is older than `deadman_stale_minutes` (10), and the container
health check (#27) reads the same value.

### `heartbeats` (v7, #25)

One row per daily heartbeat window, so a window is reported once even across
restarts.

| column       | type    | notes                                                    |
|--------------|---------|----------------------------------------------------------|
| window_start | TEXT    | PK; ISO-8601 UTC start of the 24-hour window             |
| window_end   | TEXT    | ISO-8601 UTC end (exclusive)                             |
| outbox_id    | INTEGER | the queued `heartbeat` post; NULL for the window a brand-new ledger skipped |
| created_at   | TEXT    | ISO-8601 UTC                                             |

Every heartbeat figure is a query over `messages`, `ignore_log`, `outbox`,
`stage_results` and `llm_calls` for that window; there are no counters.
"Captured" is a message in `written` or `done` whose immutable `written_at`
falls in the window, so a reaction retry or restart recovery after 07:00 never
moves a capture into the next window. "Flagged" is the captured subset with
findings. Legacy policy (migration 8): rows written before it keep
`written_at` NULL, because their exact capture time was never recorded and is
not invented. The heartbeat counts such a row by `COALESCE(written_at,
updated_at)` at query time (the old behaviour, an approximation that can only
be late), and nothing writes that guess back. Migration 8 only adds a nullable
column, so the previous release still opens the ledger; rows it writes keep
`written_at` NULL and fall under the same fallback.

## Attempt accounting

A failed step increments `messages.attempts` and sets `next_attempt_at` to the
failure time plus `retry_schedule_seconds[attempts - 1]` (30 s, 2 min, 5 min,
10 min), or the provider's `Retry-After` when that is longer. The failure that
uses up the schedule (the fifth) sets `exhausted_at` and queues one outage
alert in the same transaction. Any successful state change, and storing the
created page id, resets `attempts` to 0. `outbox.attempts` is a separate
counter on the same schedule. A row left in `writing` by a crash has no
`next_attempt_at`; it is picked up once it has been untouched for the first
retry delay.

## Pipeline states

`received` → `extracted` → `planned` → `writing` → `written` → `done`, and
`received` → `ignored` (terminal).

Version 2 adds `masked_text`, `masker_version`, and `masked_count`; opening a
version-1 ledger backfills those fields and rewrites any stored plan through the
same outbound boundary. Version 3 adds `extracted` (validated DeepSeek output)
and `ignored` (`not_jo` and `no_text`). A failed classify call leaves the row in
`received` with `attempts` incremented and `next_attempt_at` set; there is no
partial-row fallback. `writing` is the crash-window state:
before the Notion create call a row moves to `writing`. The created page id is stored
at once, so a later read-back failure retries only the retrieve. A `writing` row
with no stored page id is uncertain: after at least the first retry delay the
watchdog queries Notion by `Telegram Key`, adopts the one match, creates only
when there is none, and reports (never merges or deletes) several. Normal
success never queries by `Telegram Key`. The move to `planned` and the stored
plan commit in one transaction.

Migrations are keyed by number and applied in ascending order of the numbers
not yet recorded in `schema_version`.
