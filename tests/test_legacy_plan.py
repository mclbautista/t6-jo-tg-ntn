"""Plans stored before #134 held Notion create-page properties; they still load as fields."""

import unittest

from jo_watchdog.core.job_order import job_order_fields, load_plan
from jo_watchdog.core.legacy_plan import LEGACY_WATCHDOG_MARKER


def text(value: str) -> dict:
    return {"rich_text": [{"type": "text", "text": {"content": value}}]}


def stored(**properties) -> dict:
    return {"telegram_key": "-1001:42", "name": "n", "properties": properties}


class UpgradeTests(unittest.TestCase):
    def test_every_stored_property_becomes_its_field(self):
        plan = load_plan(
            stored(
                Name={"title": [{"text": {"content": "MFG - TOC - JO #74"}}]},
                **{
                    "Telegram Key": text("-1001:42"),
                    "Status": {"status": {"name": "Needs Intake Review"}},
                    "Department": {"select": {"name": "TOC"}},
                    "JO Number": text("#74"),
                    "Blocker ": text("waiting"),
                    "Source / Source Drive": text("Drive A"),
                    "JO Notes": text("poster note"),
                    "Watchdog Notes": text("Deadline as posted: 1pm"),
                    "Assigned": {"relation": [{"id": "a1"}]},
                    "cc": {"relation": [{"id": "c1"}, {"id": "c2"}]},
                    "Projects": {"relation": [{"id": "p1"}]},
                    "Deliverable": {"relation": [{"id": "d1"}]},
                    "Date": {"date": {"start": "2026-10-27T17:20:02+08:00"}},
                },
            )
        )
        self.assertEqual(
            plan.fields,
            {
                "name": "MFG - TOC - JO #74",
                "telegram_key": "-1001:42",
                "status": "Needs Intake Review",
                "department": "TOC",
                "number": "#74",
                "blocker": "waiting",
                "source": "Drive A",
                "notes": "poster note",
                "watchdog_notes": "Deadline as posted: 1pm",
                "assigned_ids": ["a1"],
                "cc_ids": ["c1", "c2"],
                "project_id": "p1",
                "deliverable_ids": ["d1"],
                "date": "2026-10-27T17:20:02+08:00",
            },
        )

    def test_a_rich_text_in_several_parts_is_joined(self):
        long = {"rich_text": [{"text": {"content": "a" * 2000}}, {"text": {"content": "b"}}]}
        self.assertEqual(load_plan(stored(Task=long)).fields["task"], "a" * 2000 + "b")

    def test_an_empty_relation_is_kept_empty_and_a_blank_project_is_left_out(self):
        fields = load_plan(
            stored(Assigned={"relation": []}, Projects={"relation": []})
        ).fields
        self.assertEqual(fields, {"assigned_ids": []})

    def test_a_date_with_a_time_zone_and_no_offset_becomes_the_instant_at_the_manila_offset(self):
        for start, zone, expected in [
            ("2026-10-27T09:20:02+00:00", "Asia/Manila", "2026-10-27T17:20:02+08:00"),
            ("2026-10-27T09:20:02", "UTC", "2026-10-27T17:20:02+08:00"),
            ("2026-10-27T09:20:02", None, "2026-10-27T09:20:02+08:00"),
            ("2026-10-30", None, "2026-10-30"),
        ]:
            plan = load_plan(stored(Deadline={"date": {"start": start, "time_zone": zone}}))
            self.assertEqual(plan.fields["deadline"], expected, start)

    def test_a_replayed_legacy_plan_sends_instants_and_days(self):
        fields = job_order_fields(
            load_plan(stored(Date={"date": {"start": "2026-10-27T17:20:02+08:00"}}))
        )
        self.assertEqual(fields["date"].isoformat(), "2026-10-27T17:20:02+08:00")


class LegacyNotesTests(unittest.TestCase):
    def test_a_marker_block_in_notes_is_split_into_watchdog_notes(self):
        plan = load_plan(
            stored(Notes=text(f"poster note\n\n{LEGACY_WATCHDOG_MARKER}\nDeadline as posted: 1pm"))
        )
        self.assertEqual(plan.fields["notes"], "poster note")
        self.assertEqual(plan.fields["watchdog_notes"], "Deadline as posted: 1pm")

    def test_a_plan_with_only_the_marker_block_has_no_notes(self):
        plan = load_plan(stored(Notes=text(f"{LEGACY_WATCHDOG_MARKER}\nEarlier row: u")))
        self.assertNotIn("notes", plan.fields)
        self.assertEqual(plan.fields["watchdog_notes"], "Earlier row: u")

    def test_a_marker_quoted_in_post_99_poster_notes_is_left_alone(self):
        quoted = f"see earlier JO\n\n{LEGACY_WATCHDOG_MARKER}\nsomething posted by hand"
        plan = load_plan(
            stored(**{"JO Notes": text(quoted), "Watchdog Notes": text("Deadline as posted: 1pm")})
        )
        self.assertEqual(plan.fields["notes"], quoted)
        self.assertEqual(plan.fields["watchdog_notes"], "Deadline as posted: 1pm")

    def test_a_skeleton_plan_with_a_marker_block_drops_the_marker_line(self):
        plan = load_plan(
            {
                "telegram_key": "-1001:1",
                "name": "n",
                "notes": f"{LEGACY_WATCHDOG_MARKER}\nEarlier row: u (exact re-post)",
            }
        )
        self.assertEqual(plan.fields["watchdog_notes"], "Earlier row: u (exact re-post)")

    def test_a_skeleton_plan_is_upgraded_to_a_replayable_one(self):
        plan = load_plan(
            {
                "name": "First line",
                "telegram_key": "-100:1",
                "body_blocks": ["body"],
                "notes": "Earlier row: x",
                "duplicate": {"code": "exact_repost"},
            }
        )
        fields = job_order_fields(plan)
        self.assertEqual(fields["status"], "Needs Intake Review")
        self.assertEqual(fields["name"], "First line")
        self.assertNotIn("notes", fields)
        self.assertEqual(fields["watchdog_notes"], "Earlier row: x")
        self.assertEqual(fields["source_text"], "body")
        self.assertEqual(plan.duplicate, {"code": "exact_repost"})


if __name__ == "__main__":
    unittest.main()
