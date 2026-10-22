"""The worker: poll (commit-before-offset) then drain (capture pipeline).

One asyncio process, one ledger, one Notion writer. The poller and drainer run
sequentially in ``run``/``run_once``; the commit-before-offset invariant lives
entirely in ``Ledger.accept_message`` (single transaction).
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime

from jo_watchdog.adapters.protocols import NotionClient, TelegramClient
from jo_watchdog.app.config import Config
from jo_watchdog.core.payload import (
    build_notion_payload,
    extract_readback_fields,
    verify_readback,
)
from jo_watchdog.core.plan import build_plan, first_line_of
from jo_watchdog.core.report import forward_origin_from_raw, intake_link, render_capture_report
from jo_watchdog.core.verify import MANILA, CaptureFacts, Finding, verify
from jo_watchdog.ledger.db import Ledger, OutboxMessage, telegram_key
from jo_watchdog.ledger.states import State

log = logging.getLogger("jo_watchdog.worker")


def _raw_update(msg: dict) -> dict:
    try:
        raw = json.loads(msg["raw_source"])
    except (TypeError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _posted_at(raw: dict) -> datetime | None:
    """The intake message's Telegram ``date`` (unix seconds or ISO) in Manila time."""
    value = (raw.get("message") or {}).get("date")
    if isinstance(value, int | float):
        return datetime.fromtimestamp(value, MANILA)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value).astimezone(MANILA)
        except ValueError:
            return None
    return None


class Worker:
    def __init__(
        self,
        *,
        ledger: Ledger,
        telegram: TelegramClient,
        notion: NotionClient,
        config: Config,
    ):
        self.ledger = ledger
        self.telegram = telegram
        self.notion = notion
        self.config = config

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
            )

    # -- draining (pipeline) ----------------------------------------------

    async def drain_once(self) -> None:
        for key in self.ledger.pending(State.RECEIVED):
            await self.process(key)

    async def process(self, key: str) -> None:
        msg = self.ledger.get_message(key)
        if msg is None:
            return

        # Plan is built and stored before any external call.
        plan = build_plan(name=msg["first_line"], telegram_key=key, raw_text=msg["text"])
        if not self.ledger.transition(key, State.RECEIVED, State.PLANNED):
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

        # Verification annotates, never gates: findings and their report are
        # committed in the same transaction that marks the row written.
        findings = verify(
            self._capture_facts(msg, plan, mismatches),
            header_date_tolerance_days=self.config.header_date_tolerance_days,
        )
        reports = self._capture_reports(msg, plan, page_ref.url, findings)
        self.ledger.mark_written(
            key,
            page_ref.id,
            findings=[f.model_dump() for f in findings],
            reports=reports,
        )
        if mismatches:
            self.ledger.record_mismatch(key, "; ".join(mismatches))
            log.warning("read-back mismatch for %s: %s", key, "; ".join(mismatches))

        # The captured reaction fires only after a confirmed write and read-back.
        # Its failure never touches the outbox and never changes its meaning.
        try:
            await self.telegram.set_reaction(
                msg["chat_id"], msg["message_id"], self.config.captured_reaction
            )
        except Exception as exc:
            self.ledger.set_reaction_state(key, "failed")
            log.warning("captured reaction failed for %s: %s", key, type(exc).__name__)
        else:
            self.ledger.set_reaction_state(key, "set")
        self.ledger.mark_done(key)

    def _capture_facts(self, msg: dict, plan, mismatches: list[str]) -> CaptureFacts:
        """The facts verification can see for this capture.

        Extraction, planning and resolution facts join here as #20–#26 land;
        until then those checks have no input and stay silent.
        """
        raw = _raw_update(msg)
        return CaptureFacts(
            posted_at=_posted_at(raw),
            forward_origin=forward_origin_from_raw(raw),
            readback_mismatches=mismatches,
            plan_findings=[
                Finding.model_validate(f, from_attributes=True)
                for f in getattr(plan, "findings", [])
            ],
        )

    def _capture_reports(
        self, msg: dict, plan, page_url: str, findings: list[Finding]
    ) -> list[OutboxMessage]:
        origin = forward_origin_from_raw(_raw_update(msg))
        text = render_capture_report(
            name=plan.name,
            findings=findings,
            page_url=page_url,
            intake_url=intake_link(
                msg["chat_id"], self.config.intake_topic_id, msg["message_id"]
            ),
            original_url=origin.original_link if origin else None,
            leizette_handle=self.config.leizette_telegram_handle,
        )
        if text is None:
            return []
        return [
            OutboxMessage(
                kind="capture_report",
                chat_id=self.config.ops_chat_id,
                topic_id=self.config.ops_topic_id,
                text=text,
            )
        ]

    # -- ops outbox (at-least-once) ----------------------------------------

    async def deliver_outbox(self) -> None:
        """Post every due outbox row; a crash before ``outbox_sent`` re-posts it."""
        for row in self.ledger.due_outbox():
            try:
                sent_id = await self.telegram.send_message(
                    row["chat_id"], row["topic_id"], row["text"]
                )
            except Exception as exc:
                state = self.ledger.outbox_failed_attempt(row["id"], str(exc))
                log.warning(
                    "ops post outbox=%s kind=%s failed (%s): now %s",
                    row["id"],
                    row["kind"],
                    type(exc).__name__,
                    state,
                )
                continue
            self.ledger.outbox_sent(row["id"], sent_id)
            log.info(
                "ops post outbox=%s kind=%s sent as telegram message %s",
                row["id"],
                row["kind"],
                sent_id,
            )

    # -- loop --------------------------------------------------------------

    async def run_once(self) -> None:
        try:
            await self.poll_once()
            await self.drain_once()
        finally:
            # A capture stuck on a failing dependency must not starve ops posts.
            await self.deliver_outbox()

    async def run(self) -> None:
        while True:
            try:
                await self.run_once()
            except Exception:
                log.exception("pipeline cycle failed; retrying next cycle")
            await asyncio.sleep(self.config.poll_interval)
