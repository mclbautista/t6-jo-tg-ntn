"""Offline tests for the typed-model smoke checks (#61). No network, no live model calls."""

import copy
import json
import unittest
from pathlib import Path

import httpx

from jo_watchdog.app.model_smoke import (
    MASK_NEGATIVE,
    MASK_POSITIVE,
    REGRESSION_PAIRS,
    SECRET,
    PairError,
    build_regression_request,
    check_deepseek,
    check_jev,
    load_pairs,
)
from jo_watchdog.app.smoke_gate import FAIL, MANUAL, PASS
from jo_watchdog.core.masker import WITHHELD

FIXTURES = Path(__file__).parent / "fixtures"
PAIRS_PATH = FIXTURES / "jev" / "regression_pairs.json"
PAIRS = json.loads(PAIRS_PATH.read_text(encoding="utf-8"))
DEEPSEEK = json.loads((FIXTURES / "deepseek_recorded.json").read_text(encoding="utf-8"))

JEV = dict(
    url="https://openrouter.test/decisions",
    model="~typesafe/jev-latest",
    timeout=5,
    t_link=0.9,
    t_hint=0.5,
)
HEADERS = {"X-RateLimit-Limit": "20", "X-RateLimit-Remaining": "19"}


def jev_body(probabilities: list[tuple[float, float, float]], skip=None) -> dict:
    answers = {}
    for i, (match, maybe, no) in enumerate(probabilities):
        if i == skip:
            continue
        choice = max(("match", match), ("maybe", maybe), ("no", no), key=lambda kv: kv[1])[0]
        answers[f"c{i}"] = {
            "type": "choice",
            "choice": choice,
            "probabilities": {"match": match, "maybe": maybe, "no": no},
        }
    return {
        "id": "gen-dec-test",
        "model": "typesafe/jev-1.14-20261014",
        "answers": answers,
        "usage": {"input_tokens": 600, "output_tokens": 90},
    }


# The fixtures' expected verdicts: hint, link, link.
GOOD = [(0.41, 0.52, 0.07), (0.94, 0.05, 0.01), (0.92, 0.07, 0.01)]


def serve(status: int, body, headers=None, seen: list | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(json.loads(request.content))
        return httpx.Response(status, json=body, headers=headers)

    return httpx.MockTransport(handler)


async def jev(inner, path=PAIRS_PATH):
    return await check_jev("test-key", path, inner=inner, **JEV)


class JevRequestTests(unittest.IsolatedAsyncioTestCase):
    def test_all_three_fixed_pairs_are_loaded_in_order(self):
        pairs = load_pairs(PAIRS_PATH)
        self.assertEqual([p["name"] for p in pairs], list(REGRESSION_PAIRS))

    def test_one_request_represents_every_pair_in_state_and_questions(self):
        body, candidates = build_regression_request(load_pairs(PAIRS_PATH), JEV["model"])
        self.assertEqual(body["model"], "~typesafe/jev-latest")
        self.assertEqual(set(body["state"]["job_orders"]), {"c0", "c1", "c2"})
        self.assertEqual(set(body["questions"]), {"c0", "c1", "c2"})
        for i, pair in enumerate(PAIRS):
            order = body["state"]["job_orders"][f"c{i}"]
            deliverable = pair["candidates"][0]["name"]
            self.assertEqual(order["project"], pair["project"]["name"])
            self.assertIn(deliverable, body["questions"][f"c{i}"]["instructions"])
        self.assertEqual([c.name for c in candidates], [p["candidates"][0]["name"] for p in PAIRS])

    async def test_exactly_one_request_is_sent_and_reports_all_facts(self):
        seen: list = []
        check = await jev(serve(200, jev_body(GOOD), HEADERS, seen))
        self.assertEqual(check.result.status, PASS, check.result.detail)
        self.assertEqual(len(seen), 1)
        self.assertEqual(check.requests_sent, 1)
        text = "\n".join(check.lines)
        self.assertIn("typesafe/jev-1.14-20261014", text)
        for pair in PAIRS:
            self.assertIn(f"{pair['name']} vs {pair['candidates'][0]['name']}", text)
        self.assertIn("match=0.41 maybe=0.52 no=0.07", text)
        self.assertIn("X-RateLimit-*".lower(), text.lower())
        self.assertIn('"x-ratelimit-remaining": "19"', text)
        self.assertIn("Retry-After: absent", text)

    async def test_200_is_never_reported_as_an_observed_429(self):
        check = await jev(serve(200, jev_body(GOOD), HEADERS))
        self.assertIn("429 observed: no", "\n".join(check.lines))

    async def test_a_real_429_reports_retry_after_and_fails(self):
        headers = {"Retry-After": "7", "X-RateLimit-Remaining": "0"}
        check = await jev(serve(429, {"error": {"code": 429, "message": "slow down"}}, headers))
        self.assertEqual(check.result.status, FAIL)
        text = "\n".join(check.lines)
        self.assertIn("429 observed: yes", text)
        self.assertIn("Retry-After: 7", text)
        self.assertIn("retry after 7s", check.result.detail)

    async def test_missing_pair_answer_fails_through_production_validation(self):
        check = await jev(serve(200, jev_body(GOOD, skip=2), HEADERS))
        self.assertEqual(check.result.status, FAIL)
        self.assertIn("missing", check.result.detail)

    async def test_extra_answer_field_is_rejected(self):
        body = jev_body(GOOD)
        body["answers"]["c0"]["reason"] = "because"
        check = await jev(serve(200, body))
        self.assertEqual(check.result.status, FAIL)

    async def test_probabilities_that_do_not_sum_to_one_fail(self):
        check = await jev(serve(200, jev_body([(0.9, 0.5, 0.5)] + GOOD[1:])))
        self.assertEqual(check.result.status, FAIL)

    async def test_missing_snapshot_fails(self):
        body = jev_body(GOOD)
        body["model"] = ""
        check = await jev(serve(200, body))
        self.assertEqual(check.result.status, FAIL)

    async def test_unexpected_verdict_needs_owner_review_not_a_pass(self):
        check = await jev(serve(200, jev_body([(0.1, 0.1, 0.8)] + GOOD[1:])))
        self.assertEqual(check.result.status, MANUAL)
        self.assertIn("moonglow-jo-32 is none, expected hint", check.result.detail)

    async def test_linking_is_never_enabled_by_the_check(self):
        check = await jev(serve(200, jev_body(GOOD)))
        self.assertNotIn("linked", "\n".join(check.lines).lower())


class JevFixtureTests(unittest.IsolatedAsyncioTestCase):
    def write(self, tmp, pairs) -> Path:
        path = Path(tmp) / "pairs.json"
        path.write_text(json.dumps(pairs), encoding="utf-8")
        return path

    async def test_a_missing_fixture_fails_and_sends_nothing(self):
        import tempfile

        for dropped in range(3):
            with self.subTest(dropped=PAIRS[dropped]["name"]), tempfile.TemporaryDirectory() as tmp:
                pairs = [p for i, p in enumerate(PAIRS) if i != dropped]
                seen: list = []
                check = await jev(serve(200, jev_body(GOOD), None, seen), self.write(tmp, pairs))
                self.assertEqual(check.result.status, FAIL)
                self.assertIn(PAIRS[dropped]["name"], check.result.detail)
                self.assertEqual(seen, [])
                self.assertEqual(check.requests_sent, 0)

    async def test_a_pair_with_two_deliverables_is_rejected(self):
        import tempfile

        pairs = copy.deepcopy(PAIRS)
        pairs[0]["candidates"].append(pairs[1]["candidates"][0])
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(PairError):
                load_pairs(self.write(tmp, pairs))

    async def test_an_unreadable_fixture_fails(self):
        check = await jev(serve(200, jev_body(GOOD)), FIXTURES / "jev" / "absent.json")
        self.assertEqual(check.result.status, FAIL)
        self.assertEqual(check.requests_sent, 0)


def deepseek_transport(case: str, seen: list | None = None):
    recorded = DEEPSEEK[case]

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(json.loads(request.content))
        return httpx.Response(recorded["status"], json=recorded["body"])

    return httpx.MockTransport(handler)


async def deepseek(inner):
    return await check_deepseek(
        "sk-test", base_url="https://api.deepseek.test", model="deepseek-chat", timeout=5,
        inner=inner,
    )  # fmt: skip


class DeepSeekSmokeTests(unittest.IsolatedAsyncioTestCase):
    async def test_mask_positive_and_no_mask_cases_both_run(self):
        seen: list = []
        check = await deepseek(deepseek_transport("jo", seen))
        self.assertEqual(check.result.status, PASS, check.result.detail)
        self.assertEqual(len(seen), 2)
        positive, negative = (body["messages"][-1]["content"] for body in seen)
        self.assertNotIn(SECRET, json.dumps(seen))
        self.assertIn(WITHHELD, positive)
        self.assertEqual(negative, MASK_NEGATIVE)
        self.assertIn("pw:", MASK_POSITIVE)

    async def test_reports_snapshot_and_token_metadata(self):
        check = await deepseek(deepseek_transport("jo"))
        recorded = DEEPSEEK["jo"]["body"]
        text = "\n".join(check.lines)
        self.assertIn(recorded["model"], text)
        self.assertIn(
            f"{recorded['usage']['prompt_tokens']}/{recorded['usage']['completion_tokens']}", text
        )

    async def test_invalid_structured_output_fails(self):
        for case in ("malformed_json", "schema_mismatch", "empty_content", "provider_failure"):
            with self.subTest(case=case):
                check = await deepseek(deepseek_transport(case))
                self.assertEqual(check.result.status, FAIL)

    async def test_non_jo_verdict_needs_review_not_a_pass(self):
        check = await deepseek(deepseek_transport("not_jo"))
        self.assertEqual(check.result.status, MANUAL)
