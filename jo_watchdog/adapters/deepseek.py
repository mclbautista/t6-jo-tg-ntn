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
            choice = payload["choices"][0]
            content = choice["message"].get("content")
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            return self._result("api_error", started, error=f"malformed envelope: {exc!r}")

        usage = payload.get("usage") or {}
        trace = {
            "returned_model": payload.get("model"),
            "input_tokens": usage.get("prompt_tokens"),
            "output_tokens": usage.get("completion_tokens"),
        }
        if not content or not content.strip():
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
