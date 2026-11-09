<!-- Mirror of https://github.com/mclbautista/t6-jo-tg-ntn/issues/28. Keep identical: change the issue and this file together. -->

# Build the JO Telegram Watchdog for durable Telegram-to-Notion capture

## Problem Statement

Job Orders arrive as inconsistent plaintext messages and forwards in a dedicated Telegram forum topic. Coordinators currently have to recognize which messages are Job Orders, interpret loosely formatted fields, resolve people and Projects, normalize deadlines, create Job Orders in Notion, and notice malformed or ambiguous details manually.

This creates several risks:

- A Job Order can be overlooked among ordinary Telegram chatter.
- Manual capture can omit fields, introduce inconsistent formatting, or create duplicate rows.
- A restart or partial Notion failure can lose a message or create the same Job Order twice.
- Credentials embedded in a message can leak into Notion, LLM requests, logs, or ops reports.
- Incorrect People, Project, Department, or Deliverable links can appear authoritative and corrupt downstream reporting.
- Ambiguous deadlines can leave Job Order Health incalculable.
- Problems can remain invisible when the Notion write itself succeeds.
- A silent watchdog outage is indistinguishable from a quiet intake topic unless the service reports its own liveness.

The larger operational requirement is that **Projects are built from Deliverables which are built from Job Orders**. The Job Order layer must therefore be captured consistently enough to support Job Order Health → Deliverable Health → Project Health without hiding uncertainty or inventing unsupported facts.

## Solution

Build a Docker-hosted JO Telegram Watchdog that passively watches one configured intake topic, classifies and extracts each candidate through DeepSeek, deterministically resolves and verifies the result, optionally evaluates Deliverable candidates through Jev, and writes the Job Order immediately to the existing Notion Job Orders database with Status `Needs Intake Review`.

The watchdog will:

- Run as one Python 3.12 asyncio process with one inspectable SQLite ledger and one Notion writer.
- Control Telegram long-polling offsets so an update is committed locally before the offset advances.
- Use DeepSeek for one structured classify/extract call.
- Use deterministic code for masking, identity resolution, schema validation, deadline resolution, canonical formatting, verification, and write planning.
- Use Jev only for project-scoped Deliverable matching through typed Choice probabilities.
- Store the exact planned Notion payload before writing it.
- Reconcile uncertain Notion writes through `Telegram Key` after a crash.
- Read back every created Notion row and compare it with the stored plan.
- Add one fixed-meaning reaction to the original Telegram message only after confirmed capture.
- Report data-quality findings and outages to a private ops topic without blocking successful capture.
- Preserve every candidate outcome in Notion, the ops topic, or an explicit ignore log.
- Ship straight to live while keeping Deliverable linking disabled by default in shadow mode.

## User Stories

1. As a traffic coordinator, I want each Job Order posted in the intake topic captured automatically, so that I do not have to create every Notion row by hand.
2. As a traffic coordinator, I want ordinary chatter excluded from Job Orders, so that the database remains usable.
3. As a traffic coordinator, I want uncertain messages captured as `Needs Intake Review`, so that a possible Job Order is not silently lost.
4. As an operator, I want every ignored message recorded with its first line and reason, so that a false `not_jo` decision can be discovered.
5. As an operator, I want no-text messages and forwards explicitly counted, so that unsupported content is not silently discarded.
6. As a coordinator, I want one Telegram message to create at most one Job Order, so that compound or malformed messages do not break idempotency.
7. As a coordinator, I want suspected multiple-Job-Order messages captured as one row and flagged for manual splitting, so that the full source remains available.
8. As a coordinator, I want every captured row to use Status `Needs Intake Review`, so that human review remains the next workflow step.
9. As a coordinator, I want the Job Order Name canonicalized when Project, Department, and JO Number parse, so that rows follow a consistent display convention.
10. As a coordinator, I want the posted first line retained when a canonical Name cannot be built, so that unsupported formatting does not erase useful context.
11. As a coordinator, I want JO Number preserved as posted, including leading zeros, so that the visible identifier matches the source.
12. As a coordinator, I want Title, Task, Purpose, Source / Source Drive, Platform, Link Settings, Watermark, Mode of Transfer, Blocker, Pending, and Notes populated from the message, so that the row is useful without reopening Telegram.
13. As a coordinator, I want Taglish and other source wording kept close to verbatim, so that operational meaning is not lost through translation.
14. As a coordinator, I want Subtitle inferred only as `Burned-in: <language>`, `None`, or blank, so that inference remains bounded.
15. As a coordinator, I want missing values left blank rather than filled with model-generated placeholders, so that blanks remain honest facts.
16. As a coordinator, I want placeholders actually written by the poster preserved verbatim, so that the watchdog does not reinterpret the source.
17. As a coordinator, I want the complete source message retained in the page body after credential masking, so that extraction can be checked against the original wording.
18. As an operator, I want the unmasked original retained only in Telegram and the protected local ledger, so that lossless capture does not broaden credential exposure.
19. As a coordinator, I want the source block to identify who posted the message and when it entered the intake topic, so that every row is traceable.
20. As a coordinator, I want a forwarded Job Order to distinguish “posted by” from “originally from,” so that forwarding does not collapse two identities.
21. As a coordinator, I want Date and relative-deadline calculations based on the forward time, so that the Job Order reflects when it became actionable in the intake topic.
22. As a coordinator, I want the original forward time shown only as context, so that it cannot distort Date or deadline calculations.
23. As an operator, I want every forward-origin type represented in the source block, so that hidden users, chats, and channels do not become blank provenance.
24. As an operator, I want non-`user` forward origins reported to the ops topic, so that limited Telegram provenance is visible without blocking capture.
25. As a coordinator, I want a link to the original forwarded message only when Telegram exposes a real message-level reference, so that the system never fabricates links.
26. As a coordinator, I want People resolved by TelegramUserID before weaker identifiers, so that exact identity data wins.
27. As a coordinator, I want People resolved by TelegramHandle when no user entity is available, so that bare handles remain useful.
28. As a coordinator, I want nicknames resolved through Contacts `Aliases`, so that aliases can be maintained without a deployment.
29. As a coordinator, I want `ate`, `kuya`, and `sir` normalized during alias matching, so that common honorifics do not prevent exact resolution.
30. As a coordinator, I want guarded fuzzy matching against the Contacts page title only after exact resolution fails, so that likely spelling variations can be recovered conservatively.
31. As an operator, I want every fuzzy-only person match reported, so that a potentially wrong identity choice is visible the same day.
32. As a coordinator, I want ambiguous aliases and weak fuzzy matches left unresolved, so that the watchdog does not guess a person.
33. As a coordinator, I want `@handle` mentions to require exact matching, so that fuzzy matching cannot redirect a real Telegram identity.
34. As a coordinator, I want every resolvable assignee added to Assigned, so that multi-person Job Orders retain all named assignees.
35. As a coordinator, I want per-task assignment wording preserved in Task even when Assigned cannot represent the split, so that the operational detail remains readable.
36. As a coordinator, I want Priya included in cc on every Job Order, so that the standing traffic-coordinator rule is consistently applied.
37. As a coordinator, I want Coordinator left blank, so that poster identity is not incorrectly treated as workflow ownership.
38. As a coordinator, I want Projects resolved against the live Project Code vocabulary, so that relation values are backed by existing Project pages.
39. As a coordinator, I want longer Project Codes matched before shorter codes, so that substring collisions do not select the wrong Project.
40. As a coordinator, I want unresolved or ambiguous People and Projects left blank and reported, so that a human can fill the gap without a false relation.
41. As a coordinator, I want Department validated against live Notion schema options, so that the watchdog cannot write a guessed or stale option.
42. As an operator, I want unknown Department tokens quoted in the ops report, so that schema or source-data problems are actionable.
43. As a coordinator, I want Date and Deadline written with an explicit Asia/Manila timezone, so that values do not acquire an accidental UTC interpretation.
44. As a coordinator, I want a bare stated day resolved to 23:59, so that the deadline remains orderable.
45. As a coordinator, I want a time-only deadline applied to the posting day, so that the system follows the literal source.
46. As a coordinator, I want a stated deadline that has already passed kept as stated and reported, so that the watchdog does not invent a later occurrence.
47. As a coordinator, I want `tonight` resolved to 23:59 on the posting day, so that it has a consistent operational meaning.
48. As a coordinator, I want End of Shift phrases resolved from each assignee’s Contacts `End of Shift`, so that the deadline reflects the assigned person rather than a hard-coded day/night category.
49. As a coordinator, I want End of Shift resolved to the next occurrence of the configured hour at or after posting time, so that overnight shifts resolve correctly.
50. As a coordinator, I want the latest resolved End of Shift used when several assignees are named, so that the Job Order is not marked late before all assignees’ shifts end.
51. As a coordinator, I want a blank or `Flexible / Variable` End of Shift to fall back to 23:59 on the posting day, so that Job Order Health remains calculable.
52. As an operator, I want every End of Shift fallback reported with the affected person or unresolved condition, so that a derived default is never mistaken for a stated fact.
53. As a coordinator, I want `ASAP` to use End of Shift resolution and also be reported as high priority, so that the row remains orderable without losing the urgency signal.
54. As a coordinator, I want deadline hedges removed from the normalized Deadline but retained in Notes, so that the datetime stays valid without discarding conditions.
55. As a coordinator, I want phrases with no clock meaning left blank and preserved in Notes, so that external events are not converted into invented times.
56. As a coordinator, I want every normalized deadline phrase copied into Notes, so that the original operational meaning remains visible beside the datetime.
57. As a coordinator, I want Deliverable matching limited to Deliverables related to the resolved Project, so that candidates from other Projects cannot be linked.
58. As a coordinator, I want Deliverable candidates queried live for each Job Order, so that recently created Deliverables are not missed by a six-hour cache.
59. As a coordinator, I want Archived, Distributed, Master, and Archival candidates included, so that valid archive work is not excluded by an assumed lifecycle filter.
60. As a coordinator, I want the watchdog to link existing Deliverables only, so that it never creates lifecycle records it cannot fully define.
61. As a coordinator, I want each Deliverable candidate evaluated independently, so that a Job Order may legitimately link to zero, one, or several Deliverables.
62. As a coordinator, I want only a high-confidence Jev `match` linked, so that precision is favored over recall.
63. As a coordinator, I want lower-confidence or ambiguous Deliverable outcomes reported as hints with probabilities, so that a human can decide without receiving a false relation.
64. As a coordinator, I want plain no-match outcomes left silent per message, so that expected absence does not flood the ops topic.
65. As an operator, I want Deliverable linked, hinted, none, and skipped counts in the daily heartbeat, so that shadow-mode behavior remains observable.
66. As an operator, I want the Deliverable matcher to run in shadow mode while linking is disabled, so that real traffic can be reviewed before automatic links are enabled.
67. As a coordinator, I want a Deliverable matcher failure to leave the relation blank without delaying the Job Order write, so that an optional convenience cannot block capture.
68. As a coordinator, I want a matcher failure reported after one inline retry, so that the missing relation is visible and traceable.
69. As an operator, I want each classifier and matcher call to store the model ID actually returned, so that unpinned `latest` models remain traceable.
70. As an operator, I want a heartbeat line only when a returned model ID changes, so that model movement is visible without daily noise.
71. As an operator, I want prompt tags, candidate snapshots, token counts, and masker versions stored locally, so that decisions can be replayed and inspected.
72. As an owner, I want LLM token counts queryable without a dashboard or heartbeat cost report, so that traceability does not create maintenance work.
73. As a coordinator, I want labeled credentials masked before DeepSeek sees the message, so that passwords do not leave the trusted boundary.
74. As a coordinator, I want the same deterministic masker applied to Notion, ops posts, heartbeat excerpts, and logs, so that every outbound path follows one rule.
75. As a coordinator, I want credential labels retained while their values become `[password withheld – see Telegram message]`, so that users know a credential exists and where to retrieve it.
76. As a coordinator, I want links and embedded link keys preserved, so that working delivery links are not broken.
77. As a coordinator, I want phrases such as “dl with pw” left untouched when no `:` or `=` credential assignment is present, so that normal Link Settings text is not falsely redacted.
78. As an owner, I want the ledger protected with owner-only filesystem permissions, so that retaining original source text does not create broad local access.
79. As an operator, I want every message keyed by `chat_id:message_id`, so that Telegram identity is stable across retries and restarts.
80. As an operator, I want the ledger to be the sole authority for whether a Telegram message was captured, so that deduplication is not split between systems.
81. As an operator, I want the Telegram polling offset advanced only after the update is durably committed, so that a crash cannot acknowledge an unqueued message.
82. As an operator, I want an uncertain Notion POST reconciled by `Telegram Key` after restart, so that a crash cannot create a second row for the same message.
83. As an operator, I want duplicate `Telegram Key` rows detected and reported rather than deleted or merged automatically, so that destructive recovery is avoided.
84. As a coordinator, I want exact reposts with new Telegram message IDs to create new rows and be flagged, so that one-row-per-message remains consistent.
85. As a coordinator, I want reused JO Numbers to create new rows and be flagged as possible number reuse, so that legitimate reuse and mistakes remain human decisions.
86. As a coordinator, I want duplicate detection based on Project, Department, and JO Number normalized as an integer, so that leading zeros and `#` formatting do not hide reuse. D2: reuse findings display the posted number (`074`/`#074` show `#074`, `#74` stays `#74`); only the key is integer-normalized.
87. As a coordinator, I want content-duplicate checks skipped when the complete normalized key cannot be built, so that partial data does not create false-positive warnings.
88. As a coordinator, I want edits received and reported without modifying the captured row, so that a changed source is visible while capture remains one-way.
89. As a coordinator, I want corrections submitted as fresh messages, so that the watchdog never performs hidden write-back or in-place synchronization.
90. As an operator, I want every Notion write read back field by field, so that destination mismatches are detected without automatic rewriting.
91. As a coordinator, I want verification to report findings without preventing a Notion write, so that reviewable gaps do not become missing Job Orders.
92. As an operator, I want deadline-in-past, expected-but-unparsed deadline, blank Task or Title, and header-date mismatch checks, so that common capture failures are visible.
93. As an operator, I want unresolved People or Projects, unknown Department, duplicate or reused JO Number, non-`user` forward origin, and `unsure` verdict checks, so that previously decided conditions share one reporting pipeline.
94. As a traffic coordinator, I want to be tagged only for findings that require my decision, so that mentions remain meaningful.
95. As an owner, I want to be tagged on exhausted outage alerts, so that a true service or dependency failure reaches the responsible person.
96. As an ops-topic member, I want immediate vertical, line-broken reports with one fact per line, so that findings are easy to scan.
97. As an ops-topic member, I want reports to include the Notion row, Job Order Name, triggered checks, intake message link, and relevant model reasons, so that each finding is actionable.
98. As an intake-topic member, I want one reaction with the fixed meaning “captured,” so that I can see when the Job Order reached Notion.
99. As an operator, I want the captured reaction selected from the real group’s available reactions, so that the deployment does not assume an unsupported emoji.
100. As an operator, I want reaction failures retried briefly and counted in the heartbeat rather than alerted individually, so that a cosmetic failure does not overwhelm the ops worklist.
101. As an operator, I want ops posts placed in a durable outbox in the same transaction that marks a row written, so that a crash cannot separate capture from its findings.
102. As an operator, I want failed dependency calls retried five times with bounded exponential backoff, so that transient failures recover without intervention.
103. As an operator, I want an exhausted message retained for manual retry, so that “given up” never means deleted.
104. As an operator, I want a deadman to restart the process when polling is stale for ten minutes, so that a hung poller does not remain silently alive.
105. As an ops-topic member, I want a 07:00 Asia/Manila heartbeat covering the previous 24 hours, so that silence itself becomes an outage signal.
106. As an ops-topic member, I want the heartbeat derived from ledger records rather than mutable counters, so that totals cannot drift from captured history.
107. As an ops-topic member, I want the heartbeat to show captured, flagged, ignored, no-text, passwords withheld, Deliverable outcomes, reaction failures, queue status, and recent ignored first lines, so that the last day is auditable.
108. As an operator, I want People, Projects, and Departments refreshed at startup and every six hours, so that relation and schema resolution use current Notion data.
109. As an operator, I want successful cache snapshots persisted, so that a temporary refresh failure does not erase the last known schema.
110. As an operator, I want a stale non-empty cache to remain usable and an empty cache to block planning, so that an outage cannot turn every relation into a false miss.
111. As an owner, I want the watchdog deployed straight to live, so that capture begins without a mandatory dry-run phase.
112. As an owner, I want an off-by-default dry-run rollback control, so that writes and reactions can be suspended while plans remain visible if needed.
113. As an owner, I accept that Telegram outages longer than 24 hours can lose updates, so that v1 does not require another external monitoring account.
114. As an owner, I want the missing daily heartbeat to be the human-visible detector for that accepted outage gap, so that the limitation is explicit rather than hidden.
115. As a future integrator, I want the SQLite schema documented and versioned, so that other tools can inspect the ledger read-only.
116. As a future integrator, I want pure core behavior exposed through a small JSON command interface, so that future orchestration can reuse the same decisions without becoming a second writer.
117. As an owner, I want one process and one Notion writer for watchdog-owned Job Orders, so that deduplication and masking guarantees remain centralized.
118. As an operator, I want Docker deployment, persistent data, health checks, and rollback-safe migrations, so that the service can be maintained on an always-on machine.

## Implementation Decisions

1. **Standing principles**
   - **Precision over recall when a human can fill the gap:** if an automatic relation could be wrong, leave it blank and report or hint instead.
   - **Scope at the filter, not the grant:** the bot and Notion integration may have broad access, but every operation is re-scoped to the configured intake chat/topic or resolved Project.
   - **Annotate, don’t gate:** deterministic verification and optional matching findings never block a valid Notion capture.
   - **Never drop silently:** every candidate ends in Notion, the ops topic, or an explicit ignore log.
   - **Idempotent writes:** one Telegram message creates at most one Notion row, including after a crash.
   - **Own your dedupe key:** one process, one ledger, and one writer own `Telegram Key` and duplicate decisions.
   - **Resolve, don’t generate:** LLMs return bounded decisions or raw mentions; deterministic code owns identities, datetimes, canonical names, schema values, and page IDs.
   - **Blank is a fact, filler is a fabrication:** absent source values remain blank, except for the documented and reported 23:59 fallback for derived shift deadlines.
   - **Traceable by default:** DeepSeek and Jev remain unpinned, but every call stores the returned model ID and supporting trace metadata.
   - **Never grade on the training set:** prompt examples and held-out extraction fixtures remain disjoint.
   - **Schema is the contract:** validation and writes use live Notion property and option names.
   - **Mask at the boundary, keep the source:** original source text remains local; outbound text is deterministically masked.
   - **A queue you can’t inspect isn’t a safety net:** SQLite is a documented ledger, state machine, queue, and outbox rather than a library-private store.
   - **Silence is a failure mode:** the daily heartbeat exists so its absence can signal an outage.

2. **Runtime and architecture**
   - Use one Python 3.12 asyncio process in one container.
   - Separate a pure core from Telegram, Notion, DeepSeek, and Jev I/O adapters.
   - Keep SQLite ledger/state-machine, migrations, offsets, persisted cache snapshots, ignore records, stage results, LLM traces, and durable outbox in the same inspectable database.
   - Use standard SQLite access in WAL mode with numbered, forward-only migrations that remain backward-compatible for one release.
   - Keep runtime orchestration out of n8n. Future n8n use may invoke the pure core, but nothing outside this process may write watchdog-owned Job Orders.
   - Document and version the ledger schema for read-only consumers.
   - Ledger additions are numbered migrations owned by the build tickets: 3 (`extracted`/`ignored` states, retry columns, `stage_results`, `llm_calls`) in #20; 4 (`cache_snapshot`) in #22; 5 (`outbox`, findings, reaction state) in #23; 6 (`jo_key`, `text_sha256`, `edits`) in #24; 7 (`exhausted_at`, `runtime_state`, `heartbeats`) in #25. Each ticket lists the exact columns, and `docs/ledger-schema.md` is updated with each migration.

3. **Telegram ingestion**
   - Make the watchdog an admin of the intake group only, allowing it to receive every message without changing bot-wide privacy mode.
   - Keep it a posting-capable member of the private ops group.
   - Use long polling rather than a webhook.
   - Use aiogram as a typed Bot API client, not as an offset-owning dispatcher.
   - Poll messages and edit updates while retaining direct control of the update offset.
   - Filter on both configured chat ID and topic thread ID before intake processing. Out-of-scope traffic receives only an explicit ignore-log entry.
   - Persist each accepted update before advancing the committed offset.
   - Treat text and captions as supported input.
   - Send no-text messages directly to the ignore log with reason `no_text`.
   - Receive edits and report edits to already captured messages without changing the Notion row.
   - Edits to messages still in the pipeline are recorded and reported once the row is captured; edits to ignored messages are recorded only; edits to unknown messages go to the ignore log (#24).
   - Fetch available reactions from the real intake group and select one build-time “captured” reaction.
   - The available set comes from `getChat(intake_chat_id).available_reactions`; an absent list means every standard emoji is allowed (#27).
   - Apply the reaction only after a confirmed Notion write and read-back. Its meaning never changes based on verification findings.

4. **DeepSeek classify/extract**
   - DeepSeek is the only classify/extract provider. Laya is not used.
   - Run one structured call at temperature 0 using JSON mode plus Pydantic validation.
   - The call returns `verdict`, `confidence`, short `reasons`, `multiple_jo_suspected`, and raw/verbatim extraction fields.
   - The three verdicts are:
     - `jo`: write normally.
     - `unsure`: write and report the model reasons.
     - `not_jo`: do not write; record in the ignore log and include it in the heartbeat.
   - DeepSeek sees masked message text or caption plus forward-origin metadata for classification context.
   - DeepSeek does not see the People roster, Project roster, current date, or reply chain.
   - Forward metadata may inform classification but may not supply extracted field values.
   - DeepSeek returns raw strings, never page IDs, normalized datetimes, canonical Names, or generated placeholders.
   - Invalid JSON, schema violations, empty content, timeouts, and API errors enter the standard retry path. There is no repair call and no bare-row fallback.
   - If multiple Job Orders are suspected, create exactly one row from the first extracted Job Order, retain the full source text, and report that manual splitting is required.
   - Use roughly 15 representative corpus examples as few-shot prompt material and keep roughly 60 separate as extraction evaluation fixtures.
   - Classification precision and recall remain non-gating until real chatter and near-miss examples are available.

5. **Credential masking**
   - Detect only labels `pw`, `pass`, `password`, `passcode`, and `PIN`, case-insensitively, when immediately followed by `:` or `=`.
   - Mask the remainder of the same line or, when the separator ends the line, the next non-blank line.
   - Replace only the value with `[password withheld – see Telegram message]`.
   - Do not mask link URLs, embedded link keys, or phrases such as “dl with pw” that do not use the credential-label syntax.
   - Mask before DeepSeek and before all text leaves the ledger for Notion, ops reports, heartbeat excerpts, or logs.
   - Store unmasked source text only in the owner-protected ledger and Telegram.
   - Store the masker version used for each message.
   - Count masked messages in the daily heartbeat rather than posting a per-message alert.
   - Accept unlabeled credentials and unsupported labels as known v1 false negatives.

6. **Job Order field mapping**
   - Build Name as `[PROJECT] - [DEPT] - JO #[N]` only when Project Code, Department, and JO Number all parse; otherwise use the posted first line.
   - `[PROJECT]` is the header's project token as posted, `[DEPT]` the validated Department option, and `[N]` the digits as posted with leading zeros, so `SOMSOM - TOC - #0155` becomes `SOMSOM - TOC - JO #0155` (#21).
   - Preserve JO Number as posted with a leading `#` and any leading zeros.
   - Write Title from the posted title line.
   - Infer Subtitle only as `Burned-in: <language>`, `None`, or blank.
   - Write Date from the Telegram posting or forwarding time with an explicit Asia/Manila timezone.
   - Write Deadline as an Asia/Manila datetime when deterministic rules resolve it.
   - Validate Department against live Notion options; unmatched tokens remain blank and are reported.
   - Always write Status `Needs Intake Review`.
   - Fill Task, Purpose, Source / Source Drive, Platform, Link Settings, Watermark, Mode of Transfer, Blocker, Pending, and Notes close to verbatim. Infer between fields only where meaning is clear and retain the raw source.
   - Write Blocker only from an explicit blocker.
   - Notes holds the poster's own notes, then, only when needed, a `— added by the watchdog —` block with `Deadline as posted: …`, `ASAP — deprioritize other JOs`, and `Earlier row: <url> (…)` lines in that order (#21, #24).
   - Fill Projects, Assigned, cc, and Deliverable only with resolved Notion page IDs.
   - Always add Priya to cc.
   - Leave Coordinator blank.
   - Never write Health because it is a formula.
   - Write `Telegram Key` as `<chat_id>:<message_id>`.
   - Put a source block followed by masked raw source text in the page body, splitting content to respect Notion limits.
   - Keep property names exact, including punctuation, emoji, and the existing trailing-space property name where applicable.

7. **People, Projects, and Departments**
   - Read People, Projects, and Departments from Notion at startup and every six hours.
   - Persist successful snapshots in the ledger.
   - Continue using the latest non-empty snapshot if refresh fails; report only when it becomes more than 24 hours stale.
   - If no snapshot has ever loaded, hold messages at the extracted stage without consuming retry attempts.
   - Resolve People in this order: TelegramUserID, TelegramHandle, Contacts `Aliases`, guarded fuzzy match against the Contacts page title.
   - Split `Aliases` on commas, trim and casefold values, and strip `ate`, `kuya`, and `sir` consistently from source and alias values.
   - Never use fuzzy matching for an `@handle`.
   - Require fuzzy score at least 90 and a lead of at least 8 points over the runner-up, with both thresholds configurable.
   - Report each fuzzy-only match without tagging anyone.
   - Treat alias collisions and insufficient fuzzy separation as unresolved.
   - Resolve Project mentions against live Project Code values as whole tokens, case-insensitively and longest-code-first.
   - Keep a small configurable Department/header alias map, such as `TO` → `TOC`; do not hard-code the live Department option set.
   - Leave unresolved or ambiguous relations blank, quote the raw mention in ops, and do not backfill later.

8. **Deadline resolution**
   - Resolve a bare date to 23:59 on that date.
   - Resolve a time-only value against the posting day.
   - Keep a stated datetime even when it precedes posting time; report it rather than rolling it forward.
   - Resolve `tonight` to 23:59 on the posting day.
   - Resolve End of Shift and within-shift phrases from Contacts `End of Shift`.
   - For a configured shift hour, choose its next occurrence at or after the posting time.
   - For several assignees, choose the latest resolved shift datetime.
   - If an assignee has blank or `Flexible / Variable` End of Shift, or no assignee resolves, use 23:59 on the posting day and report the fallback.
   - Resolve `ASAP` through the same shift logic and also report it as high priority.
   - Strip hedges from the normalized datetime while preserving them in Notes.
   - Leave phrases with no clock meaning blank and preserve them in Notes.
   - Preserve every original deadline phrase in Notes.
   - Hand-roll the supported phrase parser and fail closed rather than using a parser that fills missing components implicitly.
   - A month and day without a year take the year closest to the posting date; weekday names are ignored for computation.
   - A stated date with a shift phrase (`Aug 26, end of shift`) uses the assignee's End of Shift hour on that date, or the next day when the hour is before 12:00.
   - `today` and `within the day` resolve to 23:59 on the stated or posting day; `pm` without an hour resolves to 23:59 on that date.
   - Hedges are parentheticals and text starting at `if`, `kung` or ` or `; they are stripped before parsing and the whole original goes to Notes.
   - #21 holds the full rule list and a table of every distinct corpus deadline with its expected result. The resolver takes assignee End of Shift values as input so #21 can test it before #22 supplies live Contacts data.

9. **Deliverable matching**
   - Use Jev only for Deliverable matching.
   - Run matching after extraction and Project resolution and before the single Notion write.
   - Skip matching when Project is unresolved or the Project has no Deliverables.
   - Query Deliverables live for the resolved Project using its `🗺️ Projects` relation.
   - Apply no Status or Deliverable Class filter.
   - Never create a Deliverable.
   - Provide Jev the masked source text and resolved Project name.
   - Ask one typed Choice question per candidate, keyed by candidate page ID, with `match`, `maybe`, and `no` options.
   - Do not request candidate-name echoing or free-text reasons.
   - Link a candidate only when `match` is the top choice and its probability meets configurable `T_link`, initially 0.90.
   - Produce a hint when `match + maybe` meets configurable `T_hint`, initially 0.50.
   - Keep lower-confidence outcomes silent.
   - Treat duplicate normalized candidate names as ambiguous: never link them and produce a hint.
   - Support zero-to-many links, evaluating every candidate independently.
   - Retry a failed matcher call once inline, then write with Deliverable blank and queue one ops report.
   - Store returned model ID, prompt tag, token counts, candidate names and page IDs, and returned probabilities.
   - Run Jev on every eligible Job Order in shadow mode while `deliverable_link_enabled` defaults off.
   - While linking is disabled, retain decisions and hints but write no Deliverable relation.
   - Automatic linking begins only when the owner enables the flag after reviewing live shadow-mode behavior.
   - Keep the three known real JO-to-Deliverable pairs as permanent regression fixtures.
   - The Jev adapter exposes `match(state, project_name, candidates) -> MatchResult` with per-candidate `match`/`maybe`/`no` probabilities (#26). Jev is called through OpenRouter's Decisions API (`POST /api/alpha/decisions`, model `~typesafe/jev-latest`), which returns Choice probabilities natively; #26 holds the request and response format.
   - Perform no automated backfill when a Deliverable is created after capture.

10. **Verification and ops reporting**
    - Verification is deterministic and runs without an additional LLM call.
    - The nine-check verification set includes:
      1. Unresolved or ambiguous person or Project.
      2. Unknown Department.
      3. Duplicate or reused JO Number.
      4. Non-`user` forward origin.
      5. DeepSeek `unsure`.
      6. Deadline before posting time.
      7. Deadline-shaped language that failed to parse.
      8. Blank Task or Title.
      9. Header date materially inconsistent with the actual posting date.
    - Also report `multiple_jo_suspected`, fuzzy-only People matches, End of Shift fallbacks, Deliverable matcher skips, duplicate `Telegram Key` rows, edits after capture, and read-back mismatches.
    - Treat reports as binary: report or do not report. Do not add severity tiers.
    - Post immediately, with one report per triggered capture rather than a digest.
    - Use vertical, line-broken formatting with one fact per line.
    - Include the Notion row link, Job Order Name or fallback first line, triggered checks, relevant model reasons, and the intake-topic message link.
    - Add an original-message link only for channel-origin forwards with sufficient Telegram metadata.
    - Do not include the full raw message in an ops report.
    - Tag no one by default.
    - Tag Priya for unresolved People or Projects, unknown Department, and suspected multiple Job Orders.
    - Tag the owner only on true outage alerts after retry exhaustion.
    - Show Deliverable hints with match probability rather than a generated reason.
    - Use a durable outbox. Insert pending ops posts in the same transaction that moves a message to `written`.
    - Deliver outbox entries at least once. A traceable duplicate ops post is preferable to a missing report.
    - All findings for one capture share one combined report; #23 holds the template and the exact line wording per finding. Edits after capture and stale caches are separate system messages.
    - "Deadline-shaped text that failed to parse" means a non-empty, non-placeholder `deadline_raw` that produced a blank Deadline. "Materially inconsistent" header date means more than `header_date_tolerance_days` (default 1) from the Manila posting date.

11. **Idempotency, duplicates, and the write state machine**
    - Key ledger messages and Notion rows by `Telegram Key`.
    - Use the local ledger, not Notion query freshness, as the primary deduplication authority.
    - Use states `received` → `extracted` → `planned` → `writing` → `written` → `done`.
    - Store DeepSeek output at `extracted`.
    - Store the complete exact Notion payload, resolved IDs, deadlines, Deliverable result, body blocks, and reports at `planned`.
    - Resume retries from stored stage results rather than recomputing against changed caches.
    - Move to `writing` before issuing the Notion create call.
    - On recovery from `writing`, wait at least the first retry delay and query Notion by `Telegram Key`.
    - Adopt one matching row when found; create the row when none is found.
    - Report multiple matches without deleting or merging them.
    - After create, retrieve the page by ID and compare fields against the stored plan.
    - Report mismatches and do not automatically patch the row.
    - Normalize the content-duplicate key as Project + Department + JO Number parsed as an integer.
    - Skip all content-duplicate detection, exact re-post and number reuse alike, unless Project page ID, validated Department and integer JO Number are all resolved and unambiguous.
    - Always create a new row for a new Telegram message, including exact reposts and reused JO Numbers.
    - Add a reference to the earlier row in Notes and report the repost or possible number reuse.

12. **Retries, liveness, and heartbeat**
    - Retry classifier, Notion, and durable outbox failures up to five total attempts: attempt one is immediate, and the delays between attempts are 30 seconds, 2 minutes, 5 minutes and 10 minutes (each extended by a longer provider `Retry-After`).
    - Respect provider retry timing where available.
    - After attempt five, retain the message for manual retry and issue one outage alert.
    - Do not substitute a partial Notion row when DeepSeek is unavailable.
    - Message attempts reset on every successful state change; Jev, reactions, cache refreshes and empty-cache holds never consume them; ops posts have their own counter (#25).
    - `python -m jo_watchdog.app retry <telegram_key>` (or `--all-exhausted`) makes an exhausted message eligible again without changing its plan.
    - Retry reaction failures briefly inline, then count them without durable per-message alerting.
    - Maintain a poller heartbeat and exit non-zero if it is stale for ten minutes, allowing the container restart policy to recover.
    - Send a daily heartbeat at 07:00 Asia/Manila covering the preceding 24 hours and label the date range.
    - Derive heartbeat values directly from ledger records.
    - Include captured, flagged, ignored, no-text, queue, stuck, passwords withheld, Deliverables linked/hinted/none/skipped, and reaction-failure lines.
    - Include actual first lines of recent ignored messages after applying the masker.
    - Include `Model changed: <old> → <new>` only when the returned model ID changed day over day.
    - Store token counts but do not add cost totals to the heartbeat or build a cost dashboard.
    - Accept Telegram’s 24-hour update-retention limit as an explicit v1 gap. Do not add an external uptime service; the missing heartbeat is the human outage signal.

13. **Notion integration**
    - Use a hand-written asynchronous HTTP adapter with Pydantic request and response models.
    - Pin the Notion API version, not the LLM model versions.
    - Give the integration Read and Insert content access and share Job Orders, Contacts, Projects, and Deliverables with it.
    - Use live schema names and page IDs for relations.
    - Validate schema-backed values before writing.
    - Paginate all data-source queries.
    - Respect Notion text, block, relation, request-size, and rate limits.
    - Treat unknown-select behavior, query freshness, and retry headers as smoke-test concerns rather than assumptions.
    - Use retrieve-by-ID for normal read-back; use a `Telegram Key` query only for uncertain `writing` recovery.

14. **Configuration and observability**
    - Validate all non-secret configuration at startup and fail boot on invalid values.
    - Configure intake and ops chat/topic IDs, Priya’s Contacts page ID, the owner’s Telegram handle, Deliverable thresholds, fuzzy thresholds, heartbeat schedule, timezone, cache interval, retry schedule, `deliverable_link_enabled`, and dry-run behavior.
    - Inject Telegram, Notion, DeepSeek, and Jev credentials through deployment environment secrets.
    - Emit masked structured logs to stdout and a rotated persistent log.
    - Include message key, state, stage, attempt, duration, and returned model ID in structured logs.
    - Never log unmasked source text.
    - Secrets are `TELEGRAM_BOT_TOKEN`, `NOTION_API_KEY`, `DEEPSEEK_API_KEY` and `OPENROUTER_API_KEY` (Jev). #27 holds the full configuration reference with defaults, and each ticket adds its own keys.

15. **Deployment and rollout**
    - Package in a slim Python 3.12 image running as a non-root user.
    - Bind-mount the persistent data directory with owner-only permissions.
    - Use `restart: unless-stopped` and a container health check.
    - Deploy straight to live with no mandatory dry-run period.
    - Keep dry-run off by default as a rollback tool. In dry-run, stop at `planned`, post the plan to ops, and perform no Notion write or reaction.
    - Keep Deliverable linking as the only staged rollout, disabled by default while Jev runs in shadow mode.
    - Support rollback to the prior image while maintaining one-version schema compatibility.
    - The container health check runs `python -m jo_watchdog.app healthcheck`, which fails when the poller's liveness stamp is older than 120 seconds.

16. **Build order**
    - Build tracer bullets rather than horizontal layers:
      1. Walking skeleton from real Telegram message through ledger, minimal plan, Notion write, read-back, and reaction.
      2. Deterministic masker and corpus fixtures.
      3. DeepSeek classify/extract.
      4. Pure Name, JO Number, Department, Notes, and Deadline construction.
      5. Persisted caches and deterministic People, Projects, and Departments resolution.
      6. Verification, durable outbox, and locked ops formats.
      7. Forwards, edits, reposts, duplicate detection, and number reuse.
      8. Retry exhaustion, `writing` reconciliation, deadman, and heartbeat.
      9. Jev Deliverable matcher in shadow mode.
      10. Container packaging, dry-run behavior, and real-account smoke coverage.

## Testing Decisions

1. Tests must assert externally observable behavior and safety invariants rather than private implementation details. Refactoring pure functions, adapters, or orchestration must not require test rewrites when inputs, outputs, state transitions, and side effects remain unchanged.

2. The four confirmed seams are:

   1. **Pure core corpus tests**
      - Exercise masking, extraction-result interpretation, canonical Name construction, JO Number normalization, Department normalization, People and Project resolution, guarded fuzzy matching, deadline parsing, End of Shift resolution, verification, duplicate classification, and exact Notion plan construction.
      - Use the 77 real redacted Job Orders as fixtures and golden outputs.
      - Preserve separate prompt examples and held-out extraction fixtures.
      - Include the required credential-mask positive fixture and the established no-mask phrases.
      - Require unsupported or unparseable values to fail closed as blank rather than receive guessed values.
      - Cover every deadline phrase family, Asia/Manila boundary, overnight shift, multiple-assignee latest shift, 23:59 fallback, hedge preservation, and past stated deadline.
      - Cover TelegramUserID, TelegramHandle, Contacts `Aliases`, alias collision, fuzzy pass, fuzzy insufficient lead, fuzzy insufficient score, and the no-fuzzy-for-`@handle` rule.
      - Cover canonical and fallback Names, leading-zero JO Numbers, Department aliases, and live-option mismatch behavior.

   2. **Recorded adapter contract tests**
      - Run without network access in CI.
      - Use recorded Telegram updates for ordinary text, captions, edits, and all four forward-origin types.
      - Verify message-link availability only where Telegram supplies sufficient metadata.
      - Use recorded Notion schemas, pages, paginated query responses, create responses, retrieve responses, relation shapes, validation errors, rate limits, and retry responses.
      - Use recorded DeepSeek success, `jo`, `unsure`, `not_jo`, empty content, invalid JSON, schema violation, timeout, and API failure responses.
      - Use recorded Jev Choice responses, probabilities, candidate-key mapping, token counts, returned model IDs, malformed responses, and unavailable-service behavior.
      - Verify adapter models against recorded provider contracts rather than mocking internal HTTP helper calls.

   3. **Whole-pipeline crash recovery — highest automated seam**
      - This is the highest and most important automated seam.
      - Exercise the full worker and state machine with **real SQLite, fake Telegram/Notion/DeepSeek/Jev adapters, and a fake clock**.
      - Inject crashes at every state boundary and immediately before and after each external call.
      - Restart from the same SQLite database and run recovery to completion.
      - Assert the primary invariant: exactly one Notion row per `Telegram Key` after recovery.
      - Assert that Telegram offsets never advance past an update that was not committed.
      - Assert that a crash after the Notion POST but before response persistence is recovered through `Telegram Key` reconciliation rather than a second create.
      - Assert that stored plans are replayed unchanged after cache contents change.
      - Assert that ops outbox records cannot be lost when a row reaches `written`.
      - Assert that at-least-once ops delivery may duplicate a report but cannot omit it.
      - Assert that reactions occur only after confirmed capture and do not affect capture durability.
      - Assert retry schedules, exhausted-message retention, manual retry eligibility, empty-cache planning holds, stale-cache behavior, deadman exit, and heartbeat windows using the fake clock.

   4. **Manual real-account `smoke.py`**
      - Run by hand against the actual Telegram, Notion, DeepSeek, and Jev accounts; do not run in CI.
      - Prove the walking skeleton end to end.
      - Confirm integration access and sharing for Job Orders, Contacts, Projects, and Deliverables.
      - Confirm live reading of Contacts `Aliases` and `End of Shift`.
      - Confirm Department and Status schema behavior and exact property names.
      - Confirm behavior for an unknown select name.
      - Confirm query visibility after a new Notion write and after a newly created Deliverable.
      - Confirm Notion 429 retry information.
      - Confirm a `relation_contains` query on `🗺️ Projects`.
      - Confirm that writing the Job Order `Deliverable` relation populates the Deliverable-side `Job Orders` relation.
      - Confirm channel-origin forward links against a real forwarded channel post.
      - Fetch the real intake group’s available reactions and verify the selected captured reaction.
      - Resolve and verify the owner’s real Telegram handle for outage tagging.
      - Confirm Jev typed Choice behavior and returned probability/model metadata.
      - Tune fuzzy score and lead thresholds against corpus examples without weakening ambiguity protection.
      - Exercise the credential-mask positive and no-mask cases.
      - Verify read-back comparison, ops-topic posting, outbox retry, heartbeat formatting, dry-run behavior, and Deliverable shadow mode.

3. The three known real JO-to-Deliverable pairs remain fixed regression fixtures even though the pre-launch labeled Deliverable evaluation was waived.

4. DeepSeek provider selection and Jev provider selection are owner decisions and are not gated by a comparative model bake-off. Extraction behavior remains corpus-tested; Deliverable link/hint decisions remain threshold-tested and shadow-observed.

5. Classifier precision and recall cannot be meaningfully accepted from the all-Job-Order corpus alone. When real chatter and near-miss examples become available, add them as a disjoint evaluation set without delaying the current implementation.

   Extraction is scored per field (exact match after whitespace normalization) on the held-out set and reported without gating CI (owner decision: no pass bar).

6. Test reports must distinguish supported Telegram limitations from resolution failures. A missing original-message link for `user`, `hidden_user`, or `chat` forwards is expected behavior, not a defect.

## Out of Scope

- Notion-to-Telegram write-back or status synchronization.
- Updating an existing Notion row from an edited or deleted Telegram message.
- Automatically merging reposts, duplicate rows, or reused JO Numbers.
- Automatically splitting several Job Orders from one Telegram message into several rows.
- Photos, stickers, files without captions, OCR, and multi-message Job Orders.
- Replying to posters in the intake topic beyond the captured reaction.
- Creating Deliverable pages.
- Backfilling Deliverable, People, Project, Department, or deadline relations after capture.
- Making Coordinator follow the Telegram poster or forwarded original author.
- Correcting historical Deadline values that were stored as UTC but intended as Manila time.
- Duration reporting that mixes those historical deadlines with corrected writes.
- Masking unlabeled credentials, unsupported credential labels, or credentials embedded in links.
- Removing or altering links with embedded access keys.
- Model-version pinning or a model-version bump routine for DeepSeek or Jev.
- A DeepSeek/Jev provider-selection bake-off.
- A pre-launch labeled acceptance gate for the Deliverable matcher.
- A required dry-run day before go-live.
- A cost dashboard or heartbeat cost report.
- An external uptime-monitoring service.
- Recovery of Telegram updates lost during outages longer than Telegram’s 24-hour retention window.
- Using n8n in the runtime write path.
- Allowing another process to write Notion rows owned by the watchdog.
- Per-message alerts for ordinary `not_jo`, `no_text`, password masking, plain Deliverable no-match, or exhausted reaction retries.

## Further Notes

- Expected volume is approximately 10–12 Job Orders per day, so correctness and traceability take priority over LLM cost and latency optimization.
- The Job Orders schema already includes `Telegram Key` and `Pending`.
- Contacts owns `Aliases`, `End of Shift`, `TelegramHandle`, and `TelegramUserID`.
- `End of Shift` contains hourly options plus `Flexible / Variable`.
- Departments must always come from the live Notion schema; any currently observed option list is informational, not a hard-coded contract.
- DeepSeek runs classify/extract. Jev runs Deliverable matching only.
- Both providers use unpinned `latest` models, and the model ID actually returned must be stored for every call.
- Jev is a typed-decision model. Any older name-echo, generated-reason, or free-text matcher design is superseded.
- The Deliverable matcher ships in shadow mode with `deliverable_link_enabled` off.
- The End of Shift 23:59 fallback is a deliberate exception for a derived deadline, not permission to fabricate a poster-stated value. It must always be surfaced to ops.
- Straight-to-live applies to the capture pipeline; it does not override the Deliverable matcher’s separate shadow-mode safeguard.
- Outages longer than 24 hours are an accepted gap. The private ops topic’s human members noticing a missing 07:00 heartbeat is the chosen detector.
- Build-time values include the real intake and ops chat/topic IDs, Priya’s Contacts page ID, the owner’s real Telegram handle, and the captured reaction supported by the real group.

## Owner decisions (2026-10-21)

- **Extraction pass bar**: none. Per-field extraction accuracy is reported only and does not gate CI.
- **Date plus shift phrase**: `Aug 26, end of shift` means the assignee's End of Shift hour on Aug 26, or the next morning for a shift ending before noon (Implementation Decision 8).
- **Jev transport**: Jev is called through OpenRouter's Decisions API with `OPENROUTER_API_KEY`. #26 holds the request and response format.

## Revision log

- 2026-10-21: resolved the gaps found when comparing this PRD with the design tickets: ledger migrations per ticket, Name and Notes formats, deadline year inference, date-plus-shift phrases and the full corpus deadline table, combined ops report wording, exact triggers for the unparsed-deadline and header-date checks, attempt accounting and manual retry, edit handling for uncaptured messages, the Jev adapter interface, reaction lookup via `getChat`, the health check, and the configuration reference. Details live in #20–#27; `docs/issues/` mirrors these issues.
- 2026-10-21: owner accepted the defaults for the extraction pass bar and the date-plus-shift rule, and chose OpenRouter as Jev's transport (#26).

