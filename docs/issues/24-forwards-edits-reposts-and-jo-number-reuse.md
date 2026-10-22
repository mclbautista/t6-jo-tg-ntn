<!-- Mirror of https://github.com/mclbautista/t6-jo-tg-ntn/issues/24. Keep identical: change the issue and this file together. -->

# 7. Forwards, edits, reposts, and JO-number reuse

## Parent

#28

## What to build

Complete Telegram provenance and correction behavior for forwards, edits, reposts, and JO Number reuse. Every supported source remains traceable, every new Telegram message still creates its own row, and suspicious duplication is annotated without merging, suppressing, or rewriting captured Job Orders.

## Acceptance criteria

- [ ] `user`, `hidden_user`, `chat`, and `channel` forward origins all produce truthful source-block text with `posted by` and the best available `originally from` identity.
- [ ] Date and relative-deadline calculations use the intake forward time; original-forward time is preserved as context only.
- [ ] Channel-origin forwards include an original-message link only when Telegram supplies both chat and message identity; other origin types never receive fabricated links.
- [ ] Every non-`user` forward origin produces the required ops finding without blocking capture.
- [ ] Recorded Telegram contract fixtures cover ordinary text, captions, all four forward origins, public/private channel links, edits, and missing provenance fields.
- [ ] Edits to an already captured message are received and reported as “edited after capture, row not updated”; no Notion property or body is changed.
- [ ] Corrections require a fresh Telegram message and therefore a fresh `Telegram Key` and row.
- [ ] Content-level duplicate detection runs only when Project, Department, and JO Number parsed as an integer are all available.
- [ ] Leading-zero and `#` variants normalize to the same integer for reuse detection while the displayed JO Number remains verbatim.
- [ ] An exact repost or reused number with a new Telegram message ID always creates a new row, adds the earlier-row reference to Notes, and creates the correct ops finding.
- [ ] Partial or ambiguous duplicate keys skip content-duplicate detection instead of generating a false-positive finding.
- [ ] Tests verify one-row-per-message behavior, exact reposts, reused numbers, incomplete keys, edit handling, and provenance output through the whole pipeline with real SQLite and fake adapters.
- [ ] Edits to messages that are not yet captured, ignored, or unknown follow the edit table under Technical details.
- [ ] Migration 6 is applied as specified under Technical details and documented in `docs/ledger-schema.md`.

## Technical details

### Source block by origin type

| Origin | `originally from` | Original link |
|---|---|---|
| none | (line omitted) | — |
| `user` | `<first last> (@username)`; resolved Contact name in brackets when the user id or handle matches | none |
| `hidden_user` | `<sender_user_name>` | none |
| `chat` | `<chat title>`, plus `— <author_signature>` when present | none |
| `channel` | `<channel title>`, plus `— <author_signature>` when present | yes |

The block always shows `Posted by: <sender display name> (@username)`, `Posted at: <Manila time>`, `Origin: <type>`, `Original time: <Manila time>` (forwards only, context), and the intake link.

### Edits

`allowed_updates` includes `edited_message`. Edits pass the same chat and topic filter.

| Original message state | Action |
|---|---|
| `written` or `done` | `edits` row, `edit_after_capture` system message (#23) |
| `received` … `writing` | `edits` row; the row is written from the original text; the same system message is posted once it is captured |
| `ignored` | `edits` row only; not re-classified, not reported |
| not in the ledger | `ignore_log` row with reason `edit_unknown_message` |

### Duplicate keys

- `jo_key` = `<Project page id>|<Department option>|<jo_number_int>`, set only when all three resolve.
- `text_sha256` = SHA-256 of the whitespace-normalized original text.
- Exact re-post: an earlier message with the same `text_sha256` and a Notion page. Number reuse: an earlier message with the same `jo_key` and different text. Exact re-post wins if both apply. The earliest matching row is referenced.

### Migration 6

- New `messages` columns: `jo_key TEXT`, `text_sha256 TEXT`, `forward_origin_type TEXT`. Indexes on `jo_key` and `text_sha256`.
- New table `edits`: `id INTEGER PK`, `telegram_key TEXT NOT NULL`, `update_id INTEGER NOT NULL`, `edited_at TEXT NOT NULL`, `outcome TEXT NOT NULL` (`reported` | `pending_capture` | `ignored_message`), `created_at TEXT NOT NULL`.

## Blocked by

- #23
- #39
