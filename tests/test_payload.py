"""Tests for the pure Notion payload builder and read-back comparison."""

import unittest

from jo_watchdog.core.payload import build_notion_payload, verify_readback
from jo_watchdog.core.plan import Plan


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


if __name__ == "__main__":
    unittest.main()
