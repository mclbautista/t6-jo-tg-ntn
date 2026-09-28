---
title: Job Order field mapping into Notion
type: grilling
mode: HITL
status: closed
assignee: claude (session 2026-09-28)
blocked_by: []
---

## Question

Which Job Orders properties does the watchdog fill, and from what?

Decide, property by property: Name vs Title vs Subtitle, Date vs Deadline, Department, JO Number, Task, Purpose, Notes, Blocker, Source / Source Drive, Platform, Watermark, Link Settings, Mode of Transfer, and whether the Deliverable relation is filled or left blank.
Also decide: what status is written (always Needs Intake Review?), how a missing field is represented (blank, or a marker), whether the raw message text goes in the page body, and where the Telegram chat id, message id and poster live (new properties, page body, or a local ledger).

Reasoning aid: an explicit rule per field of the form 'if the message states X, write Y; if not, write Z'.

Added after the Telegram research: Telegram supplies the poster (the message's sender), the message date, and for forwards a separate origin type, original date and original sender. Decide which of these get a home in Notion (property, page body or local ledger) and record only the slots here; the Forwarded Job Orders ticket decides which value fills them.

Added after the Notion write-path research: each text item holds at most 2000 characters and Telegram messages can be longer, so long values (Task, Notes, raw text) either split across items or go in the page body. Health is a formula and is never written. Status is written by option name. A Telegram message id column can be added to Job Orders (by hand or by a data source PATCH). Decide what the raw-text body looks like and whether the message id is a new property or ledger-only.

## Facts from existing rows (12 most recent rows read 2026-09-28; several are obvious test rows)

- Name varies: `MG - TOC - JO #32`, `SOMSOM - TOC - #0155`, `SOY - TOC - JOB ORDER 27`. JO Number varies: `#32`, `#0155`, `27`.
- Title holds the film title plus a cut tag (for example `Moonglow FTR`). Subtitle is empty on every sampled row; "Subtitle: English" sits in Notes.
- Blocker and Mode of Transfer are empty on every sampled row; a blocker-like phrase ("Waiting for waiver") sits in Notes.
- Coordinator relation is empty on every sampled row; one People/Contacts page is in cc on nearly every row. Deliverable is filled on roughly half.
- Date is date-only (the issue day). Deadline is date-only or a datetime stored in UTC (for example 2026-09-14T21:00Z, which is 5 a.m. Manila on the 15th).
- Some Notes were written by an earlier automation ("no matching Contacts page was found").

## Resolution

Resolved 2026-09-28 in a grilling session with the owner. Verified in Notion the same day: the new `Telegram Key` and `Pending` text columns exist on Job Orders. Copy property names exactly, including `Blocker ` (trailing space), `🗂️ Coordinator` and `Source / Source Drive`.

| Property | Rule |
| --- | --- |
| Name (title) | Canonical `[PROJECT] - [DEPT] - JO #[N]` when project code, department and number all parse; otherwise the posted first line unchanged. |
| JO Number | The number as posted, leading zeros kept, with a leading "#" (`#0155`). Dedupe uses its own normalized form. |
| Title | The posted title line verbatim (film plus cut tag). |
| Subtitle | Inferred from the message. Values: `Burned-in: <language>`, `None`, or blank when the message is silent. |
| Date | Posting time as a datetime with an explicit `Asia/Manila` zone (for forwards see Forwarded Job Orders). |
| Deadline | Always a datetime with `Asia/Manila`. A stated time is used; a bare day becomes 23:59; relative phrases resolve against the posting day; unparseable stays blank (text is still in the body). |
| Department | Exact match to a Notion option after case and whitespace normalization; otherwise blank, and the unknown token is named in the ops report. |
| Status | Always `Needs Intake Review`, whatever the JO says. |
| Task, Purpose, Source / Source Drive, Platform, Link Settings, Watermark, Mode of Transfer | Filled from what the message says, close to verbatim, no translating of Taglish. Inference between fields is allowed where the meaning is clear. |
| Blocker | Only from an explicit blocker. |
| Pending (new text column) | The JO's "Pending" line: what is supposed to happen next in a handoff. |
| Notes | The message's own notes or instructions; anything that fits no field, as posted. |
| Projects, Assigned, cc | Filled when resolvable (rules in Resolving people and projects). cc always includes Leizette, the traffic coordinator, as a standing rule kept in config. |
| Coordinator | Blank in v1. |
| Deliverable | Blank in v1. Matching is an open gap and a goal (see the map). |
| Telegram Key (new text column) | Chat id plus message id; exact format is set in Re-posts, duplicates and idempotency. |
| Health | Never written (formula). |
| Page body | A source block (poster display name, Manila timestamp, forward origin if any, link to the original message) followed by the raw message text verbatim in blocks of 2000 characters or fewer. Poster lives in the body only. |
| Missing values | Blank. The watchdog writes no commentary into properties or body; findings go to the ops topic. |

Named principles: **Forgiving in, strict out** (accept messy human input, write only strict schema values), **Display for humans, key for machines**, **Blank is a fact, filler is a fabrication**, **Lossless capture** (raw text always in the body), **Schema is the contract**.

Confirmed by the owner after the resolution was written (2026-09-28):
- The Task, Purpose, Source and other text-field rule is confirmed: fill close to verbatim and infer where clear. Principle: **Infer where clear, keep the original** (inference is safe because the raw text stays in the body).
- The old `21:00Z`-style deadlines were meant as 9 p.m. Manila. Those rows were stored as UTC, so they read 8 hours late. Every new write carries an explicit `Asia/Manila` zone. Fixing the old rows is out of scope for the watchdog (see the map).
