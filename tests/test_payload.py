"""Tests for the pure Notion payload builder and read-back comparison."""

import unittest

from jo_watchdog.core.job_order import JobOrderPlan
from jo_watchdog.core.payload import (
    build_notion_payload,
    extract_readback_fields,
    verify_readback,
)
from jo_watchdog.core.plan import Plan
from jo_watchdog.core.verify import readback_properties


class BuildPayloadTests(unittest.TestCase):
    def test_payload_has_parent_properties_and_children(self):
        plan = Plan(name="First line", telegram_key="-100:1", body_blocks=["hello"])
        payload = build_notion_payload(plan, data_source_id="ds-1", status="Needs Intake Review")

        self.assertEqual(payload["parent"], {"data_source_id": "ds-1"})
        self.assertEqual(
            payload["properties"]["Name"],
            {"title": [{"text": {"content": "First line"}}]},
        )
        self.assertEqual(
            payload["properties"]["Telegram Key"],
            {"rich_text": [{"text": {"content": "-100:1"}}]},
        )
        self.assertEqual(
            payload["properties"]["Status"], {"status": {"name": "Needs Intake Review"}}
        )

    def test_children_are_masked_paragraph_blocks(self):
        plan = Plan(name="n", telegram_key="k", body_blocks=["a", "b"])
        payload = build_notion_payload(plan, data_source_id="ds", status="s")
        children = payload["children"]
        self.assertEqual(len(children), 2)
        for block in children:
            self.assertEqual(block["object"], "block")
            self.assertEqual(block["type"], "paragraph")
        self.assertEqual(children[0]["paragraph"]["rich_text"][0]["text"]["content"], "a")
        self.assertEqual(children[1]["paragraph"]["rich_text"][0]["text"]["content"], "b")

    def test_empty_body_has_no_children(self):
        plan = Plan(name="n", telegram_key="k", body_blocks=[])
        payload = build_notion_payload(plan, data_source_id="ds", status="s")
        self.assertEqual(payload["children"], [])


class VerifyReadbackTests(unittest.TestCase):
    def test_match_returns_no_mismatches(self):
        plan = Plan(name="N", telegram_key="k", body_blocks=["b"])
        self.assertEqual(verify_readback(plan, retrieved_name="N", retrieved_telegram_key="k"), [])

    def test_name_mismatch_is_reported(self):
        plan = Plan(name="N", telegram_key="k", body_blocks=["b"])
        mismatches = verify_readback(plan, retrieved_name="WRONG", retrieved_telegram_key="k")
        self.assertIn("Name", " ".join(mismatches))

    def test_telegram_key_mismatch_is_reported(self):
        plan = Plan(name="N", telegram_key="k", body_blocks=["b"])
        mismatches = verify_readback(plan, retrieved_name="N", retrieved_telegram_key="other")
        self.assertIn("Telegram Key", " ".join(mismatches))


class DateReadbackTests(unittest.TestCase):
    PLAN = JobOrderPlan(
        telegram_key="k",
        name="N",
        properties={
            "Date": {"date": {"start": "2026-10-27T17:20:02+08:00"}},
            "Deadline": {"date": {"start": "2026-10-30T05:00:00+08:00"}},
        },
    )

    def check(self, dates):
        return verify_readback(
            self.PLAN, retrieved_name="N", retrieved_telegram_key="k", retrieved_dates=dates
        )

    def test_live_shape_with_dropped_seconds_and_null_zone_matches(self):
        self.assertEqual(
            self.check(
                {
                    "Date": {"start": "2026-10-27T17:20:00.000+08:00", "time_zone": None},
                    "Deadline": {"start": "2026-10-30T05:00:00.000+08:00", "time_zone": None},
                }
            ),
            [],
        )

    def test_same_instant_at_another_offset_matches(self):
        dates = {
            "Date": {"start": "2026-10-27T09:20:00+00:00"},
            "Deadline": {"start": "2026-10-29T21:00:00Z"},
        }
        self.assertEqual(self.check(dates), [])

    def test_the_issue_85_eight_hour_shift_is_reported_for_both_properties(self):
        mismatches = self.check(
            {
                "Date": {"start": "2026-10-27T09:20:00.000+08:00", "time_zone": None},
                "Deadline": {"start": "2026-10-29T21:00:00.000+08:00", "time_zone": None},
            }
        )
        self.assertEqual(readback_properties(mismatches), ["Date", "Deadline"])

    def test_missing_date_is_reported(self):
        mismatches = self.check({"Date": None, "Deadline": {"start": "2026-10-30T05:00:00+08:00"}})
        self.assertEqual(readback_properties(mismatches), ["Date"])

    def test_naive_start_is_read_in_its_time_zone(self):
        dates = {
            "Date": {"start": "2026-10-27T17:20:00", "time_zone": "Asia/Manila"},
            "Deadline": {"start": "2026-10-30T05:00:00", "time_zone": "Asia/Manila"},
        }
        self.assertEqual(self.check(dates), [])

    def test_extract_reads_both_dates_from_a_live_page(self):
        page = {"properties": {"Date": {"date": {"start": "x"}}, "Deadline": {"date": None}}}
        self.assertEqual(
            extract_readback_fields(page)["dates"], {"Date": {"start": "x"}, "Deadline": None}
        )


if __name__ == "__main__":
    unittest.main()
