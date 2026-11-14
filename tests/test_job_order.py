"""The deterministic Job Order plan: Name, JO Number, fields, Notes, and replay."""

import unittest
from datetime import datetime
from types import SimpleNamespace

from t6.domain.job_order_rules import canonical_name, parse_header
from t6.domain.studio_time import MANILA

from jo_watchdog.core.job_order import (
    ASAP_NOTE,
    build_notes,
    build_watchdog_notes,
    job_order_fields,
    load_plan,
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
        source_block="Posted by: Priya (@priya)",
        **kwargs,
    )


class NameTests(unittest.TestCase):
    def test_canonical_name_keeps_leading_zeros(self):
        p = plan(extraction(header_raw="SOMSOM - TOC - #0155"))
        self.assertEqual(p.name, "SOMSOM - TOC - JO #0155")
        self.assertEqual(p.fields["number"], "#0155")
        self.assertEqual(p.jo_number_int, 155)
        self.assertEqual(p.fields["department"], "TOC")

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

    def test_leading_tag_line_is_not_the_header(self):
        raw = "[ADVANCED]\nSH1 - TOC - #0034\nbody"
        p = plan(extraction(header_raw="SH1 - TOC - #0034", jo_number_raw="#0034"), raw_text=raw)
        self.assertEqual(p.name, "SH1 - TOC - JO #0034")
        self.assertEqual(p.fields["number"], "#0034")

    def test_escaped_tag_line_is_skipped(self):
        raw = "\\[ADVANCED\\]\nPTG - TOC - #047\nbody"
        self.assertEqual(plan(extraction(), raw_text=raw).name, "PTG - TOC - JO #047")

    def test_unparsed_header_falls_back_to_first_line(self):
        p = plan(
            extraction(header_raw="TOC - Job Order", department_raw="TOC", jo_number_raw="#12"),
            raw_text="TOC - Job Order\nbody",
        )
        self.assertEqual(p.name, "TOC - Job Order")
        self.assertEqual(p.fields["number"], "#12")
        self.assertEqual(p.jo_number_int, 12)
        self.assertEqual(p.fields["department"], "TOC")

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
        self.assertNotIn("department", p.fields)
        self.assertEqual(p.fields["number"], "#74")

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
        self.assertEqual(p.fields["department"], " toc ")

    def test_fallback_name_is_masked(self):
        p = plan(extraction(header_raw="pw: hunter2"), raw_text="pw: hunter2")
        self.assertEqual(p.name, f"pw: {WITHHELD}")
        self.assertNotIn("hunter2", p.model_dump_json())

    def test_no_number_means_no_jo_number(self):
        p = plan(extraction(header_raw="JOB ORDER"), raw_text="JOB ORDER")
        self.assertNotIn("number", p.fields)
        self.assertIsNone(p.jo_number_int)

    def test_canonical_name_without_header(self):
        self.assertEqual(canonical_name(None, "first line"), "first line")
        header = parse_header("MFG - TOC - JO #74", {})
        self.assertEqual(canonical_name(header, "x"), "MFG - TOC - JO #74")


class FieldTests(unittest.TestCase):
    def test_text_fields_are_verbatim(self):
        p = plan(
            extraction(
                header_raw="MFG - TOC - JO #74",
                title="Show Alpha FTR",
                task="D14-15 Upload synced dailies",
                purpose="For prod and NF review",
                source="Offline Owen",
                platform="Frame.io",
                link_settings="N/A",
                watermark="Follow netflix guidelines",
                mode_of_transfer="Contenthub, share thru PIX",
                blocker="Waiting for waiver",
                pending="Upload to PIX",
            )
        )
        expected = {
            "title": "Show Alpha FTR",
            "task": "D14-15 Upload synced dailies",
            "purpose": "For prod and NF review",
            "source": "Offline Owen",
            "platform": "Frame.io",
            "link_settings": "N/A",
            "watermark": "Follow netflix guidelines",
            "mode_of_transfer": "Contenthub, share thru PIX",
            "blocker": "Waiting for waiver",
            "pending": "Upload to PIX",
        }
        for name, value in expected.items():
            self.assertEqual(p.fields[name], value, name)

    def test_absent_values_stay_blank(self):
        p = plan(extraction(header_raw="MFG - TOC - JO #74", watermark="  "))
        for name in [
            "title", "watermark", "blocker", "subtitle", "notes", "watchdog_notes", "deadline",
        ]:  # fmt: skip
            self.assertNotIn(name, p.fields)

    def test_status_date_and_telegram_key(self):
        p = plan(extraction(header_raw="MFG - TOC - JO #74"))
        self.assertEqual(p.fields["status"], "Needs Intake Review")
        self.assertEqual(p.fields["date"], "2026-09-15T10:00:00+08:00")
        self.assertEqual(p.fields["telegram_key"], "-1001:42")

    def test_subtitle_is_only_burned_in_or_none(self):
        for value, expected in [
            ("Burned-in: English", "Burned-in: English"),
            ("None", "None"),
            ("English", None),
            (None, None),
        ]:
            p = plan(extraction(header_raw="MFG - TOC - JO #74", subtitle=value))
            actual = p.fields.get("subtitle")
            self.assertEqual(actual, expected, value)

    def test_poster_subtitle_placeholders_survive_as_posted(self):
        for value in ["-", "N/A", "none"]:
            p = plan(extraction(header_raw="MFG - TOC - JO #74", subtitle=value))
            self.assertEqual(p.fields["subtitle"], value)

    def test_title_and_notes_keep_whitespace_and_nbsp(self):
        p = plan(
            extraction(
                header_raw="MFG - TOC - JO #74", title="  keep\u00a0this\n", notes="  a\u00a0b\n"
            )
        )
        self.assertEqual(p.fields["title"], "  keep\u00a0this\n")
        self.assertEqual(p.fields["notes"], "  a\u00a0b\n")

    def test_whitespace_only_title_and_notes_are_blank(self):
        p = plan(extraction(header_raw="MFG - TOC - JO #74", title="\u00a0 ", notes=" \n"))
        self.assertNotIn("title", p.fields)
        self.assertNotIn("notes", p.fields)

    def test_poster_notes_and_watchdog_notes_are_separate_fields(self):
        poster = " " + "x" * 2500 + "\n"
        p = plan(
            extraction(header_raw="MFG - TOC - JO #74", notes=poster, deadline_raw="ASAP"),
        )
        self.assertEqual(p.fields["notes"], poster)
        self.assertEqual(
            p.fields["watchdog_notes"],
            f"Deadline as posted: ASAP\n{ASAP_NOTE}",
        )

    def test_poster_notes_never_carry_watchdog_text(self):
        p = plan(extraction(header_raw="MFG - TOC - JO #74", deadline_raw="ASAP"))
        self.assertNotIn("notes", p.fields)
        self.assertEqual(
            p.fields["watchdog_notes"],
            f"Deadline as posted: ASAP\n{ASAP_NOTE}",
        )

    def test_watchdog_notes_absent_when_watchdog_adds_nothing(self):
        p = plan(extraction(header_raw="MFG - TOC - JO #74", notes="poster only"))
        self.assertEqual(p.fields["notes"], "poster only")
        self.assertNotIn("watchdog_notes", p.fields)

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

    def test_deadline_field_and_source(self):
        p = plan(extraction(header_raw="MFG - TOC - JO #74", deadline_raw="September 16, 2am"))
        self.assertEqual(p.fields["deadline"], "2026-09-16T02:00:00+08:00")
        self.assertEqual(p.deadline_source, "stated")

    def test_shift_values_flow_through(self):
        p = plan(
            extraction(header_raw="MFG - TOC - JO #74", deadline_raw="end of shift"),
            assignee_shifts=[("Paul", "5:00 AM")],
        )
        self.assertEqual(p.fields["deadline"], "2026-09-16T05:00:00+08:00")
        self.assertEqual(p.deadline_source, "shift")

    def test_body_is_source_block_then_masked_raw_text(self):
        p = plan(extraction(), raw_text="MFG - TOC - JO #74\nPW: secret")
        self.assertEqual(
            p.body_blocks,
            ["Posted by: Priya (@priya)", f"MFG - TOC - JO #74\nPW: {WITHHELD}"],
        )

    def test_long_raw_text_splits_into_2000_character_blocks(self):
        p = plan(extraction(), raw_text="a" * 4500)
        self.assertTrue(all(len(b) <= 2000 for b in p.body_blocks))
        self.assertEqual("".join(p.body_blocks[1:]), "a" * 4500)


class NotesTests(unittest.TestCase):
    def test_poster_notes_verbatim(self):
        self.assertEqual(build_notes("waiting for waiver"), "waiting for waiver")

    def test_watchdog_notes_items_in_order(self):
        self.assertEqual(
            build_watchdog_notes("September 10, asap", True),
            "Deadline as posted: September 10, asap\nASAP — deprioritize other JOs",
        )

    def test_watchdog_notes_deadline_alone(self):
        self.assertEqual(
            build_watchdog_notes("Tonight if keri or tom", False),
            "Deadline as posted: Tonight if keri or tom",
        )

    def test_nothing_to_add(self):
        self.assertEqual(build_notes(None), "")
        self.assertEqual(build_notes(""), "")
        self.assertEqual(build_watchdog_notes(None, False), "")
        self.assertEqual(build_watchdog_notes("  ", False), "")

    def test_notes_are_masked(self):
        self.assertEqual(build_notes("pw: abc"), f"pw: {WITHHELD}")

    def test_plan_notes_keep_hedges_and_asap(self):
        p = plan(
            extraction(
                header_raw="SOY - AE - JOB ORDER 25",
                deadline_raw="September 10, asap",
                notes="pls prioritize",
            )
        )
        self.assertEqual(p.fields["notes"], "pls prioritize")
        self.assertEqual(
            p.fields["watchdog_notes"],
            "Deadline as posted: September 10, asap\nASAP — deprioritize other JOs",
        )
        self.assertEqual([f.code for f in p.findings], ["asap", "shift_fallback", "deadline_past"])

    def test_unparsed_deadline_is_kept_in_watchdog_notes(self):
        p = plan(extraction(deadline_raw="kung kelan matapos yung render"))
        self.assertNotIn("deadline", p.fields)
        self.assertIn(
            "Deadline as posted: kung kelan matapos yung render",
            p.fields["watchdog_notes"],
        )
        self.assertEqual([f.code for f in p.findings], ["deadline_unparsed"])

    def test_long_notes_are_kept_whole_for_t6_core_to_split(self):
        p = plan(extraction(notes="n" * 4100))
        self.assertEqual(p.fields["notes"], "n" * 4100)


class ReplayTests(unittest.TestCase):
    def test_stored_plan_replays_to_identical_fields(self):
        p = plan(
            extraction(
                header_raw="MFG - TOC - JO #74",
                deadline_raw="within shift",
                notes="note",
                title="T",
            ),
            assignee_shifts=[("Lena", "7:00 PM")],
        )
        fields = job_order_fields(p)
        replayed = load_plan(p.model_dump_json())
        self.assertEqual(replayed, p)
        self.assertEqual(job_order_fields(replayed), fields)

    def test_fields_are_what_t6_core_takes(self):
        p = plan(extraction(header_raw="MFG - TOC - JO #74"))
        fields = job_order_fields(p)
        self.assertEqual({k: v for k, v in fields.items() if k != "source_text"}, {
            **p.fields,
            "date": datetime(2026, 9, 15, 10, 0, tzinfo=MANILA),
        })
        self.assertEqual(fields["source_text"], "\n".join(p.body_blocks))


if __name__ == "__main__":
    unittest.main()
