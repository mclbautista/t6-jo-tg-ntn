---
label: wayfinder:amendment-log
title: Amendment log
---

# Amendment log

Every statement below is superseded by `technical-design-and-architecture.md`. The older ticket files are left as-is (history); this log is what tells a future reader that the old text is no longer current, and why. Read top to bottom; later entries can further amend earlier ones.

## 1. Laya removed; DeepSeek added

- **Superseded** (`map.md` Notes; `classifier-and-extractor-design.md`): "model is picked by bake-off (Jev vs. Laya) against the corpus."
- **Now**: DeepSeek is the classify/extract provider (JSON mode + Pydantic validation, no bake-off). Jev is kept, but only for the Deliverable matcher, also without a bake-off — the owner chose it directly. Laya is dropped entirely.
- **Why**: the owner's own decision (no eval desired for model *selection* — see amendment 3 for what still isn't waived).

## 2. Deliverable matcher redesigned for a typed-decision model

- **Superseded** (`deliverable-matching.md`): "Jev sees masked text + candidate names only and echoes zero or more names with a `match`/`maybe` verdict and a `reason`."
- **Now**: Jev is a System One model — it returns typed Choice decisions with probabilities, not text. It cannot echo a name or write a `reason`. The redesigned call sends one Choice question per candidate (keyed by page id, options `match`/`maybe`/`no`); code decides link/hint/silent from the returned probabilities against `T_link`/`T_hint` config thresholds. The ops hint format changes from a `reason` sentence to a match probability.
- **Why**: the original matcher ticket assumed a generative model; Jev's actual API doesn't support that shape.

## 3. The Deliverable eval is waived; shadow mode replaces it

- **Superseded** (`deliverable-matching.md`): "acceptance is zero wrong `match` links on an owner-labeled set of about 30 JOs."
- **Now**: no pre-launch labeled-set eval. The matcher runs in **shadow mode** on every live JO (hints only, `deliverable_link_enabled` defaults off) until the owner reviews accumulated real hints and flips the flag on.
- **Why**: an explicit owner trade-off, not an oversight — recorded so **Decide by eval, not by reputation** isn't silently violated. The fixed 3 real pairs stay as regression fixtures regardless.

## 4. Model versions are not pinned

- **Superseded** (`classifier-and-extractor-design.md`, `deliverable-matching.md`; `map.md` principle text): "model version is pinned at temperature 0... stored per message."
- **Now**: both providers run "latest" with no pin and no version-bump routine. What's kept: temperature 0, and the model id the API actually *returns* is stored per call (not just the alias requested). A heartbeat line (`Model changed: <old> → <new>`) fires only when the returned id changes day over day.
- **Why**: the owner explicitly does not want to maintain this. The **Reproducible by default** principle is redefined for these two calls as "traceable by default": every row records what produced it, even though nothing is pinned.

## 5. Person alias table moves from an abstract table into Contacts columns

- **Superseded** (`resolve-people-and-projects.md`, `deadline-phrases.md`; `map.md` Notes): "the person alias/matching table... also carries each person's shift (day, ends 7pm / night, ends 5am)."
- **Now**: two real columns on the live Contacts data source — `Aliases` (Text, comma-separated) and `End of Shift` (Select, 24 hourly options + `Flexible / Variable`), read by the existing 6-hour People cache. The coarse day/night model is replaced by an exact hour. A guarded fuzzy-match step (`rapidfuzz`, on the Contacts page title) is added as a fourth and final resolution step, after TelegramUserID, TelegramHandle, and alias.
- **Why**: an alias table the coordinator can't edit without a deploy doesn't survive contact with reality; the owner chose to store it where the coordinator already works and specified the exact shape.

## 6. End-of-Shift deadline math, and a new fallback

- **Superseded** (`deadline-phrases.md`): "shift phrases resolve via a day(7pm)/night(5am) shift attribute... using the later shift when assignees span both... reporting to ops when no assignee resolves."
- **Now**: resolves to the next occurrence of the assignee's exact `End of Shift` hour at or after posting time; latest across multiple assignees; and — this is the new part — **a blank or `Flexible / Variable` shift, or no assignee resolving at all, now defaults to 23:59 on the posting day** rather than leaving Deadline blank. Each such default still posts an ops line naming who was defaulted.
- **Why the fallback is not a violation of "blank is a fact, filler is a fabrication"**: that principle protects a poster's *stated* value from being overwritten or invented around. A shift-based deadline is inherently derived, never stated verbatim, so deriving "next occurrence" or a documented 23:59 fallback for it doesn't fabricate a fact the poster asserted — it's the same category of derivation the system already performs for a bare date. The trade is made explicitly, in service of the owner's stated goal: **Projects are built from Deliverables which are built from Job Orders** — a Job Order Health rollup (feeding Deliverable Health, then Project Health) needs a calculable Deadline on every row, and an honest ops line keeps the default from being silently mistaken for a stated fact.

## 7. Departments read from Notion — confirmed

- **Superseded** (`job-order-field-mapping.md` and the original static-config assumption): department options as a hard-coded list.
- **Now**: confirmed (was "pending owner OK" in `map.md`). Departments are read from the live schema on the same 6-hour cycle as People and Projects. A token with no live match leaves Department blank and the ops report names the token. Only a tiny header-alias map ("TO" → "TOC") stays hard-coded, and it lives in config.

## 8. Go-live: straight to live, no dry-run phase

- **Superseded** (an earlier draft of this design pass that proposed a Phase-0 dry-run day before go-live).
- **Now**: ships straight to live. `dry_run: true` stays available in config as an off-by-default rollback tool, not a required phase. The Deliverable matcher's `deliverable_link_enabled` flag remains the one staged rollout in the system, because it's the one failure mode (a wrong link) that stays silently plausible on a page the coordinator trusts.
- **Why**: owner's explicit call.

## 9. Ops-topic formats updated again

- **Superseded** (`ops-topic-mockups.md`, and the version amended by `secrets-in-raw-text.md`/`deliverable-matching.md`): the Deliverable hint format ("Reason: `<sentence>`") and the heartbeat's fixed line set.
- **Now**: the Deliverable hint shows a match probability instead of a reason sentence. The heartbeat gains: `Model changed: <old> → <new>` (only on change), and continues to carry `Passwords withheld: N` and the four Deliverable lines (linked/hinted/none/skipped) from prior amendments. A new untagged ops line appears whenever a fuzzy-only person match is used ("matched by name: '`X`' → Contact `Y`").
- **Why**: consequences of amendments 2, 4, 5 and 6 above.

## 10. LLM usage/cost visibility — resolved as a non-feature

- **Superseded** (`map.md` "Not yet specified": "LLM usage and cost visibility... waits on the model choice").
- **Now**: resolved, deliberately minimal. Token counts from both providers are stored per call (two integer columns), queryable on demand. No heartbeat line, no dashboard — the owner asked for zero upkeep here and volume (~12 JOs/day, now 2 LLM calls each) makes cost immaterial.

## 11. Outages beyond 24 hours — resolved as an accepted gap

- **Superseded** (`map.md` "Not yet specified": "possible future addition: an external uptime check on the container itself... not designed further").
- **Now**: closed as a documented, accepted gap. Detection is the three human ops-topic members noticing a missing 07:00 heartbeat. No external uptime service, by owner's explicit preference against adding another account to maintain.

## 12. The capture reaction now signals flagged captures (#91)

- **Superseded** (`tickets/verification-and-ops-reporting.md` "Reaction semantics": "one reaction, fixed meaning: captured; problems never signalled by swapping the reaction").
- **Now**: 👍 (`captured_reaction`) means "perfect, nothing for anyone to do"; 🤔 (`flagged_reaction`, default `🤔`) means "captured, but flagged". Flagged is any finding recorded at capture, the same set that triggers an ops report. The emoji is chosen from the findings `mark_written` stored, so a resumed row gets the right one. 🤔 carries no detail; the reasons stay in the ops topic. Findings still annotate and never gate the write.
- **Unchanged**: one reaction on the original intake message only; reaction failure retries inline, then sets `reaction_state = failed` and is counted on the heartbeat (a failed 🤔 is never downgraded to 👍). No backfill, no flip-back when a flag is fixed in Notion, no change on edits. A repost is its own message and gets 🤔 if it trips the duplicate or reuse finding.
- **Why**: owner's call. The earlier decision protected the wider intake topic from seeing "something's wrong"; the owner accepts that for a faster signal on which JOs need a look.
