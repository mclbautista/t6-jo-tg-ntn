"""The Deliverable match stage: live candidates, one Jev call, one inline retry.

Runs after extraction and Project resolution and before the one Notion write
(#26). It never raises: any failure becomes ``outcome = "skipped"`` with a
``matcher_skipped`` finding, so the Job Order is still written with Deliverable
blank. The result carries the ``stage_results`` JSON and one ``llm_calls``
record per attempt for the ledger to store.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable

from pydantic import BaseModel, Field

from jo_watchdog.adapters.jev import PROMPT_TAG, MatcherError
from jo_watchdog.adapters.protocols import DeliverableMatcher, DeliverableSource
from jo_watchdog.app.config import Config
from jo_watchdog.core.masker import MASKER_VERSION, mask_text, outbound_text
from jo_watchdog.core.matching import (
    MATCHER_SKIPPED_LINE,
    Candidate,
    JoOutcome,
    MatchDecision,
    MatchResult,
    decide,
    deliverable_property,
    hint_line,
)

MAX_ATTEMPTS = 2  # the call plus one inline retry

Sleep = Callable[[float], Awaitable[None]]


class ResolvedProject(BaseModel):
    page_id: str
    name: str


class Finding(BaseModel):
    code: str
    line: str


class LlmCallRecord(BaseModel):
    """One ``llm_calls`` row (migration 3, #20), minus ids and timestamps."""

    provider: str = "jev"
    purpose: str = "deliverable_match"
    requested_model: str
    returned_model: str | None = None
    prompt_tag: str
    masker_version: str = MASKER_VERSION
    input_tokens: int | None = None
    output_tokens: int | None = None
    latency_ms: int
    outcome: str  # "ok" or a failure class
    error: str | None = None
    detail_json: str


class MatchStageResult(BaseModel):
    """Everything the planned payload, ledger, ops report and heartbeat need."""

    outcome: JoOutcome
    link_enabled: bool
    skip_reason: str | None = None
    project: ResolvedProject | None = None
    candidates: list[Candidate] = Field(default_factory=list)
    decision: MatchDecision | None = None
    findings: list[Finding] = Field(default_factory=list)
    llm_calls: list[LlmCallRecord] = Field(default_factory=list)

    @property
    def linked_page_ids(self) -> list[str]:
        return self.decision.linked_page_ids if self.decision else []

    def stage_result_json(self) -> str:
        """The ``stage_results`` row (``stage = 'match'``) body."""
        return self.model_dump_json(exclude={"llm_calls"})


def with_deliverable(payload: dict, stage: MatchStageResult) -> dict:
    """Add the ``Deliverable`` relation to a Notion payload only when links were decided."""
    if not stage.linked_page_ids:
        return payload
    properties = {
        **payload["properties"],
        "Deliverable": deliverable_property(stage.linked_page_ids),
    }
    return {**payload, "properties": properties}


async def run_match_stage(
    *,
    matcher: DeliverableMatcher,
    deliverables: DeliverableSource,
    config: Config,
    source_text: str,
    project: ResolvedProject | None,
    sleep: Sleep = asyncio.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> MatchStageResult:
    link_enabled = config.deliverable_link_enabled
    if project is None:
        return MatchStageResult(
            outcome="none", link_enabled=link_enabled, skip_reason="project_unresolved"
        )

    candidates: list[Candidate] | None = None
    for _ in range(MAX_ATTEMPTS):
        try:
            candidates = await deliverables.query_project_deliverables(
                config.deliverables_data_source_id, project.page_id
            )
            break
        except Exception:
            continue
    if candidates is None:
        return _skipped(project, [], [], link_enabled, "candidate_query_failed")
    if not candidates:
        return MatchStageResult(
            outcome="none",
            link_enabled=link_enabled,
            skip_reason="no_candidates",
            project=project,
        )

    state = outbound_text(source_text).text
    calls: list[LlmCallRecord] = []
    for attempt in range(MAX_ATTEMPTS):
        started = clock()
        result: MatchResult | None = None
        try:
            result = await matcher.match(state, project.name, candidates)
            decision = decide(
                candidates,
                result,
                t_link=config.t_link,
                t_hint=config.t_hint,
                link_enabled=link_enabled,
            )
        except Exception as exc:
            calls.append(_failed_call(config, project, candidates, exc, clock() - started, result))
            if attempt + 1 < MAX_ATTEMPTS:
                wait = exc.retry_after if isinstance(exc, MatcherError) else None
                if wait:
                    await sleep(wait)
            continue
        calls.append(_ok_call(project, candidates, result, clock() - started))
        return MatchStageResult(
            outcome=decision.outcome,
            link_enabled=link_enabled,
            project=project,
            candidates=candidates,
            decision=decision,
            findings=[Finding(code="deliverable_hint", line=hint_line(h)) for h in decision.hints],
            llm_calls=calls,
        )
    return _skipped(project, candidates, calls, link_enabled, "matcher_failed")


def _skipped(
    project: ResolvedProject,
    candidates: list[Candidate],
    calls: list[LlmCallRecord],
    link_enabled: bool,
    reason: str,
) -> MatchStageResult:
    return MatchStageResult(
        outcome="skipped",
        link_enabled=link_enabled,
        skip_reason=reason,
        project=project,
        candidates=candidates,
        findings=[Finding(code="matcher_skipped", line=MATCHER_SKIPPED_LINE)],
        llm_calls=calls,
    )


def _detail(
    project: ResolvedProject, candidates: list[Candidate], result: MatchResult | None
) -> str:
    probs = {d.page_id: d for d in result.decisions} if result else {}
    return json.dumps(
        {
            "project": project.model_dump(),
            "candidates": [
                {
                    "page_id": c.page_id,
                    "name": c.name,
                    **(
                        {
                            "p_match": probs[c.page_id].p_match,
                            "p_maybe": probs[c.page_id].p_maybe,
                            "p_no": probs[c.page_id].p_no,
                        }
                        if c.page_id in probs
                        else {}
                    ),
                }
                for c in candidates
            ],
        },
        ensure_ascii=False,
    )


def _ok_call(
    project: ResolvedProject, candidates: list[Candidate], result: MatchResult, elapsed: float
) -> LlmCallRecord:
    return LlmCallRecord(
        requested_model=result.requested_model,
        returned_model=result.returned_model,
        prompt_tag=PROMPT_TAG,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        latency_ms=round(elapsed * 1000),
        outcome="ok",
        detail_json=_detail(project, candidates, result),
    )


def _failed_call(
    config: Config,
    project: ResolvedProject,
    candidates: list[Candidate],
    exc: Exception,
    elapsed: float,
    result: MatchResult | None,
) -> LlmCallRecord:
    """A failed attempt; a parsed-but-invalid result still keeps its model metadata."""
    failure = exc.failure if isinstance(exc, MatcherError) else "invalid_result"
    return LlmCallRecord(
        requested_model=result.requested_model if result else config.jev_model,
        returned_model=result.returned_model if result else None,
        prompt_tag=PROMPT_TAG,
        input_tokens=result.input_tokens if result else None,
        output_tokens=result.output_tokens if result else None,
        latency_ms=round(elapsed * 1000),
        outcome=failure,
        error=mask_text(str(exc))[:500],
        detail_json=_detail(project, candidates, None),
    )
