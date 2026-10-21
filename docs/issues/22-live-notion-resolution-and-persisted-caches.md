<!-- Mirror of https://github.com/mclbautista/t6-jo-tg-ntn/issues/22. Keep identical: change the issue and this file together. -->

# 5. Live Notion resolution and persisted caches

## Parent

#28

## What to build

Resolve People, Projects, and Departments against live Notion data while preserving precision over recall. Successful six-hour snapshots must survive restarts and refresh failures; an empty cache must hold planning instead of silently converting every relation into a miss.

## Acceptance criteria

- [ ] People, Projects, and Department options load from the shared Notion data sources at startup and every six hours, with complete pagination.
- [ ] Every successful non-empty refresh is persisted as a versioned cache snapshot and loaded before the first live refresh after restart.
- [ ] A failed refresh continues using the latest non-empty snapshot and emits an ops finding only after it is more than 24 hours stale.
- [ ] If a required snapshot has never loaded, messages remain held at `extracted` without consuming retry attempts or producing false unresolved findings.
- [ ] People resolution order is TelegramUserID, TelegramHandle, Contacts `Aliases`, then guarded fuzzy matching against the Contacts page title.
- [ ] Aliases split on commas, trim and casefold consistently, and normalize `ate`, `kuya`, and `sir` on both sides.
- [ ] `@handle` mentions never use fuzzy matching; alias collisions remain unresolved.
- [ ] Fuzzy matching requires configurable score and runner-up-lead thresholds, initially 90 and 8, and every fuzzy-only match creates an untagged ops finding.
- [ ] Project Codes match as whole tokens, case-insensitively and longest-code-first, and ambiguous or absent matches remain blank.
- [ ] Department uses the configured small header-alias map and then exact validation against live Notion options; the live option set is not hard-coded.
- [ ] Assigned supports every resolved assignee, Projects receives only a resolved page ID, Leizette is always included in cc, and Coordinator remains blank.
- [ ] Unresolved or ambiguous raw mentions remain visible for reporting and are not backfilled automatically later.
- [ ] Recorded Notion contract fixtures cover pagination, current schema/property names, Contacts `Aliases` and `End of Shift`, persisted snapshots, stale-cache behavior, and empty-cache holds.
- [ ] Corpus tests cover every exact resolution step, alias collision, fuzzy pass/fail/lead cases, the `@handle` guard, Project Code collisions, and unknown Department behavior.
- [ ] Resolved assignees' `End of Shift` values feed the #21 deadline resolver, so shift deadlines use live Contacts data end to end.
- [ ] A Department token that fails live validation leaves Department blank, produces the `unknown_department` finding, and makes Name fall back to the posted first line.
- [ ] Migration 4 is applied as specified under Technical details and documented in `docs/ledger-schema.md`.

## Technical details

### Data sources

| Cache | Data source | Fields read |
|---|---|---|
| People | Contacts `77b2a701-71db-4e88-a694-37f07303e77e` | page id, `Name` (title), `TelegramUserID`, `TelegramHandle`, `Aliases`, `End of Shift` |
| Projects | Projects `cc1dc02a-8f32-4c97-81e4-dc1e08d7f134` | page id, `Name`, `Project Code` (select) |
| Departments | Job Orders `3d8fb4d5-9e59-8073-a0d0-000b77632947` schema | `Department` select options |

### Mention extraction

- `assignees_raw` and `cc_raw` split on commas, `/`, `&`, ` and `, and line breaks. Trailing courtesy words (`thanks`, `ty`, `po`) are dropped.
- Telegram `mention`/`text_mention` entities in the message supply user ids and handles for step 1 and 2.
- A mention containing a hedge (`Piolo but maybe Majo`) is treated as one ambiguous mention: unresolved, quoted as written.

### Fuzzy step

`rapidfuzz.fuzz.WRatio` on the casefolded, honorific-stripped mention against each Contacts title. A match needs score ≥ `fuzzy_min_score` (90) and a lead ≥ `fuzzy_min_lead` (8) over the runner-up. Never applied to a mention that starts with `@`.

### Migration 4

New table `cache_snapshot`: `id INTEGER PK`, `kind TEXT NOT NULL` (`people` | `projects` | `departments`), `format_version INTEGER NOT NULL`, `payload_json TEXT NOT NULL`, `item_count INTEGER NOT NULL`, `fetched_at TEXT NOT NULL`. Index on `(kind, fetched_at)`. The latest row per kind is the live snapshot. Keep the last 10 per kind; older rows are deleted after a successful insert.

A failed refresh writes nothing to `cache_snapshot`; its time and error are logged. The stale-cache ops message is defined in #23.

### Configuration

`contacts_data_source_id`, `projects_data_source_id`, `leizette_contact_page_id`, `cache_refresh_hours` (6), `cache_stale_alert_hours` (24), `fuzzy_min_score` (90), `fuzzy_min_lead` (8), `department_aliases` (map, e.g. `TO: TOC`), `honorifics` (`ate`, `kuya`, `sir`).

## Blocked by

- #21
