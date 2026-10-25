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

    async def test_unmasked_credentials_never_reach_the_outbound_json(self):
        secrets = ("probe-secret", "hunter2", "origin-secret")
        classifier, seen = classifier_for(replay("jo"))
        await classifier.classify(
            "PW: probe-secret\nPassword:\n\nhunter2",
            ["Origin type: channel", "Forwarded by: Lei pw: origin-secret"],
        )
        outbound = seen[0].content.decode("utf-8")
        for secret in secrets:
            self.assertNotIn(secret, outbound)
        last = json.loads(outbound)["messages"][-1]["content"]
        self.assertIn("PW: [password withheld", last)

    async def test_text_without_credentials_is_sent_unchanged(self):
        classifier, seen = classifier_for(replay("jo"))
        text = "MFG - TOC - JO #74\nTask: upload\nNotes: pass the file along"
        await classifier.classify(text, None)
        self.assertEqual(json.loads(seen[0].content)["messages"][-1]["content"], text)


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

    async def test_connection_error_is_api_error(self):
        def refuse(request):
            raise httpx.ConnectError("refused", request=request)

        classifier, _ = classifier_for(refuse)
        result = await classifier.classify("x", None)
        self.assertEqual(result.outcome, "api_error")


OK_CONTENT = RECORDED["not_jo"]["body"]["choices"][0]["message"]["content"]


def envelope(**overrides) -> dict:
    body = {
        "model": "deepseek-chat-v9",
        "choices": [{"message": {"content": OK_CONTENT}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 3},
    }
    body.update(overrides)
    return body


MALFORMED_ENVELOPES = {
    "list envelope": [envelope()],
    "string envelope": "oops",
    "null envelope": None,
    "choices not a list": envelope(choices={"message": {}}),
    "empty choices": envelope(choices=[]),
    "choice not a mapping": envelope(choices=["x"]),
    "message not a mapping": envelope(choices=[{"message": "x"}]),
    "message missing": envelope(choices=[{}]),
    "content is a number": envelope(choices=[{"message": {"content": 5}}]),
    "content is a list": envelope(choices=[{"message": {"content": ["a"]}}]),
    "usage is a string": envelope(usage="many"),
    "usage is a list": envelope(usage=[1, 2]),
}


class MalformedEnvelopeTests(unittest.IsolatedAsyncioTestCase):
    async def test_structure_failures_are_returned_not_raised(self):
        for name, body in MALFORMED_ENVELOPES.items():
            if name.startswith("usage"):
                continue
            with self.subTest(name):
                classifier, seen = classifier_for(lambda r, b=body: httpx.Response(200, json=b))
                result = await classifier.classify("x", None)
                self.assertEqual(result.outcome, "api_error")
                self.assertIn("malformed envelope", result.error)
                self.assertIsNone(result.extraction)
                self.assertEqual(len(seen), 1)  # no repair call

    async def test_malformed_usage_is_dropped_but_the_call_still_succeeds(self):
        for name in ("usage is a string", "usage is a list"):
            with self.subTest(name):
                body = MALFORMED_ENVELOPES[name]
                classifier, _ = classifier_for(lambda r, b=body: httpx.Response(200, json=b))
                result = await classifier.classify("x", None)
                self.assertEqual(result.outcome, "ok")
                self.assertEqual(result.returned_model, "deepseek-chat-v9")
                self.assertIsNone(result.input_tokens)
                self.assertIsNone(result.output_tokens)

    async def test_safe_trace_is_kept_on_a_malformed_choice(self):
        body = envelope(choices=[{"message": {"content": 5}}])
        classifier, _ = classifier_for(lambda r: httpx.Response(200, json=body))
        result = await classifier.classify("x", None)
        self.assertEqual(result.outcome, "api_error")
        self.assertEqual(result.returned_model, "deepseek-chat-v9")
        self.assertEqual(result.input_tokens, 10)
        self.assertEqual(result.output_tokens, 3)

    async def test_unsafe_trace_values_are_dropped(self):
        body = envelope(
            model=["x"], usage={"prompt_tokens": "10", "completion_tokens": True}, choices=[]
        )
        classifier, _ = classifier_for(lambda r: httpx.Response(200, json=body))
        result = await classifier.classify("x", None)
        self.assertEqual(result.outcome, "api_error")
        self.assertIsNone(result.returned_model)
        self.assertIsNone(result.input_tokens)
        self.assertIsNone(result.output_tokens)

    async def test_non_json_body_is_api_error(self):
        classifier, _ = classifier_for(lambda r: httpx.Response(200, content=b"<html>"))
        result = await classifier.classify("x", None)
        self.assertEqual(result.outcome, "api_error")


if __name__ == "__main__":
    unittest.main()
