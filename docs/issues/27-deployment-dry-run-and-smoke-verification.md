<!-- Mirror of https://github.com/mclbautista/t6-jo-tg-ntn/issues/27. Keep identical: change the issue and this file together. -->

# 10. Deployment, dry-run, and real-account smoke verification

## Parent

#28

## What to build

Package the complete watchdog for its always-on host, retain an off-by-default dry-run rollback control, and provide one manual real-account smoke command that verifies the assumptions deliberately excluded from CI. The capture pipeline goes straight to live; Deliverable linking remains the separate shadow-mode safeguard.

## Acceptance criteria

- [ ] The production image uses Python 3.12 slim and runs as a non-root user.
- [ ] Compose injects secrets through the deployment environment, bind-mounts owner-only persistent data, uses `restart: unless-stopped`, and exposes a meaningful health check.
- [ ] Configuration validates at startup and includes intake/ops chat and topic IDs, Priya’s page ID, owner handle, timezone, cache/retry/heartbeat values, fuzzy and Deliverable thresholds, `dry_run`, and `deliverable_link_enabled`.
- [ ] Migrations are forward-only and remain backward-compatible for one release so the prior image can read the current ledger during rollback.
- [ ] Dry-run defaults off; when enabled it stops at the exact stored `planned` payload, posts that plan to ops through the masker, and performs no Notion create or captured reaction.
- [ ] Straight-to-live deployment is documented and does not introduce a mandatory dry-run day.
- [ ] Deliverable linking remains disabled by default independently of capture go-live.
- [ ] The manual `smoke.py` path exercises a real Telegram message through ledger, classification, planning, Notion write/read-back, ops reporting, and captured reaction.
- [ ] Smoke verification confirms integration access to Job Orders, Contacts, Projects, and Deliverables; live `Aliases`, `End of Shift`, Department, Status, and exact property names; and owner-only storage permissions.
- [ ] Smoke verification covers unknown-select behavior, Notion query visibility, Retry-After handling, live `relation_contains`, and reciprocal Job Order/Deliverable relation behavior.
- [ ] Smoke verification covers all four forward origins where real fixtures exist, a real channel-origin link, the intake group’s available captured reaction, and the owner’s real Telegram handle.
- [ ] Smoke verification confirms DeepSeek structured output, Jev typed Choice probabilities/model metadata, credential-mask positive and negative cases, and threshold tuning against the corpus.
- [ ] The Jev smoke step sends one Decisions request through OpenRouter with `~typesafe/jev-latest`, and prints the returned snapshot id, whether a 429 carries `Retry-After` or `X-RateLimit-*`, and the probabilities for the three regression pairs.
- [ ] Smoke verification exercises read-back mismatch reporting, outbox retry, heartbeat formatting, dry-run behavior, matcher failure, and Deliverable shadow mode.
- [ ] The real-account smoke path is manual and excluded from CI; automated CI remains the pure-core, recorded-contract, and whole-pipeline crash seams.
- [ ] Operator documentation states the accepted outage gap beyond Telegram’s 24-hour retention window and identifies a missing 07:00 heartbeat as the chosen human signal.
- [ ] The health check runs `python -m jo_watchdog.app healthcheck`, which fails when `runtime_state.poller_alive_at` is older than 120 seconds.
- [ ] The captured reaction is chosen from `getChat(intake_chat_id).available_reactions`; `smoke.py` prints that list and fails if `captured_reaction` is not in it (an absent list means all standard emoji are allowed).
- [ ] `config.example.yaml` and `.env.example` list every key from the configuration reference under Technical details.
- [ ] A setup runbook in the README covers the one-time steps under Technical details.

## Technical details

### Configuration reference

Every key below is validated at startup; a bad value fails the boot.

| Key | Default | Ticket |
|---|---|---|
| `intake_chat_id`, `intake_topic_id`, `captured_reaction`, `poll_timeout`, `poll_interval`, `job_orders_data_source_id`, `notion_version`, `ledger_path` | as today | #18 |
| `deepseek_base_url`, `deepseek_model`, `deepseek_timeout_seconds` | `https://api.deepseek.com`, latest alias, 60 | #20 |
| `department_aliases` | `{TO: TOC}` | #21 |
| `contacts_data_source_id`, `projects_data_source_id`, `standing_cc_contact_page_id`, `cache_refresh_hours`, `cache_stale_alert_hours`, `fuzzy_min_score`, `fuzzy_min_lead`, `honorifics` | —, —, —, 6, 24, 90, 8, `[ate, kuya, sir]` | #22 |
| `ops_chat_id`, `ops_topic_id`, `standing_cc_telegram_handle`, `header_date_tolerance_days` | —, —, —, 1 | #23 |
| `owner_telegram_handle`, `retry_schedule_seconds`, `deadman_stale_minutes`, `heartbeat_time`, `timezone` | —, `[30,120,300,600]`, 10, `07:00`, `Asia/Manila` | #25 |
| `deliverables_data_source_id`, `jev_url`, `jev_model`, `jev_timeout_seconds`, `t_link`, `t_hint`, `deliverable_link_enabled` | —, `https://openrouter.ai/api/alpha/decisions`, `~typesafe/jev-latest`, 30, 0.90, 0.50, false | #26 |
| `dry_run` | false | #27 |

Secrets in `.env`: `TELEGRAM_BOT_TOKEN`, `NOTION_API_KEY`, `DEEPSEEK_API_KEY`, `OPENROUTER_API_KEY`.

### Container

- `python:3.12-slim`, a dedicated non-root user, `WORKDIR /app`, entry point `python -m jo_watchdog.app --config /app/config.yaml`.
- Compose: `env_file: .env`, `./data:/app/data` (host directory 0700, ledger file 0600), `./config.yaml:/app/config.yaml:ro`, `restart: unless-stopped`, `healthcheck` every 60s with 3 retries.
- Deploy: `git pull && docker compose up -d --build`. Rollback: start the previous image tag.

### Dry-run

With `dry_run: true`, messages stop at `planned`; one `dry_run_plan` outbox message per JO shows the masked plan (Name, each property, Deadline source, findings). No Notion write and no reaction.

### Setup runbook

1. Create the bot in BotFather; put the token in `.env`.
2. Make the bot an admin of the intake group only; add it as a member with posting rights to the ops group.
3. Read the intake and ops chat and topic ids and fill them into `config.yaml`.
4. Create the Notion internal integration with Read content and Insert content; share Job Orders, Contacts, Projects and Deliverables with it.
5. Fill Priya's Contacts page id and both Telegram handles.
6. Run `smoke.py`, pick `captured_reaction` from the printed list, re-run until it passes.
7. Start the container and watch for the first capture and the next 07:00 heartbeat.

## Blocked by

- #26
