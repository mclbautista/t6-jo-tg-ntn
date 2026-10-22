# Notion contract fixtures (#22)

Response bodies in the shape the pinned Notion API (`2026-03-11`) returns for:

- `contacts_query_page1.json` / `contacts_query_page2.json`: `POST /v1/data_sources/{Contacts}/query`, split across two pages (`has_more` + `next_cursor`) to exercise pagination.
- `projects_query.json`: `POST /v1/data_sources/{Projects}/query`.
- `job_orders_data_source.json`: `GET /v1/data_sources/{Job Orders}`, for the live `Department` options.

Property names, types and the `Department` / `End of Shift` option sets were copied from the live schemas on 2026-10-22 (`Name`, `TelegramUserID`, `TelegramHandle`, `Aliases`, `End of Shift` on Contacts; `Name`, `Project Code` on Projects; `Department` on Job Orders). Rows are synthetic: first names match the redacted corpus, everything else is made up. Re-record when a property is renamed.
