---
title: Resolving people and projects from free text
type: grilling
mode: HITL
status: closed
assignee: claude
blocked_by: [Notion write path for Job Orders, Job Order field mapping into Notion]
---

## Question

How does the watchdog turn names and project references in free text into the page ids that Assigned, Coordinator, cc and Projects need?

Decide: who does the matching (LLM given the cached list, or fuzzy matching in code, or both); how nicknames, first names and project abbreviations are handled; what happens on no match and on multiple matches (leave the relation blank and report to the ops topic?); how the cache refreshes; and whether the poster's Telegram identity maps to the Coordinator relation.

Added after the Notion write-path research: relations are written as page ids, so matching produces ids from a cache built with paginated queries of the People and Projects data sources (100 per page). The integration must be shared with both, or relation values read back empty. Decide the refresh cadence and what a stale cache does to a brand-new project or person.

Added after the field mapping: Assigned and cc are filled when resolvable; Leizette, the traffic coordinator, is a standing cc on every JO (a config rule, resolved once to her page id and always added, whether or not the message names her). Coordinator and Deliverable stay blank in v1. An unresolved name is not lost, because the raw text is in the body; it is reported to the ops topic.

Added after the sample corpus (77 JOs): 69 have an assignee line; 37 use @handles, 12 add ate or kuya, 15 name several people, 18 add thanks, and some hedge ("Piolo but maybe Majo") or split work per task ("ate @debz38 / Piolo"). 61 have a cc line and all include the traffic coordinator; 16 have none, so the standing cc matters. Other cc names are @-handles or names with ate. Project codes are 2 to 5 characters, sometimes numeric ("58") or with spaces and brackets ("PP EP 2 (APC)"), and 8 JOs have no assignee line. Check whether the People (Contacts) data source has a Telegram handle property; handles would be the most reliable match key.

Added after fetching the live Contacts and Projects schemas (2026-09-28):
- Contacts (People, `collection://77b2a701-71db-4e88-a694-37f07303e77e`) has both a **TelegramHandle** (text) and a **TelegramUserID** (text) property. A handle-or-id match key exists already; it doesn't need to be invented.
- Projects (`collection://cc1dc02a-8f32-4c97-81e4-dc1e08d7f134`) has a **Project Code** property that is a `select`, not free text — a closed vocabulary of 142 short codes (mostly 2-6 letters, a few numeric like "58th", a few multi-word like "COLD CUT", "TAKING FLIGHT"). Matching a project mention is therefore matching free text against a bounded, known list, not open-ended lookup. `Name` is the separate title property (the full project name). Projects also has its own `Coordinator`, but it's a read-only formula there, unrelated to the Job Orders' `Coordinator` relation (which stays blank in v1).

## Resolution

**Two-stage matching, not one.** The classifier/extractor LLM only extracts raw mentions (names, handles, project text) as plain strings — it already reads the message for other fields, so this is free. A separate, deterministic matching step then resolves those strings to page ids against the cached People/Projects lists. If-then: matching against a bounded, known vocabulary (142 project codes, a finite contact roster) is a lookup problem, not a generation problem, so it belongs in testable code, not in a model call that can hallucinate an id. Principle: **resolve, don't generate** — the LLM proposes text, code decides identity.

**Person matching, in priority order:**
1. `TelegramUserID` — when the message (an @mention, a forward) carries a real Telegram user entity, this is the exact key.
2. `TelegramHandle` — when only a bare `@handle` is present in text with no resolvable entity behind it.
3. An alias table (nicknames, and names with `ate`/`kuya` stripped) mapping to a Contacts page id, as fallback for plain first names ("Piolo", "kuya Ronald").
Nothing here guesses past this chain; if none of the three resolve, it's a miss (see below).

**Project matching:** scan the raw project-code text for any of the 142 known `Project Code` values as a whole token, case-insensitive, longest-match-first (so a 4-letter code isn't shadowed by a 2-letter one that happens to be a substring). Everything else in the phrase — episode numbers, parentheticals like "(APC)" — is left alone; it's not parsed further, it just stays verbatim in the JO's own Title/Notes text.

**No match or multiple/ambiguous matches:** the relation is left blank and the raw mention is reported to the ops topic, quoted as written — same treatment whether it's zero matches or an unresolved ambiguity ("Piolo but maybe Majo"). Per **Annotate, don't gate**, this never blocks the Notion write.

**Multiple people in one line:** Assigned is a relation and accepts more than one page id, so every name that resolves is added to it. A per-task split ("ate @debz38 does X, Piolo does Y") isn't represented structurally in v1 — Assigned just lists everyone named, and the split itself stays legible in the verbatim Task text.

**Cache refresh:** both the People and Projects caches refresh on container start and on a fixed timer (every 6 hours) — frequent enough that a same-day addition is rarely missed, cheap enough to stay well inside the ~3 req/sec Notion limit. A brand-new project or hire added inside the stale window is simply a normal miss: blank relation + ops report, exactly like any other unresolved mention. Because this is one-way, edit-free capture, there's no automatic backfill once the cache catches up — the traffic coordinator links the relation by hand during intake review, the same cleanup she already does for every "Needs Intake Review" row.

**Poster identity:** does not map to Coordinator (which stays blank in v1, per the field-mapping ticket) or to anything else structural. The poster is already captured as plain identity in the source block in the page body, which is enough for traceability without wiring it to a relation.

Principles carried into this ticket: **Annotate, don't gate** (blank + report, never a block); **resolve, don't generate** (deterministic lookup owns identity resolution, the LLM only proposes text); **scope at the filter, not the grant** (each cache lookup is scoped to a known, bounded vocabulary, so a miss is informative rather than a shrug).
