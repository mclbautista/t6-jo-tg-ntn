"""Notion schema and relation smoke probes (#59): assertions and honest results, offline.

The recorded schemas below stand in for the live Notion responses; the live run
stays manual, so nothing here is live evidence.
"""

import unittest

from jo_watchdog.adapters.notion import NotionError, QueryResponse
from jo_watchdog.app.smoke_gate import FAIL, MANUAL, PASS
from jo_watchdog.app.smoke_notion_probes import (
    NOTES_PROPERTY,
    check_schemas,
    probe_reciprocal_relation,
    probe_retry_after,
)

JO_DS, CONTACTS_DS, PROJECTS_DS, DELIVERABLES_DS = "jo-ds", "contacts-ds", "projects-ds", "dl-ds"
IDS = {
    "Job Orders": JO_DS,
    "Contacts": CONTACTS_DS,
    "Projects": PROJECTS_DS,
    "Deliverables": DELIVERABLES_DS,
}


def _prop(kind, **extra):
    return {"type": kind, **extra}


def _relation(target, synced=None):
    body = {"data_source_id": target, "type": "single_property", "single_property": {}}
    if synced:
        body = {
            "data_source_id": target,
            "type": "dual_property",
            "dual_property": {"synced_property_name": synced},
        }
    return _prop("relation", relation=body)


def good_schemas():
    jo = {
        n: _prop("rich_text")
        for n in (
            "JO Number", "Title", "Subtitle", "Task", "Purpose", "Source / Source Drive",
            "Platform", "Link Settings", "Watermark", "Mode of Transfer", "Blocker ", "Pending",
            NOTES_PROPERTY, "Watchdog Notes", "Telegram Key",
        )
    }
    jo.update(
        {
            "Name": _prop("title"),
            "Date": _prop("date"),
            "Deadline": _prop("date"),
            "Department": _prop("select", select={"options": [{"name": "TOC"}]}),
            "Status": _prop("status", status={"options": [{"name": "Needs Intake Review"}]}),
            "Projects": _relation(PROJECTS_DS),
            "Assigned": _relation(CONTACTS_DS),
            "cc": _relation(CONTACTS_DS),
            "🗂️ Coordinator": _relation(CONTACTS_DS),
            "Deliverable": _relation(DELIVERABLES_DS, synced="Job Orders"),
        }
    )
    deliverables = {
        "Name": _prop("title"),
        "🗺️ Projects": _relation(PROJECTS_DS),
        "Job Orders": _relation(JO_DS, synced="Deliverable"),
        "Status": _prop("status"),
        "Class": _prop("select"),
        "Playdate": _prop("date"),
        "Deadline": _prop("date"),
    }
    return {
        "Job Orders": {"properties": jo},
        "Contacts": {
            "properties": {
                "Name": _prop("title"),
                "TelegramUserID": _prop("number"),
                "TelegramHandle": _prop("rich_text"),
                "Aliases": _prop("rich_text"),
                "End of Shift": _prop("rich_text"),
            }
        },
        "Projects": {"properties": {"Name": _prop("title"), "Project Code": _prop("select")}},
        "Deliverables": {"properties": deliverables},
    }


class SchemaAssertionTests(unittest.TestCase):
    def test_the_recorded_good_schema_has_no_problems(self):
        self.assertEqual(check_schemas(good_schemas(), IDS), [])

    def test_a_renamed_property_is_named_in_the_problems(self):
        schemas = good_schemas()
        props = schemas["Deliverables"]["properties"]
        props["Projects"] = props.pop("🗺️ Projects")  # the audited drift: emoji dropped
        problems = check_schemas(schemas, IDS)
        self.assertTrue(any("Deliverables" in p and "🗺️ Projects" in p for p in problems))

    def test_a_wrong_type_is_reported(self):
        schemas = good_schemas()
        schemas["Job Orders"]["properties"]["Department"] = _prop("rich_text")
        problems = check_schemas(schemas, IDS)
        self.assertTrue(any("Department" in p and "rich_text" in p for p in problems))

    def test_the_trailing_space_in_blocker_is_exact(self):
        schemas = good_schemas()
        props = schemas["Job Orders"]["properties"]
        props["Blocker"] = props.pop("Blocker ")
        self.assertTrue(any("'Blocker '" in p for p in check_schemas(schemas, IDS)))

    def test_missing_aliases_end_of_shift_and_status_option_are_reported(self):
        schemas = good_schemas()
        for name in ("Aliases", "End of Shift"):
            del schemas["Contacts"]["properties"][name]
        schemas["Job Orders"]["properties"]["Status"]["status"]["options"] = []
        problems = " | ".join(check_schemas(schemas, IDS))
        for part in ("Aliases", "End of Shift", "Needs Intake Review"):
            self.assertIn(part, problems)

    def test_the_relations_must_point_at_the_configured_data_sources(self):
        schemas = good_schemas()
        schemas["Job Orders"]["properties"]["Deliverable"] = _relation("elsewhere", "Job Orders")
        self.assertTrue(any("Deliverable" in p for p in check_schemas(schemas, IDS)))

    def test_a_one_way_deliverable_relation_is_not_reciprocal(self):
        schemas = good_schemas()
        schemas["Job Orders"]["properties"]["Deliverable"] = _relation(DELIVERABLES_DS)
        self.assertTrue(any("reciprocal" in p for p in check_schemas(schemas, IDS)))

    def test_a_reciprocal_name_that_does_not_exist_on_deliverables_is_reported(self):
        schemas = good_schemas()
        del schemas["Deliverables"]["properties"]["Job Orders"]
        problems = check_schemas(schemas, IDS)
        self.assertTrue(any("Job Orders" in p and "Deliverables" in p for p in problems))


class FakeProbeNotion:
    """Records created pages; the Deliverable's reciprocal side fills in after ``lag`` reads."""

    def __init__(self, *, reciprocal=True, lag=0, visible=True):
        self.reciprocal, self.lag, self.visible = reciprocal, lag, visible
        self.created, self.reads = [], 0

    async def create_page(self, payload):
        self.created.append(payload)
        from jo_watchdog.adapters.notion import PageRef

        return PageRef(id="jo-new", url="https://notion.so/jo-new")

    async def retrieve_page(self, page_id):
        self.reads += 1
        linked = self.reciprocal and self.reads > self.lag
        return {"properties": {"Job Orders": {"type": "relation", "relation": (
            [{"id": "jo-new"}] if linked else []), "has_more": False}}}

    async def query_data_source(self, data_source_id, request):
        return QueryResponse(results=[{"id": "jo-new"}] if self.visible else [])


async def no_sleep(_):
    return None


class ReciprocalProbeTests(unittest.IsolatedAsyncioTestCase):
    async def _probe(self, notion):
        return await probe_reciprocal_relation(
            notion,
            job_orders_ds=JO_DS,
            relation_name="Deliverable",
            reciprocal_name="Job Orders",
            deliverable_id="dl-1",
            status_option="Needs Intake Review",
            sleep=no_sleep,
        )

    async def test_the_write_is_read_back_from_both_sides(self):
        notion = FakeProbeNotion()
        result = await self._probe(notion)
        self.assertEqual(result.status, PASS, result.detail)
        written = notion.created[0]["properties"]["Deliverable"]
        self.assertEqual(written, {"relation": [{"id": "dl-1"}]})
        self.assertIn("https://notion.so/jo-new", result.detail)  # cleanup instruction

    async def test_a_reciprocal_side_that_syncs_late_is_polled(self):
        self.assertEqual((await self._probe(FakeProbeNotion(lag=2))).status, PASS)

    async def test_a_reciprocal_side_that_never_fills_fails_and_still_names_the_row(self):
        result = await self._probe(FakeProbeNotion(reciprocal=False))
        self.assertEqual(result.status, FAIL)
        self.assertIn("https://notion.so/jo-new", result.detail)

    async def test_relation_contains_missing_the_new_row_fails(self):
        result = await self._probe(FakeProbeNotion(visible=False))
        self.assertEqual(result.status, FAIL)

    async def test_a_rejected_write_fails_rather_than_passes(self):
        class Rejecting(FakeProbeNotion):
            async def create_page(self, payload):
                raise NotionError(400, "validation_error", "bad relation")

        result = await self._probe(Rejecting())
        self.assertEqual(result.status, FAIL)
        self.assertIn("validation_error", result.detail)


class RetryAfterProbeTests(unittest.IsolatedAsyncioTestCase):
    class Notion:
        def __init__(self, limit_after=None, retry_after=2.0):
            self.limit_after, self.retry_after, self.calls = limit_after, retry_after, 0

        async def retrieve_data_source(self, data_source_id):
            self.calls += 1
            if self.limit_after is not None and self.calls == self.limit_after + 1:
                raise NotionError(429, "rate_limited", "slow down", retry_after=self.retry_after)
            return {}

    async def test_a_429_with_retry_after_is_observed_and_honoured(self):
        slept = []

        async def sleep(seconds):
            slept.append(seconds)

        notion = self.Notion(limit_after=5)
        result = await probe_retry_after(notion, "ds", max_requests=20, sleep=sleep)
        self.assertEqual(result.status, PASS, result.detail)
        self.assertEqual(slept, [2.0])
        self.assertEqual(notion.calls, 7)  # six to the limit, then one after the wait

    async def test_no_429_inside_the_bound_is_manual_not_a_fabricated_pass(self):
        notion = self.Notion(limit_after=None)
        result = await probe_retry_after(notion, "ds", max_requests=10, sleep=no_sleep)
        self.assertEqual(result.status, MANUAL)
        self.assertIn("not observed", result.detail)
        self.assertEqual(notion.calls, 10)

    async def test_a_429_without_retry_after_is_reported_as_unverified(self):
        notion = self.Notion(limit_after=2, retry_after=None)
        result = await probe_retry_after(notion, "ds", max_requests=10, sleep=no_sleep)
        self.assertEqual(result.status, MANUAL)
        self.assertIn("Retry-After", result.detail)

    async def test_another_error_fails_the_probe(self):
        class Denied:
            async def retrieve_data_source(self, data_source_id):
                raise NotionError(403, "restricted_resource", "no")

        self.assertEqual((await probe_retry_after(Denied(), "ds", sleep=no_sleep)).status, FAIL)

    async def test_the_request_bound_is_capped(self):
        notion = self.Notion()
        await probe_retry_after(notion, "ds", max_requests=10_000, sleep=no_sleep)
        self.assertLessEqual(notion.calls, 100)


class SmokeStepTests(unittest.IsolatedAsyncioTestCase):
    """The smoke steps skip loudly without their opt-in flags; a skip is never a pass."""

    def _smoke(self, **args):
        from types import SimpleNamespace

        import smoke
        from tests.test_worker import make_config

        return smoke, smoke.Smoke(make_config(), SimpleNamespace(fixture_message_id=1, **args))

    async def test_both_probes_are_registered_and_required(self):
        import smoke

        names = [name for name, _ in smoke.STEPS]
        self.assertIn("reciprocal_relation", names)
        self.assertIn("notion_retry_after", names)
        from jo_watchdog.app.smoke_gate import OPTIONAL_STEPS

        self.assertFalse({"reciprocal_relation", "notion_retry_after"} & OPTIONAL_STEPS)

    async def test_without_their_flags_the_probes_skip(self):
        from jo_watchdog.app.smoke_gate import SKIP

        smoke, s = self._smoke(probe_relation_write=False, probe_retry_after=False)
        self.assertEqual((await smoke.step_reciprocal_relation(s)).status, SKIP)
        self.assertEqual((await smoke.step_notion_retry_after(s)).status, SKIP)


if __name__ == "__main__":
    unittest.main()
