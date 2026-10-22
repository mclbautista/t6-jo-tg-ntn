<!-- Mirror of https://github.com/mclbautista/t6-jo-tg-ntn/issues/25. Keep identical: change the issue and this file together. -->

# 8. Crash-safe reliability and heartbeat

## Parent

#28

## What to build

Make the completed capture path recover correctly from dependency failures and crashes, and make its liveness visible through the private ops topic. Recovery must preserve the stored plan and the one-row-per-Telegram-Key invariant across every state boundary.

## Acceptance criteria

- [ ] Classifier, Notion, and durable outbox failures use five attempts scheduled at 30 seconds, 2 minutes, 5 minutes, 10 minutes, and 15 minutes, while respecting provider retry timing when present.
- [ ] Attempt exhaustion retains the message for manual retry and sends one owner-tagged outage alert; no failed work is deleted or replaced with a partial Job Order.
- [ ] The pipeline persists `received` → `extracted` → `planned` → `writing` → `written` → `done`, with stage results sufficient to resume without recomputing against changed caches.
- [ ] Recovery from `writing` waits at least the first retry delay, queries Notion by `Telegram Key`, adopts one matching row, creates only when none exists, and reports multiple matches without deleting or merging them.
- [ ] Normal success still uses retrieve-by-ID read-back; Telegram-Key queries are reserved for uncertain write recovery.
- [ ] The poller stamps liveness on every cycle and exits non-zero after ten stale minutes so the container can restart it.
- [ ] Reaction failures retry briefly, remain best-effort, and are counted without per-message outage alerts.
- [ ] A 07:00 Asia/Manila heartbeat covers and labels the preceding 24 hours and is derived directly from ledger SQL rather than mutable counters.
- [ ] The heartbeat includes captured, flagged, ignored/no-text with masked first lines, queue/stuck state, passwords withheld, Deliverable linked/hinted/none/skipped placeholders, reaction failures, and a model-change line only when an actual returned model ID changed.
- [ ] Token counts remain queryable but no cost dashboard or heartbeat cost total is added.
- [ ] Whole-pipeline crash-injection tests use real SQLite, fake Telegram/Notion/DeepSeek/Jev adapters, and a fake clock; crashes occur at every state boundary and immediately before and after every external call.
- [ ] The crash suite proves exactly one Notion row per `Telegram Key`, commit-before-offset ordering, unchanged plan replay, durable outbox atomicity, acceptable at-least-once report duplication, and reaction independence.
- [ ] Fake-clock tests cover the retry schedule, exhaustion, manual retry eligibility, empty/stale caches, deadman exit, and heartbeat window.
- [ ] Telegram’s 24-hour retention limit and missing-heartbeat human detection remain documented accepted gaps; no external uptime account is introduced.
- [ ] Attempt accounting follows the table under Technical details.
- [ ] `python -m jo_watchdog.app retry <telegram_key>` and `retry --all-exhausted` make exhausted messages eligible again.
- [ ] The outage alert and heartbeat render exactly as the templates under Technical details.
- [ ] Migration 7 is applied as specified under Technical details and documented in `docs/ledger-schema.md`.

## Technical details

### Attempt accounting

| Work | Counter | Schedule | On exhaustion |
|---|---|---|---|
| DeepSeek call, Notion create, read-back retrieve, `Telegram Key` reconciliation query | `messages.attempts`, reset to 0 on every successful state change | 30s, 2m, 5m, 10m, 15m (or a longer `Retry-After`) | set `exhausted_at`, post one outage alert, keep the row in its state |
| Ops post | `outbox.attempts` | same | `outbox.state = failed`, logged, counted in the heartbeat queue line |
| Jev matcher | none | one inline retry | `matcher_skipped` finding (#26) |
| Captured reaction | none | inline retries after 2s and 5s | `reaction_state = failed`, counted in the heartbeat |
| Cache refresh | none | next 6-hour cycle | stale-cache message after 24h (#23) |
| Empty cache hold | does not count | rechecked each cycle | — |

After a successful create, the Notion page id is stored in the same transaction while the row is still `writing`, so a later read-back failure retries only the retrieve.

### Manual retry

`retry` sets `attempts = 0`, `exhausted_at = NULL`, `next_attempt_at = now` for the given message (or all exhausted ones) and logs who ran it. It never changes the stored plan.

### Outage alert

```
🔴 <Step> failed after 5 attempts
Message still queued, not lost

Telegram message: [link](INTAKE_LINK)
Last error: <masked error> (attempt 5 of 5, given up at <HH:MM>)
Will not retry automatically — run `retry <telegram key>` after fixing the cause

@<owner handle>
```

`<Step>` is `Classification`, `Notion write`, `Notion read-back` or `Notion reconciliation`.

### Daily heartbeat

Sent at `heartbeat_time` (07:00 Asia/Manila) for the window `[yesterday 07:00, today 07:00)`.

```
📋 Daily heartbeat — <Mon D> 07:00 → <Mon D> 07:00

Captured: <n>
Flagged: <n>
Ignored (not a JO): <n>
No-text forwards: <n>
Passwords withheld: <n>
Reaction failed: <n>

Deliverables linked: <n>
Deliverables hinted: <n>
Deliverables none: <n>
Deliverables skipped: <n>

Queue: <n> stuck, <n> pending retry, <n> ops posts failed
Model changed: <old> → <new>

Ignored, first lines:
"<masked first line>"
```

- `Captured`: messages that reached `written` in the window. `Flagged`: of those, the ones with at least one finding. `Passwords withheld`: messages with `masked_count > 0`.
- `stuck` = exhausted messages; `pending retry` = messages with `attempts > 0` and not exhausted.
- `Model changed` appears once per provider whose latest `returned_model` differs from the one in the previous window; otherwise the line is omitted.
- Up to 5 most recent ignored first lines; the section is omitted when there are none.
- The Deliverable lines show 0 until #26 lands.

### Migration 7

- New `messages` column `exhausted_at TEXT`.
- New table `runtime_state`: `key TEXT PRIMARY KEY`, `value TEXT NOT NULL`, `updated_at TEXT NOT NULL`. Keys: `poller_alive_at` (stamped every poll cycle; read by the deadman and the container health check in #27).
- New table `heartbeats`: `window_start TEXT PRIMARY KEY`, `window_end TEXT NOT NULL`, `outbox_id INTEGER REFERENCES outbox(id)`, `created_at TEXT NOT NULL`. A heartbeat is enqueued once per window, even across restarts.

### Configuration

`owner_telegram_handle`, `retry_schedule_seconds` (`[30, 120, 300, 600, 900]`), `deadman_stale_minutes` (10), `heartbeat_time` (`"07:00"`), `timezone` (`"Asia/Manila"`).

## Blocked by

- #24
