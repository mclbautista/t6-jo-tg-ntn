---
title: Deliverable matching
type: grilling
mode: HITL
status: closed
assignee: claude
blocked_by: [Job Order field mapping into Notion, Resolving people and projects from free text, Classifier and extractor: model and call design]
---

## Question

The field-mapping ticket left the Deliverable relation blank in v1 and named matching an open gap and a goal. How does the watchdog link a captured JO to the Deliverable pages it belongs to, without inventing links?

Facts from the live Notion data (fetched 2026-09-28):
- Deliverables (`collection://0a4ff084-3138-429c-8dbe-4d802aded74f`) has 9 rows; 3 are dated Sept 2026, the rest 2024-2025. Names follow project + cut tag + purpose or week (`Moonglow FTR SCR`, `Mag-Iina FTR SCR`, `Someone Someday WK11 VFX`, `Mainpic DCP`).
- Each Deliverable has a `🗺️ Projects` relation. Its `Job Orders` relation is the two-way counterpart of the JO's `Deliverable` relation, so writing from the JO side is enough.
- Status is a lifecycle (Staged, Waiting for Lock, In Progress, Blocked, Waiting for Comments, Mastering, Delivered, QC, QC Passed, Distributed, Archived); Deliverable Class is Master or Archival.
- `OCP Intake ID` and `OCP Intake Source` exist but are empty on all 9 rows, so they are not usable as keys.
- 6 JO rows have a Deliverable link; 3 are obvious test rows, leaving 3 real pairs: Moonglow JO #32 to `Moonglow FTR SCR`, SOMSOM #0155 to `Someone Someday WK11 VFX`, Mag-Iina JO 30 to `Mag-Iina FTR SCR`. Two are derivable from the message text; the Moonglow link (Purpose "For London FF video library") rests on coordinator knowledge, not words.
- Deliverables appear to be created by the coordinator in batches, close to when JOs reference them.

## Resolution

Resolved 2026-09-28 in a grilling session with the owner. Every question taken at the recommended answer, with one owner addition (Jev as the matcher, chosen deliberately as a way to learn Jev).

**What the link is for, and which error is worse.** Treated as a coordinator convenience. A missing link costs one manual click; a wrong link sits on a page the coordinator trusts. If the JO's text does not clearly support a Deliverable, the relation stays blank. Principle: **Precision over recall when a human can fill the gap** (and **Blank is a fact, filler is a fabrication**). If something downstream later depends on this link, revisit.

**Link-only, never create.** The watchdog never creates a Deliverable page. Deliverables carry lifecycle fields the watchdog cannot know (Status, Class, Playdate, Deadline), and the table is coordinator-curated. If no Deliverable exists, the relation is blank. Principle: **Resolve, don't generate**.

**Candidate set.** If the JO has a resolved Project, the candidates are the Deliverables whose `🗺️ Projects` relation contains that project's page id. If the Project is unresolved, there is no match attempt and the relation is blank (the unresolved project is already reported by the people-and-projects rules). Candidates come from a live query per JO (filter `relation_contains` on the project id, paginated), never from the 6-hour cache, because Deliverables are created close to the JOs that reference them and a stale cache would miss them. If the project has zero Deliverables, the matcher call is skipped and the outcome is `none`. Principle: **Scope at the filter, not the grant**.

**No status or class filter in v1.** Archived and Distributed Deliverables stay in the candidate set, because archive-purpose JOs (for example "[ARCHIVE] 2 VALID IDs") plausibly point at them. Status and Class are not passed to the model either, so any accuracy gain from adding them can be measured in the eval first. Principle: **Never drop silently**.

**Who decides: a separate Jev call over the shortlist.** The classify/extract call stays roster-blind, exactly as the classifier ticket decided. The matcher is a second, separate structured call to Jev, run after extraction and project resolution. The owner chose Jev on purpose as a way to learn it; acceptance is still decided by the eval below. This is a deliberate, bounded exception to "the model never sees the roster": the list is short, project-scoped, and the model may only choose from it. Principle: **Resolve, don't generate** (the model proposes text; code resolves identity).

Matcher contract, each as if-then:
- **Input**: the masked message text, the resolved project's name, and the candidate Deliverable names only. No page ids, no other project's Deliverables, no People roster, no today's date. If the text contains a credential, it has already been masked (see Secrets in raw text).
- **Output**: zero or more entries, each with `name` (echoed verbatim from the list), `verdict` (`match` or `maybe`) and a one-sentence `reason`.
- **Validation by code**: if an echoed name equals a candidate name after case and whitespace normalization, it resolves to that candidate's page id. If it does not, the entry is discarded. If two candidates normalize to the same name, neither is linked and the case is reported as a hint (ambiguous).
- **Only `match` is linked.** A `maybe` leaves the relation blank and is reported to ops as a hint. Principle: **Annotate, don't gate**.

**Zero to many links.** The relation allows several Deliverables, and a JO can legitimately serve more than one (a package with an FTR and a promo). Each named entry is validated and linked independently. Exactly-one is not forced, because that would make the multi-Deliverable JO worse than blank.

**No backfill.** The matcher runs before the Notion write, in the same queued job, and the row is written once. If the Deliverable is created after the JO is captured, the relation stays blank and the coordinator links it by hand during the "Needs Intake Review" cleanup. This matches how People and Projects behave and keeps writes idempotent. Principle: **Idempotent writes**.

**Matcher failure does not block the write.** If the matcher call fails (invalid JSON, schema violation, timeout, API error), retry once inline, then write the row with a blank Deliverable and post one ops line: "Deliverable match skipped: matcher unavailable" with the Notion row link. This deliberately differs from the classifier rule: a classifier failure loses the whole row's content, a matcher failure loses one optional relation the coordinator already fills by hand, and waiting through the 5-attempt backoff would delay every JO behind it. The link is never added after the fact. Principle: **Annotate, don't gate**.

**Reporting.**
- A plain no-match, and an unresolved project, get no per-message post (most JOs, such as "Delete files on frameio", legitimately have no Deliverable).
- A `maybe` or an ambiguous-name case posts one ops line in the locked format below, no @mention by default.
- The daily heartbeat gains four lines, one fact per line:
  - `Deliverables linked: N`
  - `Deliverables hinted: N`
  - `Deliverables none: N`
  - `Deliverables skipped: N`

Hint message (line-broken, no tag):

> ⚠️ **Deliverable hint (not linked)**
> `MG - TOC - JO #41`
>
> Possible Deliverable: Moonglow FTR SCR
> Reason: <one sentence from the model>
>
> [Notion row](https://notion.so/…)
> [Telegram message](https://t.me/c/…)

**Traceability.** For every message that reaches the matcher, the ledger stores the model id, the prompt-version tag, and a snapshot of the candidate list (names and page ids) that the model saw. Temperature is 0 and the model version is pinned. A wrong link can therefore be replayed against exactly what Jev saw. Principle: **Reproducible by default**.

**Acceptance and eval.** Precision first. The owner labels about 30 corpus JOs with the Deliverable each belongs to (or "none") and supplies the Deliverable names that would exist for those projects; the eval runs Jev against those per-project candidate lists. **Pass = zero wrong `match` links on the labeled set.** Recall is reported, not gated. Few-shot examples come only from outside the labeled set. The three real pairs, including the hard Moonglow case, are a fixed regression check. Principles: **Never grade on the training set**, **Decide by eval, not by reputation** (the model choice was made for learning, so passing the eval is what earns it a place in production).

**Amendments this ticket makes to earlier decisions.**
- Job Order field mapping: Deliverable is no longer always blank in v1; it is filled by this matcher when a `match` verdict validates.
- Classifier and extractor design: the classify/extract call is still roster-blind; the matcher is a separate call and is the only place a candidate list is shown to a model.
- Notion write path: the integration must also have the Deliverables data source shared with it (in addition to Job Orders, People and Projects), or the query and the relation write will fail.
- Ops-topic mock-ups: the heartbeat gains the four Deliverables lines; a new Deliverable-hint message format is added.
- Reliability: none. The matcher rides the existing queue and ledger.

**Build-time additions.** Share Deliverables with the integration. Smoke-test a `relation_contains` query on `🗺️ Projects` against the live data source, including a just-created Deliverable. Confirm that writing `Deliverable` from the JO side populates the Deliverable's `Job Orders` side. Confirm Jev's structured output and name-echo behavior against the labeled set.

**Waits on the owner.** The labeled set of about 30 JOs, plus the Deliverable names per project for those JOs.

Principles carried into this ticket: **Precision over recall when a human can fill the gap**, **Resolve, don't generate**, **Blank is a fact, filler is a fabrication**, **Scope at the filter, not the grant**, **Annotate, don't gate**, **Never drop silently**, **Idempotent writes**, **Reproducible by default**, **Never grade on the training set**, **Decide by eval, not by reputation**.
