"""👍 on a perfect capture, 🤔 on a flagged one (#91), on the original message only."""

import unittest

from jo_watchdog.ledger.states import State
from tests import test_forwards_edits as fwd
from tests.fakes import FakeTelegram, update
from tests.test_outbox import KEY, Harness, channel_forward

CLEAN = "MG - TOC - JO #41\nTask: edit"


def config(**kw):
    return dict(captured_reaction="👍", flagged_reaction="🤔", **kw)


class FlagReactionTests(unittest.IsolatedAsyncioTestCase):
    async def run_one(self, u, **kw):
        h = Harness(self, [u], **config(), **kw)
        await h.worker.poll_once()
        await h.worker.drain_once()
        return h

    async def test_perfect_capture_gets_the_captured_reaction(self):
        h = await self.run_one(update(1, message_id=42, text=CLEAN))
        self.assertEqual(h.telegram.reactions, [(-100123, 42, "👍")])
        self.assertEqual(h.ledger.get_message(KEY)["reaction_state"], "set")

    async def test_any_finding_gets_the_flagged_reaction_and_still_writes(self):
        h = await self.run_one(channel_forward())
        self.assertEqual(h.telegram.reactions, [(-100123, 42, "🤔")])
        self.assertEqual(len(h.notion.created), 1)  # findings annotate, never gate
        self.assertEqual(h.ledger.get_message(KEY)["state"], State.DONE.value)

    async def test_failed_reaction_retries_then_counts_for_each_emoji(self):
        for u, name in (
            (update(1, message_id=42, text=CLEAN), "captured"),
            (channel_forward(), "flagged"),
        ):
            with self.subTest(name):
                h = Harness(self, telegram=FakeTelegram([u], fail_reaction=True), **config())
                await h.worker.poll_once()
                await h.worker.drain_once()
                msg = h.ledger.get_message(KEY)
                self.assertEqual((msg["state"], msg["reaction_state"]), ("done", "failed"))
                self.assertEqual(h.telegram.reactions, [])  # never downgraded to 👍

    async def test_resumed_row_gets_the_emoji_for_its_stored_findings(self):
        h = Harness(self, [channel_forward()], **config())
        await h.worker.poll_once()
        real = h.worker._after_written

        async def crash(_msg):
            raise KeyboardInterrupt

        h.worker._after_written = crash
        with self.assertRaises(KeyboardInterrupt):
            await h.worker.process(KEY)
        self.assertEqual(h.ledger.get_message(KEY)["state"], State.WRITTEN.value)
        h.worker._after_written = real
        h.reopen()
        await h.worker.drain_once()
        self.assertEqual(h.telegram.reactions, [(-100123, 42, "🤔")])

    async def test_repost_is_flagged_and_the_original_is_untouched(self):
        h = fwd.Harness(self, caches=True)
        h.worker.config = fwd.make_config(captured_reaction="👍")
        text = "MFG - TOC - JO #033\nTask: tidy the folders"
        await h.feed(fwd.message_update(1, 10, text))
        await h.feed(fwd.message_update(2, 11, text, date=fwd.POSTED_AT_UNIX + 86400))
        self.assertEqual(
            h.telegram.reactions, [(fwd.CHAT, 10, "👍"), (fwd.CHAT, 11, "🤔")]
        )


if __name__ == "__main__":
    unittest.main()
