"""The worker plans with the #21 planner and the #22 resolvers, and replays the stored plan."""

import json
import tempfile
import unittest
from pathlib import Path

from jo_watchdog.app.caches import CacheManager
from jo_watchdog.app.config import Config
from jo_watchdog.app.worker import Worker
from jo_watchdog.ledger.db import Ledger
from tests.fakes import FakeClassifier, FakeNotion, FakeTelegram, ok_result, update

KEY = "-100123:42"
LEIZETTE = "c-leizette"
FETCHED = "2026-09-28T00:00:00Z"
PEOPLE = [
    {
        "page_id": "c-ben",
        "name": "Benjamin Cruz",
        "telegram_user_id": "222",
        "end_of_shift": "10:00 PM",
    },
    {"page_id": "c-ana", "name": "Anabelle Reyes", "aliases": "Ana"},
    {"page_id": LEIZETTE, "name": "Leizette"},
]
PROJECTS = [{"page_id": "proj-mfg", "name": "Manufacturing", "code": "MFG"}]


def make_caches(ledger, config, *, people=PEOPLE, departments=("TOC", "VID")):
    caches = CacheManager(ledger=ledger, notion=None, config=config)
    caches._apply("people", people, FETCHED)
    caches._apply("projects", PROJECTS, FETCHED)
    caches._apply("departments", list(departments), FETCHED)
    return caches


class Harness:
    def __init__(self, test, *, text="MFG - TOC - JO #74\nTask: cut teaser", **fields):
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
            leizette_contact_page_id=LEIZETTE,
        )
        self.notion = FakeNotion()
        self.telegram = FakeTelegram(
            [update(7, message_id=42, text=text, extra=fields.pop("extra", None))]
        )
        self.caches = make_caches(self.ledger, self.config)
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
        self.assertEqual(props["cc"], {"relation": [{"id": "c-ana"}, {"id": LEIZETTE}]})
        self.assertEqual(
            props["Deadline"]["date"]["start"], "2026-09-30T22:00:00+08:00"
        )  # Benjamin's End of Shift
        self.assertNotIn("🗂️ Coordinator", props)
        self.assertEqual(
            h.plan()["resolution"],
            {
                "project_page_id": "proj-mfg",
                "department": "TOC",
                "assigned": ["c-ben"],
                "cc": ["c-ana", LEIZETTE],
                "unresolved": [],
            },
        )
        self.assertEqual(h.ledger.get_message(KEY)["jo_key"], "proj-mfg|TOC|74")

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

    async def test_planner_and_resolver_findings_reach_the_capture_report(self):
        h = Harness(self, text="ZZZ - TOC - JO #74\nTask: cut", task="Cut", deadline_raw="ASAP")
        await h.run()

        text = h.telegram.sent[-1][2]
        self.assertIn("ASAP", text)
        self.assertIn("ZZZ", text)
        self.assertIn("asap", h.codes())
        self.assertIn("unresolved_project", h.codes())


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


class DryRunReportTests(unittest.IsolatedAsyncioTestCase):
    async def test_dry_run_post_lists_properties_as_plain_text(self):
        h = Harness(self, task="Cut", assignees_raw="Benjamin")
        h.worker.config = h.config.model_copy(update={"dry_run": True})
        await h.run()

        text = h.telegram.sent[-1][2]
        self.assertIn("Department: TOC", text)
        self.assertIn("Assigned: c-ben", text)
        self.assertNotIn("'rich_text'", text)
