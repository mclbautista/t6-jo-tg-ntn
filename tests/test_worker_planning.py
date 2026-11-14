"""The worker plans with the #21 planner and the #22 resolvers, and replays the stored plan."""

import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from t6.notion.errors import NotionError

from jo_watchdog.app.caches import CacheManager
from jo_watchdog.app.config import Config
from jo_watchdog.app.worker import Worker
from jo_watchdog.core.deadline import MANILA
from jo_watchdog.ledger.db import Ledger
from tests.fakes import FakeClassifier, FakeNotion, FakeTelegram, ok_result, update

KEY = "-100123:42"
PRIYA = "c-priya"
FETCHED = "2026-09-28T00:00:00Z"
PEOPLE = [
    {
        "page_id": "c-ben",
        "name": "Benjamin Cruz",
        "telegram_user_id": "222",
        "end_of_shift": "10:00 PM",
    },
    {"page_id": "c-ana", "name": "Anabelle Reyes", "aliases": "Ana"},
    {"page_id": PRIYA, "name": "Priya"},
]
PROJECTS = [{"page_id": "proj-mfg", "name": "Manufacturing", "code": "MFG"}]


def make_caches(ledger, config, *, people=PEOPLE, departments=("TOC", "VID")):
    caches = CacheManager(ledger=ledger, studio=None, config=config)
    caches._apply("people", people, FETCHED)
    caches._apply("projects", PROJECTS, FETCHED)
    caches._apply("departments", list(departments), FETCHED)
    return caches


class Harness:
    def __init__(
        self,
        test,
        *,
        text="MFG - TOC - JO #74\nTask: cut teaser",
        departments=("TOC", "VID"),
        **fields,
    ):
        tmp = tempfile.TemporaryDirectory()
        test.addCleanup(tmp.cleanup)
        self.ledger = Ledger(Path(tmp.name) / "ledger.db")
        test.addCleanup(self.ledger.close)
        self.config = Config(
            intake_chat_id=-100123,
            intake_topic_id=5,
            ops_chat_id=-100999,
            ops_topic_id=7,
            job_orders_data_source_id="ds-1",
            standing_cc_contact_page_id=PRIYA,
        )
        self.notion = FakeNotion()
        self.telegram = FakeTelegram(
            [update(7, message_id=42, text=text, extra=fields.pop("extra", None))]
        )
        self.caches = make_caches(self.ledger, self.config, departments=departments)
        self.worker = Worker(
            ledger=self.ledger,
            telegram=self.telegram,
            notion=self.notion,
            classifier=FakeClassifier(ok_result(**fields)),
            config=self.config,
            caches=self.caches,
        )

    async def run(self):
        await self.worker.poll_once()
        await self.worker.drain_once()
        await self.worker.deliver_outbox()

    def plan(self) -> dict:
        return json.loads(self.ledger.get_message(KEY)["plan_json"])

    def codes(self) -> list[str]:
        return [f["code"] for f in self.plan()["findings"]]


class PlannerWiringTests(unittest.IsolatedAsyncioTestCase):
    async def test_fully_resolved_jo_writes_the_full_field_set(self):
        h = Harness(
            self,
            title="Teaser",
            task="Cut teaser",
            assignees_raw="Benjamin",
            cc_raw="Ana",
            deadline_raw="Sep 30 end of shift",
            extra={
                "entities": [
                    {"type": "text_mention", "offset": 0, "length": 9, "user": {"id": 222}}
                ]
            },
        )
        await h.run()

        [fields] = h.notion.created
        self.assertEqual(fields["name"], "MFG - TOC - JO #74")
        self.assertEqual(fields["number"], "#74")
        self.assertEqual(fields["department"], "TOC")
        self.assertEqual(fields["project_id"], "proj-mfg")
        self.assertEqual(fields["assigned_ids"], ["c-ben"])
        self.assertEqual(fields["cc_ids"], ["c-ana", PRIYA])
        self.assertEqual(
            fields["deadline"].astimezone(MANILA).isoformat(), "2026-09-30T22:00:00+08:00"
        )  # Benjamin's End of Shift
        self.assertNotIn("coordinator_ids", fields)
        self.assertEqual(
            h.plan()["resolution"],
            {
                "project_page_id": "proj-mfg",
                "department": "TOC",
                "assigned": ["c-ben"],
                "cc": ["c-ana", PRIYA],
                "unresolved": [],
            },
        )
        self.assertEqual(h.ledger.get_message(KEY)["jo_key"], "proj-mfg|74")

    async def test_posted_fields_survive_extraction_plan_and_the_write(self):
        notes = "  keep\u00a0this\n" + "n" * 2100
        h = Harness(
            self,
            text="MFG - TOC - JO #74\nTask: cut teaser",
            header_raw="Some other extracted header",
            title="  Teaser\u00a0cut\n",
            notes=notes,
            subtitle="N/A",
        )
        await h.run()

        [fields] = h.notion.created
        self.assertEqual(fields["name"], "MFG - TOC - JO #74")
        self.assertEqual(fields["title"], "  Teaser\u00a0cut\n")
        self.assertEqual(fields["notes"], notes)
        self.assertEqual(fields["subtitle"], "N/A")
        self.assertEqual(h.plan()["fields"]["notes"], notes)

    async def test_the_body_is_written_as_source_text(self):
        h = Harness(self, text="MFG - TOC - JO #74\nTask: cut teaser")
        await h.run()

        [fields] = h.notion.created
        self.assertIn("Task: cut teaser", fields["source_text"])
        self.assertEqual(fields["source_text"], "\n".join(h.plan()["body_blocks"]))

    async def test_unresolved_project_and_unknown_department_stay_blank(self):
        h = Harness(self, text="ZZZ - XYZ - JO #74\nTask: cut", task="Cut")
        await h.run()

        fields = h.notion.created[0]
        self.assertNotIn("department", fields)
        self.assertNotIn("project_id", fields)
        self.assertIn("unresolved_project", h.codes())
        self.assertIn("unknown_department", h.codes())
        self.assertIsNone(h.plan()["resolution"]["project_page_id"])
        self.assertIsNone(h.ledger.get_message(KEY)["jo_key"])

    async def test_fuzzy_only_person_match_is_written_with_a_finding(self):
        h = Harness(self, task="Cut", assignees_raw="Benjamin Cruze")
        await h.run()

        self.assertEqual(h.notion.created[0]["assigned_ids"], ["c-ben"])
        self.assertIn("fuzzy_person", h.codes())

    async def test_unresolved_person_is_left_out_and_recorded(self):
        h = Harness(self, task="Cut", assignees_raw="Nobody Here")
        await h.run()

        self.assertEqual(h.notion.created[0]["assigned_ids"], [])
        self.assertEqual(
            h.plan()["resolution"]["unresolved"],
            [{"raw": "Nobody Here", "role": "assignee", "status": "unresolved"}],
        )

    async def test_conditional_assignee_is_not_assigned_and_keeps_its_wording(self):
        h = Harness(self, task="Cut", assignees_raw="Ana if available", cc_raw="Benjamin")
        await h.run()

        self.assertEqual(h.notion.created[0]["assigned_ids"], [])
        self.assertEqual(
            h.plan()["resolution"]["unresolved"],
            [{"raw": "Ana if available", "role": "assignee", "status": "ambiguous"}],
        )
        self.assertIn("ambiguous_person", h.codes())
        self.assertIn("Ana if available", h.telegram.sent[-1][2])

    async def test_overlapping_entity_does_not_assign_the_wrong_person(self):
        people = [
            {"page_id": "c-jo", "name": "Jo Lim", "telegram_user_id": "123"},
            {"page_id": "c-lena", "name": "Lena Cruz", "aliases": "Lena"},
            {"page_id": PRIYA, "name": "Priya"},
        ]
        h = Harness(
            self,
            task="Cut",
            assignees_raw="Lena",
            extra={
                "entities": [
                    {"type": "text_mention", "offset": 0, "length": 2, "user": {"id": 123}}
                ]
            },
        )
        h.worker.caches = make_caches(h.ledger, h.config, people=people)
        await h.run()

        self.assertEqual(h.notion.created[0]["assigned_ids"], ["c-lena"])

    async def test_planner_and_resolver_findings_reach_the_capture_report(self):
        h = Harness(self, text="ZZZ - TOC - JO #74\nTask: cut", task="Cut", deadline_raw="ASAP")
        await h.run()

        text = h.telegram.sent[-1][2]
        self.assertIn("ASAP", text)
        self.assertIn("ZZZ", text)
        self.assertIn("asap", h.codes())
        self.assertIn("unresolved_project", h.codes())


class ExplicitDeadlineYearTests(unittest.IsolatedAsyncioTestCase):
    """#48: a posted year is honored end to end, and past deadlines are stored and reported."""

    async def test_explicit_future_year_resolves_stores_and_notes_the_posted_text(self):
        h = Harness(
            self,
            task="Cut",
            assignees_raw="Benjamin",
            deadline_raw="Sep 30, 2030 end of shift",
            notes="keep this",
        )
        await h.run()

        fields = h.notion.created[0]
        self.assertEqual(
            fields["deadline"].astimezone(MANILA).isoformat(), "2030-09-30T22:00:00+08:00"
        )
        self.assertEqual(fields["notes"], "keep this")
        self.assertEqual(fields["watchdog_notes"], "Deadline as posted: Sep 30, 2030 end of shift")
        self.assertNotIn("deadline_past", h.codes())

    async def test_explicit_past_year_is_stored_and_reported_not_rolled_forward(self):
        h = Harness(self, task="Cut", assignees_raw="Benjamin", deadline_raw="Sep 16, 2020 5PM")
        await h.run()

        fields = h.notion.created[0]
        self.assertEqual(
            fields["deadline"].astimezone(MANILA).isoformat(), "2020-09-16T17:00:00+08:00"
        )
        self.assertIn("Deadline as posted: Sep 16, 2020 5PM", fields["watchdog_notes"])
        self.assertNotIn("notes", fields)
        self.assertIn("deadline_past", h.codes())
        self.assertIn("Deadline is before the message was posted", h.telegram.sent[-1][2])


class StoredPlanReplayTests(unittest.IsolatedAsyncioTestCase):
    async def test_resumed_write_uses_the_stored_plan_after_a_cache_swap(self):
        h = Harness(self, task="Cut", assignees_raw="Benjamin")
        h.worker.config = h.config.model_copy(update={"dry_run": True})
        await h.run()
        stored = h.ledger.get_message(KEY)["plan_json"]
        self.assertEqual(h.notion.created, [])

        # A refresh between the crash and the resume changes what resolution would say.
        h.worker.caches = make_caches(
            h.ledger,
            h.config,
            people=[{"page_id": "c-other", "name": "Benjamin Cruz"}],
            departments=("VID",),
        )
        h.worker.config = h.config.model_copy(update={"dry_run": False})
        await h.worker.drain_once()

        [fields] = h.notion.created
        self.assertEqual(fields["assigned_ids"], ["c-ben"])
        self.assertEqual(fields["department"], "TOC")
        self.assertEqual(h.ledger.get_message(KEY)["plan_json"], stored)


class ConditionalReplayTests(unittest.IsolatedAsyncioTestCase):
    async def test_conditional_mention_is_not_backfilled_on_resume(self):
        h = Harness(self, task="Cut", assignees_raw="Ana if available")
        h.worker.config = h.config.model_copy(update={"dry_run": True})
        await h.run()
        stored = h.ledger.get_message(KEY)["plan_json"]

        h.worker.config = h.config.model_copy(update={"dry_run": False})
        await h.worker.drain_once()

        [fields] = h.notion.created
        self.assertEqual(fields["assigned_ids"], [])
        self.assertEqual(h.ledger.get_message(KEY)["plan_json"], stored)


class DryRunReportTests(unittest.IsolatedAsyncioTestCase):
    async def test_dry_run_post_lists_fields_as_plain_text(self):
        h = Harness(self, task="Cut", assignees_raw="Benjamin")
        h.worker.config = h.config.model_copy(update={"dry_run": True})
        await h.run()

        text = h.telegram.sent[-1][2]
        self.assertIn("Department: TOC", text)
        self.assertIn("Assigned: c-ben", text)
        self.assertNotIn("'rich_text'", text)


def _stored_before_134(plan: dict, *, utc: bool) -> dict:
    """The plan as a build before #134 stored it: Notion properties, dates with a time_zone."""
    fields = plan["fields"]
    properties = {
        "Name": {"title": [{"text": {"content": fields["name"]}}]},
        "Telegram Key": {"rich_text": [{"text": {"content": fields["telegram_key"]}}]},
        "Status": {"status": {"name": fields["status"]}},
    }
    for notion_name, field in (("Date", "date"), ("Deadline", "deadline")):
        moment = datetime.fromisoformat(fields[field])
        start = (moment.astimezone(UTC) if utc else moment.astimezone(MANILA)).isoformat()
        properties[notion_name] = {"date": {"start": start, "time_zone": "Asia/Manila"}}
    old = {key: value for key, value in plan.items() if key != "fields"}
    return {**old, "properties": properties}


class StoredPlanFromBefore134Tests(unittest.IsolatedAsyncioTestCase):
    async def test_a_stuck_row_stored_with_either_earlier_shape_is_written_once(self):
        for utc in (False, True):
            with self.subTest(utc=utc):
                h = Harness(self, task="Cut", deadline_raw="September 16, 2am")
                h.worker.config = h.config.model_copy(update={"dry_run": True})
                await h.run()
                old = _stored_before_134(h.plan(), utc=utc)
                h.ledger._conn.execute(
                    "UPDATE messages SET plan_json = ? WHERE telegram_key = ?",
                    (json.dumps(old), KEY),
                )
                h.worker.config = h.config.model_copy(update={"dry_run": False})
                await h.worker.drain_once()

                self.assertEqual(h.notion.create_calls, 1)
                [fields] = h.notion.created
                self.assertEqual(
                    fields["deadline"].astimezone(MANILA).isoformat(), "2026-09-16T02:00:00+08:00"
                )
                self.assertEqual(fields["date"].utcoffset().total_seconds(), 8 * 3600)
                row = h.ledger.get_message(KEY)
                self.assertEqual(row["state"], "done")
                self.assertFalse(row["readback_mismatch"])


class WriteOutcomeTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_rejected_write_keeps_the_provider_message_and_creates_no_page(self):
        h = Harness(self, task="Cut")
        h.notion = h.worker.notion = FakeNotion(
            fail_create=True,
            create_error=NotionError(400, "validation_error", "time zone offsets from UTC"),
        )
        await h.run()
        row = h.ledger.get_message(KEY)
        self.assertEqual(row["state"], "writing")
        self.assertIn("validation_error", row["last_error"])
        self.assertIn("time zone offsets from UTC", row["last_error"])
        self.assertFalse(row["notion_page_id"])
