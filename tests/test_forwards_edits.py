"""Forwards, edits, reposts and JO-number reuse through the whole pipeline (#24).

Real SQLite, fake Telegram/Notion/DeepSeek adapters. Every new Telegram
message gets its own row; duplicates and edits are annotated, never merged,
suppressed or written back.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from jo_watchdog.adapters.protocols import IncomingUpdate
from jo_watchdog.app.caches import CacheManager
from jo_watchdog.app.config import Config
from jo_watchdog.app.worker import Worker
from jo_watchdog.core.duplicates import text_sha256
from jo_watchdog.ledger.db import Ledger
from jo_watchdog.ledger.migrations import MIGRATIONS
from jo_watchdog.ledger.states import State
from tests.fakes import POSTED_AT_UNIX, FakeClassifier, FakeNotion, FakeTelegram, ok_result

CHAT, TOPIC = -100123, 5
FIXTURES = json.loads(
    (Path(__file__).parent / "fixtures" / "telegram_updates.json").read_text(encoding="utf-8")
)["updates"]


def make_config(**overrides) -> Config:
    base = dict(
        intake_chat_id=CHAT,
        intake_topic_id=TOPIC,
        ops_chat_id=-100999,
        ops_topic_id=7,
        leizette_telegram_handle="leizette",
        captured_reaction="✅",
        job_orders_data_source_id="ds-1",
    )
    base.update(overrides)
    return Config(**base)


def message_update(update_id, message_id, text, *, date=POSTED_AT_UNIX, **extra) -> IncomingUpdate:
    message = {
        "message_id": message_id,
        "message_thread_id": TOPIC,
        "from": {"id": 111, "is_bot": False, "first_name": "Ana", "username": "anareyes"},
        "chat": {"id": CHAT, "type": "supergroup"},
        "date": date,
        "text": text,
        **extra,
    }
    return IncomingUpdate(
        update_id=update_id,
        chat_id=CHAT,
        message_id=message_id,
        message_thread_id=TOPIC,
        text=text,
        raw={"update_id": update_id, "message": message},
    )


def edit_update(update_id, message_id, text, *, chat_id=CHAT, topic_id=TOPIC) -> IncomingUpdate:
    message = {
        "message_id": message_id,
        "message_thread_id": topic_id,
        "chat": {"id": chat_id, "type": "supergroup"},
        "date": POSTED_AT_UNIX,
        "edit_date": POSTED_AT_UNIX + 600,
        "text": text,
    }
    return IncomingUpdate(
        update_id=update_id,
        chat_id=chat_id,
        message_id=message_id,
        message_thread_id=topic_id,
        text=text,
        raw={"update_id": update_id, "edited_message": message},
        edited=True,
        edit_date=POSTED_AT_UNIX + 600,
    )


def fixture_update(name: str, update_id: int, message_id: int) -> IncomingUpdate:
    """A recorded-contract fixture, moved into the intake chat and topic."""
    message = json.loads(json.dumps(FIXTURES[name]["message"]))
    message.update(message_id=message_id, message_thread_id=TOPIC)
    message["chat"] = {"id": CHAT, "type": "supergroup"}
    text = message.get("text") or message.get("caption") or ""
    return IncomingUpdate(
        update_id=update_id,
        chat_id=CHAT,
        message_id=message_id,
        message_thread_id=TOPIC,
        text=text,
        raw={"update_id": update_id, "message": message},
    )


class Harness:
    def __init__(self, test, *, classifier=None, caches=False):
        self.tmp = tempfile.TemporaryDirectory()
        test.addCleanup(self.tmp.cleanup)
        self.ledger = Ledger(Path(self.tmp.name) / "ledger.db")
        test.addCleanup(self.ledger.close)
        self.telegram = FakeTelegram()
        self.notion = FakeNotion()
        self.config = make_config()
        self.caches = None
        if caches:
            self.caches = CacheManager(ledger=self.ledger, notion=None, config=self.config)
            self.caches._apply(
                "people",
                [{"page_id": "c-ben", "name": "Benjamin Cruz", "telegram_user_id": "222"}],
                "2026-09-28T00:00:00Z",
            )
            self.caches._apply(
                "projects",
                [
                    {"page_id": "proj-mfg", "name": "Manufacturing", "code": "MFG"},
                    {"page_id": "proj-som", "name": "Somsom", "code": "SOMSOM"},
                ],
                "2026-09-28T00:00:00Z",
            )
            self.caches._apply("departments", ["TOC", "VID", "OE"], "2026-09-28T00:00:00Z")
        self.worker = Worker(
            ledger=self.ledger,
            telegram=self.telegram,
            notion=self.notion,
            classifier=classifier or FakeClassifier(ok_result(title="Teaser", task="Edit")),
            config=self.config,
            caches=self.caches,
        )

    async def feed(self, *updates):
        """Poll the given updates, drain the pipeline, deliver ops posts."""
        self.telegram.updates = list(updates)
        await self.worker.poll_once()
        await self.worker.drain_once()
        await self.worker.deliver_outbox()

    def row(self, message_id) -> dict:
        return self.ledger.get_message(f"{CHAT}:{message_id}")

    def payload(self, n) -> dict:
        return self.notion.created[n]

    def notes(self, n) -> str | None:
        notes = self.payload(n)["properties"].get("Notes")
        return "".join(i["text"]["content"] for i in notes["rich_text"]) if notes else None

    def body(self, n) -> str:
        return "\n".join(
            "".join(i["text"]["content"] for i in b["paragraph"]["rich_text"])
            for b in self.payload(n)["children"]
        )

    def posts(self) -> list[str]:
        return [text for _, _, text in self.telegram.sent]

    def edits(self, message_id) -> list[dict]:
        return self.ledger.edits_for(f"{CHAT}:{message_id}")

    def ignore_reasons(self) -> list[str]:
        return [r[0] for r in self.ledger._conn.execute("SELECT reason FROM ignore_log")]


# -- migration 6 -----------------------------------------------------------


class Migration6Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "ledger.db"

    def columns(self, ledger, table):
        return {r[1] for r in ledger._conn.execute(f"PRAGMA table_info({table})")}

    def test_columns_indexes_and_edits_table(self):
        ledger = Ledger(self.path)
        self.addCleanup(ledger.close)
        self.assertLessEqual(
            {"jo_key", "text_sha256", "forward_origin_type"}, self.columns(ledger, "messages")
        )
        self.assertEqual(
            self.columns(ledger, "edits"),
            {"id", "telegram_key", "update_id", "edited_at", "outcome", "created_at"},
        )
        indexes = {r[1] for r in ledger._conn.execute("PRAGMA index_list(messages)")}
        self.assertLessEqual({"messages_jo_key", "messages_text_sha256"}, indexes)
        versions = [r[0] for r in ledger._conn.execute("SELECT version FROM schema_version")]
        self.assertIn(6, versions)

    def test_upgrade_backfills_hash_and_origin_but_not_jo_key(self):
        before = {k: v for k, v in MIGRATIONS.items() if k < 6}
        with mock.patch.dict(MIGRATIONS, before, clear=True):
            old = Ledger(self.path)
            raw = {"message": {"date": POSTED_AT_UNIX, "forward_origin": {"type": "chat"}}}
            old._conn.execute(
                "INSERT INTO messages (chat_id, message_id, telegram_key, update_id, raw_source,"
                " text, first_line, masked_text, masker_version, masked_count, state,"
                " created_at, updated_at) VALUES (?, 1, ?, 10, ?, 'a  b', 'a  b', 'a  b', 'v1',"
                " 0, 'received', 't', 't')",
                (CHAT, f"{CHAT}:1", json.dumps(raw)),
            )
            old.close()
        ledger = Ledger(self.path)
        self.addCleanup(ledger.close)
        row = ledger.get_message(f"{CHAT}:1")
        self.assertEqual(row["text_sha256"], text_sha256("a b"))
        self.assertEqual(row["forward_origin_type"], "chat")
        self.assertIsNone(row["jo_key"])


# -- migration 9 (#93) ---------------------------------------------------------


class Migration9Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "ledger.db"

    def test_upgrade_drops_the_department_from_stored_keys(self):
        before = {k: v for k, v in MIGRATIONS.items() if k < 9}
        with mock.patch.dict(MIGRATIONS, before, clear=True):
            old = Ledger(self.path)
            for message_id, key in ((1, "proj-som|TOC|95"), (2, None), (3, "proj-mfg|VID|0")):
                old._conn.execute(
                    "INSERT INTO messages (chat_id, message_id, telegram_key, update_id,"
                    " raw_source, text, first_line, masked_text, masker_version, masked_count,"
                    " state, jo_key, notion_page_id, created_at, updated_at) VALUES (?, ?, ?, ?,"
                    " '{}', 'a', 'a', 'a', 'v1', 0, 'written', ?, 'page', 't', 't')",
                    (CHAT, message_id, f"{CHAT}:{message_id}", message_id, key),
                )
            old.close()
        ledger = Ledger(self.path)
        self.addCleanup(ledger.close)
        self.assertEqual(ledger.get_message(f"{CHAT}:1")["jo_key"], "proj-som|95")
        self.assertIsNone(ledger.get_message(f"{CHAT}:2")["jo_key"])
        self.assertEqual(ledger.get_message(f"{CHAT}:3")["jo_key"], "proj-mfg|0")
        # An earlier capture under the old key now matches a new key from another department.
        found = ledger.earlier_captures(f"{CHAT}:9", text_sha="x", jo_key="proj-som|95")
        self.assertEqual([r["telegram_key"] for r in found], [f"{CHAT}:1"])


# -- one row per message, reposts, reuse ---------------------------------------


class RepostAndReuseTests(unittest.IsolatedAsyncioTestCase):
    async def test_exact_repost_gets_its_own_row_note_and_finding(self):
        h = Harness(self, caches=True)
        text = "MFG - TOC - JO #033\nTask: tidy  the folders"
        await h.feed(message_update(1, 10, text))
        await h.feed(message_update(2, 11, text.replace("  ", " "), date=POSTED_AT_UNIX + 86400))

        self.assertEqual(len(h.notion.created), 2)
        self.assertIsNone(h.notes(0))
        self.assertEqual(
            h.notes(1),
            "— added by the watchdog —\nEarlier row: https://www.notion.so/page1 (exact re-post)",
        )
        self.assertEqual(len(h.posts()), 1)
        self.assertIn("Exact re-post of", h.posts()[0])
        self.assertIn("1 day ago", h.posts()[0])
        self.assertEqual(json.loads(h.row(11)["plan_json"])["duplicate"]["code"], "exact_repost")

    async def test_reused_number_with_new_text_is_flagged_as_reuse(self):
        h = Harness(self, caches=True)
        await h.feed(message_update(1, 10, "MFG - TOC - JO #074\nTask: cut teaser"))
        await h.feed(message_update(2, 11, "MFG - TOC - JO #74\nTask: color pass"))

        self.assertEqual(len(h.notion.created), 2)
        self.assertEqual(h.row(10)["jo_key"], "proj-mfg|74")
        self.assertEqual(h.row(11)["jo_key"], "proj-mfg|74")
        self.assertTrue(h.notes(1).endswith("(possible number reuse)"))
        self.assertEqual(len(h.posts()), 1)
        self.assertIn("#74 already used on", h.posts()[0])

    async def test_same_number_in_another_department_is_reuse(self):
        # #93: one JO counter per project, shared by every department.
        h = Harness(self, caches=True)
        await h.feed(message_update(1, 10, "SOMSOM - TOC - JO #95\nTask: cut teaser"))
        await h.feed(message_update(2, 11, "SOMSOM - OE - JO #95\nTask: online edit"))

        self.assertEqual(len(h.notion.created), 2)
        self.assertEqual(h.row(10)["jo_key"], "proj-som|95")
        self.assertEqual(h.row(11)["jo_key"], "proj-som|95")
        self.assertTrue(h.notes(1).endswith("(possible number reuse)"))
        self.assertEqual(json.loads(h.row(11)["plan_json"])["duplicate"]["code"], "number_reuse")
        self.assertIn("#95 already used on", h.posts()[0])

    async def test_same_number_in_another_project_is_not_reuse(self):
        h = Harness(self, caches=True)
        await h.feed(message_update(1, 10, "SOMSOM - TOC - JO #95\nTask: cut teaser"))
        await h.feed(message_update(2, 11, "MFG - TOC - JO #95\nTask: color pass"))

        self.assertEqual(len(h.notion.created), 2)
        self.assertEqual(h.row(11)["jo_key"], "proj-mfg|95")
        self.assertIsNone(h.notes(1))
        self.assertIsNone(json.loads(h.row(11)["plan_json"])["duplicate"])
        self.assertFalse(any("already used" in p for p in h.posts()))

    async def test_padded_reuse_number_reads_the_display_number(self):
        for second in ("JO 074", "JO #074"):
            with self.subTest(second=second):
                h = Harness(self, caches=True)
                await h.feed(message_update(1, 10, "MFG - TOC - JO #74\nTask: cut teaser"))
                await h.feed(message_update(2, 11, f"MFG - TOC - {second}\nTask: color pass"))
                self.assertEqual(h.row(11)["jo_key"], "proj-mfg|74")
                self.assertIn("#074 already used on", h.posts()[0])
                self.assertEqual(
                    json.loads(h.row(11)["plan_json"])["duplicate"]["jo_number"], "#074"
                )

    async def test_exact_repost_wins_over_number_reuse(self):
        h = Harness(self, caches=True)
        text = "MFG - TOC - JO #074\nTask: cut teaser"
        await h.feed(message_update(1, 10, "MFG - TOC - JO #74\nTask: older brief"))
        await h.feed(message_update(2, 11, text))
        await h.feed(message_update(3, 12, text))
        self.assertTrue(h.notes(2).endswith("https://www.notion.so/page2 (exact re-post)"))

    async def test_identical_text_without_a_complete_key_is_not_a_duplicate(self):
        # D1: no content-duplicate lookup without a Project page and a number.
        texts = {
            "no caches (nothing resolves)": (False, "MFG - TOC - JO #33\nTask: same"),
            "unknown project": (True, "ZZZ - TOC - JO #33\nTask: same"),
            "no JO number": (True, "MFG - TOC\nTask: same"),
            "unstructured": (True, "frameio cleanup\nTask: same"),
        }
        for name, (caches, text) in texts.items():
            with self.subTest(name):
                h = Harness(self, caches=caches)
                await h.feed(message_update(1, 10, text))
                await h.feed(message_update(2, 11, text))
                self.assertEqual(len(h.notion.created), 2)  # still one row per message
                self.assertIsNone(h.notes(1))
                self.assertIsNone(json.loads(h.row(11)["plan_json"])["duplicate"])
                self.assertFalse(any("re-post" in p for p in h.posts()))

    async def test_unknown_department_still_gets_a_key(self):
        # #93: the Department left the key, so an unknown one no longer skips detection.
        h = Harness(self, caches=True)
        text = "MFG - NOPE - JO #33\nTask: same"
        await h.feed(message_update(1, 10, text))
        await h.feed(message_update(2, 11, text))
        self.assertEqual(h.row(11)["jo_key"], "proj-mfg|33")
        self.assertTrue(h.notes(1).endswith("(exact re-post)"))

    async def test_incomplete_key_skips_reuse_detection(self):
        h = Harness(self, caches=True)
        # Unknown project code: no Project page, so no jo_key and no finding.
        await h.feed(message_update(1, 10, "ZZZ - TOC - JO #74\nTask: one"))
        await h.feed(message_update(2, 11, "ZZZ - TOC - JO #74\nTask: two"))
        self.assertIsNone(h.row(10)["jo_key"])
        self.assertIsNone(h.notes(1))
        self.assertFalse(any("already used" in p for p in h.posts()))

    async def test_no_caches_means_no_key(self):
        h = Harness(self)
        await h.feed(message_update(1, 10, "MFG - TOC - JO #74\nTask: one"))
        await h.feed(message_update(2, 11, "MFG - TOC - JO #74\nTask: two"))
        self.assertIsNone(h.row(11)["jo_key"])
        self.assertIsNone(h.notes(1))

    async def test_correction_is_a_fresh_message_and_a_fresh_row(self):
        h = Harness(self)
        await h.feed(message_update(1, 10, "JO 5\nTask: wrong"))
        await h.feed(message_update(2, 11, "JO 5\nTask: right"))
        keys = [
            p["properties"]["Telegram Key"]["rich_text"][0]["text"]["content"]
            for p in h.notion.created
        ]
        self.assertEqual(keys, [f"{CHAT}:10", f"{CHAT}:11"])


# -- edits ---------------------------------------------------------------------


class EditTests(unittest.IsolatedAsyncioTestCase):
    async def test_edit_after_capture_is_reported_and_row_untouched(self):
        h = Harness(self)
        await h.feed(message_update(1, 10, "JO 5\nTask: original"))
        plan_before = h.row(10)["plan_json"]
        await h.feed(edit_update(2, 10, "JO 5\nTask: changed"))

        self.assertEqual(len(h.notion.created), 1)
        self.assertEqual(h.row(10)["plan_json"], plan_before)
        self.assertEqual(h.row(10)["state"], State.DONE.value)
        self.assertEqual([e["outcome"] for e in h.edits(10)], ["reported"])
        self.assertEqual(h.edits(10)[0]["edited_at"], "2026-09-28T03:10:00Z")
        post = h.posts()[-1]
        self.assertTrue(post.startswith("✏️ Edited after capture, row not updated"))
        self.assertIn("https://www.notion.so/page1", post)
        self.assertEqual(h.ledger.last_committed_offset(), 2)

    async def test_edit_while_in_pipeline_is_reported_once_after_capture(self):
        h = Harness(self)
        h.telegram.updates = [
            message_update(1, 10, "JO 5\nTask: original"),
            edit_update(2, 10, "JO 5\nTask: changed"),
            edit_update(3, 10, "JO 5\nTask: changed again"),
        ]
        await h.worker.poll_once()
        self.assertEqual([e["outcome"] for e in h.edits(10)], ["pending_capture"] * 2)
        self.assertEqual(h.posts(), [])

        await h.worker.drain_once()
        await h.worker.deliver_outbox()
        self.assertIn("Task: original", h.body(0))
        self.assertNotIn("changed", h.body(0))
        edit_posts = [p for p in h.posts() if p.startswith("✏️")]
        self.assertEqual(len(edit_posts), 1)
        self.assertIn("https://notion.example/page-1", edit_posts[0])
        self.assertEqual([e["outcome"] for e in h.edits(10)], ["reported"] * 2)

    async def test_edit_to_ignored_message_is_recorded_only(self):
        h = Harness(self, classifier=FakeClassifier(ok_result("not_jo")))
        await h.feed(message_update(1, 10, "lunch?"))
        await h.feed(edit_update(2, 10, "JO 5\nTask: now a job"))
        self.assertEqual([e["outcome"] for e in h.edits(10)], ["ignored_message"])
        self.assertEqual(h.row(10)["state"], State.IGNORED.value)
        self.assertEqual(h.posts(), [])
        self.assertEqual(len(h.worker.classifier.calls), 1)  # not re-classified

    async def test_edit_to_unknown_message_goes_to_ignore_log(self):
        h = Harness(self)
        await h.feed(edit_update(1, 99, "JO from before the watchdog"))
        self.assertEqual(h.ignore_reasons(), ["edit_unknown_message"])
        self.assertEqual(h.edits(99), [])
        self.assertEqual(h.ledger.last_committed_offset(), 1)

    async def test_edit_outside_the_topic_is_filtered_like_messages(self):
        h = Harness(self)
        await h.feed(edit_update(1, 10, "elsewhere", topic_id=6))
        self.assertEqual(h.ignore_reasons(), ["out_of_scope_topic"])

    async def test_redelivered_edit_is_recorded_once(self):
        h = Harness(self)
        await h.feed(message_update(1, 10, "JO 5"))
        edit = edit_update(2, 10, "JO 5 (rev)")
        await h.feed(edit)
        await h.feed(edit)  # offset N+1 redelivers the last update
        self.assertEqual(len(h.edits(10)), 1)
        self.assertEqual(len([p for p in h.posts() if p.startswith("✏️")]), 1)


# -- provenance ----------------------------------------------------------------


class ProvenanceTests(unittest.IsolatedAsyncioTestCase):
    CASES = [
        ("text", "none", None),
        ("caption", "none", None),
        ("forward_user", "user", "Ben Cruz (@bencruz)"),
        ("forward_hidden_user", "hidden_user", "Dina Santos"),
        ("forward_chat", "chat", "Client Group — Admin Eli"),
        ("forward_channel_public", "channel", "Brand Updates — Fe"),
        ("forward_channel_private", "channel", "Brand Internal"),
        ("forward_channel_missing_ids", "channel", "(untitled)"),
    ]

    async def test_every_origin_through_the_pipeline(self):
        for i, (name, origin, original) in enumerate(self.CASES):
            with self.subTest(name):
                h = Harness(self)
                await h.feed(fixture_update(name, 1, 10 + i))
                row = h.row(10 + i)
                body = h.body(0)
                self.assertIn("Posted by: Ana Reyes (@anareyes)", body)
                self.assertIn(f"Origin: {origin}", body)
                self.assertIn(f"Telegram message: https://t.me/c/123/{TOPIC}/{10 + i}", body)
                self.assertEqual(row["forward_origin_type"], None if origin == "none" else origin)
                # Date math uses the intake (forward) time, never the original's.
                self.assertEqual(row["posted_at"], "2026-09-18T10:53:20+08:00")
                if original is None:
                    self.assertNotIn("Originally from", body)
                else:
                    self.assertIn(f"Originally from: {original}", body)
                    self.assertIn("Original time:", body)
                flagged = [p for p in h.posts() if "Forwarded from a" in p]
                self.assertEqual(len(flagged), 0 if origin in ("none", "user") else 1)

    async def test_original_link_only_for_channel_with_identity(self):
        expected = {
            "forward_channel_public": "https://t.me/brandupdates/42",
            "forward_channel_private": "https://t.me/c/5555555555/43",
        }
        for name in (
            "forward_hidden_user",
            "forward_chat",
            "forward_channel_missing_ids",
            *expected,
        ):
            with self.subTest(name):
                h = Harness(self)
                await h.feed(fixture_update(name, 1, 10))
                link = expected.get(name)
                if link:
                    self.assertIn(f"Original message: {link}", h.body(0))
                    self.assertIn(link, h.posts()[0])
                else:
                    self.assertNotIn("Original message", h.body(0))
                    self.assertNotIn("Original message", h.posts()[0])

    async def test_user_forward_shows_matched_contact_in_brackets(self):
        h = Harness(self, caches=True)
        await h.feed(fixture_update("forward_user", 1, 10))
        self.assertIn("Originally from: Ben Cruz (@bencruz) [Benjamin Cruz]", h.body(0))
        # The match never becomes a relation.
        self.assertNotIn("c-ben", json.dumps(h.payload(0)["properties"]))


if __name__ == "__main__":
    unittest.main()
