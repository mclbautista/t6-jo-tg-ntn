"""Jev Deliverable matcher over OpenRouter's Decisions API (#26).

One request per Job Order: the masked text and Project name go in ``state``,
and each candidate becomes a typed Choice question keyed ``c0``, ``c1``, ...
The keys map back to Notion page ids here; the model never sees a page id.
The endpoint is alpha, so every response is validated against the documented
shape and anything else is a ``MatcherError``.
"""

from __future__ import annotations

import time
from email.utils import parsedate_to_datetime
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from jo_watchdog.core.matching import Candidate, CandidateDecision, MatchResult, sums_to_one

JEV_URL = "https://openrouter.ai/api/alpha/decisions"
JEV_MODEL = "~typesafe/jev-latest"
PROMPT_TAG = "match-v1"

# Part of the prompt: change the wording only together with PROMPT_TAG.
CRITERIA = {
    "match": "The Job Order is clearly work for this Deliverable.",
    "maybe": "The Job Order could be for this Deliverable, but the text doesn't make it clear.",
    "no": "The Job Order is not for this Deliverable.",
}

# The inline retry after a 429 never waits longer than this.
MAX_RETRY_AFTER_SECONDS = 30.0


class MatcherError(Exception):
    """One failed matcher call: an HTTP error, a timeout, or an invalid body."""

    def __init__(self, failure: str, message: str, *, retry_after: float | None = None):
        super().__init__(f"{failure}: {message}")
        self.failure = failure
        self.message = message
        self.retry_after = retry_after


class _Probabilities(BaseModel):
    model_config = ConfigDict(extra="forbid")

    match: float = Field(ge=0, le=1)
    maybe: float = Field(ge=0, le=1)
    no: float = Field(ge=0, le=1)


class _ChoiceAnswer(BaseModel):
    type: Literal["choice"]
    choice: Literal["match", "maybe", "no"]
    confidence: float | None = None  # ignored: concentration, not correctness
    probabilities: _Probabilities


class _Usage(BaseModel):
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)


class DecisionsResponse(BaseModel):
    """The documented Decisions API success body."""

    model: str
    answers: dict[str, _ChoiceAnswer]
    usage: _Usage | None = None


def question_key(index: int) -> str:
    return f"c{index}"


def build_request(
    state: str, project_name: str, candidates: list[Candidate], *, model: str = JEV_MODEL
) -> dict:
    """The exact Decisions API request body for one Job Order."""
    return {
        "model": model,
        "state": {"project": project_name, "job_order": state},
        "questions": {
            question_key(i): {
                "type": "choice",
                "instructions": f"Does this Job Order belong to the Deliverable '{c.name}'?",
                "criteria": dict(CRITERIA),
            }
            for i, c in enumerate(candidates)
        },
    }


def parse_response(
    body: object, candidates: list[Candidate], *, requested_model: str
) -> MatchResult:
    """Validate a response body and map its answers back to candidate page ids."""
    try:
        parsed = DecisionsResponse.model_validate(body)
    except ValidationError as exc:
        raise MatcherError("invalid_body", f"{exc.error_count()} validation error(s)") from exc

    keys = {question_key(i): c for i, c in enumerate(candidates)}
    if set(parsed.answers) != set(keys):
        missing = len(set(keys) - set(parsed.answers))
        unknown = len(set(parsed.answers) - set(keys))
        raise MatcherError("invalid_body", f"{missing} missing and {unknown} unknown answer key(s)")

    decisions = []
    for key, candidate in keys.items():
        p = parsed.answers[key].probabilities
        if not sums_to_one(p.match, p.maybe, p.no):
            raise MatcherError("invalid_body", f"probabilities for {key} do not sum to 1")
        decisions.append(
            CandidateDecision(
                page_id=candidate.page_id, p_match=p.match, p_maybe=p.maybe, p_no=p.no
            )
        )
    return MatchResult(
        decisions=decisions,
        requested_model=requested_model,
        returned_model=parsed.model,
        input_tokens=parsed.usage.input_tokens if parsed.usage else None,
        output_tokens=parsed.usage.output_tokens if parsed.usage else None,
    )


def retry_after_seconds(headers: httpx.Headers, *, now: float | None = None) -> float | None:
    """Seconds to wait from ``Retry-After`` or ``X-RateLimit-Reset``, capped at 30."""
    now = time.time() if now is None else now
    wait: float | None = None
    raw = headers.get("Retry-After")
    if raw:
        try:
            wait = float(raw)
        except ValueError:
            try:
                wait = parsedate_to_datetime(raw).timestamp() - now
            except (TypeError, ValueError):
                wait = None
    if wait is None and (raw := headers.get("X-RateLimit-Reset")):
        try:
            reset = float(raw)
        except ValueError:
            reset = None
        if reset is not None:
            # OpenRouter sends an epoch in milliseconds; accept seconds too.
            if reset > 1e12:
                reset /= 1000
            wait = reset - now if reset > 1e9 else reset
    if wait is None:
        return None
    return max(0.0, min(wait, MAX_RETRY_AFTER_SECONDS))


class JevMatcher:
    """``DeliverableMatcher`` backed by OpenRouter's Decisions API."""

    prompt_tag = PROMPT_TAG

    def __init__(
        self,
        api_key: str,
        *,
        url: str = JEV_URL,
        model: str = JEV_MODEL,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.url = url
        self.model = model
        self._client = httpx.AsyncClient(
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            timeout=timeout,
            transport=transport,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def match(
        self, state: str, project_name: str, candidates: list[Candidate]
    ) -> MatchResult:
        body = build_request(state, project_name, candidates, model=self.model)
        try:
            resp = await self._client.post(self.url, json=body)
        except httpx.TimeoutException as exc:
            raise MatcherError("timeout", type(exc).__name__) from exc
        except httpx.HTTPError as exc:
            raise MatcherError("transport", type(exc).__name__) from exc

        if resp.status_code >= 400:
            raise MatcherError(
                f"http_{resp.status_code}",
                _error_message(resp),
                retry_after=retry_after_seconds(resp.headers) if resp.status_code == 429 else None,
            )
        try:
            data = resp.json()
        except ValueError as exc:
            raise MatcherError("invalid_body", "response is not JSON") from exc
        return parse_response(data, candidates, requested_model=self.model)


def _error_message(resp: httpx.Response) -> str:
    """The provider's error code/message, never the request or response body."""
    try:
        err = resp.json().get("error", {})
        return f"{err.get('code', resp.status_code)} {err.get('message', '')}".strip()
    except (ValueError, AttributeError):
        return str(resp.status_code)
