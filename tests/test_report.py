"""The locked ops-topic formats: exact wording, masking, mentions, links (#23)."""

import unittest
from datetime import UTC, datetime, timedelta

from jo_watchdog.core.report import (
    channel_link,
    finding_line,
    forward_origin_from_raw,
    intake_link,
    render_capture_report,
    render_edit_after_capture,
    render_stale_cache,
)
from jo_watchdog.core.verify import FINDING_ORDER, Finding

WITHHELD = "[password withheld – see Telegram message]"
ROW = "https://notion.so/row1"

# Every finding line from the #23 table, as Telegram shows it (links as HTML).
TABLE = [
    (Finding(code="unsure", params={"reasons": "No JO header."}),
     "Classifier unsure: No JO header."),
    (Finding(code="multiple_jo", params={"reasons": "Two headers."}),
     "Looks like more than one JO in this message, split by hand: Two headers."),
    (Finding(code="unresolved_person", params={"role": "assignee", "raw": "Jhun"}),
     'Assignee "Jhun" didn\'t match a contact'),
    (Finding(code="unresolved_person", params={"role": "cc", "raw": "Bea"}),
     'cc "Bea" didn\'t match a contact'),
    (Finding(code="ambiguous_person", params={"role": "assignee", "raw": "Piolo"}),
     'Assignee "Piolo" matches more than one contact'),
    (Finding(code="unresolved_project", params={"raw": "PP2 APC"}),
     'Project mention "PP2 APC" didn\'t match any known Project Code'),
    (Finding(code="unknown_department", params={"token": "XYZ"}),
     'Department "XYZ" isn\'t a Notion option'),
    (Finding(code="fuzzy_person", params={"raw": "Jhunjhun", "contact": "Jhun Reyes"}),
     'Matched by name: "Jhunjhun" → Jhun Reyes'),
    (Finding(code="exact_repost", params={"url": ROW, "days": 3}),
     f'Exact re-post of <a href="{ROW}">an earlier row</a>, 3 days ago'),
    (Finding(code="number_reuse", params={"jo_number": "#0156", "url": ROW, "days": 3}),
     f'#0156 already used on <a href="{ROW}">an earlier row</a> for this'
     " project+department, 3 days ago"),
    (Finding(code="forward_origin", params={"origin": "hidden_user", "name": "Che"}),
     "Forwarded from a hidden user: Che"),
    (Finding(code="forward_origin", params={"origin": "chat", "name": "Edit Team"}),
     "Forwarded from a group: Edit Team"),
    (Finding(code="forward_origin", params={"origin": "channel", "name": "Studio"}),
     "Forwarded from a channel: Studio"),
    (Finding(code="title_blank"), "Title is blank"),
    (Finding(code="task_blank"), "Task is blank"),
    (Finding(code="deadline_past"), "Deadline is before the message was posted"),
    (Finding(code="deadline_unparsed", params={"raw": "kung kelan"}),
     'Deadline "kung kelan" couldn\'t be turned into a date'),
    (Finding(code="asap"), "ASAP — high priority"),
    (Finding(code="shift_fallback", params={"names": ["Bea", "Jhun"]}),
     "Deadline defaulted to 23:59: Bea, Jhun have no fixed End of Shift"),
    (Finding(code="shift_fallback", params={"names": []}),
     "Deadline defaulted to 23:59: no assignee resolved"),
    (Finding(code="header_date_mismatch",
             params={"raw": "Sept 21 / SUNDAY", "posted": "2026-09-22"}),
     'Header date ("Sept 21 / SUNDAY") doesn\'t match the actual post date (Tue, Sep 22)'),
    (Finding(code="deliverable_hint", params={"name": "Teaser v2", "probability": 0.7234}),
     "Possible Deliverable: Teaser v2 (match probability 0.72)"),
    (Finding(code="matcher_skipped"), "Deliverable match skipped: matcher unavailable"),
    (Finding(code="readback_mismatch", params={"properties": ["Name", "Telegram Key"]}),
     "Notion row differs from the plan: Name, Telegram Key"),
    (Finding(code="duplicate_key", params={"urls": ["https://n/1", "https://n/2"]}),
     'More than one Notion row has this Telegram Key: <a href="https://n/1">row</a>,'
     ' <a href="https://n/2">row</a>'),
]


class FindingLineTests(unittest.TestCase):
    def test_every_line_matches_the_table(self):
        for finding, expected in TABLE:
            with self.subTest(code=finding.code):
                self.assertEqual(finding_line(finding), expected)

    def test_table_covers_every_code(self):
        self.assertEqual({f.code for f, _ in TABLE}, set(FINDING_ORDER))

    def test_one_day_is_singular(self):
        line = finding_line(Finding(code="exact_repost", params={"url": ROW, "days": 1}))
        self.assertTrue(line.endswith(", 1 day ago"))

    def test_source_fields_are_masked_and_escaped(self):
        line = finding_line(
            Finding(code="unresolved_person", params={"role": "assignee", "raw": "pw:hunter2"})
        )
        self.assertNotIn("hunter2", line)
        self.assertIn(WITHHELD, line)
        line = finding_line(Finding(code="unresolved_project", params={"raw": "<b>&"}))
        self.assertIn("&lt;b&gt;&amp;", line)

    def test_unknown_code_is_an_error(self):
        with self.assertRaises(ValueError):
            finding_line(Finding(code="nope"))


class CombinedReportTests(unittest.TestCase):
    def test_no_findings_posts_nothing(self):
        self.assertIsNone(
            render_capture_report(name="X", findings=[], page_url=ROW, intake_url="https://t")
        )

    def test_untagged_report_layout(self):
        text = render_capture_report(
            name="MG - TOC - JO #41",
            findings=[
                Finding(code="deadline_past"),
                Finding(code="header_date_mismatch",
                        params={"raw": "Sept 21 / SUNDAY", "posted": "2026-09-22"}),
            ],
            page_url=ROW,
            intake_url="https://t.me/c/123/5/42",
            leizette_handle="@leizette",
        )
        self.assertEqual(
            text,
            "⚠️ Flagged on capture\n"
            "<code>MG - TOC - JO #41</code>\n"
            "\n"
            "Deadline is before the message was posted\n"
            'Header date ("Sept 21 / SUNDAY") doesn\'t match the actual post date (Tue, Sep 22)\n'
            "\n"
            f'<a href="{ROW}">Notion row</a>\n'
            '<a href="https://t.me/c/123/5/42">Telegram message</a>',
        )

    def test_tagged_report_has_one_mention_and_original_link(self):
        text = render_capture_report(
            name="#0087",
            findings=[
                Finding(code="unresolved_project", params={"raw": "PP2 APC"}),
                Finding(code="ambiguous_person", params={"role": "assignee", "raw": "Piolo"}),
                Finding(code="forward_origin", params={"origin": "channel", "name": "Studio"}),
            ],
            page_url=ROW,
            intake_url="https://t.me/c/123/5/42",
            original_url="https://t.me/studio/77",
            leizette_handle="leizette",
        )
        lines = text.split("\n")
        self.assertEqual(lines[-3], '<a href="https://t.me/studio/77">Original message</a>')
        self.assertEqual(lines[-2:], ["", "@leizette"])
        self.assertEqual(text.count("@leizette"), 1)

    def test_tag_skipped_when_handle_not_configured(self):
        text = render_capture_report(
            name="X",
            findings=[Finding(code="unknown_department", params={"token": "Q"})],
            page_url=ROW,
            intake_url="https://t",
            leizette_handle=None,
        )
        self.assertNotIn("@", text)

    def test_name_is_masked(self):
        text = render_capture_report(
            name="PW: swordfish",
            findings=[Finding(code="title_blank")],
            page_url=ROW,
            intake_url="https://t",
        )
        self.assertNotIn("swordfish", text)
        self.assertIn(f"<code>PW: {WITHHELD}</code>", text)


class SystemMessageTests(unittest.TestCase):
    def test_edit_after_capture(self):
        self.assertEqual(
            render_edit_after_capture(name="A - B - JO #1", page_url=ROW, intake_url="https://t"),
            "✏️ Edited after capture, row not updated\n"
            "<code>A - B - JO #1</code>\n"
            "\n"
            f'<a href="{ROW}">Notion row</a>\n'
            '<a href="https://t">Telegram message</a>',
        )

    def test_stale_cache(self):
        refreshed = datetime(2026, 10, 20, 23, 0, tzinfo=UTC)  # Oct 21 07:00 Manila
        text = render_stale_cache(
            cache="People",
            last_refreshed=refreshed,
            now=refreshed + timedelta(hours=30, minutes=20),
            error="Notion 503 token=pw:abc",
        )
        self.assertEqual(
            text,
            "⚠️ Notion cache is stale\n"
            "People: last refreshed Oct 21 07:00 (30 hours ago)\n"
            f"Last error: Notion 503 token=pw:{WITHHELD}",
        )


class LinkTests(unittest.TestCase):
    def test_intake_link_strips_minus_100(self):
        self.assertEqual(intake_link(-1001234567890, 5, 42), "https://t.me/c/1234567890/5/42")

    def test_channel_links(self):
        self.assertEqual(channel_link({"username": "studio", "id": -1009}, 77),
                         "https://t.me/studio/77")
        self.assertEqual(channel_link({"id": -1009876}, 77), "https://t.me/c/9876/77")
        self.assertIsNone(channel_link({"id": -1009876}, None))
        self.assertIsNone(channel_link({}, 77))

    def test_only_channel_origin_gets_original_link(self):
        channel = {"message": {"forward_origin": {
            "type": "channel", "chat": {"id": -1005, "title": "Studio"},
            "message_id": 9, "author_signature": "Ana"}}}
        origin = forward_origin_from_raw(channel)
        self.assertEqual(origin.name, "Studio — Ana")
        self.assertEqual(origin.original_link, "https://t.me/c/5/9")

        others = [
            {"type": "user", "sender_user": {"first_name": "Che", "last_name": "Cruz"}},
            {"type": "hidden_user", "sender_user_name": "Che"},
            {"type": "chat", "sender_chat": {"id": -1007, "title": "Edit Team"}},
        ]
        for o in others:
            with self.subTest(type=o["type"]):
                origin = forward_origin_from_raw({"message": {"forward_origin": o}})
                self.assertIsNone(origin.original_link)

    def test_channel_without_message_id_gets_no_link(self):
        raw = {"message": {"forward_origin": {"type": "channel", "chat": {"id": -1005}}}}
        self.assertIsNone(forward_origin_from_raw(raw).original_link)

    def test_not_a_forward(self):
        self.assertIsNone(forward_origin_from_raw({"message": {"text": "hi"}}))


if __name__ == "__main__":
    unittest.main()
