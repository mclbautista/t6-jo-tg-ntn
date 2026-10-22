"""The worker: poll (commit-before-offset) then drain (capture pipeline).

One asyncio process, one ledger, one Notion writer. Draining classifies each
``received`` message through DeepSeek, then plans and writes the ``extracted``
ones. The poller and drainer run
sequentially in ``run``/``run_once``; the commit-before-offset invariant lives
entirely in ``Ledger.accept_message`` (single transaction).
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta, timezone

from jo_watchdog.adapters.protocols import Classifier, NotionClient, TelegramClient
from jo_watchdog.app.caches import CacheManager
from jo_watchdog.app.config import Config
from jo_watchdog.core.extraction import forward_origin_lines
from jo_watchdog.core.payload import (
    build_notion_payload,
    extract_readback_fields,
    verify_readback,
)
from jo_watchdog.core.plan import build_plan, first_line_of
from jo_watchdog.ledger.db import Ledger, telegram_key
from jo_watchdog.ledger.states import State

log = logging.getLogger("jo_watchdog.worker")

MANILA = timezone(timedelta(hours=8))
# Fixed classifier retry delay until #25 brings the 30s→15m schedule.
CLASSIFIER_RETRY_SECONDS = 30
_TRACE_FIELDS = (
    "requested_model",
    "returned_model",
    "prompt_tag",
    "input_tokens",
    "output_tokens",
    "latency_ms",
)


def posted_at_from_raw(raw: dict) -> str | None:
    """Telegram's message date as ISO-8601 in Manila time (``+08:00``)."""
    date = (raw.get("message") or {}).get("date")
    if isinstance(date, int):
        return datetime.fromtimestamp(date, MANILA).isoformat()
    return None


def _utc_after(seconds: int) -> str:
    return (datetime.now(UTC) + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


class Worker:
    def __init__(
        self,
        *,
        ledger: Ledger,
        telegram: TelegramClient,
        notion: NotionClient,
        classifier: Classifier,
        config: Config,
        caches: CacheManager | None = None,
    ):
        self.ledger = ledger
        self.telegram = telegram
        self.notion = notion
        self.classifier = classifier
        self.config = config
        self.caches = caches

    # -- scope -------------------------------------------------------------

    def in_scope(self, update) -> bool:
        return (
            update.chat_id == self.config.intake_chat_id
            and update.message_thread_id == self.config.intake_topic_id
        )

    def _scope_reason(self, update) -> str:
        if update.chat_id != self.config.intake_chat_id:
            return "out_of_scope_chat"
        return "out_of_scope_topic"

    # -- polling (commit before offset) ------------------------------------

    async def poll_once(self) -> None:
        offset = self.ledger.next_offset()
        updates = await self.telegram.get_updates(offset=offset, timeout=self.config.poll_timeout)
        for u in updates:
            if not self.in_scope(u):
                self.ledger.ignore(
                    u.chat_id,
                    u.message_id,
                    u.update_id,
                    self._scope_reason(u),
                    first_line_of(u.text),
                )
                continue
            key = telegram_key(u.chat_id, u.message_id)
            if self.ledger.has_message(key):
                # Redelivery (offset N+1 re-fetches the last update): the row
                # already exists, so just make sure the offset is past it.
                self.ledger.advance_offset(u.update_id)
                continue
            self.ledger.accept_message(
                u.chat_id,
                u.message_id,
                u.update_id,
                json.dumps(u.raw),
                u.text,
                first_line_of(u.text),
                posted_at=posted_at_from_raw(u.raw),
            )

    # -- draining (pipeline) ----------------------------------------------

    async def drain_once(self) -> None:
        for key in self.ledger.due(State.RECEIVED):
            await self.process(key)
        for key in self.ledger.pending(State.EXTRACTED):
            await self.process(key)

    async def classify(self, msg: dict) -> State:
        """Run the classify/extract stage for a ``received`` message; return its new state."""
        key = msg["telegram_key"]
        if not msg["text"].strip():
            self.ledger.record_no_text(key)
            return State.IGNORED

        raw_message = json.loads(msg["raw_source"]).get("message") or {}
        result = await self.classifier.classify(
            msg["masked_text"], forward_origin_lines(raw_message)
        )
        trace = result.model_dump(exclude={"extraction"})
        call = {
            "provider": "deepseek",
            "purpose": "classify_extract",
            "masker_version": msg["masker_version"],
            **trace,
        }
        if result.extraction is None:
            # No repair call and no bare-row fallback: the message stays queued.
            self.ledger.record_classifier_failure(
                key, call=call, retry_at=_utc_after(CLASSIFIER_RETRY_SECONDS)
            )
            log.warning("classifier %s for %s; will retry", result.outcome, key)
            return State.RECEIVED

        extraction = result.extraction
        findings: list[dict] = []
        if extraction.verdict == "unsure":
            findings.append({"code": "unsure", "reasons": extraction.reasons})
        if extraction.multiple_jo_suspected and extraction.verdict != "not_jo":
            # One row from the first extraction; the full source stays in the body.
            findings.append({"code": "multiple_jo", "reasons": extraction.reasons})
        stage_result = {
            "extraction": extraction.model_dump(),
            "findings": findings,
            "trace": {field: trace[field] for field in _TRACE_FIELDS}
            | {"masker_version": msg["masker_version"]},
        }
        return self.ledger.record_extraction(key, call=call, result=stage_result)

    async def process(self, key: str) -> None:
        msg = self.ledger.get_message(key)
        if msg is None:
            return
        if msg["state"] == State.RECEIVED.value:
            if await self.classify(msg) is not State.EXTRACTED:
                return
        elif msg["state"] != State.EXTRACTED.value:
            return
        if self.caches is not None and not self.caches.ready:
            # No People/Projects/Department snapshot has ever loaded: hold at
            # `extracted` (no attempt spent, no false unresolved finding).
            return

        # Plan is built and stored before any external call.
        plan = build_plan(name=msg["first_line"], telegram_key=key, raw_text=msg["text"])
        if not self.ledger.transition(key, State.EXTRACTED, State.PLANNED):
            return
        self.ledger.store_plan(key, plan.model_dump())

        # Move to `writing` before the create call (closes the crash window).
        if not self.ledger.mark_writing(key):
            return
        payload = build_notion_payload(
            plan, data_source_id=self.config.job_orders_data_source_id
        )
        page_ref = await self.notion.create_page(payload)  # raises -> stays `writing`

        # Read back and compare; a mismatch is recorded, never rewritten.
        retrieved = await self.notion.retrieve_page(page_ref.id)
        fields = extract_readback_fields(retrieved)
        mismatches = verify_readback(
            plan,
            retrieved_name=fields["name"],
            retrieved_telegram_key=fields["telegram_key"],
        )
        self.ledger.mark_written(key, page_ref.id)
        if mismatches:
            self.ledger.record_mismatch(key, "; ".join(mismatches))
            log.warning("read-back mismatch for %s: %s", key, "; ".join(mismatches))

        # The captured reaction fires only after a confirmed write and read-back.
        await self.telegram.set_reaction(
            msg["chat_id"], msg["message_id"], self.config.captured_reaction
        )
        self.ledger.mark_done(key)

    # -- loop --------------------------------------------------------------

    async def refresh_caches(self) -> None:
        if self.caches is None:
            return
        for stale in await self.caches.refresh_if_due():
            # The ops-topic message for this is rendered and queued by #23.
            log.warning(
                "%s cache is stale: last refreshed %s (%.1f hours ago): %s",
                stale.kind,
                stale.fetched_at,
                stale.hours_stale,
                stale.error,
            )

    async def run_once(self) -> None:
        await self.refresh_caches()
        await self.poll_once()
        await self.drain_once()

    async def run(self) -> None:
        while True:
            try:
                await self.run_once()
            except Exception:
                log.exception("pipeline cycle failed; retrying next cycle")
            await asyncio.sleep(self.config.poll_interval)
