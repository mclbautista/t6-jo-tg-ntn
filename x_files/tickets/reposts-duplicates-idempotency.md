---
title: Re-posts, duplicates and idempotency
type: grilling
mode: HITL
status: closed
assignee: claude
blocked_by: [Notion write path for Job Orders, Job Order field mapping into Notion]
---

## Question

Edits and deletes are ignored and a correction is a fresh post. What happens when a new message repeats an existing JO number or looks like a correction?

Decide: create a second row, update the first, or only report it; where the idempotency key (chat id + message id) lives; how a crash or restart avoids double-writing; and what the coordinator sees when two rows describe the same job.

Added after the Telegram research: edits can be filtered before delivery (`allowed_updates = ["message"]`) or received and deliberately not applied. Filtering is free but silent, which conflicts with Never drop silently. Decide: (a) filter edits silently, or (b) receive them and post a short "edited after capture, row not updated" note to the ops topic without changing the row. Edits stay out of scope for updating rows either way.

Added after the Notion write-path research: page creation has no documented idempotency header, so the key and the ledger are ours (Own your dedupe key). A query-before-create check is possible (data source queries take filters), but the docs don't say how fast a fresh row appears in a query, so decide whether the local ledger is the authority and the Notion query only a cross-check.

Added after the field mapping: the Telegram Key text column now exists. Decide its exact format (chat id plus message id) and the normalized JO key (project, department, number as integer) used for duplicate checks, since JO Number is stored as posted with "#" and leading zeros.

Added after the sample corpus (77 JOs): three JOs were posted twice with identical text (frameio cleanup JO 033, LS JO 101, and 58 JO 0041), so exact re-posts happen. `MFG - TOC - JO #74` appears on two different JOs two days apart, so project plus number is not unique. Numbers run per project across departments (22, 25, 27, 30, 31 on one project) and are not strictly increasing. Decide what an exact re-post does (skip, or create and report), what a repeated number with different content does, and which key the duplicate check uses.

## Resolution

Message-level idempotency (never double-write the same Telegram message, even after a crash) was already settled by the reliability ticket: the local SQLite ledger, keyed on chat id + message id, owns that. This ticket is about a separate, content-level question — recognizing when *two different messages* describe what looks like the same JO.

- **Edits**: received, not filtered at the API. Per **never drop silently**, an edit to an already-captured message posts "edited after capture, row not updated" to the ops topic without touching the row. The row itself is still never updated — write-back and edit-application stay out of scope — this only makes the silence visible instead of hiding it in an `allowed_updates` filter.
- **Telegram Key format**: `<chat_id>:<message_id>`, exactly matching the ledger's own idempotency key, so any row traces back to its source message with no separate lookup.
- **Duplicate detection runs against the local ledger, not a live Notion query.** The ledger already holds every captured message and is always available at write time; Notion's read-after-write freshness isn't documented (per the write-path ticket), so it stays a cross-check the watchdog doesn't rely on. **Own your dedupe key** extends here: the ledger owns both message identity and content-duplicate detection.
- **Normalized JO key**: project + department + JO number as an integer, stripping "#" and leading zeros — raw-string matching on JO Number is unreliable (it's stored as posted). The check only runs when project, department and number all parse; if any piece is missing, the check is silently skipped for that message. A missed hint is cheap; a false-positive hint on partial data would erode trust in the ops reports faster than an occasional miss costs.
- **Exact re-post (identical text, new message id)**: always creates a new row — one row per Telegram message, no exceptions, since merging is a Notion-side human decision (write-back is out of scope) and the watchdog can't safely treat text-equality as JO-equality on its own. It's flagged: reported to ops, and the new row's Notes gets a line referencing the earlier page (there's no "Duplicate Of" relation in the schema, so this is a text reference/link, not a relation).
- **Repeated JO number, different content**: same treatment as an exact re-post — always creates a new row, always flagged to ops and in Notes, worded as "possible number reuse" rather than "possible re-post." The watchdog can't distinguish a coordinator typo from a legitimate reused number; a human can.

Principles carried into this ticket: **never drop silently** (edits after capture are surfaced, not filtered away for free), **own your dedupe key** (the local ledger is authoritative for both message identity and content duplicates), **annotate, don't gate** (every duplicate case still creates a row; it's flagged, never blocked or merged).
