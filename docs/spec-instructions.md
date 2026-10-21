# Instructions: write the JO Watchdog build spec

You are writing `docs/SPEC.md`: one self-contained, build-ready technical specification for the JO Telegram Watchdog. An implementing agent (Hermes Agent running the Matt Pocock coding workflow) must be able to build the whole system from `SPEC.md` alone, without opening the design history and without coming back with questions.

The design phase is closed. Your job is **assembly and reconciliation**, not new design. Every decision already exists somewhere in `map/`, in the PRD (GitHub issue #28) or in the code already merged; it is scattered across 17 design tickets, several of which were later amended. Where the sources genuinely leave something open, you do not invent an answer: you list it under "Open questions for the owner" (section 4 below) with a recommended default.

**Where the build stands.** Build tickets 1 (walking skeleton, issue #18) and 2 (credential-safe capture, issue #19) are merged. Tickets 3–10 are open as issues #20–#27, labelled `ready-for-agent`, under the PRD #28. `SPEC.md` is the technical companion to #28: the PRD says *what* and *why*; the spec pins down every concrete *how* (schemas, formats, rules, tables, thresholds, fixtures) that the PRD leaves at sentence level. Where the merged code already decides something (the ledger tables in `jo_watchdog/ledger/migrations.py` and `docs/ledger-schema.md`, the plan and payload shapes in `jo_watchdog/core/`, the config model in `jo_watchdog/app/config.py`), the spec describes what exists and extends it; it does not redesign it. If code and design disagree, record the difference in Appendix C instead of silently picking one.

## 1. Source material and precedence

Design sources are in `map/` (the same documents are mirrored as closed GitHub issues #1–#17). When two sources disagree, the higher one wins:

1. `map/technical-design-and-architecture.md` — the current truth. Closes the design.
2. `map/amendment-log.md` — lists every older statement the design ticket supersedes. Read it before you read any older ticket, and keep it open while you do.
3. PRD issue #28 and build issues #20–#27 — the requirement-level restatement, written after the design closed. Where #28 is more specific than the design tickets, it wins (it already settles several items in section 4). Where #28 and the design directly contradict each other, do not pick one: record both in Appendix C. No direct contradiction was found when these instructions were written.
4. Merged code and `docs/ledger-schema.md` — authoritative for what already exists.
5. `map/map.md` — destination, standing principles, verified facts about the live Notion schemas, decisions summary, out-of-scope list.
6. `map/build-tickets.md` — the build order (tracer bullets). Carry it into the spec unchanged in substance.
7. The topic tickets in `map/tickets/`, which are history where amended and still authoritative for detail that was never amended:
   `telegram-bot-api-capabilities.md`, `notion-write-path.md` + `notion-write-path-findings.md`, `job-order-field-mapping.md`, `sample-corpus-redacted.md`, `reliability-and-operations.md`, `resolve-people-and-projects.md`, `deadline-phrases.md`, `forwarded-job-orders.md`, `reposts-duplicates-idempotency.md`, `classifier-and-extractor-design.md`, `verification-and-ops-reporting.md`, `ops-topic-mockups.md`, `secrets-in-raw-text.md`, `deliverable-matching.md`.

### How `SPEC.md` relates to PRD #28

#28 is the behavior contract: 118 numbered user stories (US-1 … US-118), 16 numbered Implementation Decisions (ID-1 … ID-16), Testing Decisions and an Out of Scope list. Each build issue #20–#27 carries its own acceptance criteria. `SPEC.md` must not restate #28 in different words, and must not change any acceptance criterion in the build issues. It adds the layer #28 deliberately leaves at sentence level:

| #28 section | What #28 already says | What `SPEC.md` must add |
|---|---|---|
| ID-2 Runtime | one process, pure core, SQLite holds ledger/caches/traces/outbox | module boundaries, `core` CLI JSON contracts, every ledger table and column (R1) |
| ID-3 Telegram | admin of intake group, own the offset, filter, edits, reaction | `allowed_updates`, fields read per update, link formats, deadman mechanics, reaction lookup call (R11) |
| ID-4 DeepSeek | one call, three verdicts, failure = retry | Pydantic schema field by field, prompt structure and tag, few-shot/eval split (R15) |
| ID-5 Masker | labels, span, placeholder | regex spec, test case table |
| ID-6 Field mapping | rule per property in one sentence | per-property table with corpus examples, header pattern table, Name format (R6), Notes layout (R4), body block layout |
| ID-7 Resolution | match chain, thresholds | cache snapshot format, normalization functions, ambiguity rules as code-level steps |
| ID-8 Deadlines | phrase families | full corpus deadline table, date+shift (R2), unlisted phrases and year inference (R3) |
| ID-9 Jev | Choice per candidate, thresholds, shadow mode | adapter interface and wire format (R8), stored trace fields |
| ID-10 Verification/ops | nine checks, tagging, one report per capture | exact trigger conditions (R9, R10), verbatim templates incl. the combined report (R5) |
| ID-11 State machine | states, reconciliation, read-back | transition table, columns touched per transition, field-by-field read-back comparison rules |
| ID-12 Retries/heartbeat | schedule, deadman, heartbeat lines | attempt accounting per stage (R12), heartbeat SQL and verbatim template |
| ID-13 Notion | adapter, limits, smoke concerns | endpoint list, error-code table, splitting algorithm |
| ID-14 Config/logging | which settings exist | full config schema with types, defaults, validation; env var names (R16); log line schema |
| ID-15 Deployment | image, mounts, dry-run | Dockerfile/Compose requirements, HEALTHCHECK definition, setup runbook |
| Testing Decisions | four seams and their assertions | fixture layout, which corpus messages are golden/few-shot/eval, recorded-fixture inventory |

Every requirement in `SPEC.md` cites the US and ID numbers it implements, e.g. `(US-48–52, ID-8)`, so a reviewer can check coverage both ways.

Known superseded statements you must **not** carry forward (full list in the amendment log): Jev-vs-Laya bake-off; Laya at all; pinned model versions; the generative name-echo Deliverable matcher with a `reason` sentence; the 30-JO labeled eval as the matcher's launch gate; the day(7pm)/night(5am) shift model; "no assignee resolves → Deadline blank" for shift phrases; Deliverable "blank in v1"; a staged dry-run go-live; "Laya/Jev" as the classify/extract provider.

## 2. How to work

1. Read the sources in the precedence order above. Build a scratch list of every concrete rule, value, name, threshold and format you find, tagged with its source file and section.
2. For each item, check the amendment log. Drop superseded items; keep the current one.
3. Write `SPEC.md` using the outline in section 3. State rules as normative requirements ("MUST", "MUST NOT", "if X then Y"). Keep the one-line **principle** next to each rule that has one (principles are listed in `map.md` Notes); the implementer needs the *why* to handle edge cases correctly.
4. Copy names exactly: Notion property names (including `Blocker ` with its trailing space, `🗂️ Coordinator`, `Source / Source Drive`, `🗺️ Projects`, `Telegram Key`, `Pending`), data source ids, option names, config keys, ops message text.
5. Do not paraphrase the locked ops-topic message formats. Reproduce them verbatim, then apply the amendments to them.
6. Resolve the reconciliation items in section 4. For each, either cite the source that settles it or move it to the spec's "Open questions for the owner" appendix with a recommended default the implementer uses until the owner answers.
7. Run the acceptance checks in section 5 before you finish.

Do not include secrets, tokens or real Telegram chat ids. Use only `map/tickets/sample-corpus-redacted.md` and `tests/fixtures/redacted_corpus.json` as the corpus. **Never read from or quote `old_files/sample-corpus.md`**: it is the unredacted original and contains a real transfer password.

Read GitHub issues with `gh issue view <n> --comments` (see `docs/agents/issue-tracker.md`).

## 3. Required outline for `SPEC.md`

Each section lists what it must contain and where to find it. Every bullet is a required detail, not a suggestion.

### 3.1 Purpose and scope
- Destination paragraph and the Job Order → Deliverable → Project Health rationale (`map/map.md` Destination).
- In scope for v1: text messages and forwards from one intake topic; one-way capture; status always `Needs Intake Review`.
- Out of scope: copy #28's "Out of Scope" list verbatim. It is a superset of `map/map.md`'s list; it adds, among others, no automatic merging or splitting, no creating Deliverables, no backfilling relations, Coordinator never following the poster, no other process writing watchdog rows, and the list of events that get no per-message alert.
- Volume assumption: ~10–12 JOs/day; cost and latency are not constraints.

### 3.2 Glossary and principles
- Terms: JO, intake topic, ops topic, Telegram Key, ledger, plan, outbox, heartbeat, shadow mode, Leizette (traffic coordinator, standing cc), owner.
- The standing principles from `map.md` Notes, one line each, including the two refined ones (Reproducible → traceable; Blank is a fact, with the shift-deadline exception).

### 3.3 Architecture
- One Python 3.12 asyncio process, one container; n8n not in the runtime (design §1).
- Package layout: `core/` (pure, no I/O: masker, deadline parser, header/name builder, person+project resolver, verification checks, plan builder), `adapters/` (Telegram, Notion, DeepSeek, Jev), `ledger/` (schema, migrations, queue, offsets, heartbeat/outbox), `app/` (worker loop, config, ops reporting). Package name `jo_watchdog`.
- The `core/` CLI: `python -m jo_watchdog.core <parse|mask|resolve> --stdin` → JSON. Define the input and output JSON for each subcommand.
- Library table from design §3 (aiogram 3.x as `Bot` client only, never `Dispatcher`; hand-written `httpx` Notion adapter, `Notion-Version: 2026-03-11`; OpenAI-compatible SDK for DeepSeek with `base_url=https://api.deepseek.com`, JSON mode; Pydantic v2; `rapidfuzz`; stdlib `sqlite3` in WAL mode with hand-written SQL; hand-rolled deadline parser, not `dateparser`).
- A diagram or numbered flow of one message from poll to reaction.

### 3.4 Telegram ingestion
- Bot setup: admin of the intake group only; plain member of the ops group with post permission (reliability ticket).
- Long polling loop that owns the offset: write update to ledger in a transaction, commit, then advance `last_committed_offset` (design §4). `allowed_updates` must include `message` and `edited_message` (edits are received, see reposts ticket).
- Scope filter on `chat_id` + `message_thread_id` in the poller; out-of-scope traffic gets only an `ignore_log` line.
- Fields read from each update: text or caption, sender, date, `message_thread_id`, `forward_origin` (all four types and their fields, from `telegram-bot-api-capabilities.md` §3), entities for @mentions with user ids.
- 429 `retry_after` handling.
- Deadman: heartbeat stamp every poll cycle; stale > 10 minutes → exit non-zero.
- Reaction: one fixed "captured" emoji, set only after a confirmed write **and read-back** (ID-3), never swapped or removed to signal a problem; inline retry once or twice (seconds), then count the failure for the heartbeat.
- Message link formats for the intake message and for channel-origin originals (`t.me/c/<id>/<msg_id>` private, `t.me/<username>/<id>` public).

### 3.5 Ledger (SQLite) — the published contract
Start from `docs/ledger-schema.md` and `jo_watchdog/ledger/migrations.py`, which define `schema_version`, `messages` (v2), `offsets` and `ignore_log`. Keep those exactly, then specify every later migration the remaining tickets need (see R1). The spec must define:
- File location (`./data` bind mount, dir 0700, db file 0600), WAL mode.
- Every table with every column, type, nullability, key and index: at least `schema_version`, `messages`, `stage_results`, `offsets`, `outbox`, `ignore_log`, `cache_snapshot`, plus whatever stores LLM call traces (model id requested and returned, prompt-version tag, masker version, token counts in/out, candidate snapshot for the matcher) and heartbeat/deadman state.
- Pipeline states and allowed transitions: `received` → `extracted` → `planned` → `writing` → `written` → `done` (design §7), plus terminal/side states for `ignored` (`not_jo`, `no_text`, out-of-scope), `failed` after 5 attempts, and "held at `extracted`" for an empty cache.
- Claim semantics (`BEGIN IMMEDIATE`), attempt counter, `next_attempt_at`, retry schedule 30s → 2m → 5m → 10m → 15m, 5 attempts, honoring a provider's own retry timing (`Retry-After`, Telegram `retry_after`) when it is longer (ID-12). How a human triggers a manual retry of an exhausted message (CLI command or SQL update) — #28 requires "manual retry eligibility" but nothing says how.
- What is stored at `planned`: the exact Notion payload (every property, resolved page ids, deadline datetime, matcher outcome, body blocks) **and the pending ops reports** (ID-11), so a retry replays the stored result instead of recomputing.
- Raw text stored unmasked only here; everything leaving the ledger goes through the masker.
- Migration rules: numbered, forward-only, backward-compatible for one release.
- A statement that this schema is a versioned read-only contract for other tools (OCP).

### 3.6 Masker
Everything in `secrets-in-raw-text.md`: labels (`pw`, `pass`, `password`, `passcode`, `PIN`, case-insensitive), must be immediately followed by `:` or `=`, value span rules (rest of line, or next non-blank line), placeholder `[password withheld – see Telegram message]`, label list configurable, masker version tag stored per message, runs before the LLM call and on every exit path (Notion body, ops posts, heartbeat first lines, logs, dry-run plan posts). Required test cases: the `PW:` line in `PP EP 2 (APC) - TOC - #0004` masks; "dl with pw", "DL w pw, 14 days expiry", "With password", "nondl with pw" do not.

### 3.7 Classify/extract call (DeepSeek)
- Inputs: masked text or caption plus forward-origin metadata (forwarder, original author, origin type) for classification only. Never the roster, today's date or the reply chain.
- Output schema as a Pydantic model, field by field, from `classifier-and-extractor-design.md` Q9: all raw fields, `subtitle` (the one inferred field: `Burned-in: <language>`, `None`, or null), `verdict` (`jo`/`unsure`/`not_jo`), `confidence` (`low`/`medium`/`high`, informational), `reasons`, `multiple_jo_suspected`. Every field string or null; the model never invents placeholders.
- Temperature 0, unpinned "latest", returned model id stored per call; daily `Model changed` heartbeat line on change.
- Failure = invalid JSON, schema violation, empty content, timeout or API error → one failed attempt on the 5-attempt queue. No repair call, no bare-row fallback.
- Verdict handling: `jo` writes; `unsure` writes and flags ops with `reasons`; `not_jo` → `ignore_log` (key, first line, reasons), no ops post. No-text forwards skip the LLM → `no_text`. Multiple JOs → one row from the first JO, flagged.
- Prompt: the spec defines the prompt structure and the prompt-version tag; few-shot (~15 corpus JOs) and eval set (~60) are disjoint, chosen to cover all header styles. List which corpus messages go in which set, or define the selection rule (see R15).

### 3.8 Building the row (pure `core/`)
Property-by-property table, merged from `job-order-field-mapping.md` and its amendments. For every Notion property: source field, transformation, blank rule, and example from the corpus. Must cover Name, JO Number, Title, Subtitle, Date, Deadline, Department, Status, Task, Purpose, `Source / Source Drive`, Platform, Watermark, Link Settings, Mode of Transfer, `Blocker `, Pending, Notes, Projects, Assigned, cc, `🗂️ Coordinator` (blank), Deliverable (from the matcher), `Telegram Key` (`<chat_id>:<message_id>`), Health (never written).
- Header parsing: project code, department, JO number in all the header styles of the corpus (`MFG - TOC - JO #74`, `TWBTY - TOC - JO No. 15`, `SOMSOM - TOC - #0155`, `SOY - TOC - JOB ORDER 27`, `PP EP 2 (APC) - TOC - #0004`, etc.). Include a table of every distinct header pattern found in the corpus with its parsed result.
- Canonical Name rule and its fallback to the posted first line (see R6 for the exact format).
- Department: exact match against live options after normalization, plus the header alias map in config (`TO` → `TOC`); unmatched → blank + ops line naming the token.
- Body: source block (posted by, originally from, origin type, Manila timestamp, original time for context, intake link, channel link when available), then the masked raw text in blocks of ≤ 2000 characters.
- Notes assembly order (see R4).

### 3.9 Deadline resolution
Merge `deadline-phrases.md` with design §9 and amendment 6. Write one if-then rule per phrase family, then a table that runs **every distinct deadline string in the corpus** through the rules with the expected Deadline value and Notes text. Must cover: full date+time, date only (23:59), time only (posting day, never rolled forward), `end of shift`/`within shift` (next occurrence of the assignee's `End of Shift` hour at or after posting time, latest across assignees, 23:59 posting-day fallback with an ops line naming who was defaulted), `asap` (shift logic + high-priority flag), `tonight` (23:59 posting day), no-clock phrases (blank, Notes), hedges (stripped from Deadline, kept in Notes), deadline before posting (kept and reported), and the phrase always copied to Notes. All datetimes carry `Asia/Manila`. Unparseable → blank. Resolve R2 and R3 first.

### 3.10 People, projects, departments
- Caches: People (Contacts `collection://77b2a701-71db-4e88-a694-37f07303e77e`), Projects (`cc1dc02a-8f32-4c97-81e4-dc1e08d7f134`, `Project Code` select, 142 values), Departments (live Job Orders schema options). Refresh on start and every 6 h, persisted as `cache_snapshot`, served stale on refresh failure (ops line only if > 24 h stale), empty cache blocks planning without spending attempts.
- Person chain: `TelegramUserID` → `TelegramHandle` → `Aliases` (comma-split, trimmed, casefolded, `ate`/`kuya`/`sir` stripped on both sides) → guarded fuzzy on the Contacts title (`rapidfuzz`, score ≥ 90 and lead ≥ 8 over runner-up, both config; never for `@handle` mentions; fuzzy-only match writes and posts an untagged ops line). Alias matching more than one contact → miss.
- Project match: whole-token, case-insensitive, longest-code-first against `Project Code`.
- Multiple assignees all go in Assigned; Leizette always added to cc from config.
- Misses and ambiguity: blank relation, raw mention quoted in the ops report, Leizette tagged.

### 3.11 Deliverable matcher (Jev, shadow mode)
From design §6 and amendments 2–3 only (the original matcher ticket is superseded where they differ). Must define: candidate query (live `relation_contains` on `🗺️ Projects` with the resolved project id, paginated, no Status/Class filter, skip if zero candidates), the per-candidate Choice question (state = masked text + project name; options `match`/`maybe`/`no`; keyed by page id), the decision rule in code (`T_link` 0.90, `T_hint` 0.50, both config), duplicate normalized names → hint not link, zero-to-many links, `deliverable_link_enabled` default off (shadow mode: compute and hint, link nothing), failure = retry once inline then blank + one ops line, never blocks the write, no backfill, ledger stores model id, prompt tag, candidate snapshot, probabilities and token counts. Regression fixtures: Moonglow JO #32 → `Moonglow FTR SCR`, SOMSOM #0155 → `Someone Someday WK11 VFX`, Mag-Iina JO 30 → `Mag-Iina FTR SCR`. Resolve R8 before writing the request/response shapes.

### 3.12 Verification checks
All nine checks, each with its trigger condition, the ops wording, and whether Leizette is tagged: unresolved person/project, unknown department, duplicate/reused JO number, non-`user` forward origin, classifier `unsure`, deadline in the past, deadline expected but missing, blank Task or Title, header date vs posting date mismatch. Plus the additional ops-producing events that are not "checks": `multiple_jo_suspected`, fuzzy person match, shift-deadline 23:59 default, ASAP high priority, Deliverable hint, matcher skipped, edit after capture, read-back mismatch, duplicate `Telegram Key` rows found during reconciliation, stale cache > 24 h, retry exhaustion (ID-10 lists the read-back and duplicate-key cases). Binary severity, immediate posting, never blocks the write. Resolve R5, R7 and R9.

### 3.13 Duplicates, re-posts, edits, idempotency
From `reposts-duplicates-idempotency.md` and design §7: message-level idempotency via the ledger key; the `writing`-state reconciliation (after the first backoff delay ≥ 30 s, query Notion on `Telegram Key`; found → adopt, none → create, two found → ops line, never delete or merge); content-level duplicate check against the ledger on project + department + JO number as an integer, skipped when any part doesn't parse; exact re-post and reused number both create a new row and are flagged in ops and in Notes with a link to the earlier page; edits are reported "edited after capture, row not updated" and never applied.

### 3.14 Notion adapter
Endpoints used (retrieve data source schema, query data source, create page, retrieve page, retrieve page property for relations > 25, PATCH only if ever needed), request/response Pydantic models, property value shapes per type, the 2000-character / 100-item / 1000-block / 500 KB limits and how the adapter splits content, error code handling table (400, 401, 403, 404, 409, 429, 500, 503, 504, 529 → retryable or not, and which ones should surface as a sharing/permission problem), rate limiting (~3 req/s), read-back by id comparing field by field against the plan (mismatch → ops line, never rewrite). Integration capabilities: Read content, Insert content. Shared data sources: Job Orders, Contacts, Projects, Deliverables.

### 3.15 Ops topic output
- Outbox: row created in the same transaction that sets `written`; at-least-once delivery with the 5-attempt backoff; duplicates acceptable and traceable by outbox id.
- Formatting rule: vertical, one fact per line, no comma-joined prose. No @mention by default; Leizette on human-decision checks; owner on the retry-exhaustion alert. Owner handle comes from config.
- Every message format, verbatim, with placeholders marked: flagged JO, needs a manual link, possible number reuse, outage alert, daily heartbeat (from `ops-topic-mockups.md`), Deliverable hint with `Match probability:` (not `Reason:`), matcher skipped, fuzzy match line, shift default line, edit after capture, multiple JOs, stale cache, model changed. See R5 for how multiple triggers combine.
- Heartbeat: 07:00 Asia/Manila, covering the previous 24 h, labeled with the date range, computed by SQL over ledger rows. Final line set (merge mock-up + amendments): Captured, Flagged, Ignored (not a JO), No-text forwards, Reaction failed, Passwords withheld, Deliverables linked / hinted / none / skipped, Queue (stuck, pending retry), Model changed (only on change), Ignored first lines (masked).
- `dry_run` posts: the plan, masked.

### 3.16 Configuration, secrets, logging
- `config.yaml` schema as a Pydantic model with every key, type, default and validation: intake chat id, intake topic id, ops chat id, ops topic id, Leizette's Contacts page id, owner Telegram handle, captured-reaction emoji, `T_link`, `T_hint`, fuzzy score and lead, heartbeat time and timezone, cache interval, retry schedule, department alias map, masker label list, `deliverable_link_enabled`, `dry_run`, data source ids. Bad config fails the boot.
- `.env`: `TELEGRAM_BOT_TOKEN`, `NOTION_TOKEN`, `DEEPSEEK_API_KEY`, Jev/TypeSafe key (exact variable names are yours to fix).
- Logging: JSON lines to stdout and a rotated file next to the ledger; fields: message key, state, stage, attempt, duration, returned model id; every line masked; raw text never logged.

### 3.17 Testing
#28's Testing Decisions already list the four seams and most of their assertions in detail; do not shorten them. Reference them by number and add only what is missing:
- The test philosophy from #28 item 1 (assert observable behavior and invariants, not private helpers) as a rule every test follows.
- Fixture directory layout, naming, and how each corpus message becomes a golden fixture (input, expected extraction, expected plan). Start from the existing `tests/fixtures/redacted_corpus.json` and `tests/fakes.py`.
- The inventory of recorded adapter fixtures #28 item 2 names (Telegram: text, caption, edit, four forward origins; Notion: schemas, paginated queries, create/retrieve, relation shapes, 400/429/5xx; DeepSeek: each verdict, empty content, invalid JSON, schema violation, timeout; Jev: Choice probabilities, malformed, unavailable), each with a file name and what it proves.
- The crash-injection matrix for #28 item 2.3: one row per (state boundary or external call) × (crash before / after), with the expected recovery path.
- The `smoke.py` steps, which section 3.20 covers.
- How extraction accuracy on the ~60 held-out JOs is scored and what passes (R15).

### 3.18 Deployment and operations
`python:3.12-slim`, non-root, Compose with `env_file`, `./data` bind mount (0700), `restart: unless-stopped`, `HEALTHCHECK` (define what it checks), deploy `git pull && docker compose up -d --build`, rollback to the previous image tag, straight to live with `dry_run` as an off-by-default rollback tool, 24 h Telegram retention as the accepted outage window. A one-time setup runbook: create bot, make it admin of the intake group, add it to the ops group, create the Notion integration with Read/Insert, share the four data sources, fill config.

### 3.19 Build plan
The ten tracer-bullet tickets, mapped to their issues (#18, #19 merged; #20–#27 open). For each: the issue number, the spec sections it implements, and its acceptance criteria **linked, not rewritten** — the issues own the criteria. Where a criterion needs a spec detail to be checkable (an exact template, a table row, a config key), point to it. Note any dependency the issue's "Blocked by" line misses. One is known: #21 (planning) owns End-of-Shift deadline resolution but is blocked only by #18, while the Contacts `End of Shift` and resolved assignees it needs come from #22 (resolution). Say how #21 handles this (for example, the deadline resolver takes resolved assignees and shift hours as inputs, tested with fixtures, and #22 wires the real data in).

### 3.20 Smoke-test checklist
Every item from design §17 and map "Not yet specified", deduplicated, each with how to run it and what pass looks like.

### Appendices
- A. Notion schemas: Job Orders, Contacts, Projects, Deliverables, with every property name and type, and the option lists known (Status, End of Shift). Department options may be listed only as informational: #28 forbids treating any observed Department list as a contract.
- B. Corpus fixture index (message id/first line → which set: few-shot, eval, regression).
- C. Open questions for the owner, each with the recommended default the build uses meanwhile.
- D. Traceability: for every spec section, the source files and sections it came from.

## 4. Reconciliation items

These are the gaps and contradictions found in the sources. Each needs a resolution in the spec. Where a recommendation is given, use it as the default and list the item in Appendix C unless the sources settle it.

- **R1. The ledger schema is only partly written down.** Design §2 points to "a documented column list per table (see §9)", but §9 is about End of Shift. `docs/ledger-schema.md` covers the tables tickets 1–2 created (`schema_version`, `messages` v2, `offsets`, `ignore_log`) and says ticket 3 adds `extracted` via migration 3. Nothing defines `stage_results`, `outbox`, `cache_snapshot`, LLM-call traces, attempt/backoff columns, or heartbeat/deadman state. Specify those as numbered migrations 3+ from the requirements in sections 3.5, 3.7, 3.11 and 3.15. This is the one place you are expected to design, because the decision (SQLite, these tables, these states, this contract role) is made and only the column list is missing.
- **R2. Date plus shift phrase.** "Aug 26, end of shift", "Sept 25 / end of shift", "September 2, within shift": does the shift hour attach to the stated date, or to the next occurrence after posting time? Recommended: the stated date at the assignee's `End of Shift` hour (for a night-shift end hour like 5:00 AM, the morning after the stated date), because the poster stated the date; next-occurrence applies only when no date is given.
- **R3. Phrases with no explicit rule.** "within the day" (recommended: 23:59 on the stated or posting day), "pm" as in "September 11, pm" (recommended: blank time is not invented; 23:59 on that date, phrase to Notes), "if not tomorrow na po" / "or tom" / "If keri rin po today" (hedges: strip, keep in Notes), parentheticals like "(papadala ko tonight ke Che yung HD nya)" (Notes). Also **year inference**: corpus dates carry no year. Recommended: the year that puts the date nearest the posting date, never more than ~6 months away.
- **R4. Notes content vs "no commentary".** The field-mapping ticket says the watchdog writes no commentary into properties; later tickets add text to Notes (deadline phrase copy, "ASAP — deprioritize other JOs", hedges, earlier-page reference for re-posts/reused numbers). Define Notes as: the poster's own notes first, then a fixed-format block of watchdog-added lines in a fixed order, and say that these later rules are deliberate exceptions.
- **R5. One ops message or many per JO.** PRD #28 (§10) settles the rule: one report per triggered capture. Still undefined is how the separate mock-up formats (flagged, manual link, number reuse) and the later standalone lines (fuzzy match, shift default, Deliverable hint, matcher skipped) combine into that one message. Recommended: one combined message per JO listing every triggered item under a single heading, with Leizette tagged if any item needs her; system-level messages (outage alert, heartbeat, stale cache, model changed) stay separate. Write the combined template.
- **R6. Canonical Name format.** The rule is `[PROJECT] - [DEPT] - JO #[N]`, but the existing row `SOMSOM - TOC - #0155` has no "JO". PRD #28 keeps leading zeros in JO Number; confirm N in the Name does the same (recommended: yes). Decide whether `[PROJECT]` is the matched `Project Code` or the code as posted (recommended: as posted in the header, since it is display text), and what happens when the project code doesn't match the select but the header still parses.
- **R7. Which checks tag Leizette.** Settled by PRD #28 §10: only unresolved People or Projects (alias collisions count as unresolved), unknown Department and `multiple_jo_suspected`; the owner only on retry exhaustion; everything else untagged. Just make the per-template tagging explicit.
- **R8. Jev has no interface documentation in the sources.** Design §5–6 describe Jev as a "System One" typed-decision model with Choice/Score questions and calibrated probabilities, keyed via a "TypeSafe (Jev)" API key, but no endpoint, request or response shape is recorded. The spec must either cite the provider's docs (ask the owner for the link) or define an adapter interface (`match(state, candidates) -> {page_id: {match, maybe, no}}`) and leave the wire format to a build-time task with recorded fixtures. Do the latter if docs are not provided.
- **R9. "Deadline missing but expected" after the 23:59 fallback.** With shift phrases now always resolving, define precisely which inputs trigger this check: recommended, a non-empty `deadline_raw` that yields no datetime and is not one of the known no-clock phrases.
- **R10. Header-date mismatch threshold.** The ticket says "more than a day or two". Pick one (recommended: more than 1 calendar day in Asia/Manila) and make it config.
- **R11. Reaction emoji lookup method.** (PRD #28 repeats "fetch available reactions" without naming the call.) The sources say to call `getAvailableReactions`. That is not a Bot API method; the allowed set comes from `getChat` → `ChatFullInfo.available_reactions` (absent means all standard emoji are allowed). Verify against current Bot API docs and correct the smoke-test item.
- **R12. Retry accounting by stage.** Clarify which failures consume one of the 5 attempts (DeepSeek, Notion create, Notion reconciliation query), which have their own inline retries (matcher once, reaction one or two), which never consume attempts (empty cache hold), and and confirm outbox delivery has its own counter on the same 30s → 15m schedule (PRD #28 §12 puts outbox on that schedule but does not say whose counter).
- **R13. Edits to messages not yet written, or ignored.** The rule only covers edits to already-captured messages. Recommended: an edit to a message still in the pipeline is reported the same way and ignored; an edit to a `not_jo` message is logged, not reported, and not re-classified.
- **R14. Repo and package name.** The design calls the repo `jo-watchdog`; the actual repo is `t6-jo-tg-ntn`. Keep the Python package name `jo_watchdog` (already in use).
- **R16. Jev credential name.** Design §13 calls it the "TypeSafe (Jev) key". Check `.env.example` and `jo_watchdog/app/config.py` for what already exists and use that name.
- **R15. Classifier evaluation.** Classifier precision/recall waits on the owner's negative examples (chatter, near-misses); extraction accuracy can be scored now on the ~60 held-out JOs. The spec should define the extraction scoring method and the pass bar, or list the bar as an open question. There is no model-selection gate (owner waiver).

## 5. Acceptance checks before you finish

- [ ] Every Notion property in the Job Orders schema has exactly one rule in 3.8, and every rule names its source.
- [ ] Every distinct `Deadline:` string in the corpus appears in the 3.9 table with an expected output.
- [ ] Every distinct header pattern in the corpus appears in the 3.8 header table with a parsed result.
- [ ] No superseded statement from the amendment log appears anywhere (search for "Laya", "pinned", "Reason:", "bake-off", "day shift", "night shift", "labeled set").
- [ ] Every ops message the system can send has a verbatim template and a tagging rule.
- [ ] Every ledger table and column is listed with type and purpose; every pipeline state has its allowed transitions.
- [ ] Every config key in 3.16 has a type, default and validation rule, and every threshold mentioned anywhere in the spec is a config key.
- [ ] Every R-item from section 4 is either resolved with a citation or listed in Appendix C with a default.
- [ ] Each of the ten build tickets has checkable acceptance criteria and maps to spec sections.
- [ ] No secrets, tokens or real chat ids; corpus text stays redacted.
- [ ] Nothing in `SPEC.md` contradicts merged code without an Appendix C entry saying so.
- [ ] Each open build issue (#20–#27) can point at the spec sections it implements, and no issue acceptance criterion is contradicted or changed.
- [ ] Every user story US-1 … US-118 in #28 is cited by at least one spec requirement, and every spec requirement cites at least one US or ID number (or is marked as an implementation detail #28 leaves open).
- [ ] Nothing in `SPEC.md` restates #28 in different words where a citation would do.
- [ ] A reader who has not opened `map/` could implement every ticket from `SPEC.md` alone.
