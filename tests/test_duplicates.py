"""Content-duplicate keys, repost / reuse decisions, and edit rules (#24)."""

import unittest
from datetime import UTC, datetime, timedelta

from jo_watchdog.core.duplicates import (
    EarlierCapture,
    append_watchdog_line,
    find_duplicate,
    jo_key,
    notes_line,
    text_sha256,
)
from jo_watchdog.core.edits import EditAction, decide_edit
from jo_watchdog.core.header import parse_header, parse_number

NOW = datetime(2026, 9, 20, 4, 0, tzinfo=UTC)
KEY = "proj-page|74"


def row(tg, text, key=KEY, url="https://notion.so/a", days=2):
    return EarlierCapture(
        telegram_key=tg,
        text_sha256=text_sha256(text),
        jo_key=key,
        page_url=url,
        posted_at=NOW - timedelta(days=days),
    )


def find(text, earlier, key=KEY):
    return find_duplicate(
        telegram_key="-1001:900",
        text_sha=text_sha256(text),
        key=key,
        posted_at=NOW,
        earlier=earlier,
    )


class KeyTests(unittest.TestCase):
    def test_hash_and_leading_zero_variants_give_one_integer(self):
        # #21's parser keeps the posted digits; the key uses their integer value.
        for raw in ("JO #074", "#074", "074", "74", "JO 0074"):
            with self.subTest(raw):
                self.assertEqual(int(parse_number(raw)), 74)
        headers = ["MFG - TOC - JO #074", "MFG - TOC - #74", "MFG-TOC-JO 0074"]
        numbers = {parse_header(h, {}).jo_number_int for h in headers}
        self.assertEqual(numbers, {74})
        self.assertEqual(parse_header(headers[0], {}).jo_number, "#074")

    def test_jo_key_is_project_and_number(self):
        # #93: one JO counter per project; the Department is not in the key.
        self.assertEqual(jo_key("p", 74), "p|74")
        self.assertEqual(jo_key("p", 0), "p|0")
        self.assertIsNone(jo_key(None, 74))
        self.assertIsNone(jo_key("p", None))

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

    def test_exact_repost_is_skipped_without_a_complete_key(self):
        self.assertIsNone(find("same", [row("-1001:1", "same", key=None)], key=None))
        self.assertIsNone(find("same", [row("-1001:1", "same")], key=None))

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

    def test_watchdog_line_starts_or_extends_watchdog_notes(self):
        line = "Earlier row: https://notion.so/a (exact re-post)"
        self.assertEqual(append_watchdog_line("", line), line)
        existing = "Deadline as posted: 1pm"
        self.assertEqual(append_watchdog_line(existing, line), f"{existing}\n{line}")


class EditTableTests(unittest.TestCase):
    def test_edit_table(self):
        cases = [
            ("written", EditAction.REPORT),
            ("done", EditAction.REPORT),
            ("received", EditAction.PENDING_CAPTURE),
            ("extracted", EditAction.PENDING_CAPTURE),
            ("planned", EditAction.PENDING_CAPTURE),
            ("writing", EditAction.PENDING_CAPTURE),
            ("ignored", EditAction.IGNORED_MESSAGE),
            (None, EditAction.UNKNOWN),
        ]
        for state, expected in cases:
            with self.subTest(state=state):
                self.assertIs(decide_edit(state), expected)

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
