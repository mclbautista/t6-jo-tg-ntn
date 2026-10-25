"""Typed-model smoke checks for DeepSeek and Jev (#61).

``smoke.py`` is manual and calls these against the live providers; the offline
tests call the same functions over ``httpx.MockTransport``. Both go through the
production adapters, so a response is judged by the same validation the worker
uses. A transport that records requests and response headers sits underneath, so
what is reported (request count, masked text, rate-limit headers) is observed,
not assumed.

Nothing here claims a live 429 unless one came back, and a live call needs the
explicit authorization that ``smoke.py`` documents.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from jo_watchdog.adapters.deepseek import DeepSeekClassifier
from jo_watchdog.adapters.jev import CRITERIA, JevMatcher, MatcherError
from jo_watchdog.app.smoke_gate import FAIL, MANUAL, PASS, Result
from jo_watchdog.core.extraction import Extraction
from jo_watchdog.core.masker import WITHHELD, outbound_text
from jo_watchdog.core.matching import Candidate, decide

# The three fixed regression pairs, in request order (tests/fixtures/jev/regression_pairs.json).
REGRESSION_PAIRS = ("moonglow-jo-32", "somsom-0155", "ina-toc-30")

SECRET = "hunter22"
MASK_POSITIVE = f"SOMSOM - TOC - #0163\nTask: conform reel 3\npw: {SECRET}"
MASK_NEGATIVE = "SOMSOM - TOC - #0163\nTask: conform reel 3\nPurpose: for grading"


@dataclass
class ModelCheck:
    result: Result
    lines: list[str] = field(default_factory=list)
    requests_sent: int = 0


class RecordingTransport(httpx.AsyncBaseTransport):
    """Wraps a transport and keeps each request body and response status/headers."""

    def __init__(self, inner: httpx.AsyncBaseTransport | None = None):
        self._inner = inner or httpx.AsyncHTTPTransport()
        self.requests: list[dict] = []
        self.responses: list[tuple[int, dict[str, str]]] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(json.loads(request.content or b"null"))
        response = await self._inner.handle_async_request(request)
        self.responses.append((response.status_code, dict(response.headers)))
        return response

    async def aclose(self) -> None:
        await self._inner.aclose()


def _request_text(body: dict) -> str:
    """The Job Order text sent: the last message (the system prompt is fixed wording)."""
    messages = body.get("messages") or [{}]
    return str(messages[-1].get("content", ""))


def _fail(lines: list[str], detail: str, sent: int) -> ModelCheck:
    return ModelCheck(Result(FAIL, detail), lines, sent)


# -- DeepSeek ---------------------------------------------------------------------


async def check_deepseek(
    api_key: str,
    *,
    base_url: str,
    model: str,
    timeout: float,
    inner: httpx.AsyncBaseTransport | None = None,
) -> ModelCheck:
    """One masked-credential call and one no-mask call through ``DeepSeekClassifier``.

    The positive case proves a supported credential is withheld from the outbound
    request; the no-mask case proves ordinary text is sent unchanged. Each reply
    must be a validated ``Extraction`` with the returned model and token counts.
    """
    recorder = RecordingTransport(inner)
    classifier = DeepSeekClassifier(
        api_key,
        base_url=base_url,
        model=model,
        timeout_seconds=timeout,
        client=httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
            transport=recorder,
        ),
    )
    lines: list[str] = []
    problems: list[str] = []
    verdicts: list[str] = []
    try:
        for label, raw, masked_expected in (
            ("mask positive", MASK_POSITIVE, True),
            ("no mask", MASK_NEGATIVE, False),
        ):
            masked = outbound_text(raw)
            if bool(masked.withheld_count) != masked_expected:
                problems.append(f"{label}: masker withheld {masked.withheld_count} value(s)")
            before = len(recorder.requests)
            result = await classifier.classify(masked.text, [])
            if len(recorder.requests) != before + 1:
                saw = len(recorder.requests) - before
                problems.append(f"{label}: expected one request, saw {saw}")
                continue
            sent = _request_text(recorder.requests[-1])
            if masked_expected and (
                SECRET in json.dumps(recorder.requests[-1]) or WITHHELD not in sent
            ):
                problems.append(f"{label}: request did not withhold the credential")
            if not masked_expected and (WITHHELD in sent or "for grading" not in sent):
                problems.append(f"{label}: request altered text with no credential")
            if result.extraction is None:
                problems.append(f"{label}: {result.outcome}: {result.error}")
                continue
            if not isinstance(result.extraction, Extraction):
                problems.append(f"{label}: reply is not a validated Extraction")
            if result.requested_model != model:
                problems.append(f"{label}: requested model {result.requested_model!r}")
            if not result.returned_model:
                problems.append(f"{label}: no returned model snapshot")
            if result.input_tokens is None or result.output_tokens is None:
                problems.append(f"{label}: token counts missing")
            verdicts.append(result.extraction.verdict)
            lines.append(
                f"{label}: model {result.returned_model}; tokens "
                f"{result.input_tokens}/{result.output_tokens}; {result.latency_ms} ms; "
                f"verdict {result.extraction.verdict}"
            )
    finally:
        await classifier.close()
    sent_count = len(recorder.requests)
    if problems:
        return _fail(lines, "; ".join(problems), sent_count)
    if any(v != "jo" for v in verdicts):
        detail = f"structured output valid but verdicts were {verdicts}; review the model"
        return ModelCheck(Result(MANUAL, detail), lines, sent_count)
    return ModelCheck(
        Result(PASS, "structured output, snapshot and tokens; mask positive and no-mask cases"),
        lines,
        sent_count,
    )


# -- Jev --------------------------------------------------------------------------


class PairError(ValueError):
    """The regression fixtures do not hold exactly the three required pairs."""


def load_pairs(path: Path) -> list[dict]:
    """The three fixed regression pairs, in order. Anything short of all three is an error."""
    by_name = {p["name"]: p for p in json.loads(path.read_text(encoding="utf-8"))}
    missing = [n for n in REGRESSION_PAIRS if n not in by_name]
    if missing:
        raise PairError(f"missing regression pair fixture(s): {', '.join(missing)}")
    pairs = [by_name[n] for n in REGRESSION_PAIRS]
    for pair in pairs:
        if len(pair["candidates"]) != 1:
            raise PairError(f"{pair['name']} must name exactly one Deliverable")
    return pairs


def build_regression_request(pairs: list[dict], model: str) -> tuple[dict, list[Candidate]]:
    """One Decisions request: every pair's Job Order in ``state``, one question per pair.

    Question ``c<i>`` asks about Job Order ``c<i>`` and its own Deliverable, so the
    production keys map back to candidates exactly as in a normal match call.
    """
    candidates = [Candidate(**pair["candidates"][0]) for pair in pairs]
    state = {
        "job_orders": {
            f"c{i}": {
                "project": pair["project"]["name"],
                "job_order": outbound_text(pair["source_text"]).text,
            }
            for i, pair in enumerate(pairs)
        }
    }
    questions = {
        f"c{i}": {
            "type": "choice",
            "instructions": (
                f"Does Job Order c{i} in the state belong to the Deliverable '{c.name}'?"
            ),
            "criteria": dict(CRITERIA),
        }
        for i, c in enumerate(candidates)
    }
    return {"model": model, "state": state, "questions": questions}, candidates


def _rate_facts(headers: dict[str, str]) -> tuple[str | None, dict[str, str]]:
    retry = headers.get("retry-after")
    limits = {k: v for k, v in sorted(headers.items()) if k.startswith("x-ratelimit")}
    return retry, limits


def _rate_lines(recorder: RecordingTransport) -> list[str]:
    if not recorder.responses:
        return ["rate limits: no response received"]
    status, headers = recorder.responses[-1]
    retry, limits = _rate_facts(headers)
    lines = [
        f"response status: {status}",
        f"Retry-After: {retry if retry is not None else 'absent'}",
        "X-RateLimit-*: " + (json.dumps(limits) if limits else "absent"),
    ]
    if status == 429:
        lines.append("429 observed: yes")
    else:
        lines.append(
            f"429 observed: no; the facts above are from an HTTP {status} response and say "
            "nothing about how a 429 would look"
        )
    return lines


async def check_jev(
    api_key: str,
    pairs_path: Path,
    *,
    url: str,
    model: str,
    timeout: float,
    t_link: float,
    t_hint: float,
    inner: httpx.AsyncBaseTransport | None = None,
) -> ModelCheck:
    """Send the one three-pair request through ``JevMatcher`` and report what came back.

    Fails, without sending anything, when a regression fixture is missing. Fails
    on any answer the production validation rejects, including a missing pair.
    """
    try:
        pairs = load_pairs(pairs_path)
    except (PairError, OSError, ValueError, KeyError) as exc:
        return _fail(["requests sent: 0"], f"regression fixtures: {exc}", 0)
    body, candidates = build_regression_request(pairs, model)

    recorder = RecordingTransport(inner)
    matcher = JevMatcher(api_key, url=url, model=model, timeout=timeout, transport=recorder)
    lines: list[str] = []
    try:
        try:
            result = await matcher.send(body, candidates)
        except MatcherError as exc:
            lines.append(f"requests sent: {len(recorder.requests)}")
            lines += _rate_lines(recorder)
            if exc.evidence and exc.evidence.returned_model:
                lines.append(f"snapshot (from the invalid body): {exc.evidence.returned_model}")
            detail = f"{exc.failure}: {exc.message}"
            if exc.retry_after is not None:
                detail += f" (retry after {exc.retry_after:g}s)"
            return _fail(lines, detail, len(recorder.requests))
    finally:
        await matcher.close()

    decision = decide(candidates, result, t_link=t_link, t_hint=t_hint, link_enabled=False)
    lines.append(f"requests sent: {len(recorder.requests)}")
    lines.append(f"requested model: {result.requested_model}")
    lines.append(f"returned snapshot: {result.returned_model}")
    lines.append(f"tokens: {result.input_tokens}/{result.output_tokens}")
    mismatches = []
    for pair, outcome in zip(pairs, decision.outcomes, strict=True):
        expected = pair["expected_verdicts"][outcome.page_id]
        lines.append(
            f"{pair['name']} vs {outcome.name}: match={outcome.p_match:.2f} "
            f"maybe={outcome.p_maybe:.2f} no={outcome.p_no:.2f} -> {outcome.verdict} "
            f"(expected {expected})"
        )
        if outcome.verdict != expected:
            mismatches.append(f"{pair['name']} is {outcome.verdict}, expected {expected}")
    lines += _rate_lines(recorder)
    if not result.returned_model:
        return _fail(lines, "response carried no model snapshot", len(recorder.requests))
    if mismatches:
        # Shadow mode exists to observe this; the owner reviews before linking is enabled.
        detail = "review before enabling linking: " + "; ".join(mismatches)
        return ModelCheck(Result(MANUAL, detail), lines, len(recorder.requests))
    return ModelCheck(
        Result(PASS, f"3 regression pairs, 1 request, snapshot {result.returned_model}"),
        lines,
        len(recorder.requests),
    )
