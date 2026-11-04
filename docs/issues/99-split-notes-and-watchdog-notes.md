<!-- Mirror of https://github.com/mclbautista/t6-jo-tg-ntn/issues/99. Keep identical: change the issue and this file together. -->

# Split coordinator notes and watchdog notes into separate Notion columns

## Problem

Notes for a Job Order (JO) currently land in one place. Notes written by the coordinator and notes the watchdog adds get mixed together, so it is unclear who wrote what.

## Proposed change

Write the two kinds of note to two different Notion columns on the Job Orders database:

- **Coordinator notes** go to the Notion column `Notes`.
- **Watchdog notes** go to the Notion column `Watchdog Notes`.

Each note keeps its own column. Neither kind overwrites the other.

## Acceptance criteria

- A coordinator note on a JO is written to `Notes` and nowhere else.
- A note the watchdog adds is written to `Watchdog Notes` and nowhere else.
- Existing JO rows are not rewritten or cleared by this change.
- The Notion column names are read from config (or the database schema), not hard-coded in the write path, so a rename does not need a code change.
- Tests cover both routes: a coordinator note to `Notes`, and a watchdog note to `Watchdog Notes`.

## Open questions

- Does `Watchdog Notes` already exist in the Job Orders data source, or does it need to be created? Confirm before the first write.
- Should the watchdog ever write to `Notes` (for example on a failed capture), or only to `Watchdog Notes`?

## Process

Per the project workflow: this issue is the spec. Build tickets go in as sub-issues once the spec is agreed. Implementation and code review follow after that.
