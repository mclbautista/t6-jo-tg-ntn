"""The deterministic Job Order plan: Name, JO Number, fields, Notes, and replay."""

import json
import unittest
from datetime import datetime
from types import SimpleNamespace

from jo_watchdog.core.deadline import MANILA
from jo_watchdog.core.header import canonical_name, parse_header
from jo_watchdog.core.job_order import (
    ASAP_NOTE,
    WATCHDOG_MARKER,
    JobOrderPlan,
    build_job_order_payload,
    build_notes,
    plan_job_order,
)
from jo_watchdog.core.resolve import resolve_department

ALIASES = {"TO": "TOC"}
POSTED = datetime(2026, 9, 15, 10, 0, tzinfo=MANILA)
WITHHELD = "[password withheld – see Telegram message]"

FIELDS = [
    "header_raw", "jo_number_raw", "project_raw", "department_raw", "title", "task", "purpose",
    "source", "platform", "link_settings", "watermark", "mode_of_transfer", "blocker", "pending",
    "notes", "deadline_raw", "subtitle",
]  # fmt: skip


def extraction(**values):
    return SimpleNamespace(**{name: values.get(name) for name in FIELDS})


def plan(ext, raw_text="SOMSOM - TOC - #0155\nbody", **kwargs):
    kwargs.setdefault("department_aliases", ALIASES)
    return plan_job_order(
        ext,
        telegram_key="-1001:42",
        raw_text=raw_text,
        posted_at=POSTED,
        source_block="Posted by: Leizette (@leizette)",
        **kwargs,
    )


def text_of(prop):
    return "".join(item["text"]["content"] for item in prop["rich_text"])


class NameTests(unittest.TestCase):
    def test_canonical_name_keeps_leading_zeros(self):
        p = plan(extraction(header_raw="SOMSOM - TOC - #0155"))
        self.assertEqual(p.name, "SOMSOM - TOC - JO #0155")
        self.assertEqual(text_of(p.properties["JO Number"]), "#0155")
        self.assertEqual(p.jo_number_int, 155)
        self.assertEqual(p.properties["Department"], {"select": {"name": "TOC"}})

    def test_department_alias_is_applied(self):
        p = plan(extraction(header_raw="ALTA-TO-JO04"), raw_text="ALTA-TO-JO04")
        self.assertEqual(p.name, "ALTA - TOC - JO #04")

    def test_aliases_come_from_configuration(self):
        p = plan(
            extraction(header_raw="ALTA-TO-JO04"),
            raw_text="ALTA-TO-JO04",
            department_aliases={"TO": "OE"},
        )
        self.assertEqual(p.name, "ALTA - OE - JO #04")

    def test_unparsed_header_falls_back_to_first_line(self):
        p = plan(
            extraction(header_raw="TOC - Job Order", department_raw="TOC", jo_number_raw="#12"),
            raw_text="TOC - Job Order\nbody",
        )
        self.assertEqual(p.name, "TOC - Job Order")
        self.assertEqual(text_of(p.properties["JO Number"]), "#12")
        self.assertEqual(p.jo_number_int, 12)
        self.assertEqual(p.properties["Department"], {"select": {"name": "TOC"}})

    def test_header_defaults_to_first_line_of_text(self):
        p = plan(extraction(), raw_text="\n\nMFG - TOC - JO #74\nbody")
        self.assertEqual(p.name, "MFG - TOC - JO #74")

    def test_invalid_department_blanks_department_and_falls_back(self):
        p = plan(
            extraction(header_raw="MFG - XYZ - JO #74"),
            raw_text="MFG - XYZ - JO #74",
            department_resolution=resolve_department("XYZ", ["TOC", "AE"], ALIASES),
        )
        self.assertEqual(p.name, "MFG - XYZ - JO #74")
        self.assertNotIn("Department", p.properties)
        self.assertEqual(text_of(p.properties["JO Number"]), "#74")

    def test_validated_department_keeps_canonical_name(self):
        p = plan(
            extraction(header_raw="MFG - toc - JO #74"),
            raw_text="MFG - toc - JO #74",
            department_resolution=resolve_department("toc", ["TOC"], ALIASES),
        )
        self.assertEqual(p.name, "MFG - TOC - JO #74")

    def test_live_option_spelling_is_authoritative(self):
        p = plan(
            extraction(header_raw="MFG - TO - JO #74"),
            raw_text="MFG - TO - JO #74",
            department_resolution=resolve_department("TO", [" toc "], ALIASES),
        )
        self.assertEqual(p.name, "MFG -  toc  - JO #74")
        self.assertEqual(p.properties["Department"], {"select": {"name": " toc "}})

    def test_fallback_name_is_masked(self):
        p = plan(extraction(header_raw="pw: hunter2"), raw_text="pw: hunter2")
        self.assertEqual(p.name, f"pw: {WITHHELD}")
        self.assertNotIn("hunter2", p.model_dump_json())

    def test_no_number_means_no_jo_number(self):
        p = plan(extraction(header_raw="JOB ORDER"), raw_text="JOB ORDER")
        self.assertNotIn("JO Number", p.properties)
        self.assertIsNone(p.jo_number_int)

    def test_canonical_name_without_header(self):
        self.assertEqual(canonical_name(None, "first line"), "first line")
        header = parse_header("MFG - TOC - JO #74", {})
        self.assertEqual(canonical_name(header, "x"), "MFG - TOC - JO #74")


class FieldTests(unittest.TestCase):
    def test_text_fields_are_verbatim_with_exact_property_names(self):
        p = plan(
            extraction(
                header_raw="MFG - TOC - JO #74",
                title="My Father's Girlfriend FTR",
                task="D14-15 Upload synced dailies",
                purpose="For prod and NF review",
                source="Offline Coby",
                platform="Frame.io",
                link_settings="N/A",
                watermark="Follow netflix guidelines",
                mode_of_transfer="Contenthub, share thru PIX",
                blocker="Waiting for waiver",
                pending="Upload to PIX",
            )
        )
        expected = {
            "Title": "My Father's Girlfriend FTR",
            "Task": "D14-15 Upload synced dailies",
            "Purpose": "For prod and NF review",
            "Source / Source Drive": "Offline Coby",
            "Platform": "Frame.io",
            "Link Settings": "N/A",
            "Watermark": "Follow netflix guidelines",
            "Mode of Transfer": "Contenthub, share thru PIX",
            "Blocker ": "Waiting for waiver",
            "Pending": "Upload to PIX",
        }
        for prop, value in expected.items():
            self.assertEqual(text_of(p.properties[prop]), value, prop)

    def test_absent_values_stay_blank(self):
        p = plan(extraction(header_raw="MFG - TOC - JO #74", watermark="  "))
        for prop in ["Title", "Watermark", "Blocker ", "Subtitle", "Notes", "Deadline"]:
            self.assertNotIn(prop, p.properties)

    def test_status_date_and_telegram_key(self):
        p = plan(extraction(header_raw="MFG - TOC - JO #74"))
        self.assertEqual(p.properties["Status"], {"status": {"name": "Needs Intake Review"}})
        self.assertEqual(
            p.properties["Date"],
            {"date": {"start": "2026-09-15T10:00:00+08:00", "time_zone": "Asia/Manila"}},
        )
        self.assertEqual(text_of(p.properties["Telegram Key"]), "-1001:42")

    def test_subtitle_is_only_burned_in_or_none(self):
        for value, expected in [
            ("Burned-in: English", "Burned-in: English"),
            ("None", "None"),
            ("English", None),
            (None, None),
        ]:
            p = plan(extraction(header_raw="MFG - TOC - JO #74", subtitle=value))
            actual = text_of(p.properties["Subtitle"]) if "Subtitle" in p.properties else None
            self.assertEqual(actual, expected, value)

    def test_poster_subtitle_placeholders_survive_as_posted(self):
        for value in ["-", "N/A", "none"]:
            p = plan(extraction(header_raw="MFG - TOC - JO #74", subtitle=value))
            self.assertEqual(text_of(p.properties["Subtitle"]), value)

    def test_title_and_notes_keep_whitespace_and_nbsp(self):
        p = plan(
            extraction(
                header_raw="MFG - TOC - JO #74", title="  keep\u00a0this\n", notes="  a\u00a0b\n"
            )
        )
        self.assertEqual(text_of(p.properties["Title"]), "  keep\u00a0this\n")
        self.assertEqual(text_of(p.properties["Notes"]), "  a\u00a0b\n")

    def test_whitespace_only_title_and_notes_are_blank(self):
        p = plan(extraction(header_raw="MFG - TOC - JO #74", title="\u00a0 ", notes=" \n"))
        self.assertNotIn("Title", p.properties)
        self.assertNotIn("Notes", p.properties)

    def test_notes_poster_text_separator_items_and_chunking(self):
        poster = " " + "x" * 2500 + "\n"
        p = plan(
            extraction(header_raw="MFG - TOC - JO #74", notes=poster, deadline_raw="ASAP"),
        )
        notes = p.properties["Notes"]["rich_text"]
        self.assertTrue(all(len(i["text"]["content"]) <= 2000 for i in notes))
        self.assertEqual(
            text_of(p.properties["Notes"]),
            f"{poster}\n\n{WATCHDOG_MARKER}\nDeadline as posted: ASAP\n{ASAP_NOTE}",
        )

    def test_name_ignores_inconsistent_extracted_header(self):
        p = plan(
            extraction(header_raw="Other header"),
            raw_text="\n  Actual posted first line \nbody",
        )
        self.assertEqual(p.name, "Actual posted first line")
        p = plan(
            extraction(header_raw="Other header"),
            raw_text="MFG - TOC - JO #74\nbody",
        )
        self.assertEqual(p.name, "MFG - TOC - JO #74")

    def test_deadline_property_and_source(self):
        p = plan(extraction(header_raw="MFG - TOC - JO #74", deadline_raw="September 16, 2am"))
        self.assertEqual(
            p.properties["Deadline"],
            {"date": {"start": "2026-09-16T02:00:00+08:00", "time_zone": "Asia/Manila"}},
        )
        self.assertEqual(p.deadline_source, "stated")

    def test_shift_values_flow_through(self):
        p = plan(
            extraction(header_raw="MFG - TOC - JO #74", deadline_raw="end of shift"),
            assignee_shifts=[("Paul", "5:00 AM")],
        )
        self.assertEqual(p.properties["Deadline"]["date"]["start"], "2026-09-16T05:00:00+08:00")
        self.assertEqual(p.deadline_source, "shift")

    def test_body_is_source_block_then_masked_raw_text(self):
        p = plan(extraction(), raw_text="MFG - TOC - JO #74\nPW: secret")
        self.assertEqual(
            p.body_blocks,
            ["Posted by: Leizette (@leizette)", f"MFG - TOC - JO #74\nPW: {WITHHELD}"],
        )

    def test_long_raw_text_splits_into_2000_character_blocks(self):
        p = plan(extraction(), raw_text="a" * 4500)
        self.assertTrue(all(len(b) <= 2000 for b in p.body_blocks))
        self.assertEqual("".join(p.body_blocks[1:]), "a" * 4500)


class NotesTests(unittest.TestCase):
    def test_poster_notes_only(self):
        self.assertEqual(build_notes("waiting for waiver", None, False), "waiting for waiver")

    def test_poster_notes_then_watchdog_block(self):
        self.assertEqual(
            build_notes("waiting for waiver", "September 10, asap", True),
            "waiting for waiver\n\n"
            "— added by the watchdog —\n"
            "Deadline as posted: September 10, asap\n"
            "ASAP — deprioritize other JOs",
        )

    def test_watchdog_block_alone(self):
        self.assertEqual(
            build_notes(None, "Tonight if keri or tom", False),
            "— added by the watchdog —\nDeadline as posted: Tonight if keri or tom",
        )

    def test_nothing_to_add(self):
        self.assertEqual(build_notes(None, None, False), "")
        self.assertEqual(build_notes("", "  ", False), "")

    def test_notes_are_masked(self):
        self.assertEqual(build_notes("pw: abc", None, False), f"pw: {WITHHELD}")

    def test_plan_notes_keep_hedges_and_asap(self):
        p = plan(
            extraction(
                header_raw="SOY - AE - JOB ORDER 25",
                deadline_raw="September 10, asap",
                notes="pls prioritize",
            )
        )
        self.assertEqual(
            text_of(p.properties["Notes"]),
            "pls prioritize\n\n— added by the watchdog —\n"
            "Deadline as posted: September 10, asap\nASAP — deprioritize other JOs",
        )
        self.assertEqual([f.code for f in p.findings], ["asap", "shift_fallback", "deadline_past"])

    def test_unparsed_deadline_is_kept_in_notes(self):
        p = plan(extraction(deadline_raw="kung kelan matapos yung render"))
        self.assertNotIn("Deadline", p.properties)
        self.assertIn(
            "Deadline as posted: kung kelan matapos yung render", text_of(p.properties["Notes"])
        )
        self.assertEqual([f.code for f in p.findings], ["deadline_unparsed"])

    def test_long_notes_split_into_2000_character_items(self):
        p = plan(extraction(notes="n" * 4100))
        items = p.properties["Notes"]["rich_text"]
        self.assertEqual([len(i["text"]["content"]) for i in items], [2000, 2000, 100])


class ReplayTests(unittest.TestCase):
    def test_stored_plan_replays_to_identical_payload(self):
        p = plan(
            extraction(
                header_raw="MFG - TOC - JO #74",
                deadline_raw="within shift",
                notes="note",
                title="T",
            ),
            assignee_shifts=[("Majo", "7:00 PM")],
        )
        payload = build_job_order_payload(p, data_source_id="ds-1")
        stored = json.loads(p.model_dump_json())
        replayed = JobOrderPlan.model_validate(stored)
        self.assertEqual(replayed, p)
        self.assertEqual(build_job_order_payload(replayed, data_source_id="ds-1"), payload)

    def test_payload_shape(self):
        p = plan(extraction(header_raw="MFG - TOC - JO #74"))
        payload = build_job_order_payload(p, data_source_id="ds-1")
        self.assertEqual(payload["parent"], {"data_source_id": "ds-1"})
        self.assertIs(payload["properties"], p.properties)
        self.assertEqual(
            [c["paragraph"]["rich_text"][0]["text"]["content"] for c in payload["children"]],
            p.body_blocks,
        )


if __name__ == "__main__":
    unittest.main()


class LoadPlanTests(unittest.TestCase):
    def test_a_skeleton_plan_is_upgraded_to_a_replayable_one(self):
        from jo_watchdog.core.job_order import build_job_order_payload, load_plan

        legacy = {
            "name": "First line",
            "telegram_key": "-100:1",
            "body_blocks": ["body"],
            "notes": "— added by the watchdog —\nEarlier row: x",
            "duplicate": {"code": "exact_repost"},
        }
        plan = load_plan(legacy)
        payload = build_job_order_payload(plan, data_source_id="ds")
        self.assertEqual(
            payload["properties"]["Status"], {"status": {"name": "Needs Intake Review"}}
        )
        self.assertEqual(payload["properties"]["Name"]["title"][0]["text"]["content"], "First line")
        self.assertIn(
            "Earlier row: x", payload["properties"]["Notes"]["rich_text"][0]["text"]["content"]
        )
        self.assertEqual(plan.duplicate, {"code": "exact_repost"})
