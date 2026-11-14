"""Masked extraction through t6-core's typed LLM task (#138).

DeepSeek still extracts from unmasked text in production. This adapter runs the
same extraction on text whose registry names were swapped for placeholders, then
restored, so the two can be compared before anything switches. Retention is
unknown for DeepSeek, so the task asks for no more than that and the privacy
comes from the masking. A failure is returned, never raised.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import partial

from t6.llm import DataPolicy, Failed, OpenAIChat, Provider, Task, run
from t6.privacy import Masked, Registry, Session

from jo_watchdog.adapters.protocols import ClassifierOutcome, ClassifyResult
from jo_watchdog.core.extraction import PROMPT_TAG, SYSTEM_PROMPT, Extraction, fewshot_messages

_ORIGIN_HEADER = "--- forward origin (context only, not field values) ---"


@dataclass(frozen=True)
class ExtractInput:
    """The masked message, and its forward-origin block when there is one."""

    text: Masked
    origin: Masked | None = None


def _render(payload: ExtractInput) -> list:
    user = payload.text.text
    if payload.origin is not None:
        user = f"{user}\n\n{payload.origin.text}"
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        *fewshot_messages(),
        {"role": "user", "content": user},
    ]


EXTRACT_MASKED = Task(
    prompt_tag=PROMPT_TAG,
    input_model=ExtractInput,
    output_model=Extraction,
    render=_render,
    policy=DataPolicy(min_retention="unknown", masking="pseudonymize"),
)

_OUTCOMES: dict[str, ClassifierOutcome] = {
    "empty": "empty_content",
    "timeout": "timeout",
    "invalid_json": "invalid_json",
    "schema_violation": "schema_violation",
}


class MaskedExtractor:
    """Runs the extraction task over pseudonymized text. The provider call is blocking, so it
    runs on one worker thread."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str,
        model: str,
        timeout_seconds: float = 60,
        provider: Provider | None = None,
    ):
        self._provider = provider or OpenAIChat(
            base_url=base_url, api_key=api_key, model=model, retention="unknown"
        )
        self._timeout = timeout_seconds
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="t6-llm")

    async def close(self) -> None:
        self._executor.shutdown(wait=False)

    async def extract(
        self, text: str, origin_lines: list[str] | None, registry: Registry
    ) -> ClassifyResult:
        """Extract from `text` with every registry name masked. Names come back restored."""
        session = Session(registry)
        origin = session.mask("\n".join([_ORIGIN_HEADER, *origin_lines])) if origin_lines else None
        payload = ExtractInput(session.mask(text), origin)
        call = partial(
            run, EXTRACT_MASKED, payload, provider=self._provider, session=session,
            timeout=self._timeout,
        )
        result = await asyncio.get_running_loop().run_in_executor(self._executor, call)
        trace = result.trace
        common = {
            "requested_model": self._provider.model,
            "returned_model": trace.model,
            "prompt_tag": trace.prompt_tag,
            "input_tokens": trace.tokens.prompt,
            "output_tokens": trace.tokens.completion,
            "latency_ms": trace.latency_ms,
        }
        if isinstance(result, Failed):
            outcome = _OUTCOMES.get(result.reason, "api_error")
            return ClassifyResult(outcome=outcome, error=f"{result.reason}: {result.detail}", **common)
        return ClassifyResult(outcome="ok", extraction=result.value, **common)
