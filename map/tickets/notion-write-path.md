---
title: Notion write path for Job Orders
type: research
mode: AFK
status: closed
assignee: claude (session 2026-09-28)
blocked_by: []
---

## Question

How does a long-running Docker service create and read back rows in the Job Orders database?

Establish, from Notion's documentation:
- Integration model for a service (internal integration token and the access it needs) versus interactive MCP tools.
- How to create a page in the data source with select, status, date, text and relation properties; relations need page ids.
- How to query the People and Projects data sources for a cache, including pagination, and how to read Department select options from the schema.
- Whether the page body can hold the raw message text.
- Read-back after write, rate limits, and error/retry behavior worth designing for.
- How a new property (for example a Telegram message id) would be added and whether it can be written by the integration.

Resolution: a short facts list with doc links; no design decisions.

## Resolution

Resolved 2026-09-28 (research, primary Notion docs). Full findings with sources and confidence tags: [notion-write-path-findings.md](notion-write-path-findings.md).

- **Path**: a container uses the REST API with an internal integration token, pinned to a `Notion-Version`, writing to the Job Orders data source id. The integration needs Read and Insert content, and Job Orders, People and Projects must all be shared with it.
- **Row shape**: properties are keyed by exact schema name; select and status by option name; relations by page id (not name); Health (a formula) is never written. Page body can carry raw text through `children` or `markdown`.
- **Limits that bite**: 2000 characters per text item, so a long raw message must be split across items or sent as body blocks; about 3 requests per second; relation values in a page object stop at 25 references.
- **Cache**: People and Projects are read with paginated data source queries (100 per page); Department and Status options come from the data source schema.
- **Schema**: a new column (for example a Telegram message id) can be added with a data source PATCH, but never send a partial option list for Department or Status because omitted options are deleted.
- **No documented idempotency header** on page creation, so duplicate protection is ours to build.
- **Untested**: unknown select/status names, query freshness after a write, `Retry-After`, capability needed to add a column. These go on a build-time smoke test.

Named principles: **Schema is the contract** (validate every select, status and relation value against the live schema before writing, because names must match exactly) and **Own your dedupe key** (the destination won't dedupe for you, so the key and the ledger belong to the watchdog).
