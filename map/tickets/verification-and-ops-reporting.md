---
title: Verification rules and ops-topic reporting
type: grilling
mode: HITL
status: closed
assignee:
blocked_by: [Job Order field mapping into Notion, Resolving people and projects from free text]
---

## Question

Given that verification annotates and never gates, what exactly is checked and what gets reported?

Decide: the list of checks (format problems, missing required fields, unresolved person or project, duplicate JO number, deadline in the past, ambiguous department); severity levels; which findings post to the ops topic and which stay silent; the content of a report (link to the Notion row, original text, reasons); who is tagged; immediate posts vs a digest; and how classifier-rejected chatter is logged.

Added after the Telegram research: the capture feedback on the original message is a single reaction (a non-premium bot gets one per message, from Telegram's fixed emoji set, and the group may restrict which reactions are allowed). Decide which emoji means "captured", whether the reaction is ever replaced to signal a problem or problems live only in the ops topic, and what happens when the reaction call itself fails. To verify: as far as I know a green check mark is not in Telegram's reaction set, so the emoji has to be picked from the allowed list and tested in the real group.

Added after the Notion write-path research: Department, Status and relation values must match the live schema exactly (Schema is the contract), and it is undocumented what Notion does with an unknown select name. Decide that the watchdog validates against the schema before writing and how an unknown department is recorded and reported.

## Resolution

**The check list.** Five checks already exist as side effects of earlier tickets and are not re-decided here — they're pulled into this ticket's pipeline as-is: unresolved person/project (blank relation + ops report, from Resolving people and projects), unknown department (blank + named in ops report, from Job Order field mapping), duplicate/reused JO number (always flagged, from Re-posts, duplicates and idempotency), non-`user` forward origin (always reported, from Forwarded Job Orders), and the classifier's `unsure` verdict (writes + flags with the model's reasons, from Classifier and extractor design). Four checks are new to this ticket:
- **Deadline in the past**: the resolved Deadline datetime is earlier than the message's posting time — catches both a typo and a stale forwarded JO.
- **Deadline missing but expected**: the message has deadline-shaped language but nothing parsed. A JO that's simply silent on timing is normal and is *not* flagged — only a phrase that looked like a deadline but failed to resolve triggers this.
- **Required-field emptiness**: Task or Title blank. These two are filled in all 77 corpus JOs, so a blank one is a stronger signal of a bad capture than a blank Purpose or Platform, which stay unflagged when empty.
- **Cross-field sanity**: `header_date_raw` (captured but never used to write Date/Deadline) disagrees with the actual posting date by more than a day or two — catches the "Sept 21 / FRIDAY posted on a Monday" pattern already seen in the corpus.

**Severity.** Binary, not tiered. A check either posts to the ops topic or it doesn't — there is no warn/alert split. *Annotate, don't gate* already does the prioritization work: the ops topic is a worklist, not a paged-alerting system. At ~12 messages/day, tiers would add a classification burden to every future check for no operational benefit; can be added later without touching what's built now.

**Report content.** One ops-topic message per triggered capture, containing:
- Link to the Notion row.
- The JO's Name (or first line, if Name fell back to raw because project/department/number didn't parse).
- The triggered check names, in plain language.
- The model's `reasons` string, when the trigger involves a model judgment (`unsure`, `multiple_jo_suspected`).
- A link to the message in the intake topic (always resolvable — the bot posted the reaction there).
- For **channel-origin forwards only**, a second link to the original channel message. Telegram's `MessageOriginChannel` is the only one of the four forward-origin types that carries both a chat and a message id, so it's the only one a real link can be built from (`t.me/<username>/<id>` for a public channel, `t.me/c/<id>/<msg_id>` for a private one the bot is in). `user`, `hidden_user`, and `chat` origins expose no message-level reference at all — not a resolvability gap, a data-availability one — so for those the report shows the same "posted by / originally from" text already in the Notion page's source block, with no link.
Not included: the full raw message text (already in the Notion body, one click from the row link).

**Tagging.** No @mention by default — the ops topic's fixed 3-person membership (traffic coordinator, lead technical supervisor, owner) is enough at this volume, and a blanket tag habituates people to ignore it. Exception: Leizette (traffic coordinator) is tagged specifically on checks that need her hand to resolve — unresolved person/project, unknown department, `multiple_jo_suspected` — since those are the ones only she can act on.

**Cadence.** Immediate — one post per triggered message, no digest. Volume doesn't justify batching, and the existing daily heartbeat (Reliability and operations) already gives the rollup view (ignored-message counts, reaction-failure counts) without duplicating this.

**Schema validation.** Department/Status matching happens against the same cache used for people/project resolution, refreshed on start and every 6 hours (Resolving people and projects) — not a live fetch on every write. *Schema is the contract* means checking the contract before writing, not checking it in real time; worst case on a same-day schema change is one JO sitting blank + reported for up to 6 hours, which the ops report already surfaces.

**Reaction semantics.** *(Superseded by amendment 12: 🤔 now marks flagged captures.)* One reaction, fixed meaning: it always means "captured, now in Notion," full stop. Problems — however severe — are never signaled by swapping or removing the reaction; they live only in the ops topic. The reaction is visible to the whole intake topic, while ops-worthy problems are for the 3-person ops topic only; swapping the reaction on a problem would leak "something's wrong here" to an audience that can't act on it. *Annotate, don't gate*, applied to the reaction itself: capture succeeded, so the reaction says so.

**Reaction failure.** Unlike a Notion-write failure, no data is at risk when `setMessageReaction` fails (network blip, emoji not in the chat's allowed set, lost admin rights) — the JO is already captured. Not queued, not ops-alerted per failure. Retry inline once or twice with a short (seconds-scale) backoff, then give up silently on that message; count the failure in a local counter surfaced on the daily heartbeat ("reaction failed on N messages today"). *Never drop silently* governs the JO's data, not a UI acknowledgment — a per-message ops post for a cosmetic failure would train people to ignore the topic.

**Forward-link constraint — build-time verify.** The channel-origin-only link availability (see Report content) is a Telegram data-model fact from the Bot API docs, not a preference; closed on citation. Added to the build-time smoke-test checklist: confirm actual behavior against a real forwarded channel post, alongside the other live-API verify items (unknown select names, query freshness, Retry-After, and the reaction emoji pick itself).

**Emoji pick.** Not a design decision — Telegram's allowed reaction set is fetched live per-chat via `getAvailableReactions` and may be further restricted by the group admin; there is no fixed global list to choose from ahead of time (a plain green ✅ is likely not in it). Build-time task: call `getAvailableReactions` against the real intake group and pick a "captured" emoji from what's actually offered.

Named principles carried into this ticket: **annotate, don't gate**; **never drop silently** (applied to both data and, separately but more loosely, to capture-status UI); **schema is the contract**.
