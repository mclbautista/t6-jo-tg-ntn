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
from datetime import UTC, datetime, timedelta

from jo_watchdog.adapters.protocols import Classifier, NotionClient, TelegramClient
from jo_watchdog.app.caches import CacheManager
from jo_watchdog.app.config import Config
from jo_watchdog.core.duplicates import (
    EarlierCapture,
    append_watchdog_line,
    find_duplicate,
    jo_key,
    notes_line,
)
from jo_watchdog.core.edits import EditAction, decide_edit
from jo_watchdog.core.extraction import forward_origin_lines
from jo_watchdog.core.header import parse_header, parse_number
from jo_watchdog.core.payload import (
    build_notion_payload,
    extract_readback_fields,
    page_url_for,
    verify_readback,
)
from jo_watchdog.core.plan import Plan, build_plan, first_line_of
from jo_watchdog.core.provenance import parse_forward_origin, source_block
from jo_watchdog.core.report import (
    forward_origin_from_raw,
    intake_link,
    render_capture_report,
    render_edit_after_capture,
    render_stale_cache,
)
from jo_watchdog.core.verify import MANILA, CaptureFacts, EarlierRow, Finding, verify
from jo_watchdog.ledger.db import Ledger, OutboxMessage, telegram_key
from jo_watchdog.ledger.states import State

log = logging.getLogger("jo_watchdog.worker")

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


def _raw_update(msg: dict) -> dict:
    try:
        raw = json.loads(msg["raw_source"])
    except (TypeError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _utc_iso(unix: int | None) -> str:
    when = datetime.fromtimestamp(unix, UTC) if unix is not None else datetime.now(UTC)
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def _ledger_time(row: dict) -> datetime:
    """A ledger row's posting time, else its acceptance time (both timezone-aware)."""
    return datetime.fromisoformat(row.get("posted_at") or row["created_at"])


def _posted_at(msg: dict, raw: dict) -> datetime | None:
    """The intake posting time: the ledger's ``posted_at``, else Telegram's ``date``."""
    if msg.get("posted_at"):
        return datetime.fromisoformat(msg["posted_at"])
    value = (raw.get("message") or {}).get("date")
    if isinstance(value, int | float):
        return datetime.fromtimestamp(value, MANILA)
    return None


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
            if u.edited:
                self.handle_edit(u)
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

    # -- edits (#24) -------------------------------------------------------

    def handle_edit(self, u) -> None:
        """Record an in-scope edit per the #24 edit table; never touch the row.

        A correction needs a fresh Telegram message, which gets its own
        ``Telegram Key`` and row.
        """
        if self.ledger.edit_seen(u.update_id):
            self.ledger.advance_offset(u.update_id)
            return
        key = telegram_key(u.chat_id, u.message_id)
        msg = self.ledger.get_message(key)
        action = decide_edit(msg["state"] if msg else None)
        if action is EditAction.UNKNOWN:
            self.ledger.ignore(
                u.chat_id, u.message_id, u.update_id, action.value, first_line_of(u.text)
            )
            return
        report = None
        if action is EditAction.REPORT:
            report = self._edit_report(msg, page_url_for(msg["notion_page_id"]))
        self.ledger.record_edit(
            key,
            u.update_id,
            edited_at=_utc_iso(u.edit_date),
            outcome=action.value,
            report=report,
        )

    def _edit_report(self, msg: dict, page_url: str, *, name: str | None = None) -> OutboxMessage:
        if name is None:
            plan = json.loads(msg["plan_json"]) if msg.get("plan_json") else {}
            name = plan.get("name") or msg["first_line"]
        return OutboxMessage(
            kind="edit_after_capture",
            chat_id=self.config.ops_chat_id,
            topic_id=self.config.ops_topic_id,
            text=render_edit_after_capture(
                name=name,
                page_url=page_url,
                intake_url=intake_link(
                    msg["chat_id"], self.config.intake_topic_id, msg["message_id"]
                ),
            ),
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
        plan = build_plan(
            name=msg["first_line"],
            telegram_key=key,
            raw_text=msg["text"],
            source_block=self._source_block(msg),
        )
        dup_key = self._annotate_duplicate(msg, plan)
        if not self.ledger.transition(key, State.EXTRACTED, State.PLANNED):
            return
        self.ledger.store_plan(key, plan.model_dump(), jo_key=dup_key)

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
        if self.ledger.has_pending_edits(key):
            # Edited while still in the pipeline: the row holds the original
            # text, and the edit is reported once, now that it is captured.
            reports.append(self._edit_report(msg, page_ref.url, name=plan.name))
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

    def _source_block(self, msg: dict) -> str:
        """The provenance block that leads the body (#24)."""
        message = _raw_update(msg).get("message") or {}
        if "date" not in message:
            return ""
        contact = None
        origin = parse_forward_origin(message)
        if self.caches is not None and origin is not None and origin.sender_user is not None:
            contact = self.caches.contact_for_telegram_user(
                origin.sender_user.id, origin.sender_user.username
            )
        return source_block(
            message,
            intake_url=intake_link(msg["chat_id"], self.config.intake_topic_id, msg["message_id"]),
            contact_name=contact,
        )

    def _jo_key(self, msg: dict) -> tuple[str | None, str | None]:
        """The content-duplicate key and the JO Number as posted.

        The key needs a resolved Project page, a live Department option and a
        parsed number; anything less returns no key, so reuse detection skips.
        """
        if self.caches is None or not self.caches.ready:
            return None, None
        stage = self.ledger.stage_result(msg["telegram_key"], "extract") or {}
        extraction = stage.get("extraction") or {}
        header = parse_header(
            extraction.get("header_raw") or msg["first_line"],
            self.caches.settings.department_aliases,
        )
        if header is not None:
            project_raw, department_token, digits = header.project, header.department, header.number
        else:
            project_raw = extraction.get("project_raw")
            department_token = extraction.get("department_raw")
            digits = parse_number(extraction.get("jo_number_raw"))
        if digits is None:
            return None, None
        project = self.caches.resolve_project(project_raw)
        department = self.caches.resolve_department(department_token)
        return jo_key(project.page_id, department.department, int(digits)), f"#{digits}"

    def _annotate_duplicate(self, msg: dict, plan: Plan) -> str | None:
        """Look for an earlier exact re-post or reused JO Number; return the jo_key.

        A match never blocks or merges: the new row is still written, with an
        ``Earlier row:`` Notes line and a finding stored in the plan.
        """
        key = msg["telegram_key"]
        dup_key, jo_number = self._jo_key(msg)
        posted_at = _ledger_time(msg)
        earlier = [
            EarlierCapture(
                telegram_key=row["telegram_key"],
                text_sha256=row["text_sha256"],
                jo_key=row["jo_key"],
                page_url=page_url_for(row["notion_page_id"]),
                posted_at=_ledger_time(row),
            )
            for row in self.ledger.earlier_captures(
                key, text_sha=msg["text_sha256"], jo_key=dup_key
            )
        ]
        match = find_duplicate(
            telegram_key=key,
            text_sha=msg["text_sha256"],
            key=dup_key,
            posted_at=posted_at,
            earlier=earlier,
        )
        if match is not None:
            plan.notes = append_watchdog_line(plan.notes, notes_line(match))
            plan.duplicate = {
                "code": match.kind,
                "url": match.earlier.page_url,
                "days": match.days_ago,
                "jo_number": jo_number,
            }
        return dup_key

    def _capture_facts(self, msg: dict, plan, mismatches: list[str]) -> CaptureFacts:
        """The facts verification can see for this capture.

        The extraction (#20) supplies the verdict, reasons, Title, Task and
        header date. Planner and resolver findings (#21, #22) join through
        ``plan.findings`` once the pipeline plans with them; until then those
        checks have no input and stay silent.
        """
        raw = _raw_update(msg)
        stage = self.ledger.stage_result(msg["telegram_key"], "extract") or {}
        extraction = stage.get("extraction") or {}
        duplicate = getattr(plan, "duplicate", None) or {}
        earlier = (
            EarlierRow(
                url=duplicate["url"], days=duplicate["days"], jo_number=duplicate.get("jo_number")
            )
            if duplicate
            else None
        )
        return CaptureFacts(
            posted_at=_posted_at(msg, raw),
            verdict=extraction.get("verdict"),
            reasons=extraction.get("reasons"),
            multiple_jo_suspected=bool(extraction.get("multiple_jo_suspected")),
            title=extraction.get("title"),
            task=extraction.get("task"),
            header_date_raw=extraction.get("header_date_raw"),
            exact_repost=earlier if duplicate.get("code") == "exact_repost" else None,
            number_reuse=earlier if duplicate.get("code") == "number_reuse" else None,
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

    async def refresh_caches(self) -> None:
        if self.caches is None:
            return
        for stale in await self.caches.refresh_if_due():
            self.ledger.enqueue_system(
                OutboxMessage(
                    kind="stale_cache",
                    chat_id=self.config.ops_chat_id,
                    topic_id=self.config.ops_topic_id,
                    text=render_stale_cache(
                        cache=stale.kind.capitalize(),
                        last_refreshed=datetime.strptime(
                            stale.fetched_at, "%Y-%m-%dT%H:%M:%SZ"
                        ).replace(tzinfo=UTC),
                        now=datetime.now(UTC),
                        error=stale.error,
                    ),
                )
            )
            log.warning(
                "%s cache is stale: last refreshed %s (%.1f hours ago): %s",
                stale.kind,
                stale.fetched_at,
                stale.hours_stale,
                stale.error,
            )

    async def run_once(self) -> None:
        try:
            await self.refresh_caches()
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
