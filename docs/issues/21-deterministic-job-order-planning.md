<!-- Mirror of https://github.com/mclbautista/t6-jo-tg-ntn/issues/21. Keep identical: change the issue and this file together. -->

# 4. Deterministic Job Order planning

## Parent

#28

## What to build

Turn a validated extraction into a deterministic, exact Notion plan for the Job Order fields that do not require live relation resolution. The slice owns canonical/fallback naming, JO Number handling, Department token normalization, Manila-time Date and Deadline rules, Notes assembly, and fail-closed behavior over the redacted corpus.

## Acceptance criteria

- [ ] Name is `[PROJECT] - [DEPT] - JO #[N]` only when Project Code, Department, and JO Number all parse; otherwise it is the posted first line.
- [ ] JO Number preserves its posted `#` form and leading zeros, while a separately parsed integer is available for later reuse detection. D2 clarification: reuse findings and reports display the stored JO Number as posted (`074` and `#074` show `#074`, `#74` stays `#74`); only the integer reuse key is normalized, and the leading-`#` normalization of the displayed value is unchanged.
- [ ] Title, Task, Purpose, Source / Source Drive, Platform, Link Settings, Watermark, Mode of Transfer, Blocker, Pending, and Notes follow the exact field-mapping rules and preserve source wording where required.
- [ ] Subtitle is inferred only as `Burned-in: <language>`, `None`, or blank; poster-provided placeholders pass through, while absent values remain blank.
- [ ] Date is derived from the intake posting/forwarding time and carries an explicit Asia/Manila offset; forwarded-original time is contextual only.
- [ ] A bare stated day resolves to 23:59, a time-only value uses the posting day, `tonight` resolves to 23:59, and a stated past deadline is retained and marked for reporting rather than rolled forward.
- [ ] End of Shift resolves to the next occurrence at or after posting time, using the latest result across assignees.
- [ ] Blank or `Flexible / Variable` shift data, or no resolved assignee, produces the documented 23:59 posting-day fallback and a finding that makes the default explicit.
- [ ] `ASAP` uses the same shift logic and records a high-priority finding; hedges and original deadline phrases remain in Notes.
- [ ] Phrases with no clock meaning fail closed to a blank Deadline and remain in Notes.
- [ ] Department header aliases are configuration, while live option validation remains deferred to the resolution slice.
- [ ] The exact planned properties and body blocks are stored before any write and can be replayed without recomputation.
- [ ] Golden tests over the 77-Job-Order corpus cover canonical and fallback Names, leading zeros, all deadline families, overnight shifts, multiple assignees, the 23:59 fallback, past dates, hedges, and unparseable values.
- [ ] The deadline resolver is a pure function that takes the resolved assignees' End of Shift values as an input argument; this ticket tests it with fixture shift values, and #22 wires in the live Contacts data.
- [ ] Every row of the deadline table under Technical details is a passing golden test.
- [ ] Every distinct header line in the corpus has a golden parse result (project token, department token, JO number, or "no parse").
- [ ] Notes follows the layout under Technical details exactly.

## Technical details

### Header parsing and Name

- The header is the first non-empty line (`header_raw`). It parses when it splits on ` - ` (spaces optional) into a project token, a department token and a number part.
- The number part parses when it matches `(JO|JOB ORDER)?\s*(No\.?|#)?\s*(\d+)`, case-insensitive. Examples: `JO #74`, `JO No. 15`, `#0155`, `JOB ORDER 27`.
- The project token is kept as posted (trimmed). It may contain spaces and brackets, e.g. `PP EP 2 (APC)`.
- The department token is looked up in the config alias map (`department_aliases`, e.g. `TO: TOC`), then uppercased. Live-option validation happens in #22; a token that later fails validation leaves Department blank and makes Name fall back.
- Canonical Name: `<project token> - <DEPT> - JO #<digits as posted>`, e.g. `SOMSOM - TOC - #0155` becomes `SOMSOM - TOC - JO #0155`. Otherwise Name is the posted first line, masked.
- JO Number property: `#<digits as posted>` (leading zeros kept). `jo_number_int` is the integer value, stored in the plan for #24.

### Dates and years

- Deadline and Date go to Notion as the same instant at UTC offset zero with `time_zone: "Asia/Manila"` (Notion rejects `time_zone` beside a non-zero offset, #83); local-time reasoning stays in Asia/Manila.
- Date = intake posting (or forwarding) time.
- A month-and-day without a year takes the year that puts it closest to the posting date (so a late-December JO posted on 2 January resolves to the previous year).
- Weekday names next to a date (`/ Monday`, `Tues`) are ignored for computation.
- Times accept `5AM`, `5:00 AM`, `7pm`, `530PM` (= 17:30), `12:00 AM` (= 00:00 of that date).

### Deadline rules (in order)

1. Strip hedges before parsing and keep the whole original in Notes. A hedge is a parenthetical, or text starting at `if`, `kung`, or ` or ` (e.g. `if keri`, `if kaya`, `if not tomorrow na po`, `or tom`), plus the filler words `rin`, `po`, `na`.
2. Empty, or a poster placeholder (`-`, `n/a`, `none`): blank, no finding.
3. Date + time: that datetime, even if before posting (past-deadline finding in #23).
4. Date only: 23:59 on that date. A date followed by a no-clock phrase (`before photoshoot`) is still 23:59 on that date.
5. Time only: posting day at that time, never rolled forward.
6. `tonight`, `today`, `within the day`: 23:59 on the stated date, or the posting day if no date. `pm` with no hour (`September 11, pm`) is 23:59 on that date.
7. `end of shift`, `within shift`, `asap`, with no date: for each resolved assignee, the next occurrence of their End of Shift hour at or after the posting time; take the latest. With a date (`Aug 26, end of shift`): the End of Shift hour on that date, or on the next day if the hour is before 12:00 (a shift ending in the morning). Assignees with blank or `Flexible / Variable` shifts are skipped; if none remain, use 23:59 on the posting day (or stated date) and record a `shift_fallback` finding naming who was defaulted, or "no assignee resolved". `asap` also records an `asap` finding.
8. Anything else (`kung kelan matapos yung render`): blank, and a `deadline_unparsed` finding.

### Corpus deadline table

P = posting datetime; S = latest resolved End of Shift occurrence by rule 7; F = 23:59 fallback.

| Deadline as posted | Deadline | Findings |
|---|---|---|
| `September 29 / Tues 5AM` | Sep 29 05:00 | |
| `September 9, 8PM (if keri)` | Sep 9 20:00 | |
| `September 15, 530PM` | Sep 15 17:30 | |
| `September 23 / Wednesday 12:00 AM` | Sep 23 00:00 | |
| `September 27 / Sunday 5AM (pickup D16 shoot)` | Sep 27 05:00 | |
| `August 17, 11AM if keri` | Aug 17 11:00 | |
| `Sep 26`, `Sep 8 / Tue`, `Wednesday September 23` | that date 23:59 | |
| `August 20, Thurs (papadala ko tonight ke Che yung HD nya)` | Aug 20 23:59 | |
| `August 27 / Thurs / before photoshoot` | Aug 27 23:59 | |
| `September 11, pm` | Sep 11 23:59 | |
| `Sept 25 / within the day`, `Sept 28 / Mon / within the day` | that date 23:59 | |
| `1pm` | P's date 13:00 | `deadline_past` if before P |
| `Tonight`, `Tonight if keri or tom`, `If keri rin po today` | P's date 23:59 | |
| `end of shift`, `within shift`, `within shift if kaya`, `end of shift if kaya, if not tomorrow na po` | S, else F | `shift_fallback` when F |
| `Aug 26, end of shift`, `Sept 23 / end of shift`, `September 2, within shift` | shift hour on that date (next day if before 12:00), else that date 23:59 | `shift_fallback` when defaulted |
| `September 10, asap` | shift hour on Sep 10, else Sep 10 23:59 | `asap`, plus `shift_fallback` when defaulted |
| `kung kelan matapos yung render` | blank | `deadline_unparsed` |
| empty / missing | blank | |

### Notes layout

1. The poster's own Notes text, verbatim (masked).
2. If the watchdog adds anything, a blank line, then the line `— added by the watchdog —`, then one line per item in this order:
   - `Deadline as posted: <deadline_raw>` whenever `deadline_raw` is non-empty.
   - `ASAP — deprioritize other JOs` when the `asap` finding applies.
   - `Earlier row: <Notion URL> (exact re-post)` or `(possible number reuse)` (added by #24).
3. The field is split into 2000-character rich-text items.

These lines are a deliberate exception to "no commentary in properties": they come from the deadline and duplicate decisions in `map/tickets/deadline-phrases.md` and `map/tickets/reposts-duplicates-idempotency.md`.

### Plan shape

The stored plan grows to hold every Notion property value, `jo_number_int`, `deadline_source` (`stated` | `shift` | `fallback` | `blank`), the list of findings (code plus parameters, rendered later by #23), and the body blocks: a source block (posted by, originally from, origin type, Manila timestamp, intake link) followed by the masked raw text split into blocks of 2000 characters or fewer.

## Blocked by

- #18
