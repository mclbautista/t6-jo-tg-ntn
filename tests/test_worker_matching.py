"""The worker runs the Jev match stage between planning and the one Notion write (#26)."""

import json
import unittest

from jo_watchdog.adapters.jev import MatcherError
from jo_watchdog.app.worker import Worker
from tests.fakes import FakeClassifier, ok_result
from tests.test_match_stage import CANDS, LINKABLE, FakeDeliverables, FakeMatcher
from tests.test_worker_planning import KEY, Harness


class MatchHarness(Harness):
    def __init__(self, test, matcher, deliverables=None, *, link_enabled=False, **kwargs):
        super().__init__(test, **kwargs)
        fields = {k: v for k, v in kwargs.items() if k != "text"}
        self.config = self.config.model_copy(update={"deliverable_link_enabled": link_enabled})
        self.matcher = matcher
        self.deliverables = deliverables if deliverables is not None else FakeDeliverables(CANDS)
        self.worker = Worker(
            ledger=self.ledger,
            telegram=self.telegram,
            notion=self.notion,
            classifier=FakeClassifier(ok_result(**fields)),
            config=self.config,
            caches=self.caches,
            matcher=matcher,
            deliverables=self.deliverables,
        )

    def stage(self) -> dict:
        return self.ledger.stage_result(KEY, "match")


class WorkerMatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_shadow_mode_runs_jev_keeps_the_decision_and_writes_no_relation(self):
        h = MatchHarness(self, FakeMatcher(LINKABLE))
        await h.run()
        self.assertNotIn("Deliverable", h.notion.created[0]["properties"])
        self.assertEqual(len(h.matcher.calls), 1)
        self.assertEqual(h.matcher.calls[0][1], "Manufacturing")
        stage = h.stage()
        self.assertEqual(stage["outcome"], "hinted")
        self.assertFalse(stage["link_enabled"])
        self.assertEqual([c["page_id"] for c in stage["candidates"]], ["d-ftr", "d-promo"])
        self.assertIn("deliverable_hint", h.codes())
        self.assertEqual(h.plan()["match"]["outcome"], "hinted")
        calls = h.ledger._conn.execute(
            "SELECT provider, purpose, prompt_tag, returned_model FROM llm_calls"
            " WHERE provider = 'jev'"
        ).fetchall()
        self.assertEqual(
            [tuple(c) for c in calls],
            [("jev", "deliverable_match", "match-v1", "typesafe/jev-1.14-20261014")],
        )

    async def test_hint_report_shows_probability_and_tags_no_one(self):
        h = MatchHarness(self, FakeMatcher(LINKABLE))
        await h.run()
        text = " ".join(t for _, _, t in h.telegram.sent)
        self.assertIn("Possible Deliverable: Moonglow FTR SCR (match probability 0.95)", text)

    async def test_enabled_linking_writes_the_relation(self):
        h = MatchHarness(self, FakeMatcher(LINKABLE), link_enabled=True)
        await h.run()
        self.assertEqual(
            h.notion.created[0]["properties"]["Deliverable"], {"relation": [{"id": "d-ftr"}]}
        )
        self.assertEqual(h.stage()["outcome"], "linked")

    async def test_matcher_failure_still_writes_and_queues_one_line(self):
        h = MatchHarness(
            self, FakeMatcher(MatcherError("timeout", "a"), MatcherError("timeout", "b"))
        )
        await h.run()
        self.assertEqual(len(h.notion.created), 1)
        self.assertNotIn("Deliverable", h.notion.created[0]["properties"])
        self.assertEqual(h.stage()["outcome"], "skipped")
        self.assertEqual(h.codes().count("matcher_skipped"), 1)
        self.assertEqual(h.ledger.get_message(KEY)["state"], "done")

    async def test_unresolved_project_skips_the_call(self):
        h = MatchHarness(self, FakeMatcher(), text="ZZZ - TOC - JO #74\nTask: cut teaser")
        await h.run()
        self.assertEqual(h.matcher.calls, [])
        self.assertEqual(h.stage()["outcome"], "none")
        self.assertEqual(len(h.notion.created), 1)

    async def test_planned_plan_replays_without_a_second_call(self):
        h = MatchHarness(self, FakeMatcher(LINKABLE), link_enabled=True)
        h.config.dry_run = True
        await h.run()
        self.assertEqual(len(h.matcher.calls), 1)
        h.config.dry_run = False
        await h.worker.drain_once()
        self.assertEqual(len(h.matcher.calls), 1)
        self.assertEqual(
            json.loads(h.ledger.get_message(KEY)["plan_json"])["properties"]["Deliverable"],
            {"relation": [{"id": "d-ftr"}]},
        )
        self.assertEqual(len(h.notion.created), 1)

    async def test_no_matcher_wired_leaves_planning_unchanged(self):
        h = Harness(self)
        await h.run()
        self.assertIsNone(h.ledger.stage_result(KEY, "match"))
        self.assertEqual(len(h.notion.created), 1)
