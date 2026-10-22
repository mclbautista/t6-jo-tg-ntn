"""The worker: poll (commit-before-offset) then drain (capture pipeline).

One asyncio process, one ledger, one Notion writer. The poller and drainer run
sequentially in ``run``/``run_once``; the commit-before-offset invariant lives
entirely in ``Ledger.accept_message`` (single transaction).
"""

from __future__ import annotations

import asyncio
import json
import logging

from jo_watchdog.adapters.protocols import NotionClient, TelegramClient
from jo_watchdog.app.config import Config
from jo_watchdog.core.masker import outbound_text
from jo_watchdog.core.payload import (
    build_notion_payload,
    extract_readback_fields,
    verify_readback,
)
from jo_watchdog.core.plan import Plan, build_plan, first_line_of
from jo_watchdog.ledger.db import Ledger, telegram_key
from jo_watchdog.ledger.states import State

log = logging.getLogger("jo_watchdog.worker")


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
        # Plans held while dry-run was on resume once it is off; `planned` is
        # before any external call, so resuming is safe.
        if not self.config.dry_run:
            for key in self.ledger.pending(State.PLANNED):
                await self.process(key)
        for key in self.ledger.pending(State.RECEIVED):
            await self.process(key)

    async def process(self, key: str) -> None:
        msg = self.ledger.get_message(key)
        if msg is None:
            return

        if msg["state"] == State.RECEIVED.value:
            # Plan is built and stored before any external call.
            plan = build_plan(name=msg["first_line"], telegram_key=key, raw_text=msg["text"])
            if not self.ledger.transition(key, State.RECEIVED, State.PLANNED):
                return
            self.ledger.store_plan(key, plan.model_dump())
            if self.config.dry_run:
                self.report_dry_run(key, plan)
                return
        elif msg["state"] == State.PLANNED.value and msg["plan_json"]:
            plan = Plan.model_validate_json(msg["plan_json"])
        else:
            return

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

    def report_dry_run(self, key: str, plan: Plan) -> None:
        """Dry-run stops at `planned`: show the masked plan, write nothing.

        The `dry_run_plan` outbox kind lands with #23; until then the plan is
        reported through the masked log.
        """
        text = outbound_text(render_dry_run_plan(key, plan)).text
        log.info("dry_run_plan %s", text)

    # -- loop --------------------------------------------------------------

    async def run_once(self) -> None:
        await self.poll_once()
        await self.drain_once()

    async def run(self) -> None:
        while True:
            try:
                await self.run_once()
            except Exception:
                log.exception("pipeline cycle failed; retrying next cycle")
            await asyncio.sleep(self.config.poll_interval)


def render_dry_run_plan(key: str, plan: Plan) -> str:
    """The human-readable dry-run plan (Name, then each planned property)."""
    lines = [f"🧪 Dry run — would capture {key}", f"Name: {plan.name}"]
    for field, value in plan.model_dump().items():
        if field in ("name", "body_blocks"):
            continue
        lines.append(f"{field}: {value}")
    lines.append(f"Body: {len(plan.body_blocks)} block(s)")
    return "\n".join(lines)
