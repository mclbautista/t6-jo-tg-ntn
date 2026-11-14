"""Deterministic verification: each check fires on exactly its trigger (#23)."""

import unittest
from datetime import datetime

from jo_watchdog.core.verify import (
    FINDING_ORDER,
    MANILA,
    CaptureFacts,
    DeliverableHint,
    EarlierRow,
    Finding,
    ForwardOrigin,
    FuzzyMatch,
    PersonMention,
    parse_header_date,
    tags_standing_cc,
    verify,
)

POSTED = datetime(2026, 9, 22, 10, 0, tzinfo=MANILA)


def extracted(**overrides) -> CaptureFacts:
    """A clean, fully extracted `jo` capture that triggers nothing."""
    base = dict(
        posted_at=POSTED,
        verdict="jo",
        reasons="Has a JO header and fields.",
        title="Teaser",
        task="Edit the teaser",
        deadline_raw="September 29 / Tues 5AM",
        deadline=datetime(2026, 9, 29, 5, 0, tzinfo=MANILA),
        header_date_raw="Sept 22 / MONDAY",
    )
    base.update(overrides)
    return CaptureFacts(**base)


def codes(facts: CaptureFacts, **kw) -> list[str]:
    return [f.code for f in verify(facts, **kw)]


class CleanCaptureTests(unittest.TestCase):
    def test_clean_extracted_capture_has_no_findings(self):
        self.assertEqual(verify(extracted()), [])

    def test_no_extraction_means_extraction_checks_stay_silent(self):
        # The walking pipeline has no extraction yet: no Title/Task/deadline noise.
        self.assertEqual(verify(CaptureFacts(posted_at=POSTED)), [])


class NineChecksTests(unittest.TestCase):
    def test_unresolved_and_ambiguous_people_and_projects(self):
        facts = extracted(
            unresolved_people=[PersonMention(raw="Jhun"), PersonMention(role="cc", raw="Bea")],
            ambiguous_people=[PersonMention(raw="Ramon but maybe Lena")],
            unresolved_projects=["PP2 APC"],
        )
        found = verify(facts)
        self.assertEqual(
            [(f.code, f.params) for f in found],
            [
                ("unresolved_person", {"role": "assignee", "raw": "Jhun"}),
                ("unresolved_person", {"role": "cc", "raw": "Bea"}),
                ("ambiguous_person", {"role": "assignee", "raw": "Ramon but maybe Lena"}),
                ("unresolved_project", {"raw": "PP2 APC"}),
            ],
        )

    def test_unknown_department(self):
        self.assertEqual(codes(extracted(unknown_department="XYZ")), ["unknown_department"])

    def test_exact_repost_wins_over_number_reuse(self):
        row = EarlierRow(url="https://notion.so/a", days=3, jo_number="#0156")
        self.assertEqual(
            codes(extracted(exact_repost=row, number_reuse=row)), ["exact_repost"]
        )
        self.assertEqual(codes(extracted(number_reuse=row)), ["number_reuse"])

    def test_forward_origin_only_for_non_user(self):
        self.assertEqual(codes(extracted(forward_origin=ForwardOrigin(type="user"))), [])
        for kind in ("hidden_user", "chat", "channel"):
            with self.subTest(kind=kind):
                facts = extracted(forward_origin=ForwardOrigin(type=kind, name="X"))
                self.assertEqual(codes(facts), ["forward_origin"])

    def test_unsure_carries_reasons(self):
        found = verify(extracted(verdict="unsure", reasons="No header."))
        self.assertEqual(found, [Finding(code="unsure", params={"reasons": "No header."})])

    def test_deadline_before_posting(self):
        facts = extracted(deadline=datetime(2026, 9, 22, 9, 59, tzinfo=MANILA))
        self.assertEqual(codes(facts), ["deadline_past"])
        facts = extracted(deadline=POSTED)
        self.assertEqual(codes(facts), [])

    def test_deadline_unparsed_for_any_non_placeholder_text(self):
        facts = extracted(deadline_raw="kung kelan matapos yung render", deadline=None)
        self.assertEqual(
            verify(facts),
            [Finding(code="deadline_unparsed", params={"raw": "kung kelan matapos yung render"})],
        )

    def test_deadline_unparsed_silent_for_empty_and_placeholders(self):
        for raw in (None, "", "  ", "-", "N/A", "none", "None"):
            with self.subTest(raw=raw):
                self.assertEqual(codes(extracted(deadline_raw=raw, deadline=None)), [])

    def test_blank_title_or_task(self):
        self.assertEqual(codes(extracted(title=" ")), ["title_blank"])
        self.assertEqual(codes(extracted(task=None)), ["task_blank"])

    def test_header_date_outside_tolerance(self):
        self.assertEqual(codes(extracted(header_date_raw="Sept 23 / TUESDAY")), [])
        found = verify(extracted(header_date_raw="Sept 20 / SATURDAY"))
        self.assertEqual(
            found,
            [
                Finding(
                    code="header_date_mismatch",
                    params={"raw": "Sept 20 / SATURDAY", "posted": "2026-09-22"},
                )
            ],
        )

    def test_header_date_tolerance_is_configurable(self):
        facts = extracted(header_date_raw="Sept 20")
        self.assertEqual(codes(facts, header_date_tolerance_days=2), [])
        self.assertEqual(codes(facts, header_date_tolerance_days=0), ["header_date_mismatch"])

    def test_header_date_compares_manila_dates(self):
        # 23:30 UTC on Sep 21 is already Sep 22 in Manila.
        posted = datetime.fromisoformat("2026-09-21T23:30:00+00:00")
        facts = extracted(posted_at=posted, header_date_raw="Sep 23", deadline=None,
                          deadline_raw=None)
        self.assertEqual(codes(facts, header_date_tolerance_days=0), ["header_date_mismatch"])
        self.assertEqual(codes(facts, header_date_tolerance_days=1), [])

    def test_unparseable_header_date_is_silent(self):
        self.assertEqual(codes(extracted(header_date_raw="FRIDAY")), [])


class AdditionalFindingTests(unittest.TestCase):
    def test_multiple_jo_fuzzy_shift_asap_hints_skips(self):
        facts = extracted(
            multiple_jo_suspected=True,
            fuzzy_people=[FuzzyMatch(raw="Jhunjhun", contact="Jhun Reyes")],
            asap=True,
            shift_fallback=["Bea"],
            deliverable_hints=[DeliverableHint(name="Teaser v2", probability=0.72)],
            matcher_skipped=True,
            readback_mismatches=["name"],
            duplicate_key_urls=["https://notion.so/1", "https://notion.so/2"],
        )
        found = verify(facts)
        self.assertEqual(
            [f.code for f in found],
            [
                "multiple_jo",
                "fuzzy_person",
                "asap",
                "shift_fallback",
                "deliverable_hint",
                "matcher_skipped",
                "readback_mismatch",
                "duplicate_key",
            ],
        )
        self.assertEqual(found[-2].params, {"properties": ["Name"]})

    def test_shift_fallback_with_no_assignee(self):
        found = verify(extracted(shift_fallback=[]))
        self.assertEqual(found, [Finding(code="shift_fallback", params={"names": []})])

    def test_plan_findings_merge_without_duplicates_in_table_order(self):
        facts = extracted(
            deadline_raw="kung kelan", deadline=None,
            unknown_department="XYZ",
            plan_findings=[
                Finding(code="asap"),
                Finding(code="deadline_unparsed", params={"raw": "kung kelan"}),
            ],
        )
        self.assertEqual(codes(facts), ["unknown_department", "deadline_unparsed", "asap"])

    def test_planner_copy_of_a_single_code_wins(self):
        # #21 records deadline_past with the deadline as a parameter.
        planned = Finding(code="deadline_past", params={"deadline": "2026-09-22T09:00:00+08:00"})
        facts = extracted(
            deadline=datetime(2026, 9, 22, 9, 0, tzinfo=MANILA), plan_findings=[planned]
        )
        self.assertEqual(verify(facts), [planned])

    def test_verification_is_deterministic(self):
        facts = extracted(verdict="unsure", unknown_department="X", title="")
        self.assertEqual(verify(facts), verify(facts))


class TaggingTests(unittest.TestCase):
    def test_only_her_findings_tag_standing_cc(self):
        tagged = {
            "multiple_jo", "unresolved_person", "ambiguous_person",
            "unresolved_project", "unknown_department",
        }
        for code in FINDING_ORDER:
            with self.subTest(code=code):
                self.assertEqual(tags_standing_cc([Finding(code=code)]), code in tagged)


class HeaderDateParseTests(unittest.TestCase):
    def test_corpus_shapes(self):
        posted = POSTED.date()
        cases = {
            "Sept 21 / MONDAY": "2026-09-21",
            "Sep 23 / WEDNESDAY": "2026-09-23",
            "September 21, 2025": "2025-09-21",
            "9/21": "2026-09-21",
            "Dec 30": "2025-12-30" if posted.month < 7 else "2026-12-30",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(parse_header_date(raw, posted).isoformat(), expected)

    def test_closest_year_across_new_year(self):
        jan2 = datetime(2027, 1, 2, tzinfo=MANILA).date()
        self.assertEqual(parse_header_date("Dec 30", jan2).isoformat(), "2026-12-30")

    def test_invalid_returns_none(self):
        self.assertIsNone(parse_header_date("Feb 31", POSTED.date()))
        self.assertIsNone(parse_header_date("", POSTED.date()))


if __name__ == "__main__":
    unittest.main()
