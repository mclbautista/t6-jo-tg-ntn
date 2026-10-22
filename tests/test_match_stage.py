"""Match-stage tests: eligibility, retry, shadow mode, storage records, and the
three real JO-to-Deliverable regression pairs (#26).

Fake Notion candidate source and fake/recorded Jev; no network access.
"""

import json
import unittest
from pathlib import Path

import httpx

from jo_watchdog.adapters.jev import JevMatcher, MatcherError
from jo_watchdog.app.config import Config
from jo_watchdog.app.matching import ResolvedProject, run_match_stage, with_deliverable
from jo_watchdog.core.masker import WITHHELD
from jo_watchdog.core.matching import Candidate, CandidateDecision, MatchResult
from jo_watchdog.core.payload import build_notion_payload
from jo_watchdog.core.plan import build_plan
from tests.fakes import FakeNotion

PAIRS = json.loads(
    (Path(__file__).parent / "fixtures" / "jev" / "regression_pairs.json").read_text(
        encoding="utf-8"
    )
)
PROJECT = ResolvedProject(page_id="proj-1", name="Moonglow")
CANDS = [
    Candidate(page_id="d-ftr", name="Moonglow FTR SCR"),
    Candidate(page_id="d-promo", name="Moonglow Promo"),
]


def config(**overrides) -> Config:
    return Config(
        intake_chat_id=-100123,
        intake_topic_id=5,
        job_orders_data_source_id="ds-jo",
        **overrides,
    )


class FakeDeliverables:
    def __init__(self, candidates=(), *, failures: int = 0):
        self.candidates = list(candidates)
        self.failures = failures
        self.calls: list[tuple[str, str]] = []

    async def query_project_deliverables(self, data_source_id, project_page_id):
        self.calls.append((data_source_id, project_page_id))
        if self.failures:
            self.failures -= 1
            raise RuntimeError("notion 503")
        return list(self.candidates)


class FakeMatcher:
    """Replays queued outcomes: a probability map, or an exception to raise."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls: list[tuple[str, str, list[Candidate]]] = []

    async def match(self, state, project_name, candidates):
        self.calls.append((state, project_name, candidates))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return MatchResult(
            decisions=[
                CandidateDecision(page_id=pid, p_match=m, p_maybe=y, p_no=n)
                for pid, (m, y, n) in outcome.items()
            ],
            requested_model="~typesafe/jev-latest",
            returned_model="typesafe/jev-1.14-20261014",
            input_tokens=200,
            output_tokens=30,
        )


class RecordingSleep:
    def __init__(self):
        self.waits: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)


LINKABLE = {"d-ftr": (0.95, 0.04, 0.01), "d-promo": (0.02, 0.08, 0.90)}


async def stage(matcher, deliverables=None, *, project=PROJECT, text="JO text", sleep=None, **cfg):
    return await run_match_stage(
        matcher=matcher,
        deliverables=deliverables if deliverables is not None else FakeDeliverables(CANDS),
        config=config(**cfg),
        source_text=text,
        project=project,
        sleep=sleep or RecordingSleep(),
    )


class EligibilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_unresolved_project_skips_query_and_call(self):
        source, jev = FakeDeliverables(CANDS), FakeMatcher()
        result = await stage(jev, source, project=None)
        self.assertEqual(result.outcome, "none")
        self.assertEqual(result.skip_reason, "project_unresolved")
        self.assertEqual(source.calls, [])
        self.assertEqual(jev.calls, [])
        self.assertEqual(result.findings, [])

    async def test_no_candidates_skips_call(self):
        jev = FakeMatcher()
        result = await stage(jev, FakeDeliverables([]))
        self.assertEqual((result.outcome, result.skip_reason), ("none", "no_candidates"))
        self.assertEqual(jev.calls, [])
        self.assertEqual(result.llm_calls, [])

    async def test_candidates_come_from_the_resolved_project(self):
        source = FakeDeliverables(CANDS)
        await stage(FakeMatcher(LINKABLE), source)
        self.assertEqual(source.calls, [("0a4ff084-3138-429c-8dbe-4d802aded74f", "proj-1")])

    async def test_jev_gets_masked_text_and_project_name(self):
        jev = FakeMatcher(LINKABLE)
        await stage(jev, text="Task: upload\nPW: hunter2-secret")
        state, project_name, candidates = jev.calls[0]
        self.assertNotIn("hunter2-secret", state)
        self.assertIn(WITHHELD, state)
        self.assertEqual(project_name, "Moonglow")
        self.assertEqual(candidates, CANDS)


class ShadowAndLinkTests(unittest.IsolatedAsyncioTestCase):
    async def test_shadow_mode_is_the_default_and_writes_no_relation(self):
        result = await stage(FakeMatcher(LINKABLE))
        self.assertFalse(result.link_enabled)
        self.assertEqual(result.linked_page_ids, [])
        self.assertEqual(result.outcome, "hinted")
        self.assertEqual(
            [f.line for f in result.findings],
            ["Possible Deliverable: Moonglow FTR SCR (match probability 0.95)"],
        )
        self.assertEqual(result.decision.outcomes[0].verdict, "link")

    async def test_enabled_linking_links_and_posts_nothing_for_the_link(self):
        result = await stage(FakeMatcher(LINKABLE), deliverable_link_enabled=True)
        self.assertEqual(result.linked_page_ids, ["d-ftr"])
        self.assertEqual(result.outcome, "linked")
        self.assertEqual(result.findings, [])

    async def test_thresholds_come_from_config(self):
        probs = {"d-ftr": (0.80, 0.15, 0.05), "d-promo": (0.0, 0.0, 1.0)}
        result = await stage(FakeMatcher(probs), deliverable_link_enabled=True, t_link=0.75)
        self.assertEqual(result.linked_page_ids, ["d-ftr"])


class FailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_inline_retry_then_success(self):
        jev = FakeMatcher(MatcherError("http_503", "down"), LINKABLE)
        sleep = RecordingSleep()
        result = await stage(jev, sleep=sleep)
        self.assertEqual(result.outcome, "hinted")
        self.assertEqual([c.outcome for c in result.llm_calls], ["http_503", "ok"])
        self.assertEqual(sleep.waits, [])

    async def test_two_failures_skip_with_one_finding(self):
        jev = FakeMatcher(MatcherError("timeout", "ReadTimeout"), MatcherError("http_502", "x"))
        result = await stage(jev)
        self.assertEqual(result.outcome, "skipped")
        self.assertEqual(result.linked_page_ids, [])
        self.assertEqual(
            [(f.code, f.line) for f in result.findings],
            [("matcher_skipped", "Deliverable match skipped: matcher unavailable")],
        )
        self.assertEqual(len(jev.calls), 2)
        self.assertEqual([c.outcome for c in result.llm_calls], ["timeout", "http_502"])

    async def test_invalid_result_is_a_failure_and_keeps_metadata(self):
        bad = {"d-ftr": (0.95, 0.04, 0.01)}  # missing d-promo
        result = await stage(FakeMatcher(bad, bad))
        self.assertEqual(result.outcome, "skipped")
        call = result.llm_calls[0]
        self.assertEqual(call.outcome, "invalid_result")
        self.assertEqual(call.returned_model, "typesafe/jev-1.14-20261014")
        self.assertEqual(call.input_tokens, 200)

    async def test_unexpected_exception_never_escapes(self):
        result = await stage(FakeMatcher(ValueError("bug"), KeyError("bug")))
        self.assertEqual(result.outcome, "skipped")

    async def test_429_waits_for_retry_after_before_the_retry(self):
        sleep = RecordingSleep()
        jev = FakeMatcher(MatcherError("http_429", "slow down", retry_after=7.0), LINKABLE)
        result = await stage(jev, sleep=sleep)
        self.assertEqual(sleep.waits, [7.0])
        self.assertEqual(result.outcome, "hinted")

    async def test_no_wait_after_the_last_attempt(self):
        sleep = RecordingSleep()
        err = MatcherError("http_429", "slow down", retry_after=7.0)
        await stage(FakeMatcher(err, err), sleep=sleep)
        self.assertEqual(sleep.waits, [7.0])

    async def test_candidate_query_failure_retries_once_then_skips(self):
        jev = FakeMatcher(LINKABLE)
        ok = await stage(jev, FakeDeliverables(CANDS, failures=1))
        self.assertEqual(ok.outcome, "hinted")
        result = await stage(FakeMatcher(), FakeDeliverables(CANDS, failures=2))
        self.assertEqual(
            (result.outcome, result.skip_reason), ("skipped", "candidate_query_failed")
        )
        self.assertEqual([f.code for f in result.findings], ["matcher_skipped"])

    async def test_failure_error_text_is_masked(self):
        result = await stage(FakeMatcher(RuntimeError("PW: leaked-secret"), RuntimeError("x")))
        self.assertNotIn("leaked-secret", result.llm_calls[0].error)


class StorageTests(unittest.IsolatedAsyncioTestCase):
    async def test_llm_call_record_has_trace_metadata(self):
        result = await stage(FakeMatcher(LINKABLE))
        (call,) = result.llm_calls
        self.assertEqual((call.provider, call.purpose), ("jev", "deliverable_match"))
        self.assertEqual(call.requested_model, "~typesafe/jev-latest")
        self.assertEqual(call.returned_model, "typesafe/jev-1.14-20261014")
        self.assertEqual(call.prompt_tag, "match-v1")
        self.assertEqual((call.input_tokens, call.output_tokens), (200, 30))
        self.assertTrue(call.masker_version)
        detail = json.loads(call.detail_json)
        self.assertEqual(
            detail["candidates"][0],
            {
                "page_id": "d-ftr",
                "name": "Moonglow FTR SCR",
                "p_match": 0.95,
                "p_maybe": 0.04,
                "p_no": 0.01,
            },
        )

    async def test_stage_result_json_has_snapshot_outcome_and_link_flag(self):
        result = await stage(FakeMatcher(LINKABLE))
        stored = json.loads(result.stage_result_json())
        self.assertEqual(stored["outcome"], "hinted")
        self.assertFalse(stored["link_enabled"])
        self.assertEqual([c["page_id"] for c in stored["candidates"]], ["d-ftr", "d-promo"])
        self.assertEqual(stored["decision"]["outcomes"][0]["verdict"], "link")
        self.assertNotIn("llm_calls", stored)


class PipelineShapeTests(unittest.IsolatedAsyncioTestCase):
    """Matching feeds the one Notion write; it never blocks or adds a relation in shadow."""

    async def write(self, result) -> dict:
        plan = build_plan(name="MG - TOC - JO #41", telegram_key="-100123:9", raw_text="JO text")
        payload = with_deliverable(build_notion_payload(plan, data_source_id="ds-jo"), result)
        notion = FakeNotion()
        await notion.create_page(payload)
        return notion.created[0]

    async def test_shadow_mode_never_writes_a_relation(self):
        written = await self.write(await stage(FakeMatcher(LINKABLE)))
        self.assertNotIn("Deliverable", written["properties"])

    async def test_matcher_failure_still_writes_without_backoff(self):
        sleep = RecordingSleep()
        result = await stage(
            FakeMatcher(MatcherError("http_500", "x"), MatcherError("timeout", "y")), sleep=sleep
        )
        written = await self.write(result)
        self.assertNotIn("Deliverable", written["properties"])
        self.assertEqual(sleep.waits, [])

    async def test_enabled_link_writes_the_relation(self):
        written = await self.write(
            await stage(FakeMatcher(LINKABLE), deliverable_link_enabled=True)
        )
        self.assertEqual(written["properties"]["Deliverable"], {"relation": [{"id": "d-ftr"}]})


class RegressionPairTests(unittest.IsolatedAsyncioTestCase):
    """The three known real JO-to-Deliverable pairs, through the real adapter."""

    async def run_pair(self, pair, *, link_enabled):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["body"] = json.loads(request.content)
            return httpx.Response(pair["response"]["status"], json=pair["response"]["body"])

        jev = JevMatcher(api_key="k", transport=httpx.MockTransport(handler))
        try:
            result = await stage(
                jev,
                FakeDeliverables([Candidate(**c) for c in pair["candidates"]]),
                project=ResolvedProject(**pair["project"]),
                text=pair["source_text"],
                deliverable_link_enabled=link_enabled,
            )
        finally:
            await jev.close()
        return result, seen["body"]

    async def test_pairs(self):
        self.assertEqual(len(PAIRS), 3)
        for pair in PAIRS:
            with self.subTest(pair=pair["name"]):
                result, request = await self.run_pair(pair, link_enabled=True)
                self.assertEqual(request, pair["expected_request"])
                verdicts = {o.page_id: o.verdict for o in result.decision.outcomes}
                self.assertEqual(verdicts, pair["expected_verdicts"])
                expected_links = [p for p, v in pair["expected_verdicts"].items() if v == "link"]
                self.assertEqual(result.linked_page_ids, expected_links)

                shadow, _ = await self.run_pair(pair, link_enabled=False)
                self.assertEqual(shadow.linked_page_ids, [])


if __name__ == "__main__":
    unittest.main()
