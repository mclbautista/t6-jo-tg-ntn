"""Recorded contract tests for the Jev adapter (OpenRouter Decisions API, #26).

Offline via ``httpx.MockTransport``: the fixtures pin the request format and
the response shapes the adapter accepts; nothing here touches the network.
"""

import copy
import json
import unittest
from pathlib import Path

import httpx

from jo_watchdog.adapters.jev import (
    JEV_MODEL,
    JEV_URL,
    JevMatcher,
    MatcherError,
    build_request,
    retry_after_seconds,
)
from jo_watchdog.core.matching import Candidate

FIXTURES = Path(__file__).parent / "fixtures" / "jev"
CONTRACT = json.loads((FIXTURES / "decisions_contract.json").read_text(encoding="utf-8"))
CANDIDATES = [Candidate(**c) for c in CONTRACT["candidates"]]


def matcher(handler) -> JevMatcher:
    return JevMatcher(api_key="test-key", transport=httpx.MockTransport(handler))


def responding(status: int, body, headers=None, seen: dict | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen["url"] = str(request.url)
            seen["headers"] = dict(request.headers)
            seen["body"] = json.loads(request.content)
        if isinstance(body, str):
            return httpx.Response(status, text=body, headers=headers)
        return httpx.Response(status, json=body, headers=headers)

    return handler


async def call(handler, candidates=CANDIDATES):
    m = matcher(handler)
    try:
        return await m.match(CONTRACT["source_text"], CONTRACT["project"]["name"], candidates)
    finally:
        await m.close()


def ok_body() -> dict:
    return copy.deepcopy(CONTRACT["response"]["body"])


class RequestFormatTests(unittest.IsolatedAsyncioTestCase):
    async def test_request_matches_pinned_fixture(self):
        seen: dict = {}
        await call(responding(200, ok_body(), seen=seen))
        self.assertEqual(seen["url"], JEV_URL)
        self.assertEqual(seen["url"], "https://openrouter.ai/api/alpha/decisions")
        self.assertEqual(seen["headers"]["authorization"], "Bearer test-key")
        self.assertEqual(seen["headers"]["content-type"], "application/json")
        self.assertEqual(seen["body"], CONTRACT["expected_request"])

    def test_request_has_no_chat_parameters_or_page_ids(self):
        body = build_request("text", "Moonglow", CANDIDATES)
        self.assertEqual(body["model"], JEV_MODEL)
        self.assertEqual(JEV_MODEL, "~typesafe/jev-latest")
        for forbidden in ("messages", "temperature", "response_format", "logprobs"):
            self.assertNotIn(forbidden, body)
        serialized = json.dumps(body)
        for c in CANDIDATES:
            self.assertNotIn(c.page_id, serialized)
        self.assertNotIn("reason", serialized.lower())


class TypedResponseTests(unittest.IsolatedAsyncioTestCase):
    async def test_maps_keys_to_page_ids_and_keeps_metadata(self):
        result = await call(responding(200, ok_body()))
        by_id = {d.page_id: d for d in result.decisions}
        self.assertEqual(set(by_id), {"page-ftr-scr", "page-mainpic-dcp"})
        self.assertEqual(
            (
                by_id["page-ftr-scr"].p_match,
                by_id["page-ftr-scr"].p_maybe,
                by_id["page-ftr-scr"].p_no,
            ),
            (0.93, 0.06, 0.01),
        )
        self.assertEqual(by_id["page-mainpic-dcp"].p_no, 0.97)
        self.assertEqual(result.requested_model, "~typesafe/jev-latest")
        self.assertEqual(result.returned_model, "typesafe/jev-1.14-20261014")
        self.assertEqual((result.input_tokens, result.output_tokens), (240, 40))

    async def test_answer_order_does_not_matter(self):
        body = ok_body()
        body["answers"] = dict(reversed(list(body["answers"].items())))
        result = await call(responding(200, body))
        by_id = {d.page_id: d for d in result.decisions}
        self.assertEqual(by_id["page-ftr-scr"].p_match, 0.93)

    async def test_probabilities_read_by_name_not_choice_or_confidence(self):
        body = ok_body()
        body["answers"]["c0"]["choice"] = "no"
        body["answers"]["c0"]["confidence"] = 0.01
        result = await call(responding(200, body))
        self.assertEqual(result.decisions[0].p_match, 0.93)

    async def test_missing_usage_leaves_tokens_blank(self):
        body = ok_body()
        del body["usage"]
        result = await call(responding(200, body))
        self.assertIsNone(result.input_tokens)


class MalformedResponseTests(unittest.IsolatedAsyncioTestCase):
    async def assert_invalid(self, body):
        with self.assertRaises(MatcherError) as ctx:
            await call(responding(200, body))
        self.assertEqual(ctx.exception.failure, "invalid_body")

    async def test_missing_candidate(self):
        body = ok_body()
        del body["answers"]["c1"]
        await self.assert_invalid(body)

    async def test_unknown_key(self):
        body = ok_body()
        body["answers"]["c9"] = body["answers"]["c0"]
        await self.assert_invalid(body)

    async def test_probabilities_not_summing_to_one(self):
        body = ok_body()
        body["answers"]["c0"]["probabilities"] = {"match": 0.5, "maybe": 0.3, "no": 0.3}
        await self.assert_invalid(body)

    async def test_probability_out_of_range(self):
        body = ok_body()
        body["answers"]["c0"]["probabilities"] = {"match": 1.2, "maybe": -0.2, "no": 0.0}
        await self.assert_invalid(body)

    async def test_missing_option(self):
        body = ok_body()
        del body["answers"]["c0"]["probabilities"]["maybe"]
        await self.assert_invalid(body)

    async def test_extra_option(self):
        body = ok_body()
        body["answers"]["c0"]["probabilities"]["unsure"] = 0.0
        await self.assert_invalid(body)

    async def test_wrong_answer_type(self):
        body = ok_body()
        body["answers"]["c0"]["type"] = "text"
        await self.assert_invalid(body)

    async def test_generated_reason_is_rejected_by_shape(self):
        body = ok_body()
        body["answers"]["c0"]["choice"] = "Moonglow FTR SCR"  # a name echo
        await self.assert_invalid(body)

    async def test_extra_reason_field_is_rejected(self):
        body = ok_body()
        body["answers"]["c0"]["reason"] = "It is clearly the FTR SCR."
        await self.assert_invalid(body)

    async def test_extra_name_echo_field_is_rejected(self):
        body = ok_body()
        body["answers"]["c0"]["name"] = "Moonglow FTR SCR"
        await self.assert_invalid(body)

    async def test_rejection_keeps_model_usage_and_probabilities_as_evidence(self):
        body = ok_body()
        body["answers"]["c0"]["reason"] = "echo"
        with self.assertRaises(MatcherError) as ctx:
            await call(responding(200, body))
        ev = ctx.exception.evidence
        self.assertEqual(ev.returned_model, body["model"])
        self.assertEqual(ev.input_tokens, body["usage"]["input_tokens"])
        self.assertEqual(ev.output_tokens, body["usage"]["output_tokens"])
        self.assertEqual(ev.requested_model, JEV_MODEL)
        self.assertEqual(ev.probabilities[CANDIDATES[0].page_id]["match"], 0.93)

    async def test_partial_body_keeps_only_safely_parsed_evidence(self):
        body = ok_body()
        del body["answers"]["c1"]
        body["answers"]["c0"]["probabilities"]["no"] = "lots"
        body["usage"] = {"input_tokens": -5, "output_tokens": 12}
        with self.assertRaises(MatcherError) as ctx:
            await call(responding(200, body))
        ev = ctx.exception.evidence
        self.assertEqual(ev.probabilities[CANDIDATES[0].page_id].keys(), {"match", "maybe"})
        self.assertNotIn(CANDIDATES[1].page_id, ev.probabilities)
        self.assertIsNone(ev.input_tokens)
        self.assertEqual(ev.output_tokens, 12)

    async def test_nonsense_body_yields_empty_evidence(self):
        with self.assertRaises(MatcherError) as ctx:
            await call(responding(200, [1, 2]))
        self.assertIsNone(ctx.exception.evidence.returned_model)
        self.assertEqual(ctx.exception.evidence.probabilities, {})

    async def test_missing_model(self):
        body = ok_body()
        del body["model"]
        await self.assert_invalid(body)

    async def test_chat_completion_shape(self):
        await self.assert_invalid({"choices": [{"message": {"content": "{}"}}]})

    async def test_not_json(self):
        await self.assert_invalid("<html>gateway</html>")


class ServiceFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_documented_error_statuses(self):
        for status in (400, 401, 402, 403, 404, 413, 429, 500, 502, 503, 524, 529):
            with self.subTest(status=status):
                body = {"error": {"code": status, "message": "boom"}}
                with self.assertRaises(MatcherError) as ctx:
                    await call(responding(status, body))
                self.assertEqual(ctx.exception.failure, f"http_{status}")

    async def test_429_carries_retry_after(self):
        err = CONTRACT["error_response"]
        with self.assertRaises(MatcherError) as ctx:
            await call(responding(err["status"], err["body"], headers=err["headers"]))
        self.assertEqual(ctx.exception.retry_after, 7.0)

    async def test_timeout(self):
        def handler(request):
            raise httpx.ReadTimeout("slow", request=request)

        with self.assertRaises(MatcherError) as ctx:
            await call(handler)
        self.assertEqual(ctx.exception.failure, "timeout")

    async def test_connection_error(self):
        def handler(request):
            raise httpx.ConnectError("down", request=request)

        with self.assertRaises(MatcherError) as ctx:
            await call(handler)
        self.assertEqual(ctx.exception.failure, "transport")


class RetryAfterTests(unittest.TestCase):
    def test_seconds_and_cap(self):
        self.assertEqual(retry_after_seconds(httpx.Headers({"Retry-After": "3"})), 3.0)
        self.assertEqual(retry_after_seconds(httpx.Headers({"Retry-After": "120"})), 30.0)

    def test_rate_limit_reset_epoch_ms(self):
        headers = httpx.Headers({"X-RateLimit-Reset": "1700000005000"})
        self.assertEqual(retry_after_seconds(headers, now=1_700_000_000.0), 5.0)

    def test_absent_or_garbage(self):
        self.assertIsNone(retry_after_seconds(httpx.Headers({})))
        self.assertIsNone(retry_after_seconds(httpx.Headers({"Retry-After": "soon"})))


if __name__ == "__main__":
    unittest.main()
