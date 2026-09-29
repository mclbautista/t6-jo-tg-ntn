"""Unit tests for the pure core: plan builder and the masking seam."""

import unittest

from jo_watchdog.core.masker import mask_text
from jo_watchdog.core.plan import build_plan, split_blocks


class SplitBlocksTests(unittest.TestCase):
    def test_short_text_is_one_block(self):
        self.assertEqual(split_blocks("hello", limit=2000), ["hello"])

    def test_text_over_limit_is_split(self):
        text = "a" * 2500
        blocks = split_blocks(text, limit=2000)
        self.assertEqual(len(blocks), 2)
        self.assertEqual("".join(blocks), text)
        self.assertTrue(all(len(b) <= 2000 for b in blocks))

    def test_split_prefers_newline_boundaries(self):
        # 1200 + 1200 chars joined by newline; limit 2000 should keep lines intact
        text = "x" * 1200 + "\n" + "y" * 1200
        blocks = split_blocks(text, limit=2000)
        self.assertEqual(blocks, ["x" * 1200, "y" * 1200])

    def test_long_single_line_hard_splits(self):
        text = "z" * 4500  # no newlines
        blocks = split_blocks(text, limit=2000)
        self.assertEqual(len(blocks), 3)
        self.assertTrue(all(len(b) <= 2000 for b in blocks))
        self.assertEqual("".join(blocks), text)


class BuildPlanTests(unittest.TestCase):
    def test_plan_contains_name_telegram_key_and_body_blocks(self):
        plan = build_plan(name="First line here", telegram_key="-1001:42", raw_text="the body")
        self.assertEqual(plan.name, "First line here")
        self.assertEqual(plan.telegram_key, "-1001:42")
        self.assertEqual(plan.body_blocks, ["the body"])

    def test_plan_splits_long_raw_text(self):
        raw = "a" * 3000
        plan = build_plan(name="n", telegram_key="k", raw_text=raw)
        self.assertTrue(all(len(b) <= 2000 for b in plan.body_blocks))
        self.assertEqual("".join(plan.body_blocks), raw)


class MaskerSeamTests(unittest.TestCase):
    def test_masker_redacts_credentials_at_the_existing_seam(self):
        self.assertEqual(
            mask_text("pw: secret"),
            "pw: [password withheld – see Telegram message]",
        )

    def test_masker_is_applied_to_blocks(self):
        from jo_watchdog.core.masker import mask_blocks

        self.assertEqual(
            mask_blocks(["pw: secret", "dl with pw"]),
            ["pw: [password withheld – see Telegram message]", "dl with pw"],
        )


if __name__ == "__main__":
    unittest.main()
