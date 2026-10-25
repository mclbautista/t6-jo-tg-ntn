---
title: Classifier and extractor: model and call design
type: grilling
mode: HITL
status: closed
assignee:
blocked_by: [Sample corpus of real intake messages, Job Order field mapping into Notion, Telegram Bot API capabilities and limits]
---

## Question

Which model runs classification and extraction, and how is the call shaped?

Decide: which model (Jev or Laya; what each is, where it runs, cost, latency, quality on the corpus); one structured call returning is-JO, fields, confidence and reasons, versus two calls; what the model sees (text, forward metadata, anything else); what happens on low confidence (still write as Needs Intake Review, or ignore with a log line); how chatter is recorded; and what happens if the model is unreachable.

Reasoning aid: for each borderline outcome, apply Never drop silently and Annotate, don't gate.

Added after the Telegram research: the model receives message text or caption only. Edited messages never arrive if edits are filtered at the API. For forwards, decide whether the four origin types (user, hidden user, chat, channel) are passed to the model as context, and how a forward with no text or caption is treated (photos are out of scope).

## Resolution

**Model choice (Q1).** Jev and Laya are both System One models with structured (JSON-schema) output, which makes them fast and cheap; volume is ~12 messages a day, so cost and latency are not deciding factors. Don't pick by spec sheet — run both against the 77-JO corpus as a bake-off and take whichever passes extraction; if they tie, take the one with native JSON-schema output. *Decide by eval, not by reputation.* Note for later: the owner flagged Jev as a plausible fit for the verification step (checking whether a captured row is actually a JO), separate from this classify/extract call — carried into the Verification and ops-topic reporting ticket, not decided here.

**One call, not two (Q2).** A single structured call returns `verdict`, `confidence`, `reasons`, and the extracted fields together. If it's a JO, the fields are the evidence for that verdict — a message with no recognizable task is probably not a JO, so splitting classification from extraction buys nothing. One call also means one failure point in the retry queue instead of two, and at 12 messages/day there's no cost reason to split.

**Where the LLM stops and code starts (Q3).** The LLM returns verbatim raw strings only, never a resolved datetime, page id, or canonical name: `header_raw`, `deadline_raw`, `assignees_raw`, `cc_raw`, `project_raw` (etc. — full field list below). Code applies the deadline-phrase rules (see Deadline phrases ticket), the canonical Name build, and person/project resolution (see Resolving people and projects ticket). A model-generated datetime can hallucinate a shift end; code applying tested rules can be unit-tested against the corpus. *Resolve, don't generate* — the same principle from identity resolution, extended to time.

**What the model sees (Q4).** Message text (or caption) plus the forward-origin metadata already decided in the Forwarded Job Orders ticket (forwarder, original author, origin type) — for classification context only, per that ticket's existing rule that origin metadata never supplies field values. The model does **not** see the project/people roster (it would "helpfully" guess a match, contradicting Q3), today's date (code resolves deadlines against the posting day, not model time), or the reply chain (out of scope for v1; multi-message JOs are already excluded).

**Verdict handling (Q5).** Three verdicts, not a numeric threshold — a missed JO costs more than a junk row, since the coordinator deletes a junk row in seconds but a silently-ignored JO becomes a missed job:
- `jo` → writes to Notion normally (status "Needs Intake Review").
- `unsure` → still writes to Notion as "Needs Intake Review," flagged to the ops topic with the model's `reasons`.
- `not_jo` → not written to Notion; instead logged to an explicit ignore log (SQLite table: message key, first line, reasons). No per-message ops post.
The daily heartbeat (from Reliability and operations) adds the ignored count plus the first lines of the last few ignored messages, so a wrong `not_jo` is catchable within a day. Principles: *never drop silently*; *annotate, don't gate* — with the asymmetry stated explicitly: when unsure, write.

**Degraded LLM (Q6).** No bare-row fallback. When the LLM is unreachable, the message stays in the existing SQLite retry queue (5 total attempts, 30s→10m backoff, per Reliability and operations) and triggers the existing ops alert after exhaustion — it is not written to Notion with partial/blank fields. A bare row can't be upgraded later (row updates are out of scope), and re-running extraction after recovery would create a second row for the same Telegram Key, violating one-row-per-message. The alert is what tells a human to act, not a degraded row. *Idempotent writes beat partial visibility.*

**No text/caption (Q7).** Photo-only, sticker, or bare file forwards with no text or caption skip the LLM call entirely and go straight to the ignore log with reason `no_text`, counted in the daily heartbeat, no per-message ops post. A photo or file *with* a caption is treated as ordinary text and goes through the normal call. *Never drop silently* — the heartbeat count is the safety net for a real JO screenshot getting missed.

**Multiple JOs in one message (Q8).** Always exactly one row per message — a single Telegram message can only have one Telegram Key, so fan-out would break `chat_id:message_id` as the dedupe key. The schema carries a boolean `multiple_jo_suspected`; when true, the row is written from the first JO's fields, the full raw text still goes in the body, and ops gets a flag ("two JOs in one message, split by hand"). None appear in the 77-message corpus, but it's an easy human mistake to guard against. Principles: *idempotent writes*; *annotate, don't gate*.

**Output schema (Q9).** Every field is a string or `null` — never a placeholder marker like "N/A" or "-" invented by the model. Placeholder values the *poster* actually typed ("-", "none", "N/A") pass through verbatim; deciding whether those mean "blank" is a verification-time concern, not the extractor's. *Blank is a fact, filler is a fabrication.*

Fields:
- Raw/verbatim: `header_raw`, `jo_number_raw`, `project_raw`, `department_raw`, `header_date_raw`, `title`, `task`, `purpose`, `source`, `watermark`, `link_settings`, `mode_of_transfer`, `platform`, `blocker`, `pending`, `notes`, `deadline_raw` (hedges kept exactly as written, e.g. "Sept 9, 8PM (if keri)" — code does stripping/Notes-copy per the deadline ticket), `assignees_raw`, `cc_raw`.
- Inferred (the one exception): `subtitle`, restricted to `Burned-in: <language>`, `None`, or `null`.
- Meta: `verdict` (`jo` / `unsure` / `not_jo`), `confidence` (`low`/`medium`/`high`, informational only, not a threshold), `reasons` (one or two short sentences), `multiple_jo_suspected` (boolean).
- `header_date_raw` exists solely so verification can compare the header's stated date against actual posting time (the corpus has "Sept 21 / FRIDAY" posted on a Monday) — it is not used to build Date/Deadline itself.

**Failure definition and traceability (Q10).** Invalid JSON, a schema violation, a timeout, or an API error all count as one failed attempt and feed the existing 5-attempt queue path — there is no separate "repair" call. Model version is pinned exactly and temperature is set to 0. The model id and a prompt-version tag are stored per message in the local ledger (not in Notion), so any row can be traced back to the exact prompt/model that produced it. *Reproducible by default* — a wrong extraction has to be replayable and debuggable.

**Prompt examples vs. eval set (Q11).** Hold out ~60 of the 77 corpus JOs, chosen to cover all header styles, as an untouched eval set; use the remaining ~15 as few-shot examples in the prompt. Never eval on a message that appears in the prompt. *Never grade on the training set.* Classifier evals (precision/recall on `jo` vs `not_jo` vs `unsure`) stay blocked on the owner's negative examples (chatter, near-misses) — until those arrive, only extraction accuracy can be scored, not classification.

## Follow-ups opened by this resolution

- Jev-for-verification is a candidate for the Verification and ops-topic reporting ticket, not decided here.
- Bake-off methodology (Jev vs. Laya against the 60-message eval set) and acceptance criteria for "passes" belong to a build-time task, not this design ticket.
