---
title: Ops-topic message mock-ups
type: prototype
mode: HITL
status: closed
assignee:
blocked_by: [Verification rules and ops-topic reporting]
---

## Question

How should the ops-topic messages read and look?

Make a cheap set of mock messages to react to: a flagged JO, an unresolved person or project, a duplicate, an outage alert, and a heartbeat. Show the owner wording, length and tagging so the spec can lock them. Link the mock-ups as an asset.

## Resolution

Locked with the owner 2026-09-28. Format rule applied throughout: default to line breaks for vertical readability — one fact per line, not comma-joined lists — since these are scanned fast, not read as prose. Tagging follows the Verification and ops-topic reporting ticket's rule (no @mention by default; tag only when a specific person is the one who has to act).

### 1. Flagged JO
*(a check that needs no one's specific hand — e.g. deadline-in-past + header-date mismatch — gets no tag)*

> ⚠️ **Flagged on capture**
> `MG - TOC - JO #41`
>
> Deadline is before the message was posted
> Header date ("Sept 21 / SUNDAY") doesn't match the actual post date (Mon, Sept 22)
>
> [Notion row](https://notion.so/…)
> [Telegram message](https://t.me/c/…)

### 2. Unresolved person/project
*(unresolved person/project is one of the three checks that tags Priya, per the verification ticket)*

> ⚠️ **Needs a manual link**
> `#0087`
>
> Project mention "PP2 APC" didn't match any known Project Code
> Assignee "Ramon but maybe Lena" is ambiguous
>
> [Notion row](https://notion.so/…)
> [Telegram message](https://t.me/c/…)
>
> @priya

### 3. Duplicate / reused number
*(not in the tag-Priya list — no tag, consistent with the settled rule)*

> ⚠️ **Possible number reuse**
> `SOMSOM - TOC - #0156`
>
> #0156 already used on [an earlier row](https://notion.so/…) for this project+department, 3 days ago
>
> [Notion row](https://notion.so/…)
> [Telegram message](https://t.me/c/…)

### 4. Outage alert
*(5-attempt retry exhaustion, from Reliability and operations — tags the owner, per the owner's explicit call, since this is the one message type closest to a true system outage rather than a per-JO data quality issue)*

> 🔴 **Write failed after 5 attempts**
> Message still queued, not lost
>
> Telegram message: [link](https://t.me/c/…)
> Last error: Notion 503 (attempt 5 of 5, given up at 22:47)
> Will not retry automatically — needs a manual nudge or a code fix
>
> @cyril

Build-time note: `@cyril` is a placeholder — the real Telegram handle isn't on file here and goes into config the same way chat/topic ids do (Reliability and operations).

### 5. Daily heartbeat

> 📋 **Daily heartbeat — Sept 28**
>
> Captured: 11
> Flagged: 2
> Ignored (not a JO): 3
> No-text forwards: 1
> Reaction failed: 0
>
> Queue: 0 stuck, 0 pending retry
>
> Ignored, first lines:
> "lunch ba tayo mamaya"
> "ok noted"
> "👍"

Owner confirmed showing the actual ignored-message first lines (not just a count) is fine given the ops topic is private to 3 people — this is what makes a wrongly-ignored real JO catchable, per *never drop silently* from the classifier ticket.

Named principles carried into this ticket: **never drop silently** (heartbeat shows real content, not just counts); **annotate, don't gate** (every mock-up format assumes the Notion row already exists — these report on a completed write, never block one).
