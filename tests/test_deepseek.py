"""Contract tests for the DeepSeek adapter over recorded responses (no network)."""

import json
import unittest
from pathlib import Path

import httpx

from jo_watchdog.adapters.deepseek import DeepSeekClassifier
from jo_watchdog.core.extraction import PROMPT_TAG, SYSTEM_PROMPT

RECORDED = json.loads(
    (Path(__file__).parent / "fixtures" / "deepseek_recorded.json").read_text(encoding="utf-8")
)


def classifier_for(handler) -> tuple[DeepSeekClassifier, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    client = httpx.AsyncClient(
        base_url="https://api.deepseek.test", transport=httpx.MockTransport(record)
    )
    return DeepSeekClassifier("sk-test", model="deepseek-chat", client=client), seen


def replay(name: str):
    case = RECORDED[name]
    return lambda request: httpx.Response(case["status"], json=case["body"])


class RequestShapeTests(unittest.IsolatedAsyncioTestCase):
    async def test_request_is_temperature_zero_json_mode_with_masked_text_only(self):
        classifier, seen = classifier_for(replay("jo"))
        await classifier.classify("MFG - TOC - JO #74\nTask: upload", None)

        body = json.loads(seen[0].content)
        self.assertEqual(seen[0].url.path, "/chat/completions")
        self.assertEqual(body["model"], "deepseek-chat")
        self.assertEqual(body["temperature"], 0)
        self.assertEqual(body["response_format"], {"type": "json_object"})
        self.assertEqual(body["messages"][0], {"role": "system", "content": SYSTEM_PROMPT})
        self.assertEqual(body["messages"][-1]["content"], "MFG - TOC - JO #74\nTask: upload")

    async def test_forward_origin_block_follows_the_text(self):
        classifier, seen = classifier_for(replay("jo"))
        await classifier.classify("text", ["Origin type: channel", "Forwarded by: Lei"])
        last = json.loads(seen[0].content)["messages"][-1]["content"]
        self.assertTrue(last.startswith("text\n\n--- forward origin"))
        self.assertIn("Origin type: channel", last)


class OutcomeTests(unittest.IsolatedAsyncioTestCase):
    async def test_verdicts_validate_and_carry_trace(self):
        for name in ("jo", "unsure", "not_jo"):
            with self.subTest(name):
                classifier, _ = classifier_for(replay(name))
                result = await classifier.classify("x", None)
                self.assertEqual(result.outcome, "ok")
                self.assertEqual(result.extraction.verdict, name)
                self.assertEqual(result.requested_model, "deepseek-chat")
                self.assertEqual(result.returned_model, "deepseek-chat")
                self.assertEqual(result.prompt_tag, PROMPT_TAG)
                self.assertEqual(result.input_tokens, 6180)
                self.assertEqual(result.output_tokens, 214)
                self.assertIsNotNone(result.latency_ms)

    async def test_jo_fields_stay_verbatim(self):
        classifier, _ = classifier_for(replay("jo"))
        result = await classifier.classify("x", None)
        self.assertEqual(result.extraction.deadline_raw, "September 29 / Tues 5AM")
        self.assertEqual(result.extraction.jo_number_raw, "JO #74")

    async def test_failure_classes(self):
        cases = {
            "empty_content": "empty_content",
            "malformed_json": "invalid_json",
            "schema_mismatch": "schema_violation",
            "provider_failure": "api_error",
        }
        for recording, outcome in cases.items():
            with self.subTest(recording):
                classifier, seen = classifier_for(replay(recording))
                result = await classifier.classify("x", None)
                self.assertEqual(result.outcome, outcome)
                self.assertIsNone(result.extraction)
                self.assertTrue(result.error)
                self.assertEqual(len(seen), 1)  # no repair call

    async def test_timeout(self):
        def hang(request):
            raise httpx.ReadTimeout("timed out", request=request)

        classifier, _ = classifier_for(hang)
        result = await classifier.classify("x", None)
        self.assertEqual(result.outcome, "timeout")
        self.assertIsNone(result.extraction)

    async def test_retry_after_header_on_an_error_status_is_carried(self):
        def limited(request):
            return httpx.Response(429, headers={"Retry-After": "90"}, text="slow down")

        classifier, _ = classifier_for(limited)
        result = await classifier.classify("x", None)
        self.assertEqual((result.outcome, result.retry_after), ("api_error", 90.0))

    async def test_unusable_retry_after_is_ignored(self):
        def limited(request):
            return httpx.Response(
                503, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}, text="down"
            )

        classifier, _ = classifier_for(limited)
        result = await classifier.classify("x", None)
        self.assertIsNone(result.retry_after)

    async def test_connection_error_is_api_error(self):
        def refuse(request):
            raise httpx.ConnectError("refused", request=request)

        classifier, _ = classifier_for(refuse)
        result = await classifier.classify("x", None)
        self.assertEqual(result.outcome, "api_error")


if __name__ == "__main__":
    unittest.main()
