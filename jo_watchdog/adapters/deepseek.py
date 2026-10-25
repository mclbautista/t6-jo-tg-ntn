"""DeepSeek classify/extract adapter (OpenAI-compatible chat completions, httpx).

One call per message at temperature 0 in JSON mode, then Pydantic validation.
Every failure is classified (``invalid_json``, ``schema_violation``,
``empty_content``, ``timeout``, ``api_error``) and returned, never raised, so the
worker can record it as one failed attempt. There is no repair call.
"""

from __future__ import annotations

import json
import time

import httpx
from pydantic import ValidationError

from jo_watchdog.adapters.protocols import ClassifyResult
from jo_watchdog.core.extraction import PROMPT_TAG, Extraction, build_messages

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-chat"  # the provider's "latest" alias; never pinned


def _count(value) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _safe_trace(payload) -> dict:
    """Trace fields that are safe to store; anything of the wrong type is dropped."""
    if not isinstance(payload, dict):
        return {}
    usage = payload.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    model = payload.get("model")
    return {
        "returned_model": model if isinstance(model, str) else None,
        "input_tokens": _count(usage.get("prompt_tokens")),
        "output_tokens": _count(usage.get("completion_tokens")),
    }


def _content_of(payload) -> tuple[str | None, str | None]:
    """The first choice's message content, or ``(None, why it is malformed)``.

    A missing or null content is not malformed here: it is ``empty_content``.
    """
    if not isinstance(payload, dict):
        return None, f"envelope is {type(payload).__name__}, not an object"
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return None, "choices is missing or empty"
    choice = choices[0]
    if not isinstance(choice, dict):
        return None, f"choice is {type(choice).__name__}, not an object"
    message = choice.get("message")
    if not isinstance(message, dict):
        return None, f"message is {type(message).__name__}, not an object"
    content = message.get("content")
    if content is not None and not isinstance(content, str):
        return None, f"content is {type(content).__name__}, not a string"
    return content or "", None


class DeepSeekClassifier:
    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        model: str = DEFAULT_MODEL,
        timeout_seconds: float = 60,
        client: httpx.AsyncClient | None = None,
    ):
        self._model = model
        self._client = client or httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout_seconds,
        )

    async def close(self) -> None:
        await self._client.aclose()

    def _result(self, outcome: str, started: float, **fields) -> ClassifyResult:
        return ClassifyResult(
            outcome=outcome,
            requested_model=self._model,
            prompt_tag=PROMPT_TAG,
            latency_ms=round((time.monotonic() - started) * 1000),
            **fields,
        )

    async def classify(
        self, masked_text: str, origin_lines: list[str] | None = None
    ) -> ClassifyResult:
        body = {
            "model": self._model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": build_messages(masked_text, origin_lines),
        }
        started = time.monotonic()
        try:
            response = await self._client.post("/chat/completions", json=body)
        except httpx.TimeoutException as exc:
            return self._result("timeout", started, error=type(exc).__name__)
        except httpx.HTTPError as exc:
            return self._result("api_error", started, error=f"{type(exc).__name__}: {exc}")

        if response.status_code != 200:
            return self._result(
                "api_error",
                started,
                error=f"HTTP {response.status_code}: {response.text[:500]}",
                retry_after=_retry_after(response.headers.get("Retry-After")),
            )
        try:
            payload = response.json()
        except ValueError as exc:
            return self._result("api_error", started, error=f"malformed envelope: {exc!r}")
        trace = _safe_trace(payload)
        content, problem = _content_of(payload)
        if problem:
            return self._result(
                "api_error", started, error=f"malformed envelope: {problem}", **trace
            )
        if content is None or not content.strip():
            return self._result("empty_content", started, error="empty content", **trace)
        try:
            data = json.loads(content)
        except json.JSONDecodeError as exc:
            return self._result("invalid_json", started, error=str(exc), **trace)
        try:
            extraction = Extraction.model_validate(data)
        except ValidationError as exc:
            return self._result(
                "schema_violation", started, error=str(exc)[:1000], **trace
            )
        return self._result("ok", started, extraction=extraction, **trace)


def _retry_after(value: str | None) -> float | None:
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None
