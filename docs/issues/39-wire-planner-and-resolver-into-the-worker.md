<!-- Mirror of https://github.com/mclbautista/t6-jo-tg-ntn/issues/39. Keep identical: change the issue and this file together. -->

# Wire planner and resolver into the worker

## Parent

#28

## What to build

Connect the #21 planner and the #22 resolver to the live pipeline. On `main`, `Worker.process` still plans with the walking-skeleton `build_plan(name=first_line, ...)`, so captured rows carry only Name, Telegram Key and Status. `plan_job_order` (`core/job_order.py`) and `CacheManager.resolve_people` / `resolve_project` / `resolve_department` (`app/caches.py`) exist and are tested, but nothing calls them, and `Worker._capture_facts` says planner and resolver findings join "once the pipeline plans with them". This ticket makes the pipeline plan with them, so every live capture gets the full property set, the resolved relations, the deadline, and the planner and resolver findings, and so #24 has the Project page id, Department and `jo_number_int` it needs for `jo_key`.

This ticket wires existing code. It does not change parsing, resolution or verification rules.

## Acceptance criteria

- [ ] `Worker.process` builds the plan with `plan_job_order` from the stored #20 extraction (the `extract` stage result), not with `build_plan`. `posted_at` is the intake posting time already used by `_posted_at`.
- [ ] People are resolved with `CacheManager.resolve_people(assignees_raw, cc_raw, entities)`, using the message's Telegram mention entities, and the resolved assignees' `End of Shift` values are passed to `plan_job_order` as `assignee_shifts`, so shift deadlines use live Contacts data end to end.
- [ ] The Project is resolved with `CacheManager.resolve_project(project_raw)`, and the Department with `CacheManager.resolve_department(...)`; the live Department option set is passed to `plan_job_order` as `valid_departments`.
- [ ] `relation_properties(people, project)` is merged into the plan's properties, so Assigned, cc (Leizette always included) and Projects are written; Coordinator stays blank.
- [ ] Resolver findings (unresolved, ambiguous, fuzzy-only, unknown Department) and planner findings (deadline, ASAP, and so on) are all in `plan.findings`, and reach `verify()` through `_capture_facts`. The "once the pipeline plans with them" note is removed.
- [ ] Planning still holds at `extracted` while `caches.ready` is false, without spending an attempt or producing an unresolved finding.
- [ ] The source block passed to `plan_job_order` is built from what `main` already has (posted by, Manila timestamp, intake link, and `forward_origin_lines`). #24 later swaps in `provenance.source_block`; keep this behind one small function so that swap is a one-line change.
- [ ] The create payload is built from the stored plan with `build_job_order_payload`, and read-back compares at least Name and Telegram Key as today.

### Persisted plan (resume)

- [ ] Everything resolution decided is stored in the plan at the `extracted` → `planned` transition, before any Notion call: the full `JobOrderPlan` (properties, body blocks, `jo_number_int`, `deadline_source`, findings) plus a `resolution` object holding the resolved Project page id (or null), the Department option (or null), Assigned and cc page ids, and the unresolved raw mentions.
- [ ] A row at `planned` or `writing` is replayed from the stored plan only: it never re-runs extraction or resolution, so a cache refresh between crash and resume cannot change what gets written.
- [ ] `jo_number_int`, the Project page id and the Department are readable from the stored plan by a ledger helper, so #24 can compute `jo_key` without parsing Notion properties.
- [ ] No ledger migration is needed (the plan lives in the existing `plan_json` column). If one turns out to be needed, it is numbered after the latest migration on `main` and documented here.

### Tests

- [ ] Worker tests with fake Notion and fake caches cover: a fully resolved JO (Project, Department, Assigned, cc, shift deadline) producing the exact expected payload; an unresolved Project and an unknown Department leaving those properties blank and producing their findings; a fuzzy-only person match producing its finding; and the cache-not-ready hold.
- [ ] A test stores a plan, simulates a crash after `planned`, swaps the cache contents, and shows the resumed write uses the stored plan unchanged.
- [ ] A test shows planner and resolver findings appear in the capture report rendered by #23.
- [ ] Existing worker, ledger and smoke tests still pass; `build_plan` stays only where the pre-v1 ledger backfill needs it.

## Out of scope

- Forward provenance, edits, reposts and `jo_key` itself (#24).
- Retry/backoff loops and heartbeat for stuck `planned` or `writing` rows (#25).
- Jev matching (#26).

## Blocked by

- #21
- #22
- #23

## Blocks

- #24
