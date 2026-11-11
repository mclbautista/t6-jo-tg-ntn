<!-- Mirror of https://github.com/mclbautista/t6-jo-watchdog/issues/20. Keep identical: change the issue and this file together. -->

# 3. DeepSeek classification and extraction

## Parent

#28

## What to build

Extend the capture pipeline with one roster-blind DeepSeek classify/extract call. Supported text and captions must produce a validated, traceable `jo`, `unsure`, or `not_jo` outcome with verbatim raw fields; no-text input must bypass the model explicitly. Classification failures remain queued and never fall back to a fabricated bare Job Order.

## Acceptance criteria

- [ ] DeepSeek is called at temperature 0 through its OpenAI-compatible API in JSON mode, followed by Pydantic schema validation.
- [ ] One response contains verdict, confidence, short reasons, `multiple_jo_suspected`, and verbatim raw extraction fields; it never supplies page IDs, canonical Names, normalized datetimes, or invented placeholders.
- [ ] DeepSeek sees only masked text or caption plus permitted forward-origin metadata, and does not see People/Project rosters, today’s date, or the reply chain.
- [ ] `jo` continues through planning, `unsure` continues and creates an ops finding, and `not_jo` creates no Notion row but is written to the ignore log with its masked first line and reason.
- [ ] A supported forward with no text or caption bypasses DeepSeek and is recorded as `no_text`.
- [ ] Invalid JSON, schema violations, empty content, timeout, and provider errors all enter the retryable classifier-failure path; there is no repair call and no partial-row fallback.
- [ ] If several Job Orders are suspected, exactly one row is planned from the first extraction, the complete source remains available, and a manual-split finding is recorded.
- [ ] The returned model ID, requested alias, prompt tag, token counts, latency, and validated result are stored with the stage result.
- [ ] Roughly 15 few-shot examples remain disjoint from the roughly 60 held-out extraction fixtures.
- [ ] Recorded contract tests cover `jo`, `unsure`, `not_jo`, no-text, empty content, malformed JSON, schema mismatch, timeout, and provider failure without network access in CI.
- [ ] Classification precision/recall is not presented as accepted until real chatter and near-miss fixtures exist.
- [ ] Migration 3 is applied exactly as specified under Technical details, and `docs/ledger-schema.md` documents it.
- [ ] The few-shot message list is committed as a fixture, and the held-out extraction report prints a per-field exact-match rate without gating CI.

## Technical details

### Extraction schema (Pydantic, every field `str | None` unless noted)

- Raw/verbatim: `header_raw`, `jo_number_raw`, `project_raw`, `department_raw`, `header_date_raw`, `title`, `task`, `purpose`, `source`, `watermark`, `link_settings`, `mode_of_transfer`, `platform`, `blocker`, `pending`, `notes`, `deadline_raw` (hedges kept exactly as written), `assignees_raw`, `cc_raw`.
- Inferred: `subtitle`, one of `Burned-in: <language>`, `None`, or null.
- Meta: `verdict` (`jo` | `unsure` | `not_jo`), `confidence` (`low` | `medium` | `high`, informational only), `reasons` (one or two short sentences), `multiple_jo_suspected` (bool).
- A poster's own placeholder (`-`, `N/A`, `none`) passes through verbatim; the model never invents one.

### Model input

A single user message containing the masked text or caption, followed, for forwards only, by an origin block: origin type, forwarder display name, original author display name or chat/channel title. No date, no roster, no reply chain. The prompt carries a version tag (`prompt_tag`, e.g. `extract-v1`) stored per call; changing the prompt text means a new tag.

### Few-shot and evaluation split

- About 15 corpus Job Orders covering every header style become few-shot examples. Their ids are committed as `tests/fixtures/fewshot_ids.json`.
- The remaining corpus Job Orders form the held-out extraction set. Never evaluate on a few-shot message.
- The extraction report scores each extracted field as an exact match after whitespace normalization, against hand-written golden values in the fixtures. It reports per-field rates and is **not a CI gate**; the owner chose no pass bar (#28, Owner decisions).

### Failure classes

`invalid_json`, `schema_violation`, `empty_content`, `timeout`, `api_error`. Each is one failed attempt on the message's retry counter (see #25 for the schedule; this ticket may use a simple fixed retry until #25 lands).

### Migration 3

SQLite cannot alter a `CHECK` constraint, so migration 3 rebuilds `messages` (create `messages_new`, copy, drop, rename, recreate indexes) inside one transaction.

- `messages.state` allows `received`, `extracted`, `planned`, `writing`, `written`, `done`, `ignored`.
- New `messages` columns: `posted_at TEXT` (Telegram message date as ISO-8601 with `+08:00`), `attempts INTEGER NOT NULL DEFAULT 0`, `next_attempt_at TEXT`, `last_error TEXT`, `verdict TEXT`, `ignore_reason TEXT` (`not_jo` | `no_text`).
- New table `stage_results`: `id INTEGER PK`, `message_id INTEGER NOT NULL REFERENCES messages(id)`, `stage TEXT NOT NULL` (`extract` now, `match` in #26), `result_json TEXT NOT NULL`, `created_at TEXT NOT NULL`, `UNIQUE(message_id, stage)`.
- New table `llm_calls`: `id INTEGER PK`, `message_id INTEGER REFERENCES messages(id)`, `provider TEXT NOT NULL` (`deepseek` | `jev`), `purpose TEXT NOT NULL` (`classify_extract` | `deliverable_match`), `requested_model TEXT NOT NULL`, `returned_model TEXT`, `prompt_tag TEXT NOT NULL`, `masker_version TEXT NOT NULL`, `input_tokens INTEGER`, `output_tokens INTEGER`, `latency_ms INTEGER`, `outcome TEXT NOT NULL` (`ok` or a failure class), `error TEXT`, `detail_json TEXT` (Jev candidates and probabilities in #26), `created_at TEXT NOT NULL`. Index on `(created_at)` for the heartbeat.
- `not_jo` and `no_text` messages move to `ignored` and also get an `ignore_log` row (reason `not_jo` or `no_text`, masked first line).

### Configuration and secrets

- `.env`: `DEEPSEEK_API_KEY`.
- `config.yaml`: `deepseek_base_url` (default `https://api.deepseek.com`), `deepseek_model` (the "latest" alias, no pin), `deepseek_timeout_seconds` (default 60).

## Blocked by

- #18
