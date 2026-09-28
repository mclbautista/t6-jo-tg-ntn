---
title: Reliability, outages and operations
type: grilling
mode: HITL
status: closed
assignee: claude (session 2026-09-28b)
blocked_by: [Telegram Bot API capabilities and limits, Notion write path for Job Orders]
---

## Question

How does the watchdog behave when things break, and how is it run?

Decide: queue technology and persistence volume; retry and backoff; what counts as repeated failure and where the operator alert goes; how liveness is shown (a heartbeat, so the watchdog itself is watched); logging; secrets and config (topic ids); container restart policy; how to roll out and roll back. Graduate any deployment detail sitting in the fog.

Added after the Telegram research (folds in the former "Downtime catch-up" fog):
- **Getting every message**: privacy mode off in BotFather (a bot-wide setting; re-add the bot to each group afterwards) or make the bot admin of the intake group. Decide which, weighing how much permission the bot gets against the re-add step.
- **Scoping**: with privacy off or admin rights the bot can see more than the intake topic (including the ops group if it is a member); decide the exact filter on chat id and topic thread id, and that everything else is ignored.
- **Transport**: long polling versus webhook; the research says polling fits (outbound-only, no domain or TLS).
- **Downtime**: decide whether the polling offset is persisted and what happens to messages posted while the container was down. To verify in the getUpdates docs: as far as I know Telegram keeps undelivered updates for up to 24 hours, so a restart within a day replays them (which makes idempotent writes essential) and a longer outage loses messages.
- **Rate limits**: irrelevant at about 12 messages a day; the queue exists for Notion and LLM failures, not for Telegram limits. Only HTTP 429 `retry_after` needs handling.
- **Ops group access**: the bot must be a member of the ops group with permission to post in the ops topic.

Added after the Notion write-path research: the container needs a Notion integration token (secret), a pinned Notion-Version, and Job Orders, People and Projects shared with the integration. Notion recommends a request queue, retries on 429 (and 529) and a retry limit that surfaces the final error; volume is far below the roughly 3 requests per second limit. Decide token storage, how a 403 or 404 (sharing revoked) is reported, and the retry limit.

## Resolution

Resolved 2026-09-28 in a grilling session with the owner.

- **Bot permissions**: the watchdog bot is made a group **admin of the intake group only** (not a bot-wide privacy-mode change), so it sees every message there without a re-add step and without affecting any other group it's in. It stays a plain member of the ops group, with permission to post.
- **Scope filter**: since admin rights and privacy-mode-off both make a bot see *everything* in a group it's in (not just one topic), the watchdog filters every incoming message on `chat_id` plus `message_thread_id`, discarding anything outside the intake topic. It never has a reason to post into the intake topic and never treats the ops group as a source.
- **Queue**: a SQLite file on the host filesystem, bind-mounted rather than kept in an internal/anonymous Docker volume, so other tools and future modules can open it directly without going through the container. It durably queues Notion and LLM writes and survives restarts.
- **Retry policy**: exponential backoff, 5 attempts, delays 30s → 2m → 5m → 10m → 15m. After the 5th failure the watchdog posts one alert to the ops topic and leaves the message in the queue (never drops it) for manual retry.
- **Heartbeat**: a daily message to the ops topic reporting queue depth and any messages stuck past their retry limit, so a silent hang is caught before someone notices missing JOs.
- **Secrets and config**: Telegram bot token, Notion integration token and the LLM key are injected as environment variables via Docker Compose's `.env`, kept out of any git repo. Chat and topic ids live in a small non-secret config file, still injected at deploy time rather than hard-coded.
- **Restart and rollout**: `restart: unless-stopped` in Compose. Deploys accept a brief downtime (pull and restart); this is safe only because of the confirmed update-retention window below.
- **Update retention (verified)**: per Telegram's own `getUpdates` documentation, as quoted by the owner: "Incoming updates are stored on the server until the bot receives them either way, but they will not be kept longer than 24 hours." So: (a) any restart or deploy shorter than 24 hours is safe — Telegram replays what the bot missed once polling resumes; (b) an outage longer than 24 hours loses messages permanently, with no way to detect the gap from Telegram's side alone; (c) because a resumed poll can redeliver an update the watchdog already queued before crashing, idempotent writes (already required) are what makes safe restarts possible, not just convenient.
- **Logging**: stdout (for `docker logs`) plus a rotated file on the same host path as the SQLite queue, so logs survive container recreation and can be handed over for debugging.

Named principles: **Scope at the filter, not the grant** (broad bot permissions are fine when every message is re-checked against chat and topic id before it's acted on), **A queue you can't inspect isn't a safety net** (bind-mount it, don't bury it in a Docker volume), **Silence is a failure mode** (the heartbeat exists because "nothing arrived" and "the bot died" look identical from the outside), and **Retention windows are guarantees, not guesses** (the 24-hour window is a documented Telegram behavior the whole restart/rollout design leans on, not an assumption).

Open item carried forward: an outage longer than 24 hours is a silent data-loss scenario with no detection path from Telegram alone. Not designed further here; noted in the map's fog as a possible future monitoring addition (external uptime check on the container, not on the queue).
