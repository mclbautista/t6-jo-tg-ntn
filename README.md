# JO Telegram Watchdog

Durable Telegram-to-Notion capture for Job Orders. One Python 3.12 asyncio
process polls a configured Telegram intake topic, commits each in-scope message
to a SQLite ledger before advancing the polling offset, plans the exact Notion
payload, writes it once, reads it back, and acknowledges with a fixed-meaning
"captured" reaction.

Status: the build tickets (#20 to #27, #39) are closed. Classification and
extraction (DeepSeek), credential masking, people and project resolution,
verification with ops reports, retries and the heartbeat, gateway mode and
Deliverable matching (shadow mode) are all in. The one open issue is #103, the
planned rename to t6-sixto-watchdog and the move onto t6.

## This repo is public

Anyone can read this repository and its full history. Never commit:

- `.env` or any real secret: the Telegram bot token, the Notion API key, the
  DeepSeek or OpenRouter keys, gateway secrets
- `config.yaml` (it holds the real chat, topic and Notion ids; only
  `config.example.yaml` is tracked)
- `data/`, `*.db` or any SQLite file (the ledger holds raw JO text, including
  transfer passwords before masking)
- raw Telegram exports or unredacted JOs (the old `old_files/sample-corpus.md`
  stays local and ignored)
- real client, show or person names, in code, tests, fixtures, prompts or docs

`.gitignore` covers the files above, and the **Public repo safety** workflow
(from t6-core, `.github/workflows/public-repo-safety.yml`) fails any push or pull
request that adds a secret, one of those paths or a name from the
`T6_NAMES_DENYLIST` Actions secret. Run it by hand (Actions > Public repo safety >
Run workflow) for a full-history secret scan. If a secret ever lands in a commit,
rotate it; deleting the commit is not enough.

## Layout

```
jo_watchdog/
  core/       pure: plan builder, payload builder, read-back compare, masker seam,
              verification findings, ops-report rendering
  ledger/     SQLite (WAL, versioned schema), offsets, state machine
  adapters/   Telegram (aiogram-as-client), Notion (httpx, pinned version), DeepSeek,
              Jev Deliverable matcher (OpenRouter Decisions API)
  prompts/    versioned few-shot turns for the classify/extract prompt
  app/        config, worker loop, entry point
tests/        unittest (runs under pytest too); real SQLite + fake adapters
smoke.py      manual real-account smoke verification (never in CI)
Dockerfile, compose.yaml   production image and always-on deployment
docs/         ledger schema contract, runbooks, issue mirrors
map/          design sources (mirrors of the closed design issues)
tools/        orphan-call reconciliation rehearsal
```

## Run locally

```sh
cp config.example.yaml config.yaml   # fill in every FILL IN value
cp .env.example .env                 # fill in the four secrets
pip install -e '.[dev]'
set -a; . ./.env; set +a
python -m jo_watchdog.app --config config.yaml
```

`config.yaml` is validated at startup: an unknown key, a bad value, or a
missing chat id, page id, handle or data source id stops the boot. All four
secrets in `.env` (`TELEGRAM_BOT_TOKEN`, `NOTION_API_KEY`, `DEEPSEEK_API_KEY`,
`OPENROUTER_API_KEY`) must be set. `config.example.yaml` documents every key.

Boot also rejects structurally invalid values before any provider call:
malformed Notion ids, non-positive topic ids, non-http(s) provider URLs, empty
model aliases or ledger path, and a `notion_version` that is not `YYYY-MM-DD`.
The error names each bad key, never its value. Negative Telegram chat ids are
valid. The example file passes this check but still holds `FILL IN`
placeholders; `smoke.py`'s config step flags those separately.

## Test

```sh
python -m unittest discover -s tests -t .    # or: pytest
```

CI runs only the pure core, the recorded contracts and the whole-pipeline
seams. The real-account checks live in `smoke.py` and are run by hand.

## Setup runbook (one time)

1. Create the bot in BotFather and put its token in `.env` as
   `TELEGRAM_BOT_TOKEN`.
2. Make the bot an **admin of the intake group only**. Add it to the ops group
   as a plain member that can post.
3. Read the intake and ops chat ids and forum topic ids (for example, forward a
   message from each topic to a helper bot, or read `getUpdates`) and fill
   `intake_chat_id`, `intake_topic_id`, `ops_chat_id` and `ops_topic_id` in
   `config.yaml`.
4. Create a Notion internal integration with **Read content** and **Insert
   content**. Put its token in `.env` as `NOTION_API_KEY`, then share Job
   Orders, Contacts, Projects and Deliverables with it (each database's `•••`
   menu, Connections).
5. Fill `standing_cc_contact_page_id` (the standing cc's page in Contacts, or set `T6_STANDING_CC`),
   `standing_cc_telegram_handle` and `owner_telegram_handle`. Add
   `DEEPSEEK_API_KEY` and `OPENROUTER_API_KEY` to `.env`.
6. Prepare a local data directory and run the smoke check until it passes (the
   container does not use this directory; it keeps its ledger in a named volume):

   ```sh
   mkdir -p data && chmod 700 data
   set -a; . ./.env; set +a
   python smoke.py --config config.yaml
   ```

   The `reactions` step prints the intake group's allowed reactions; set
   `captured_reaction` to one of them and re-run. Then post a test JO in
   the intake topic and run once more with `--live-telegram --live-message-id <its
   Telegram message id>` (watchdog stopped) to capture that one real message end
   to end. Delete the smoke rows it creates in Notion by hand.
7. Create the ledger's volume, start the container and watch for the first capture
   in Notion and the next 07:00 heartbeat in the ops topic:

   ```sh
   docker volume create jo-watchdog-data   # once per host; compose refuses to start without it
   docker compose up -d --build
   docker compose ps          # STATUS shows (healthy) once the poller is stamping
   docker compose logs -f
   ```

## Deploy

Deployment goes straight to live; there is no mandatory dry-run day.

```sh
docker tag jo-watchdog:latest jo-watchdog:previous 2>/dev/null || true   # keep the rollback image
git pull && docker compose up -d --build
```

The container runs `python:3.12-slim` as a non-root user (uid 1000), restarts
`unless-stopped`, reads secrets from `.env`, keeps the ledger and logs in the
`jo-watchdog-data` named volume at `/app/data` (directory 0700, files 0600), and
mounts `config.yaml` read-only. Its health check runs `python -m jo_watchdog.app healthcheck` every
60 seconds and turns unhealthy after 3 failures; it fails when
`runtime_state.poller_alive_at` is older than 120 seconds.

### Retries, outage alerts and the heartbeat

Classifier, Notion and ops-post failures are retried at 30 seconds, 2 minutes,
5 minutes and 10 minutes (longer if the provider sends `Retry-After`); the
fifth failure gives up. The message stays queued with its plan, one alert
tagged with `owner_telegram_handle` goes to the ops topic, and nothing is
deleted or half-written. After fixing the cause, make it eligible again:

```sh
docker compose exec watchdog python -m jo_watchdog.app retry -100123:42 --config /app/config.yaml
docker compose exec watchdog python -m jo_watchdog.app retry --all-exhausted --config /app/config.yaml
```

`retry` clears the attempt count and never changes the stored plan.

If the poller stops making progress for `deadman_stale_minutes` (10), the
process exits non-zero so the restart policy brings it back. At `heartbeat_time`
(07:00 `Asia/Manila`) the ops topic gets a heartbeat for the preceding 24 hours,
computed from the ledger. A brand-new ledger skips its first window.

### Missed messages: gap alerts and backfill (#101)

Telegram message ids in a supergroup are one counter for the whole chat. When an
intake chat message arrives whose id skips ids the ledger has never seen (any
topic counts as seen), the ops topic gets a "Possibly missed intake messages"
alert with links to the missing ids. A skip can be harmless, for example posts in
a topic the gateway does not send to the watchdog, so open the links and check.
Set `message_gap_alerts: false` to turn the alert off.

To capture a missed Job Order without asking the coordinator to re-post it, queue
it with `ingest`. The running watchdog captures it on its next cycle through the
normal pipeline (classification, resolution, duplicate check, Notion write,
read-back, reaction and capture report); the polling offset is not touched.
Prefer the original update JSON (for example the row in the gateway's `updates`
table), which keeps mentions, forward origin and the posting time:

```sh
docker compose exec -T watchdog python -m jo_watchdog.app ingest --json - --wait 120 --config /app/config.yaml < update.json
docker compose exec watchdog python -m jo_watchdog.app ingest --text "JO-TOC #12 ..." --message-id 3121 --from-user @user_eight --date 2026-11-04T11:39 --config /app/config.yaml
```

`--json` takes a Telegram update, a bare Telegram message or a gateway item.
`--text` rebuilds the message in the intake topic; `--date` without an offset is
Manila time and, when left out, the capture time stands in for the posting time.
Only messages in the intake chat and topic are accepted, and a message already in
the ledger is never queued twice. `--wait N` waits up to N seconds, prints the
final state and Notion URL, and exits 3 if the capture has not finished by then.

### Ledger storage and backups (#131)

The ledger must stay inside the Docker VM. On 2026-11-12 the production ledger was
found corrupt after living on a bind mount from macOS into the Colima VM, where
SQLite's file locks do not hold. The rules, the one-time move of an existing
`./data` into the volume and the restore steps are in
[`docs/runbooks/ledger-storage.md`](docs/runbooks/ledger-storage.md). In short:

- Never open the live ledger from macOS, and never `cp` it while the watchdog runs.
- Take a copy with the `backup` command, which uses SQLite's online backup API
  inside the watchdog's container, then copy that file out:

  ```sh
  docker compose exec watchdog python -m jo_watchdog.app backup /tmp/ledger-backup.db
  docker compose cp watchdog:/tmp/ledger-backup.db "./ledger-backup-$(date +%F).db"
  docker compose exec watchdog rm /tmp/ledger-backup.db
  chmod 600 ledger-backup-*.db    # raw JO text: keep it private, never commit it
  ```

- At startup the watchdog runs `PRAGMA quick_check` before any migration. A damaged
  ledger stops the boot with one line that names the file and the reason, and
  nothing is written to it.
- The log file is in the same volume:
  `docker compose exec watchdog tail -n 50 /app/data/jo-watchdog.jsonl`.

### Rollback

Start the previous image against the same ledger:

```sh
JO_WATCHDOG_TAG=previous docker compose up -d --no-build
```

Ledger migrations are forward-only and stay backward-compatible for one
release, so the previous image can read a ledger the current one migrated.

### Gateway mode (Sixto gateway)

With `telegram_mode: gateway` the watchdog no longer holds the bot token. The
Sixto gateway (`t6-sixto-gateway`) is the only `getUpdates` caller; the watchdog
reads its routed intake items from the gateway's `GET /updates`, posts ops
messages with `POST /notify` and reactions with `POST /react`. The worker, ledger
and core are unchanged, and the deadman, heartbeat and ops outbox stay here.

```yaml
telegram_mode: gateway
gateway_url: "http://192.168.88.151:8780"
```

In `.env`, set `WATCHDOG_SECRET` (the same value as the gateway's
`WATCHDOG_SECRET`); `TELEGRAM_BOT_TOKEN` is not required. Boot is refused if
`gateway_url` or `WATCHDOG_SECRET` is missing. `direct` (the default) is
unchanged.

- In gateway mode the ledger's offset holds the gateway's `seq`, not a Telegram
  update id. `last_committed_offset` is the last `seq` committed and `next_offset`
  is one more (0 on a ledger that has never committed), so the poll asks `GET /updates?after=<last_committed_offset>`
  (that is, `next_offset - 1`).
- An ops post carries `idempotency_key = watchdog-outbox-<outbox row id>`. A
  retry of the same row reuses the key, so the gateway queues it once. The
  outbox keeps the returned `message_ref` in `gateway_ref`, apart from the real
  Telegram message id (#109).
- `in_scope` still filters intake as a safety net.

**Cutover (once).** The cursor moves from Telegram update ids to gateway
sequence numbers, so reset it while the watchdog is stopped:

```sh
python -m jo_watchdog.app reset-offset --to 0 --config config.yaml
# in the container: docker compose run --rm watchdog python -m jo_watchdog.app reset-offset --to 0
```

The gateway re-delivers its items from the start; the ledger's unique
`telegram_key` and edit checks skip anything already captured. The full runbook
(deploying the gateway, copying the ledger) is section 12 of the gateway spec,
mclbautista/t6-sixto-gateway#1.

**Backfilling refs stored before #109 (once).** Rows written in gateway mode before
#109 stored the gateway ref as the Telegram message id; migration 10 moved the ref to
`gateway_ref` and left those rows with no id, so they never produce a backlink. This
command asks the gateway what each ref became. It needs `telegram_mode: gateway` and
`WATCHDOG_SECRET`, and it is a dry run unless `--apply` is given:

```sh
python -m jo_watchdog.app backfill-gateway-refs --config config.yaml          # dry run
python -m jo_watchdog.app backfill-gateway-refs --config config.yaml --apply  # writes
```

It prints a table of ref, real Telegram id and outcome (never message text or links).
With `--apply`, `sent` refs get their real id; `failed` and unknown (404) refs have their
id cleared and the row marked `failed` (no alert, no re-send); `queued` refs are left as
they are. Rows from direct mode, and rows the worker already resolved, are never touched.
A second run changes nothing. Exit code 1 means some lookups errored: run it again.
Running it against production needs the owner's approval (#108).

**Switching back to direct mode.** Follow "Rollback after cutover" in the gateway
README; do not improvise it. In short: turn gateway polling off, let the watchdog drain
the gateway (`GET /updates?after=<last committed offset>&timeout=0` returns no items),
read the gateway's `poll_state` as `T`, stop the watchdog and run
`reset-offset --to T`, stop the gateway, then start in `direct` mode with
`TELEGRAM_BOT_TOKEN` back in `.env`.

- Run `reset-offset` with a gateway-capable image (the current one); older images
  have no such command. Starting an image from before gateway mode afterwards is
  possible, but it rejects `telegram_mode` and `gateway_url` as unknown keys, so
  delete those lines rather than setting `direct`.
- The offset must be reset. Gateway `seq` numbers and Telegram update ids are separate
  counters with no order between them, so a leftover `seq` can be higher than the next
  Telegram id and skip it.
- Reset to `T`, not `T + 1`: the ledger stores the last committed id and `next_offset`
  adds one, so `--to T` makes the next poll ask for `T + 1`.
- Do not count on Telegram re-delivering what the gateway fetched: it has acknowledged
  those updates. Anything the watchdog could not drain must be entered by hand.

### Dry-run (rollback control)

`dry_run: false` is the default. Setting `dry_run: true` and restarting stops
each new message at `planned`: the masked plan goes to the ops topic, and
nothing is written to Notion and no reaction is set. Setting it back to `false`
resumes the held plans exactly as stored.

### Deliverable linking

Deliverable linking is a separate, staged switch. `deliverable_link_enabled`
stays `false` (Jev runs in shadow mode and its decisions are kept, but no
relation is written) until the owner turns it on, independently of capture
going live.

## Known gap: outages longer than 24 hours

Telegram keeps undelivered bot updates for 24 hours. If the watchdog is down
longer than that, posts from the start of the outage are lost to it and must
be entered by hand. There is no external uptime service: **a missing 07:00
heartbeat in the ops topic is the signal** that the watchdog is down, and
whoever notices should restart it and check the intake topic for posts it
missed.

## Extraction report (DeepSeek, not a CI gate)

```sh
DEEPSEEK_API_KEY=... python -m jo_watchdog.app.extraction_report --config config.yaml --save run.json
python -m jo_watchdog.app.extraction_report --from run.json   # re-score offline
```

Scores the held-out corpus Job Orders (every one not used as a few-shot
example) field by field against `tests/fixtures/extraction_golden.json`.

## Smoke test (real accounts)

```sh
python smoke.py --config config.yaml                    # access, schema, models, pipeline
python smoke.py --config config.yaml --live-telegram --live-message-id 12345  # one real post
python smoke.py --config config.yaml --only reactions   # a single step
python smoke.py --config config.yaml --forward-fixtures fw.json --reaction-message-id 12345
```

Each step prints PASS, FAIL, SKIP, MANUAL or ATTESTED. Only a run where every
required step is PASS or ATTESTED reports FULL VERIFICATION and exits 0; a
failure exits 1, and skipped or manual required steps (or `--only`) exit 3.
Record hand-checked steps with `--attestations` (actor, time, scope, evidence).
See the docstring in `smoke.py` for fixture selection, side effects and cleanup.

The operational scenarios (`readback_mismatch`, `outbox_retry`, `heartbeat_format`,
`matcher_failure`, `shadow_mode`, `dry_run_scenario`) run the production worker against
scripted stand-ins on a throwaway ledger with a fake clock: no secrets, no network, no
Notion rows. They are also covered offline by `tests/test_smoke_scenarios.py`. Adding
`--live-telegram` relays their rendered ops posts to the real ops topic (delete them by
hand); that is the only opt-in, and the only thing they leave behind.

See `docs/ledger-schema.md` for the versioned ledger contract and
`docs/runbooks/orphan-call-reconciliation.md` for the orphan-call runbook.
There are no ADRs yet (`docs/adr/` does not exist). Design sources live in `map/`
(mirrors of the closed design issues #1–#17). The PRD (#28) and the build
tickets (#20–#27, #39, #99), all closed, are mirrored in `docs/issues/`.
