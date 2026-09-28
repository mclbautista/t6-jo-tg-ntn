# Notion write path for Job Orders: findings

Researched 2026-09-28 for the ticket "Notion write path for Job Orders". Facts only, no design decisions. Sources are Notion's own developer docs (developers.notion.com) unless marked. Confidence tags: **[documented]** stated by the docs; **[inferred]** my reading of the docs; **[unverified]** not established, test before relying on it.

## 1. Integration model

- **[documented]** A service creates pages through the REST API with an integration token. For internal integrations the create-page call must name a page or data source as parent. Source: https://developers.notion.com/reference/post-page.md
- **[documented]** Creating a page needs the Insert content capability on the target; without it the call returns 403. Source: same page.
- **[documented]** An integration only reaches pages and databases shared with it ("Connect to"). Querying a data source the integration can't see returns 404. Source: https://developers.notion.com/reference/query-a-data-source
- **[documented]** Relation values come back empty unless the related database is also shared with the integration. Updating a relation property's schema also needs the related database shared. Sources: https://developers.notion.com/page/frequently-asked-questions.md and https://developers.notion.com/reference/update-a-data-source
- **[documented]** Every request needs a `Notion-Version` header; the latest documented version is 2026-03-11. Databases contain data sources, and pages and queries use the data source id, not the database id. Sources: post-page (above) and https://developers.notion.com/guides/data-apis/working-with-databases
- **[documented]** The hosted Notion MCP applies the standard API limits per user, totalled across its tools. Source: https://developers.notion.com/docs/mcp-supported-tools
- **[inferred]** The MCP tools used interactively in a chat are not the natural path for an unattended container; the REST API with an internal integration is. The MCP docs I read only covered limits, so this is a judgment, not a stated rule.
- Token handling: tokens must stay server-side, out of browser bundles and public repos. Source: https://developers.notion.com/guides/data-apis/read-page-property-values

## 2. Creating a row in Job Orders

- **[documented]** `POST /v1/pages` with `parent: { data_source_id }`; property keys must match the data source schema by name or id. Job Orders data source id (from the database fetch in this session): `3d8fb4d5-9e59-8073-a0d0-000b77632947`. Source: post-page.
- **[documented]** Value shapes: title and rich_text are arrays of text objects; select and status can be set by `name` or `id`; date is `{ start, end, time_zone }`; relation is an array of `{ id }` page ids, at most 100 per request. Sources: post-page (OpenAPI section) and https://developers.notion.com/reference/page-property-values
- **[documented]** Formula, rollup, id, created and last-edited values are set by Notion and must be left out of writes. In Job Orders, Health is a formula, so it is never written. Source: post-page.
- **[documented]** Size limits: one text item holds at most 2000 characters; an array holds at most 100 items; a whole request holds at most 1000 blocks and 500 KB. Sources: post-page (OpenAPI) and https://developers.notion.com/reference/request-limits.md
- **[documented]** Commas are not valid in select values. Source: https://developers.notion.com/reference/property-value-object
- **[documented]** Page body content can be sent with `children` (up to 100 blocks) or with a `markdown` string (mutually exclusive). `allow_async` on the markdown form returns a 202 task. Templates can be applied instead, but not together with `children`, and they apply asynchronously. Source: post-page.
- **[documented]** The response is a 200 page object with `id`, `url` and `properties`. Relation values in a page object show only the first 25 references; read more via the property-item endpoint. Sources: post-page, property-value-object, https://developers.notion.com/reference/retrieve-a-page-property.md
- **[unverified]** What happens when a select or status write uses a name that isn't an existing option. The docs I read don't say. Test it; a silent new option would pollute the Department list.

## 3. Reading People and Projects for the cache; queries for dedupe

- **[documented]** `POST /v1/data_sources/{id}/query` with optional `filter`, `sorts`, `page_size` (max 100) and `start_cursor`; follow `next_cursor` while `has_more` is true; `filter_properties` trims the response; one query returns at most 10,000 results. Sources: query-a-data-source and https://developers.notion.com/guides/data-apis/query-large-data-sources
- Related data sources from the Job Orders schema: People `77b2a701-71db-4e88-a694-37f07303e77e` (Assigned, Coordinator, cc), Projects `cc1dc02a-8f32-4c97-81e4-dc1e08d7f134`, Deliverables `0a4ff084-3138-429c-8dbe-4d802aded74f`.
- **[documented]** Select and status options live in the data source schema, which the retrieve-a-data-source call returns. Source: https://developers.notion.com/reference/property-object
- **[unverified]** How quickly a newly created row shows up in a query. The docs I read don't say, so a query-before-create duplicate check may not see a row written a moment ago.

## 4. Schema changes

- **[documented]** `PATCH /v1/data_sources/{id}` with `properties` adds a column by name and type, for example a rich_text column for a Telegram message id. Sources: update-a-data-source and https://developers.notion.com/cli/guides/data-sources
- **[documented]** In a select or multi-select update, any existing option left out is removed. Status option groups can't be reconfigured through the API. Source: https://developers.notion.com/reference/update-data-source-properties.md
- **[inferred]** Adding a column probably needs the Update content capability; the docs I read don't name it. Adding the column by hand once in Notion avoids the question.

## 5. Limits, errors and retries

- **[documented]** The rate limit is an average of about 3 requests per second per connection, with some bursts allowed. Going over returns 429 `rate_limited`. Notion recommends queueing outgoing requests, retrying after a pause, and setting a retry limit that surfaces the final error. Source: https://developers.notion.com/reference/request-limits
- **[documented]** The FAQ says to slow down on HTTP 429 or 529. Source: frequently-asked-questions (above).
- **[documented]** Error codes that matter: 400 `validation_error`, 401 `unauthorized`, 403 `restricted_resource`, 404 `object_not_found`, 409 `conflict_error`, 429 `rate_limited`, 500 `internal_server_error`, 503 `service_unavailable`, 504 `gateway_timeout`, 529 `service_overload`. Source: post-page (OpenAPI).
- **[unverified]** A `Retry-After` header on 429. A secondary source describes it for a similar API; confirm against Notion's own docs before coding to it.
- At about 12 JOs a day, plus an occasional cache refresh, traffic is far below the limit.

## 6. Idempotency

- **[documented, by omission]** The create-page docs describe no idempotency header. The error list includes `idempotency_key_reused` (409), but nothing in the create-page text explains a header or its behavior, so it should be treated as undocumented. Source: post-page.
- Consequence: any duplicate protection has to be built on our side (a stored Telegram key plus a local ledger).

## Items to test at build time

1. Unknown select or status name: error, or silently created option?
2. Whether relation writes work when only Job Orders is shared, or People and Projects must be shared too (reads need it).
3. Query freshness after a write.
4. `Retry-After` on 429.
5. Which capabilities adding a column needs.
6. Whether a written `Name` title, `Date` and `Deadline` behave as expected with the Manila time zone.
