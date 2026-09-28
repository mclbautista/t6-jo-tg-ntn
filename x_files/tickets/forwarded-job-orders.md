---
title: Forwarded Job Orders: poster, timestamp and origin
type: grilling
mode: HITL
status: closed
assignee: claude
blocked_by: [Job Order field mapping into Notion]
---

## Question

For a forwarded JO, whose identity and whose time does the watchdog treat as "the JO's", and what does it do when the origin is not fully known?

Facts from the Telegram research: the forwarder is the message's own sender; the original author and original date exist only in `forward_origin`; a `hidden_user` origin carries a display name with no user id; `chat` and `channel` origins identify a source chat rather than a person.

Decide:
- Who counts as the poster (the forwarder, the original author, or both recorded), and which one can fill the Coordinator relation.
- Which timestamp fills Date and any deadline logic (forward time or original time).
- What is recorded when the origin is a hidden user, a chat or a channel (name text in Notes? ignored?), and whether a hidden or unresolvable origin is reported to the ops topic.
- Whether the LLM sees the origin metadata or only the text.

Reasoning aid: for each origin type, write the rule as 'if origin is X, the poster is Y and the time is Z'.

Added after the field mapping: the poster lives in the page body only (source block), and Date is the posting time as a datetime with an explicit Asia/Manila zone. Decide for a forward whether the posting time is the forward time or the original message time, and what the source block shows for each origin type.

## Resolution

- **Poster identity**: the source block records both, distinctly labeled — the forwarder as "posted by," the original author (when `forward_origin` resolves one) as "originally from." They can genuinely differ (the traffic coordinator forwarding someone else's JO), and collapsing them into one line would lose that. Coordinator stays blank either way, per the field-mapping ticket — this is body text, not a relation.
- **Date and deadline math**: always the forward time — when the message entered the intake topic — never the original message time. That's when the JO became actionable to the watchdog and the team; an original timestamp from days earlier would make "end of shift today" (from the deadline-phrases ticket) resolve against the wrong day. The original time, if present, is recorded in the source block for context only, not used in any calculation.
- **Source block by origin type** (if-then, all four always show something, never blank):
  - `user` (named, has a Telegram user id): name + username recorded; the same TelegramUserID/handle match already built for Assigned/cc is attempted here too, purely for the source block's benefit — never written to a relation.
  - `hidden_user` (display name, no id): the display name text, as-is; no id to match against.
  - `chat`: the source chat's title, as-is.
  - `channel`: the source channel's title, as-is.
- **Ops reporting**: any non-`user` origin (`hidden_user`, `chat`, `channel`) is reported to the ops topic — a human should notice the watchdog is working without a Telegram-resolvable person behind the forward. Per **annotate, don't gate**, this never blocks the Notion write; the row lands normally either way.
- **LLM visibility**: the LLM sees the full origin metadata (forwarder, original author if any, origin type) alongside the message text, but only for classification — a bare forward with little surrounding text ("see attached") is easier to correctly recognize as a real JO with that context. It is never a source of field *values*; Task/Purpose/Source and every other extracted field still come from the message text alone, per **resolve, don't generate** (the LLM proposes text, code resolves identity — origin metadata doesn't get a backdoor into extraction).

Principles carried into this ticket: **annotate, don't gate**, **never drop silently** (an unusual origin is always reported), **resolve, don't generate** (origin metadata informs classification, never field extraction or relations).
