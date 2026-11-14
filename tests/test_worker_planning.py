from jo_watchdog.core.plan import NOTES_PROPERTY
"""The worker plans with the #21 planner and the #22 resolvers, and replays the stored plan."""

import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from jo_watchdog.adapters.notion import NotionError
from jo_watchdog.app.caches import CacheManager
from jo_watchdog.app.config import Config
from jo_watchdog.app.worker import Worker
from jo_watchdog.core.deadline import MANILA
from jo_watchdog.core.job_order import manila_iso
from jo_watchdog.ledger.db import Ledger
from tests.fakes import FakeClassifier, FakeNotion, FakeTelegram, live_date, ok_result, update

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
    async def test_fully_resolved_jo_writes_the_full_property_set(self):
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

        [payload] = h.notion.created
        props = payload["properties"]
        self.assertEqual(props["Name"]["title"][0]["text"]["content"], "MFG - TOC - JO #74")
        self.assertEqual(props["JO Number"]["rich_text"][0]["text"]["content"], "#74")
        self.assertEqual(props["Department"], {"select": {"name": "TOC"}})
        self.assertEqual(props["Projects"], {"relation": [{"id": "proj-mfg"}]})
        self.assertEqual(props["Assigned"], {"relation": [{"id": "c-ben"}]})
        self.assertEqual(props["cc"], {"relation": [{"id": "c-ana"}, {"id": PRIYA}]})
        self.assertEqual(
            manila_iso(datetime.fromisoformat(props["Deadline"]["date"]["start"])), "2026-09-30T22:00:00+08:00"
        )  # Benjamin's End of Shift
        self.assertNotIn("🗂️ Coordinator", props)
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

    async def test_posted_fields_survive_extraction_plan_and_notion_payload(self):
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

        [payload] = h.notion.created
        props = payload["properties"]

        def text(prop):
            return "".join(i["text"]["content"] for i in props[prop]["rich_text"])

        self.assertEqual(props["Name"]["title"][0]["text"]["content"], "MFG - TOC - JO #74")
        self.assertEqual(text("Title"), "  Teaser\u00a0cut\n")
        self.assertEqual(text(NOTES_PROPERTY), notes)
        self.assertEqual(text("Subtitle"), "N/A")
        self.assertTrue(all(len(i["text"]["content"]) <= 2000 for i in props[NOTES_PROPERTY]["rich_text"]))
        self.assertEqual(h.plan()["properties"][NOTES_PROPERTY], props[NOTES_PROPERTY])

    async def test_unresolved_project_and_unknown_department_stay_blank(self):
        h = Harness(self, text="ZZZ - XYZ - JO #74\nTask: cut", task="Cut")
        await h.run()

        props = h.notion.created[0]["properties"]
        self.assertNotIn("Department", props)
        self.assertEqual(props["Projects"], {"relation": []})
        self.assertIn("unresolved_project", h.codes())
        self.assertIn("unknown_department", h.codes())
        self.assertIsNone(h.plan()["resolution"]["project_page_id"])
        self.assertIsNone(h.ledger.get_message(KEY)["jo_key"])

    async def test_fuzzy_only_person_match_is_written_with_a_finding(self):
        h = Harness(self, task="Cut", assignees_raw="Benjamin Cruze")
        await h.run()

        self.assertEqual(
            h.notion.created[0]["properties"]["Assigned"], {"relation": [{"id": "c-ben"}]}
        )
        self.assertIn("fuzzy_person", h.codes())

    async def test_unresolved_person_is_left_out_and_recorded(self):
        h = Harness(self, task="Cut", assignees_raw="Nobody Here")
        await h.run()

        self.assertEqual(h.notion.created[0]["properties"]["Assigned"], {"relation": []})
        self.assertEqual(
            h.plan()["resolution"]["unresolved"],
            [{"raw": "Nobody Here", "role": "assignee", "status": "unresolved"}],
        )

    async def test_conditional_assignee_is_not_assigned_and_keeps_its_wording(self):
        h = Harness(self, task="Cut", assignees_raw="Ana if available", cc_raw="Benjamin")
        await h.run()

        props = h.notion.created[0]["properties"]
        self.assertEqual(props["Assigned"], {"relation": []})
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

        self.assertEqual(
            h.notion.created[0]["properties"]["Assigned"], {"relation": [{"id": "c-lena"}]}
        )

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

        props = h.notion.created[0]["properties"]
        self.assertEqual(manila_iso(datetime.fromisoformat(props["Deadline"]["date"]["start"])), "2030-09-30T22:00:00+08:00")
        self.assertEqual(props[NOTES_PROPERTY]["rich_text"][0]["text"]["content"], "keep this")
        self.assertEqual(
            props["Watchdog Notes"]["rich_text"][0]["text"]["content"],
            "Deadline as posted: Sep 30, 2030 end of shift",
        )
        self.assertNotIn("deadline_past", h.codes())

    async def test_explicit_past_year_is_stored_and_reported_not_rolled_forward(self):
        h = Harness(self, task="Cut", assignees_raw="Benjamin", deadline_raw="Sep 16, 2020 5PM")
        await h.run()

        props = h.notion.created[0]["properties"]
        self.assertEqual(manila_iso(datetime.fromisoformat(props["Deadline"]["date"]["start"])), "2020-09-16T17:00:00+08:00")
        self.assertIn(
            "Deadline as posted: Sep 16, 2020 5PM",
            props["Watchdog Notes"]["rich_text"][0]["text"]["content"],
        )
        self.assertNotIn(NOTES_PROPERTY, props)
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

        [payload] = h.notion.created
        self.assertEqual(payload["properties"]["Assigned"], {"relation": [{"id": "c-ben"}]})
        self.assertEqual(payload["properties"]["Department"], {"select": {"name": "TOC"}})
        self.assertEqual(h.ledger.get_message(KEY)["plan_json"], stored)


class ConditionalReplayTests(unittest.IsolatedAsyncioTestCase):
    async def test_conditional_mention_is_not_backfilled_on_resume(self):
        h = Harness(self, task="Cut", assignees_raw="Ana if available")
        h.worker.config = h.config.model_copy(update={"dry_run": True})
        await h.run()
        stored = h.ledger.get_message(KEY)["plan_json"]

        h.worker.config = h.config.model_copy(update={"dry_run": False})
        await h.worker.drain_once()

        [payload] = h.notion.created
        self.assertEqual(payload["properties"]["Assigned"], {"relation": []})
        self.assertEqual(h.ledger.get_message(KEY)["plan_json"], stored)


class DryRunReportTests(unittest.IsolatedAsyncioTestCase):
    async def test_dry_run_post_lists_properties_as_plain_text(self):
        h = Harness(self, task="Cut", assignees_raw="Benjamin")
        h.worker.config = h.config.model_copy(update={"dry_run": True})
        await h.run()

        text = h.telegram.sent[-1][2]
        self.assertIn("Department: TOC", text)
        self.assertIn("Assigned: c-ben", text)
        self.assertNotIn("'rich_text'", text)


def _notion_date_error(payload: dict) -> NotionError | None:
    """Notion's rule (#83): ``time_zone`` beside a non-zero UTC offset is a 400."""
    for prop in payload["properties"].values():
        date = prop.get("date") if isinstance(prop, dict) else None
        if not date or not date.get("time_zone"):
            continue
        for edge in ("start", "end"):
            value = date.get(edge)
            if value and datetime.fromisoformat(value).utcoffset() != timedelta(0):
                return NotionError(
                    400,
                    "validation_error",
                    "body failed validation: if time zone is explicitly provided, "
                    "start and end can't have non-zero time offsets from UTC.",
                )
    return None


class StrictDateNotion(FakeNotion):
    async def create_page(self, payload: dict):
        error = _notion_date_error(payload)
        if error:
            self.create_calls += 1
            raise error
        return await super().create_page(payload)


def _stored_with_time_zone(plan: dict, *, utc: bool) -> None:
    """Rewrite a stored plan's dates in the shape an earlier build stored them."""
    for field in ("Date", "Deadline"):
        date = plan["properties"][field]["date"]
        moment = datetime.fromisoformat(date["start"])
        date["start"] = (moment.astimezone(UTC) if utc else moment.astimezone(MANILA)).isoformat()
        date["time_zone"] = "Asia/Manila"


class NotionDatePayloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_date_and_deadline_are_sent_as_manila_offset_instants_without_time_zone(self):
        h = Harness(self, task="Cut", deadline_raw="September 16, 2am")
        h.notion = h.worker.notion = StrictDateNotion()
        await h.run()
        [payload] = h.notion.created
        for name in ("Date", "Deadline"):
            date = payload["properties"][name]["date"]
            self.assertNotIn("time_zone", date)
            self.assertEqual(datetime.fromisoformat(date["start"]).utcoffset(), timedelta(hours=8))
        deadline = datetime.fromisoformat(payload["properties"]["Deadline"]["date"]["start"])
        self.assertEqual(deadline.strftime("%m-%d %H:%M"), "09-16 02:00")

    async def test_live_readback_of_both_dates_matches_the_planned_instants(self):
        h = Harness(self, task="Cut", deadline_raw="September 16, 2am")
        await h.run()
        row = h.ledger.get_message(KEY)
        self.assertEqual(row["state"], "done")
        self.assertFalse(row["readback_mismatch"])
        page = next(iter(h.notion.pages.values()))
        deadline = page["properties"]["Deadline"]["date"]
        self.assertEqual(deadline["start"], "2026-09-16T02:00:00+08:00")
        self.assertIsNone(deadline["time_zone"])

    async def test_an_eight_hour_shift_on_readback_is_detected_and_reported(self):
        h = Harness(self, task="Cut", deadline_raw="September 16, 2am")
        shifted = FakeNotion()
        original = shifted.retrieve_page

        async def retrieve(page_id: str) -> dict:
            page = await original(page_id)
            date = page["properties"]["Deadline"]["date"]
            date["start"] = (datetime.fromisoformat(date["start"]) - timedelta(hours=8)).isoformat()
            return page

        shifted.retrieve_page = retrieve
        h.notion = h.worker.notion = shifted
        await h.run()
        row = h.ledger.get_message(KEY)
        self.assertIn("Deadline differs", row["readback_mismatch"])
        self.assertNotIn("Date differs", row["readback_mismatch"])
        self.assertEqual(shifted.create_calls, 1)

    async def test_the_old_utc_shape_would_have_read_back_eight_hours_early(self):
        # The #85 trial: a UTC start with time_zone came back at the wall clock in Manila.
        read = live_date({"start": "2026-10-27T09:20:02+00:00", "time_zone": "Asia/Manila"})
        self.assertEqual(read["start"], "2026-10-27T09:20:00+08:00")

    async def test_the_rejected_shape_is_recognised_by_the_strict_fake(self):
        bad = {"date": {"start": "2026-09-15T10:00:00+08:00", "time_zone": "Asia/Manila"}}
        self.assertIsNotNone(_notion_date_error({"properties": {"Date": bad}}))

    async def test_stuck_rows_with_either_earlier_stored_shape_recover_once(self):
        for utc in (False, True):
            with self.subTest(utc=utc):
                h = Harness(self, task="Cut", deadline_raw="September 16, 2am")
                h.notion = h.worker.notion = StrictDateNotion()
                h.worker.config = h.config.model_copy(update={"dry_run": True})
                await h.run()
                plan = h.plan()
                _stored_with_time_zone(plan, utc=utc)
                h.ledger._conn.execute(
                    "UPDATE messages SET plan_json = ? WHERE telegram_key = ?",
                    (json.dumps(plan), KEY),
                )
                h.worker.config = h.config.model_copy(update={"dry_run": False})
                await h.worker.drain_once()

                self.assertEqual(h.notion.create_calls, 1)
                [payload] = h.notion.created
                self.assertNotIn("time_zone", payload["properties"]["Date"]["date"])
                self.assertEqual(
                    payload["properties"]["Deadline"]["date"]["start"], "2026-09-16T02:00:00+08:00"
                )
                row = h.ledger.get_message(KEY)
                self.assertEqual(row["state"], "done")
                self.assertFalse(row["readback_mismatch"])

    async def test_rejected_write_keeps_the_provider_message_and_creates_no_page(self):
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
