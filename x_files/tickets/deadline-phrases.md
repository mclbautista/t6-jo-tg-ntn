---
title: Deadline phrases: shift-based and vague deadlines
type: grilling
mode: HITL
status: closed
assignee: claude
blocked_by: []
---

## Question

What does the watchdog write for a deadline that is not a clear date and time?

Facts from the sample corpus (77 JOs): 36 clear date and time; 7 date only; 1 time only ("1pm"); 13 a date plus a phrase ("within the day", "end of shift", "asap", "before photoshoot", "pm"); 12 a phrase only ("within shift", "end of shift", "tonight", "if keri rin po today"); 8 blank or missing. Seven carry hedges ("if keri", "if kaya", "pickup D16 shoot"). One deadline is earlier than the posting date.

Already decided: Deadline is always a datetime with `Asia/Manila`; a bare day becomes 23:59; relative phrases resolve against the posting day; unparseable stays blank. Not covered: shift-based and vague phrases.

Decide:
- What clock time "end of shift" and "within shift" mean (shift hours; do they differ by team or day?).
- What "tonight", "asap" and "within the day" resolve to.
- What happens to phrases with no clock meaning ("before photoshoot", "kung kelan matapos yung render"): blank, with the phrase kept in Notes?
- Time-only values ("1pm") resolve against the posting day.
- Hedges ("if keri"): stripped from Deadline and kept in Notes?
- A deadline earlier than the posting day: keep as stated and report, or assume the next occurrence?
- Whether the original phrase is always preserved somewhere when it becomes a datetime.

Reasoning aid: for each phrase family, write the rule as 'if the phrase is X, Deadline is Y and Notes gets Z'.

## Resolution

Shift hours (owner-confirmed): day-shift staff end at 7pm Manila, night-shift staff end at 5am Manila. These live as a shift attribute on each person in the same alias/matching table built for the people-and-projects resolution — not a separate lookup.

Per-phrase rules, each as if-then:

- **"end of shift" / "within shift"**: if the JO has one resolvable assignee with a known shift, Deadline = that shift's end time on the posting day (Asia/Manila), and Notes gets the phrase verbatim. If assignees span both shifts, Deadline = the later one (5am night-shift end) — safer to give more time than to mark something late that wasn't. If no assignee resolves at all, Deadline stays blank and it's reported to the ops topic, same as any other unresolved mention — there's no signal left to pick a shift from.
- **"tonight"**: Deadline = 23:59 Manila on the posting day — the same constant already used for a bare date, not a new one. Notes gets "tonight" verbatim.
- **"asap"**: Deadline still resolves using the same shift logic as "end of shift" (assignee's shift-end, or the later shift if it spans both), so the row keeps an orderable deadline. But ASAP is a priority signal, not a clock time — a plain datetime would look identical to an ordinary same-day deadline and hide that other JOs should be deferred for this one. So it's additionally flagged high-priority/urgent in the ops-topic report, and Notes gets "ASAP — deprioritize other JOs" verbatim.
- **Phrases with no clock meaning** ("before photoshoot", "kung kelan matapos yung render", "pickup D16 shoot"): Deadline stays blank, the phrase is kept verbatim in Notes. The traffic coordinator fixes these manually during intake review. Per **blank is a fact filler is a fabrication**, an external event the watchdog can't observe is never guessed at.
- **Time-only values** ("1pm", no date): Deadline = posting day at that time, Asia/Manila. If the result lands before the message's own post time (the stated time already passed that day), it's still written literally, not silently rolled to the next day — rolling forward would be inventing intent nobody wrote down.
- **Hedges** ("if keri", "if kaya", "if abutin the next day"): stripped out of the Deadline value itself; Deadline is the resolved datetime as if the hedge weren't there. The hedge phrase is kept verbatim in Notes. Per **lossless capture**, the condition isn't discarded, it's just not force-fit into a datetime field.
- **A deadline earlier than the posting day**: kept exactly as stated (as a datetime, even though it's before the post) and reported to the ops topic as an anomaly. Never silently assumed to mean "the next occurrence" — that's inventing intent; a human who knows the context fixes it.
- **Original phrase preservation**: whenever a phrase becomes a datetime, the phrase itself is always also written to Notes (not left only to the raw-text body already captured). Notes is what a human reads directly on the Notion row without opening Telegram, and a phrase like "end of shift" carries meaning ("this was never a hard time") that a bare datetime alone loses.

Principles carried into this ticket: **blank is a fact filler is a fabrication** (no clock meaning → blank, never a guess); **resolve, don't generate** (rolling a past deadline forward, or picking a shift with no assignee signal, would be generating intent); **lossless capture** (hedges and phrases always survive in Notes even after Deadline is normalized); **never drop silently** (an anomalous or unresolved deadline is always reported, never just written wrong and left).
