"""Forward-origin provenance over the Telegram contract fixtures (#24)."""

import json
import unittest
from datetime import UTC, datetime
from pathlib import Path

from jo_watchdog.core.provenance import (
    intake_time,
    originally_from,
    parse_forward_origin,
    source_block,
)
from jo_watchdog.core.report import forward_origin_from_raw

INTAKE = "https://t.me/c/1234567890/77/{}"

FIXTURES = json.loads(
    (Path(__file__).parent / "fixtures" / "telegram_updates.json").read_text(encoding="utf-8")
)["updates"]


def message(name: str) -> dict:
    update = FIXTURES[name]
    return update.get("message") or update["edited_message"]


class ParseOriginTests(unittest.TestCase):
    def test_plain_text_and_caption_are_not_forwards(self):
        self.assertIsNone(parse_forward_origin(message("text")))
        self.assertIsNone(parse_forward_origin(message("caption")))

    def test_all_four_origin_types_parse(self):
        for name, kind in [
            ("forward_user", "user"),
            ("forward_hidden_user", "hidden_user"),
            ("forward_chat", "chat"),
            ("forward_channel_public", "channel"),
        ]:
            with self.subTest(name):
                self.assertEqual(parse_forward_origin(message(name)).type, kind)

    def test_unknown_origin_type_is_not_guessed(self):
        self.assertIsNone(parse_forward_origin({"forward_origin": {"type": "story", "date": 0}}))


class OriginallyFromTests(unittest.TestCase):
    def from_(self, name, **kw):
        return originally_from(parse_forward_origin(message(name)), **kw)

    def test_user_shows_name_and_handle(self):
        self.assertEqual(self.from_("forward_user"), "Ben Cruz (@bencruz)")

    def test_user_shows_resolved_contact_in_brackets(self):
        self.assertEqual(
            self.from_("forward_user", contact_name="Benjamin Cruz"),
            "Ben Cruz (@bencruz) [Benjamin Cruz]",
        )

    def test_user_without_username(self):
        self.assertEqual(self.from_("forward_user_no_username"), "Che")

    def test_hidden_user_shows_display_name(self):
        self.assertEqual(self.from_("forward_hidden_user"), "Dina Santos")

    def test_hidden_user_without_name_is_never_blank(self):
        self.assertEqual(self.from_("forward_hidden_user_no_name"), "(hidden user, no name given)")

    def test_chat_shows_title_and_signature(self):
        self.assertEqual(self.from_("forward_chat"), "Client Group — Admin Eli")
        self.assertEqual(self.from_("forward_chat_no_signature"), "Client Group")

    def test_channel_shows_title_and_signature(self):
        self.assertEqual(self.from_("forward_channel_public"), "Brand Updates — Fe")
        self.assertEqual(self.from_("forward_channel_private"), "Brand Internal")

    def test_channel_missing_title_is_never_blank(self):
        self.assertEqual(self.from_("forward_channel_missing_ids"), "(untitled)")

    def test_non_forward_has_no_line(self):
        self.assertIsNone(self.from_("text"))


class LinkTests(unittest.TestCase):
    """Original-message links come from #23's ``forward_origin_from_raw``."""

    def link(self, name):
        summary = forward_origin_from_raw(message(name))
        return summary.original_link if summary else None

    def test_public_channel_link(self):
        self.assertEqual(self.link("forward_channel_public"), "https://t.me/brandupdates/42")

    def test_private_channel_link(self):
        self.assertEqual(self.link("forward_channel_private"), "https://t.me/c/5555555555/43")

    def test_channel_without_message_identity_gets_no_link(self):
        self.assertIsNone(self.link("forward_channel_missing_ids"))

    def test_other_origins_never_get_a_link(self):
        for name in ("text", "forward_user", "forward_hidden_user", "forward_chat"):
            with self.subTest(name):
                self.assertIsNone(self.link(name))


class TimeTests(unittest.TestCase):
    def test_intake_time_is_forward_time_not_original_time(self):
        msg = message("forward_user")
        self.assertEqual(intake_time(msg), datetime.fromtimestamp(msg["date"], UTC))
        self.assertLess(parse_forward_origin(msg).date, intake_time(msg))


class SourceBlockTests(unittest.TestCase):
    def test_plain_message(self):
        self.assertEqual(
            source_block(message("text"), intake_url=INTAKE.format(501)),
            "Posted by: Ana Reyes (@anareyes)\n"
            "Posted at: Fri, Sep 18 2026 10:53\n"
            "Origin: none\n"
            "Telegram message: https://t.me/c/1234567890/77/501",
        )

    def test_user_forward_keeps_both_identities(self):
        self.assertEqual(
            source_block(message("forward_user"), intake_url=INTAKE.format(503)),
            "Posted by: Ana Reyes (@anareyes)\n"
            "Posted at: Fri, Sep 18 2026 10:53\n"
            "Origin: user\n"
            "Originally from: Ben Cruz (@bencruz)\n"
            "Original time: Wed, Sep 16 2026 10:53\n"
            "Telegram message: https://t.me/c/1234567890/77/503",
        )

    def test_channel_forward_adds_original_link(self):
        block = source_block(message("forward_channel_public"), intake_url=INTAKE.format(509))
        self.assertTrue(block.endswith("Original message: https://t.me/brandupdates/42"))

    def test_chat_forward_has_no_original_link(self):
        self.assertNotIn(
            "Original message", source_block(message("forward_chat"), intake_url=INTAKE.format(507))
        )


if __name__ == "__main__":
    unittest.main()


class AllOriginsSourceBlockTests(unittest.TestCase):
    def test_every_fixture_has_truthful_posted_by_and_originally_from(self):
        for name, update in FIXTURES.items():
            if "message" not in update:
                continue
            with self.subTest(name):
                msg = update["message"]
                block = source_block(msg, intake_url=INTAKE.format(msg["message_id"]))
                self.assertIn("Posted by: Ana Reyes (@anareyes)", block)
                origin = parse_forward_origin(msg)
                if origin is None:
                    self.assertNotIn("Originally from", block)
                else:
                    self.assertIn(f"Originally from: {originally_from(origin)}", block)
                    self.assertIn(f"Origin: {origin.type}", block)
