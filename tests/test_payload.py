"""Job Order fields for t6-core, page links and read-back finding labels."""

import unittest
from datetime import UTC, datetime

from jo_watchdog.core.deadline import MANILA
from jo_watchdog.core.job_order import JobOrderPlan, job_order_fields
from jo_watchdog.core.payload import page_url_for
from jo_watchdog.core.verify import readback_properties

WITHHELD = "[password withheld – see Telegram message]"


class JobOrderFieldsTests(unittest.TestCase):
    def plan(self, **kwargs):
        return JobOrderPlan(telegram_key="-100:1", name="N", **kwargs)

    def test_fields_are_the_stored_fields_with_the_body_joined_as_source_text(self):
        fields = job_order_fields(
            self.plan(fields={"name": "N", "status": "Needs Intake Review"}, body_blocks=["a", "b"])
        )
        self.assertEqual(
            fields, {"name": "N", "status": "Needs Intake Review", "source_text": "a\nb"}
        )

    def test_no_body_writes_no_source_text(self):
        self.assertNotIn("source_text", job_order_fields(self.plan(fields={"name": "N"})))

    def test_stored_dates_become_instants_and_days(self):
        fields = job_order_fields(
            self.plan(
                fields={"date": "2026-10-27T17:20:02+08:00", "deadline": "2026-10-30"},
            )
        )
        self.assertEqual(fields["date"], datetime(2026, 10, 27, 17, 20, 2, tzinfo=MANILA))
        self.assertEqual(fields["date"].astimezone(UTC).hour, 9)
        self.assertEqual(fields["deadline"].isoformat(), "2026-10-30")

    def test_the_body_is_masked_at_the_exit_boundary(self):
        fields = job_order_fields(self.plan(fields={}, body_blocks=["password: hunter2"]))
        self.assertNotIn("hunter2", fields["source_text"])


class LabelTests(unittest.TestCase):
    def test_a_mismatched_field_is_shown_by_its_label(self):
        self.assertEqual(
            readback_properties(["name", "telegram_key", "deadline"]),
            ["Name", "Telegram Key", "Deadline"],
        )

    def test_page_url(self):
        self.assertEqual(page_url_for("ab-cd-12"), "https://www.notion.so/abcd12")


if __name__ == "__main__":
    unittest.main()
