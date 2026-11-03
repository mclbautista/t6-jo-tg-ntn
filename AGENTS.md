## Agent skills

### Issue tracker

Issues and specs live in GitHub Issues for `mclbautista/t6-jo-tg-ntn`; use `gh`. See `docs/agents/issue-tracker.md`.

### Domain docs

This is a single-context layout. Read root `CONTEXT.md` and relevant `docs/adr/` before domain work; their absence is not an error. See `docs/agents/domain.md`.

## Workflow

Work moves through five separate stages. Finish and record one before starting the next:

1. **Grill**: stress-test the plan or decision until the open questions are settled.
2. **Spec**: write the spec as one GitHub issue in this repository.
3. **Tickets**: break the spec into sub-issues of that spec issue, each shippable on its own.
4. **Implement**: build one ticket at a time on a branch that starts with the issue number.
5. **Code review**: review the change against its ticket and the spec before merging.

Track all of this in GitHub Issues and sub-issues. Where possible, start Claude thread names and branch names with the issue number.
