---
label: wayfinder:ticket
title: Technical design and architecture
status: closed
---

# Technical design and architecture

Closes the design phase. Every decision below is if-then reasoning tied to a named principle (see `map.md` Notes), settled by grilling the owner (Cyril) round by round. Where a decision amends an earlier closed ticket, the amendment is also logged in `amendment-log.md` — this file is the current truth, the older ticket files are history.

## 1. Topology: one Python process, no n8n in the write path

**If** the closed tickets require one writer path to Notion, a SQLite ledger as the sole dedupe authority, and masking before every exit, **then** splitting the pipeline across n8n's execution store and Python would fragment the one property that makes the system safe: a single place that knows whether a message has been captured. So:

- A single Python 3.12 process, one container, asyncio-based.
- `core/` (masker, deadline parser, header/name builder, person+project resolver, verification checks, plan builder) is pure — no I/O, no network, no Notion or Telegram imports. This is what makes it wrappable in an n8n Code node later without a rewrite.
- `adapters/` holds all I/O: Telegram, Notion, DeepSeek, Jev.
- `ledger/` holds the SQLite schema, migrations, queue, offsets, and heartbeat/outbox logic.
- `app/` wires it together: the worker loop, config, ops reporting.
- n8n is the **builder's** tooling only (Hermes Agent + the Matt Pocock coding workflow), never part of the runtime in v1. `core/` exposes a small CLI (`python -m jo_watchdog.core <parse|mask|resolve> --stdin` → JSON) so an n8n Execute Command or Code node can call the same logic unchanged if the owner adds n8n later. The ledger schema is documented and versioned so other tools (n8n or otherwise) can read it safely; nothing outside this process ever writes to Notion for a JO the watchdog owns.

Principle: **Own your dedupe key**. Corollary: **A queue you can't inspect isn't a safety net** — the ledger is a plain, documented SQLite file precisely so it stays inspectable by tools that aren't this process.

## 2. Repository: standalone now, versioned contract for later reuse

**If** the bind-mounted SQLite was chosen so other tools (including the existing `t6-operations-control-plane` / OCP repo) could open it directly, **then** the ledger schema needs to be a published contract, not an implementation detail, even though this ships as its own repo (`jo-watchdog`), independent of OCP's deploy cycle and blast radius.

- Standalone repo.
- `schema_version` table plus a documented column list per table (see §9) is the contract. If OCP or anything else wants to consume Job Orders, it reads the ledger read-only against that contract.

Principle: **A queue you can't inspect isn't a safety net**.

## 3. Language, libraries and adapters

| Concern | Choice | Why |
|---|---|---|
| Runtime | Python 3.12, one asyncio process | Matches the builder's existing stack (task bot, OCP) |
| Telegram | `aiogram` 3.x, but only as a typed API client (`Bot`) — **not** its `Dispatcher** | Dispatcher advances Telegram's `getUpdates` offset on its own schedule; our crash-safety rule ("ledger commit, then offset advance") requires *us* to control the offset. aiogram still gives typed `Message`/forward-origin models, `setMessageReaction`, and built-in 429 `Retry-After` handling. |
| Notion | Hand-written adapter over `httpx`, pinned `Notion-Version: 2026-03-11` | Only ~6 endpoints are needed (query data source, retrieve schema, create page, retrieve page, PATCH). A third-party client that lags the data-source API is a bigger risk than a 100-line adapter. Request/response models are Pydantic v2, informed by (not dependent on) `notion-py-client`'s reference shapes. |
| Classify/extract LLM | DeepSeek, via the OpenAI-compatible SDK (`base_url=https://api.deepseek.com`), JSON mode + Pydantic `model_validate` | Replaces Jev/Laya for this call (see §5). JSON mode only guarantees valid JSON, not schema — schema enforcement is therefore done in code, and **empty content** (a documented DeepSeek quirk) is treated as one more classifier-failure case, not a new mechanism. |
| Deliverable matcher LLM | Jev (`jev-latest`), typed Choice questions | See §5 and §6. |
| Queue | Built on the ledger itself — `messages` + `stage_results` tables, `BEGIN IMMEDIATE` claim | `litequeue`/`persist-queue`/`huey` each want their own store; the retry schedule and per-stage outputs need to live in the *same* inspectable file as the rest of the ledger. |
| Deadline phrases | Hand-rolled regex/phrase-table parser, not `dateparser` | The corpus's phrase set is small and closed; `dateparser` fills in missing parts by design, which conflicts with "blank is a fact, filler is a fabrication." Fails closed: unparseable → blank. |
| Validation | Pydantic v2 throughout (extractor schema, matcher schema, config) | Schema is the contract, enforced identically everywhere. |
| Fuzzy name match | `rapidfuzz` | Guarded last step in person resolution (§8). |
| Storage | stdlib `sqlite3`, WAL mode, hand-written SQL + numbered migrations | ~6 tables; an ORM adds nothing at this size. |

Principles: **Schema is the contract**, **Reproducible by default** → refined to **traceable by default** for the two LLM calls (see §5).

## 4. Telegram ingestion and crash safety

**If** a crash can land between "Telegram confirms the offset" and "the message is durably queued," **then** a naive polling loop can lose a message forever (Telegram's 24h retention won't save it once the offset has moved past it). So:

- One asyncio task polls `get_updates(offset=last_committed_offset + 1, allowed_updates=[...], timeout=...)`.
- Each returned update is written into `messages` (ledger) **inside a transaction**, and only after that transaction commits does the in-memory/`offsets` table's `last_committed_offset` advance.
- A second asyncio task drains the queue (claims `received` rows, runs the pipeline).
- The scope filter (chat id + configured topic thread id) runs in the poller, before anything touches the ledger, except an `ignore_log` line for out-of-scope traffic.
- A deadman task: the poller stamps a heartbeat timestamp every cycle; if it goes stale past 10 minutes, the process exits non-zero and `restart: unless-stopped` brings it back. Durable queue + durable offset means a restart costs nothing.

Principles: **Idempotent writes**, **Scope at the filter, not the grant**, **Silence is a failure mode**.

## 5. Two LLM providers, two different interfaces

**If** DeepSeek and Jev solve different problems — free-text extraction vs. a bounded decision over a known candidate set — **then** they get two separate adapters with two separate protocols, not one shared "complete_json" call.

- **DeepSeek** (classify/extract): unchanged in spirit from the original classifier ticket — one structured call, verdict + confidence + reasons + verbatim raw fields, roster-blind, no today's-date, no reply chain. JSON mode + Pydantic validation. Invalid JSON, a schema violation, or empty content all count as one classifier-failure case.
- **Jev** (Deliverable matcher, redesigned — see §6): Jev is a **System One model** — it returns typed Choice/Score/Noul decisions with calibrated probabilities, not generated text. It cannot echo a candidate's name verbatim (the original matcher design assumed a generative model and is superseded). The **Laya bake-off is dropped**: the owner has chosen Jev directly as the System One model, no comparative eval for model choice.
- **No pinning, "latest" always.** The owner explicitly does not want to maintain model-version pinning. So: **Reproducible by default** is relaxed to **traceable by default** — every ledger row for an LLM call stores the model id the API actually returned (not just the alias requested), so nothing is silently unaccountable even though nothing is pinned. A daily heartbeat line, `Model changed: <old> → <new>`, appears only when a returned model id differs from the previous day's — otherwise it's silent. Temperature stays 0.

Principles: **Resolve, don't generate**, **Reproducible by default** (redefined as traceable), **Decide by eval, not by reputation** — explicitly and knowingly waived for model *selection* (see §6), never for the matcher's *link/hint* decision.

## 6. Deliverable matcher, redesigned for Jev's typed interface

**If** Jev has no free-text output, **then** the matcher can't ask it to echo names and write a reason — it has to be phrased as a bounded decision. So, per JO with a resolved project:

- **State**: masked message text + the resolved project's name.
- **Questions**: one Choice question per candidate Deliverable (queried live against `🗺️ Projects`, not the 6-hour cache, no Status/Class filter, skipped entirely if the project has zero Deliverables), keyed by the candidate's page id, options `match` / `maybe` / `no`.
- **Decision from probabilities** (in code, not by Jev): link if `match` is top choice and its probability ≥ `T_link`; hint if `match + maybe` ≥ `T_hint`; otherwise silent. Both thresholds are config values, starting conservative (`T_link` = 0.90, `T_hint` = 0.50) since there's no calibration data yet.
- No name echo, no name-normalization step, no free-text `reason`. The ops hint format changes from "Reason: `<sentence>`" to "Match probability: `0.62`".
- Duplicate-name ambiguity (two candidates sharing a normalized name) still can't be linked automatically — it hints instead.
- Only `match` is linked; `maybe` and ambiguous cases post an ops hint (no tag); a plain no-match or unresolved project is silent, counted in the heartbeat.
- Zero-to-many links per JO, each candidate validated independently. No backfill — the row is written once.
- Matcher failure: retry once inline, then write the row with a blank Deliverable and post one ops line ("Deliverable match skipped: matcher unavailable"). Never blocks the write.
- **Shadow mode**: the matcher runs on every JO (ledger + ops hints) but links nothing until `deliverable_link_enabled` (config, default off) is flipped on by the owner after reviewing accumulated hints against real traffic. This replaces the abandoned labeled-30-JO eval as the acceptance gate — an **explicit owner waiver** of "decide by eval," recorded here and in the amendment log so a future reader knows it was a decision, not an omission.
- The fixed 3 real Deliverable pairs (Moonglow JO #32, SOMSOM #0155, Mag-Iina JO 30) remain unit-test regression fixtures regardless of the eval waiver.

Principles: **Resolve, don't generate**, **Precision over recall when a human can fill the gap**, **Annotate, don't gate**, **Never grade on the training set** (the few-shot/held-out split stays as a costless practice even though nothing gates on it).

## 7. Pipeline state machine and the Notion-write crash window

**If** caches refresh every 6 hours, **then** resuming a retry by *recomputing* could produce a different answer than the first pass — so a retry must resume from a stored **result**, not from re-running pure logic against possibly-changed inputs.

States: `received` → `extracted` (DeepSeek output stored) → `planned` (the exact Notion payload — every property, resolved page ids, deadline datetime, matcher outcome, body blocks — stored) → `writing` → `written` (Notion page id stored) → `done`.

- Masking, deadline parsing and resolution are pure and cheap, but their *result* is what's stored in the plan; a retry after a cache refresh replays the same payload.
- **The `writing` state closes the crash window.** Notion has no documented idempotency header. Before the POST, the message moves `planned` → `writing`. If the process restarts and finds a `writing` row, the outcome is unknown: it queries Notion on `Telegram Key = chat_id:message_id` (only after the first backoff delay, ≥30s, which sidesteps unverified query-freshness). A found row is adopted (→ `written`); none found → create. If two rows for one key are ever found, read-back detects it and posts an ops line — it never deletes or merges.
- Normal-path read-back is a retrieve-by-id, field-by-field against the plan. A mismatch posts an ops line, never rewrites the row.

Principles: **Idempotent writes**, **Own your dedupe key**, **Annotate, don't gate**.

## 8. Person resolution: alias table lives in Notion, with a guarded fuzzy step

**If** a new hire or nickname needs to be usable without a code deploy, **then** the alias/shift table belongs where the coordinator already works (Contacts), not in a YAML file. Verified live against the Contacts data source (`collection://77b2a701-71db-4e88-a694-37f07303e77e`, fetched 2026-09-29):

- `Aliases` — Text, comma-separated.
- `End of Shift` — Select, 24 hourly options (`12:00 AM` … `11:00 PM`) + `Flexible / Variable`.
- `TelegramHandle`, `TelegramUserID` — already existed.

Both new columns are read by the existing 6-hour People cache; no new cache mechanism.

**Match order**: TelegramUserID → TelegramHandle → alias (split on commas, trimmed, casefolded, `ate`/`kuya`/`sir` stripped from both sides) → **fuzzy on the contact's page title** (pages are named after the person). Fuzzy is the first step that can pick the wrong person, so it is bounded:
- Only runs after the three exact steps miss.
- Never applies to an `@handle` mention (exact or nothing).
- Requires score ≥ 90 **and** a lead ≥ 8 points over the runner-up (both in config); otherwise it's a miss.
- A fuzzy-only link still writes (not blocked), but posts one untagged ops line ("matched by name: '`X`' → Contact `Y`"), visible the same day.
- One alias string matching more than one contact is a miss (blank relation, ops report, Priya tagged), never a guess — same rule as exact-alias collisions.
- Corpus becomes a `rapidfuzz` test fixture.

Principles: **Precision over recall when a human can fill the gap**, **Schema is the contract**, **Annotate, don't gate**.

## 9. End of Shift deadline resolution — always calculable

**If** one of the project's goals is Job Order Health, which feeds Deliverable Health and then Project Health (**Projects are built from Deliverables which are built from Job Orders**), **then** a blank Deadline breaks that chain at the bottom, so shift-based deadlines are designed to always resolve to *something* usable, at the cost of not always being provably a stated fact.

- A shift-phrase deadline ("end of shift") resolves to **the next occurrence of that hour, at or after the posting time, in Asia/Manila.** (A 10:00 post to someone ending `7:00 PM` → 19:00 today. A 22:00 post to someone ending `5:00 AM` → 05:00 tomorrow.)
- With several assignees, take the **latest** resolved datetime.
- **If an assignee's `End of Shift` is blank or `Flexible / Variable`, or if no assignee resolves at all, the fallback is 23:59 on the posting day** — the same default already used for a bare stated date — so Deadline is always calculable for the Health rollup. Every such default posts one ops line naming who was defaulted (**Never drop silently**); the raw phrase still goes verbatim to Notes.
- This is *not* the "never roll a stated deadline forward" rule the deadline ticket protects — that rule is about deadlines the poster actually wrote down. A shift end (or its 23:59 fallback) is a **derived** value by definition, so deriving "the next occurrence" or defaulting to 23:59 doesn't violate it. The amendment log records this distinction explicitly.
- `asap` keeps the same shift-based logic and its high-priority flag. `tonight` stays 23:59 on the posting day, unchanged.

Principles: **Resolve, don't generate**, **Precision over recall when a human can fill the gap**, **Never drop silently** — with **Blank is a fact, filler is a fabrication** knowingly traded off here, in service of the Job Order → Deliverable → Project Health chain, and made honest by the mandatory ops line.

## 10. Durability of side effects

- **Ops posts**: durable, via an `outbox` table. The outbox row is created in the *same SQLite transaction* that sets `written`, so a crash can't separate a captured JO from its pending reports. Delivery is at-least-once with the existing 5-attempt backoff; a duplicate ops post after a crash is acceptable and traceable by outbox id.
- **Reaction**: best-effort (inline retry, counted in the heartbeat as before) — unchanged from the earlier ticket.
- **Caches** (People, Projects, Departments): persisted as `cache_snapshot` rows, loaded at startup before the first live refresh. A refresh failure keeps serving the previous snapshot (ops line only if >24h stale — a normal miss). An **empty** cache (nothing ever loaded) instead **blocks planning** — held at `extracted` without spending retry attempts — because an empty cache would silently turn every JO into a false miss, which is a different failure than a stale one.

Principles: **Never drop silently**, **Blank is a fact, filler is a fabrication**.

## 11. Departments read live from Notion

Confirmed (was pending owner OK): Department options are read from the live Contacts/Job-Orders schema on the same 6-hour cycle as People and Projects, no hard-coded list. A token that doesn't match a live option leaves Department blank and the ops report names the token. The only hard-coded knowledge is a tiny header-parser alias map (e.g. "TO" → "TOC") in config, not code.

## 12. Heartbeat and outage detection

- **07:00 Asia/Manila**, covering the previous 24 hours, labeled with the date range — lands just after night-shift handover; a missing heartbeat is itself the outage signal.
- Heartbeat is a SQL query over the last day's ledger rows (no separate counters table to drift out of sync): captured, flagged, ignored (with first lines), passwords withheld, 4 Deliverable-matcher lines (linked/hinted/none/skipped), reaction failures, model-changed line (if any).
- **Outages beyond 24 hours** (Telegram's retention window) are a **documented, accepted gap**, not solved in v1 — detection is the ops-topic's three human members noticing the missing 07:00 heartbeat. Revisit only if it actually bites; an external uptime check would add an account the owner has said he doesn't want to maintain.
- **LLM usage/cost visibility**: token counts from both DeepSeek and Jev responses are stored per call (two integer columns, no new code path). No dashboard, no heartbeat line — queryable on demand via `SELECT`. At ~12 JOs/day the cost is negligible; this is a deliberate non-feature, not an oversight.

Principle: **Silence is a failure mode**, accepted as the detector for the one gap above.

## 13. Config, secrets and logging

- `config.yaml` (non-secrets, Pydantic-validated at startup — a bad value fails the boot, not a message): intake chat/topic ids, ops chat/topic ids, Priya's Contacts page id, the owner's real Telegram handle, `T_link`, `T_hint`, fuzzy score/lead thresholds, heartbeat time/timezone, cache interval, retry schedule, `deliverable_link_enabled`, `dry_run`.
- `.env`: Telegram token, Notion token, DeepSeek key, TypeSafe (Jev) key.
- Logging: JSON lines to stdout + a rotated file next to the ledger. Fields: message key, state, stage, attempt, duration, returned model id. Every log line passes through the masker first — raw text never appears in logs, only the masked first line.

Principles: **Schema is the contract**, **Mask at the boundary, keep the source**.

## 14. Test strategy

1. **Pure unit tests in `core/`** — the 77-JO corpus as fixtures with golden outputs for masker, deadline parser, name builder, resolver; any unparseable string yields blank.
2. **Adapter contract tests** — recorded fixtures (Notion schema/pages captured by Hermes over the Notion MCP; Telegram update fixtures including all four forward-origin types; recorded DeepSeek and Jev responses). No network in CI.
3. **Crash-injection tests** — real SQLite, fake adapters, fake clock. Kill the process at every state boundary and after every external call; the invariant under test is exactly one Notion row per Telegram key after recovery.
4. **`smoke.py`** — run by hand against real accounts, works through the accumulated smoke-test checklist (§16). Not part of CI.

## 15. Deployment and rollout

- `python:3.12-slim`, non-root user. Compose: `env_file`, bind-mounted `./data` (dir 0700), `restart: unless-stopped`, `HEALTHCHECK`.
- Deploy: `git pull && docker compose up -d --build`. Rollback: previous image tag.
- Migrations: forward-only, backward-compatible for one version (add columns first, drop a release later) so a rollback never meets an unreadable schema.
- **Straight to live** — no dry-run phase. `dry_run: true` stays in config as an off-by-default rollback tool (stops at `planned`, posts the plan to ops, no Notion write, no reaction), available if needed but not the default path. The Deliverable matcher's own `deliverable_link_enabled` flag is the real staged rollout mechanism for that one risk.

## 16. Build-ticket order (tracer bullets, not layers)

See `build-tickets.md` for the full breakdown. Order:
1. Walking skeleton (one message → ledger → plan → write → read-back → reaction, real accounts, thin slice)
2. Masker + corpus fixtures
3. DeepSeek classify/extract
4. Name/JO-number/department/deadline building (pure `core/`, can run parallel to #3)
5. People/Projects/Departments caches + resolver (incl. Contacts `Aliases`/`End of Shift`, fuzzy step)
6. Verification checks, outbox, five locked ops formats
7. Forwards, edits, duplicates, reuse
8. Reliability (backoff, `writing` reconciliation, deadman, 07:00 heartbeat)
9. Jev matcher in shadow mode, behind `deliverable_link_enabled`
10. Docker packaging, `dry_run` mode, `smoke.py`

## 17. Accumulated smoke-test checklist

Unknown Notion select names · query freshness after write · `Retry-After` on 429 · channel-origin forward link check against a real forwarded post · pick the "captured" reaction emoji via `getAvailableReactions` in the real group · resolve the owner's real Telegram handle · secrets-masker unit tests (`PW:` line masks; "dl with pw" / "DL w pw, 14 days expiry" / "With password" don't) · share Deliverables data source with the integration · `relation_contains` on `🗺️ Projects` incl. a just-created Deliverable · confirm writing `Deliverable` from the JO side fills the Deliverable's `Job Orders` side · Jev structured-output behavior against the real API · reading the new Contacts `Aliases` and `End of Shift` columns · fuzzy-match thresholds against the corpus.

## Amendments this ticket makes to earlier closed tickets

Full detail in `amendment-log.md`. Summary: Laya removed as a model option; DeepSeek added as the classify/extract provider; the Deliverable matcher redesigned around Jev's typed Choice interface (no name echo, probability-based hint, shadow-mode rollout replacing the labeled-eval gate); the person alias/shift table moved from an abstract "alias table" into two new Contacts columns, with fuzzy matching added; End-of-Shift deadline resolution defined precisely, including the always-23:59-fallback amendment to "blank is a fact"; departments-from-Notion confirmed; the ops heartbeat and hint formats updated again (model-changed line, probability line); go-live changed from a staged dry-run to straight-to-live.
