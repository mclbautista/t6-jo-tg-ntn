"""Masked extraction through t6.llm, and its comparison with the unmasked one (#138)."""

import json
import unittest

from t6.llm.testing import FakeProvider
from t6.privacy import Person, Project, Registry

from jo_watchdog.adapters.t6_llm import MaskedExtractor
from jo_watchdog.app.shadow import compare_extraction
from jo_watchdog.core.extraction import EXTRACTED_FIELDS, Extraction

REGISTRY = Registry(
    people=[Person("Priya Raman", aliases=("Pri",), handles=("@priya",))],
    projects=[Project("Blue Harbor", "BLH")],
)


def extraction(**changes):
    base = dict.fromkeys(
        (*EXTRACTED_FIELDS, "link_settings", "mode_of_transfer", "platform", "blocker", "pending",
         "notes", "deadline_raw", "assignees_raw", "cc_raw", "subtitle"),
    )
    base.update(
        verdict="jo", confidence="high", reasons="A job order.", multiple_jo_suspected=False
    )
    return Extraction(**{**base, **changes})


def extractor(fake):
    return MaskedExtractor("key", base_url="https://x.test", model="m", provider=fake)


class MaskedExtractorTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_placeholders_leave_and_names_come_back(self):
        fake = FakeProvider(retention="unknown")
        reply = extraction(task="Ask [person 1] about [project 1]", project_raw="[project 1]")
        fake.reply(json.dumps(reply.model_dump()), prompt_tokens=10, completion_tokens=5)
        masked = extractor(fake)
        result = await masked.extract("JO for Blue Harbor, assign Priya Raman", None, REGISTRY)
        await masked.close()
        sent = json.dumps(fake.calls[0].request)
        self.assertNotIn("Blue Harbor", sent)
        self.assertNotIn("Priya", sent)
        self.assertEqual(result.outcome, "ok")
        self.assertEqual(result.extraction.project_raw, "Blue Harbor")
        self.assertEqual(result.extraction.task, "Ask Priya Raman about Blue Harbor")
        self.assertEqual((result.input_tokens, result.output_tokens), (10, 5))

    async def test_a_bad_reply_is_returned_not_raised(self):
        fake = FakeProvider(retention="unknown")
        fake.reply("not json")
        masked = extractor(fake)
        result = await masked.extract("text", None, REGISTRY)
        await masked.close()
        self.assertEqual(result.outcome, "invalid_json")
        self.assertIsNone(result.extraction)

    async def test_a_timeout_is_returned(self):
        fake = FakeProvider(retention="unknown")
        fake.timeout()
        masked = extractor(fake)
        result = await masked.extract("text", None, REGISTRY)
        await masked.close()
        self.assertEqual(result.outcome, "timeout")


class CompareExtractionTests(unittest.TestCase):
    def test_equal_extractions_do_not_differ(self):
        self.assertEqual(compare_extraction(extraction(task="a  b"), extraction(task="a b")), [])

    def test_a_changed_field_is_named_without_its_value(self):
        found = compare_extraction(extraction(task="Priya Raman"), extraction(task="[person 1]"))
        self.assertEqual([(d.kind, d.legacy) for d in found], [("extraction", "task")])
        self.assertNotIn("Priya", found[0].line())

    def test_a_missing_value_differs_from_a_present_one(self):
        found = compare_extraction(extraction(title=None), extraction(title="x"))
        self.assertEqual([d.legacy for d in found], ["title"])


if __name__ == "__main__":
    unittest.main()


class WorkerHookTests(unittest.IsolatedAsyncioTestCase):
    def worker(self, extractor, *, enabled=True, registry=REGISTRY):
        from types import SimpleNamespace

        from jo_watchdog.app.worker import Worker

        worker = Worker.__new__(Worker)
        worker.config = SimpleNamespace(llm_shadow_masking=enabled)
        worker.masked_extractor = extractor
        worker.caches = SimpleNamespace(privacy_registry=lambda: registry)
        return worker

    async def test_a_failing_shadow_never_raises(self):
        class Broken:
            async def extract(self, *a):
                raise RuntimeError("boom")

        worker = self.worker(Broken())
        with self.assertLogs("jo_watchdog.worker", level="ERROR"):
            await worker._shadow_masked_extraction({"masked_text": "x"}, {}, extraction())

    async def test_off_by_default_makes_no_call(self):
        class Spy:
            called = False

            async def extract(self, *a):
                self.called = True

        spy = Spy()
        await self.worker(spy, enabled=False)._shadow_masked_extraction(
            {"masked_text": "x"}, {}, extraction()
        )
        self.assertFalse(spy.called)
