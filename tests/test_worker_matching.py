"""The worker runs the Jev match stage between planning and the one Notion write (#26)."""

import copy
import json
import unittest

import httpx

from jo_watchdog.adapters.jev import JevMatcher, MatcherError
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


def jev_body(**extra) -> dict:
    """A valid Decisions body for CANDS, with ``extra`` fields on the first answer."""
    answer = {
        "type": "choice",
        "choice": "match",
        "confidence": 0.9,
        "probabilities": {"match": 0.95, "maybe": 0.04, "no": 0.01},
    }
    other = copy.deepcopy(answer)
    other["probabilities"] = {"match": 0.02, "maybe": 0.08, "no": 0.90}
    return {
        "model": "typesafe/jev-1.14-20261014",
        "answers": {"c0": {**answer, **extra}, "c1": other},
        "usage": {"input_tokens": 210, "output_tokens": 31},
    }


class WorkerInvalidAnswerTests(unittest.IsolatedAsyncioTestCase):
    """Real adapter over a mock transport into the real ledger (#55)."""

    async def run_with(self, body, *, link_enabled=True):
        seen = []

        def handler(request):
            seen.append(request)
            return httpx.Response(200, json=body)

        matcher = JevMatcher("k", transport=httpx.MockTransport(handler))
        h = MatchHarness(self, matcher, link_enabled=link_enabled)
        await h.run()
        await matcher.close()
        return h, seen

    def llm_rows(self, h):
        return [
            dict(r)
            for r in h.ledger._conn.execute(
                "SELECT outcome, requested_model, returned_model, prompt_tag,"
                " input_tokens, output_tokens, detail_json FROM llm_calls"
                " WHERE provider = 'jev' ORDER BY id"
            )
        ]

    async def test_extra_reason_is_never_a_link_and_evidence_is_kept(self):
        h, seen = await self.run_with(jev_body(reason="clearly the FTR SCR"))
        self.assertEqual(len(seen), 2)  # one inline retry
        self.assertNotIn("Deliverable", h.notion.created[0]["properties"])
        self.assertEqual(h.stage()["outcome"], "skipped")
        self.assertEqual(h.codes().count("matcher_skipped"), 1)
        self.assertEqual(h.ledger.get_message(KEY)["state"], "done")
        rows = self.llm_rows(h)
        self.assertEqual([r["outcome"] for r in rows], ["invalid_body", "invalid_body"])
        for r in rows:
            self.assertEqual(r["returned_model"], "typesafe/jev-1.14-20261014")
            self.assertEqual(r["requested_model"], "~typesafe/jev-latest")
            self.assertEqual(r["prompt_tag"], "match-v1")
            self.assertEqual((r["input_tokens"], r["output_tokens"]), (210, 31))
            detail = json.loads(r["detail_json"])
            self.assertEqual(
                [(c["page_id"], c["name"]) for c in detail["candidates"]],
                [(c.page_id, c.name) for c in CANDS],
            )
            self.assertEqual(detail["candidates"][0]["p_match"], 0.95)
            self.assertEqual(detail["candidates"][1]["p_no"], 0.90)

    async def test_extra_name_echo_is_rejected(self):
        h, _ = await self.run_with(jev_body(name="Moonglow FTR SCR"))
        self.assertEqual(h.stage()["outcome"], "skipped")
        self.assertNotIn("Deliverable", h.notion.created[0]["properties"])

    async def test_partial_response_keeps_available_probabilities(self):
        body = jev_body()
        del body["answers"]["c1"]
        h, _ = await self.run_with(body)
        detail = json.loads(self.llm_rows(h)[0]["detail_json"])
        self.assertEqual(detail["candidates"][0]["p_match"], 0.95)
        self.assertNotIn("p_match", detail["candidates"][1])
        self.assertEqual(h.stage()["outcome"], "skipped")

    async def test_valid_body_still_links(self):
        h, _ = await self.run_with(jev_body())
        self.assertEqual(
            h.notion.created[0]["properties"]["Deliverable"], {"relation": [{"id": "d-ftr"}]}
        )
        self.assertEqual([r["outcome"] for r in self.llm_rows(h)], ["ok"])


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
