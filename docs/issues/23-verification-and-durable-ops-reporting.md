<!-- Mirror of https://github.com/mclbautista/t6-jo-tg-ntn/issues/23. Keep identical: change the issue and this file together. -->

# 6. Verification and durable ops reporting

## Parent

#28

## What to build

Add deterministic verification and durable private-ops reporting without gating the Notion write. Every capture must produce the correct binary findings, vertical report format, targeted mentions, and durable outbox records in the same transaction that marks the Job Order written.

## Acceptance criteria

- [ ] The nine checks cover unresolved person or Project, unknown Department, duplicate/reused JO Number, non-`user` forward origin, DeepSeek `unsure`, deadline before posting, deadline-shaped text that failed to parse, blank Task or Title, and material header-date mismatch.
- [ ] Additional findings cover suspected multiple Job Orders, fuzzy-only People matches, End of Shift fallbacks, duplicate Telegram Keys, edits after capture, matcher skips when later introduced, and read-back mismatches.
- [ ] Verification is deterministic and makes no additional LLM call.
- [ ] Findings are binary report/don’t-report; there are no severity tiers and no finding blocks an otherwise valid Notion write.
- [ ] One immediate report per triggered capture uses the locked vertical, one-fact-per-line format and includes the Notion row link, Job Order Name or fallback first line, triggered checks, relevant model reasons, and intake-topic message link.
- [ ] The full raw message is excluded from reports, and every outbound text field passes through the shared masker.
- [ ] An original-message link appears only for a channel-origin forward with sufficient Telegram metadata.
- [ ] No one is tagged by default; Leizette is tagged only for unresolved People/Projects, unknown Department, and suspected multiple Job Orders; the owner is reserved for exhausted outage alerts.
- [ ] The five locked report families and current amendments are represented, including probability-style Deliverable hints, fuzzy-match lines, `Passwords withheld`, and model-change wording.
- [ ] Pending outbox entries are inserted in the same SQLite transaction that moves a message to `written`.
- [ ] Outbox delivery is at-least-once with traceable IDs; a crash may duplicate a report but cannot omit the pending report.
- [ ] Reaction failure remains independent of ops durability and never changes the fixed meaning of the captured reaction.
- [ ] Automated tests verify report content, masking, mentions, channel-link limitations, transaction atomicity, and crash-safe at-least-once delivery using real SQLite and fake Telegram.
- [ ] Every finding renders with the exact line wording in the table under Technical details, and all of a capture's findings share one combined report.
- [ ] `deadline_unparsed` fires for any non-empty, non-placeholder `deadline_raw` that produced a blank Deadline; `header_date_mismatch` fires when the parsed header date differs from the Manila posting date by more than `header_date_tolerance_days` (default 1).
- [ ] Migration 5 is applied as specified under Technical details and documented in `docs/ledger-schema.md`.

## Technical details

### Combined capture report

All findings for one capture go into one message:

Display fields (reasons, Title, Names, finding payloads) are masked, then line breaks and runs of whitespace collapse to one space, so every labelled fact and every finding is exactly one physical line (D2). Stored and raw fields are never flattened.

```
⚠️ Flagged on capture
`<Name or masked first line>`

<one line per finding, in the order of the table below>

[Notion row](PAGE_URL)
[Telegram message](INTAKE_LINK)
[Original message](CHANNEL_LINK)      ← channel-origin forwards only

@<leizette handle>                      ← only if any finding is tagged
```

A capture with no findings posts nothing. `PAGE_URL` is the Notion page URL. `INTAKE_LINK`: `https://t.me/c/<chat id without -100>/<topic id>/<message id>`. `CHANNEL_LINK`: `https://t.me/<username>/<id>` for a public channel, `https://t.me/c/<id>/<id>` for a private one.

### Finding lines

| Code | Line | Tags Leizette |
|---|---|---|
| `unsure` | `Classifier unsure: <reasons>` | no |
| `multiple_jo` | `Looks like more than one JO in this message, split by hand: <reasons>` | yes |
| `unresolved_person` | `Assignee "<raw>" didn't match a contact` (or `cc "<raw>"`) | yes |
| `ambiguous_person` | `Assignee "<raw>" matches more than one contact` | yes |
| `unresolved_project` | `Project mention "<raw>" didn't match any known Project Code` | yes |
| `unknown_department` | `Department "<token>" isn't a Notion option` | yes |
| `fuzzy_person` | `Matched by name: "<raw>" → <Contact name>` | no |
| `exact_repost` | `Exact re-post of [an earlier row](URL), <n> days ago` | no |
| `number_reuse` | `<JO Number> already used on [an earlier row](URL) for this project+department, <n> days ago` | no |
| `forward_origin` | `Forwarded from a <hidden user / group / channel>: <name>` | no |
| `title_blank` / `task_blank` | `Title is blank` / `Task is blank` | no |
| `deadline_past` | `Deadline is before the message was posted` | no |
| `deadline_unparsed` | `Deadline "<raw>" couldn't be turned into a date` | no |
| `asap` | `ASAP — high priority` | no |
| `shift_fallback` | `Deadline defaulted to 23:59: <names> have no fixed End of Shift` or `: no assignee resolved` | no |
| `header_date_mismatch` | `Header date ("<raw>") doesn't match the actual post date (<Ddd, Mon D>)` | no |
| `deliverable_hint` | `Possible Deliverable: <name> (match probability <p>)` (#26) | no |
| `matcher_skipped` | `Deliverable match skipped: matcher unavailable` (#26) | no |
| `readback_mismatch` | `Notion row differs from the plan: <property names>` | no |
| `duplicate_key` | `More than one Notion row has this Telegram Key: [row](URL), [row](URL)` | no |

### Separate system messages

```
✏️ Edited after capture, row not updated
`<Name>`

[Notion row](URL)
[Telegram message](URL)
```

```
⚠️ Notion cache is stale
<People | Projects | Departments>: last refreshed <Mon D HH:MM> (<h> hours ago)
Last error: <masked error>
```

The outage alert and the daily heartbeat are specified in #25.

### Migration 5

- New table `outbox`: `id INTEGER PK`, `message_id INTEGER REFERENCES messages(id)` (null for system messages), `kind TEXT NOT NULL` (`capture_report`, `edit_after_capture`, `stale_cache`, `outage_alert`, `heartbeat`, `dry_run_plan`), `chat_id INTEGER NOT NULL`, `topic_id INTEGER NOT NULL`, `text TEXT NOT NULL` (rendered and masked), `state TEXT NOT NULL` (`pending` | `sent` | `failed`), `attempts INTEGER NOT NULL DEFAULT 0`, `next_attempt_at TEXT`, `last_error TEXT`, `telegram_message_id INTEGER`, `created_at TEXT NOT NULL`, `sent_at TEXT`. Index on `(state, next_attempt_at)`.
- New `messages` columns: `findings_json TEXT` (the final finding list, copied from the plan plus any read-back findings), `reaction_state TEXT` (`pending` | `set` | `failed`).

### Configuration

`ops_chat_id`, `ops_topic_id`, `leizette_telegram_handle`, `header_date_tolerance_days` (1).

## Blocked by

- #19
- #20
- #22
