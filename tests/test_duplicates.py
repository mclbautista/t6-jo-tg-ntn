"""Content-duplicate keys, repost / reuse decisions, and edit rules (#24)."""

import unittest
from datetime import UTC, datetime, timedelta

from jo_watchdog.core.duplicates import (
    EarlierCapture,
    duplicate_finding,
    find_duplicate,
    jo_key,
    jo_number_int,
    notes_line,
    text_sha256,
)
from jo_watchdog.core.edits import EditAction, decide_edit

NOW = datetime(2026, 9, 20, 4, 0, tzinfo=UTC)
KEY = "proj-page|TOC|74"


def row(tg, text, key=KEY, url="https://notion.so/a", days=2):
    return EarlierCapture(
        telegram_key=tg,
        text_sha256=text_sha256(text),
        jo_key=key,
        page_url=url,
        received_at=NOW - timedelta(days=days),
    )


def find(text, earlier, key=KEY):
    return find_duplicate(
        telegram_key="-1001:900",
        text_sha=text_sha256(text),
        key=key,
        received_at=NOW,
        earlier=earlier,
    )


class KeyTests(unittest.TestCase):
    def test_hash_and_leading_zero_variants_normalize(self):
        for raw in ("#074", "074", "74", "# 74", " #0074 "):
            with self.subTest(raw):
                self.assertEqual(jo_number_int(raw), 74)

    def test_unparseable_numbers(self):
        for raw in (None, "", "#", "74a", "JO 74-B"):
            with self.subTest(raw):
                self.assertIsNone(jo_number_int(raw))

    def test_jo_key_needs_all_three(self):
        self.assertEqual(jo_key("p", "TOC", 74), "p|TOC|74")
        self.assertEqual(jo_key("p", "TOC", 0), "p|TOC|0")
        self.assertIsNone(jo_key(None, "TOC", 74))
        self.assertIsNone(jo_key("p", None, 74))
        self.assertIsNone(jo_key("p", "TOC", None))

    def test_text_hash_ignores_whitespace_differences(self):
        self.assertEqual(text_sha256("a  b\n c "), text_sha256("a b c"))
        self.assertNotEqual(text_sha256("a b"), text_sha256("a c"))


class FindDuplicateTests(unittest.TestCase):
    def test_no_earlier_rows(self):
        self.assertIsNone(find("x", []))

    def test_exact_repost(self):
        match = find("JO #074 cut teaser", [row("-1001:1", "JO #074  cut teaser")])
        self.assertEqual(match.kind, "exact_repost")
        self.assertEqual(match.days_ago, 2)

    def test_number_reuse_with_different_text(self):
        match = find("JO #74 new task", [row("-1001:1", "JO #074 old task")])
        self.assertEqual(match.kind, "number_reuse")

    def test_exact_repost_wins_over_reuse(self):
        earlier = [row("-1001:1", "other text", days=5), row("-1001:2", "same text", days=1)]
        match = find("same text", earlier)
        self.assertEqual((match.kind, match.earlier.telegram_key), ("exact_repost", "-1001:2"))

    def test_earliest_match_is_referenced(self):
        earlier = [
            row("-1001:2", "same", url="https://notion.so/b", days=1),
            row("-1001:1", "same", url="https://notion.so/a", days=4),
        ]
        self.assertEqual(find("same", earlier).earlier.page_url, "https://notion.so/a")

    def test_incomplete_key_skips_reuse_detection(self):
        earlier = [row("-1001:1", "old", key=None)]
        self.assertIsNone(find("new", earlier, key=None))
        self.assertIsNone(find("new", [row("-1001:1", "old")], key=None))

    def test_exact_repost_still_found_without_jo_key(self):
        self.assertEqual(
            find("same", [row("-1001:1", "same", key=None)], key=None).kind, "exact_repost"
        )

    def test_rows_without_a_notion_page_are_not_referenced(self):
        self.assertIsNone(find("same", [row("-1001:1", "same", url=None)]))

    def test_message_never_matches_itself(self):
        self.assertIsNone(find("same", [row("-1001:900", "same")]))


class OutputTests(unittest.TestCase):
    def test_notes_lines(self):
        self.assertEqual(
            notes_line(find("same", [row("-1001:1", "same")])),
            "Earlier row: https://notion.so/a (exact re-post)",
        )
        self.assertEqual(
            notes_line(find("new", [row("-1001:1", "old")])),
            "Earlier row: https://notion.so/a (possible number reuse)",
        )

    def test_findings(self):
        self.assertEqual(
            duplicate_finding(find("same", [row("-1001:1", "same")])),
            {"code": "exact_repost", "params": {"url": "https://notion.so/a", "days": 2}},
        )
        self.assertEqual(
            duplicate_finding(find("new", [row("-1001:1", "old")]), jo_number="#074"),
            {
                "code": "number_reuse",
                "params": {"url": "https://notion.so/a", "days": 2, "jo_number": "#074"},
            },
        )


class EditTableTests(unittest.TestCase):
    def test_edit_table(self):
        cases = [
            ("written", False, EditAction.REPORT),
            ("done", False, EditAction.REPORT),
            ("received", False, EditAction.PENDING_CAPTURE),
            ("extracted", False, EditAction.PENDING_CAPTURE),
            ("planned", False, EditAction.PENDING_CAPTURE),
            ("writing", False, EditAction.PENDING_CAPTURE),
            ("ignored", False, EditAction.IGNORED_MESSAGE),
            (None, True, EditAction.IGNORED_MESSAGE),
            (None, False, EditAction.UNKNOWN),
        ]
        for state, ignored, expected in cases:
            with self.subTest(state=state, ignored=ignored):
                self.assertIs(decide_edit(state, ignored=ignored), expected)

    def test_outcomes_match_the_edits_table_values(self):
        self.assertEqual(
            {a.value for a in EditAction} - {"edit_unknown_message"},
            {"reported", "pending_capture", "ignored_message"},
        )

    def test_unknown_state_fails_closed(self):
        with self.assertRaises(ValueError):
            decide_edit("bogus")


if __name__ == "__main__":
    unittest.main()
