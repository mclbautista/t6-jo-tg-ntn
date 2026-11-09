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
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from jo_watchdog.adapters.protocols import (
    Classifier,
    DeliverableMatcher,
    DeliverableSource,
    GatewayRef,
    NotionClient,
    PageRef,
    TelegramClient,
)
from jo_watchdog.app.caches import CacheManager
from jo_watchdog.app.config import Config
from jo_watchdog.app.matching import ResolvedProject, run_match_stage, with_deliverable
from jo_watchdog.core.duplicates import (
    EarlierCapture,
    add_earlier_row_line,
    find_duplicate,
    jo_key,
    notes_line,
)
from jo_watchdog.core.edits import EditAction, decide_edit
from jo_watchdog.core.extraction import Extraction, forward_origin_lines
from jo_watchdog.core.findings import Finding as PlanFinding
from jo_watchdog.core.header import parse_header
from jo_watchdog.core.heartbeat import heartbeat_window
from jo_watchdog.core.job_order import (
    JobOrderPlan,
    apply_resolution,
    build_job_order_payload,
    load_plan,
    plan_job_order,
)
from jo_watchdog.core.masker import outbound_text
from jo_watchdog.core.payload import (
    extract_readback_fields,
    page_url_for,
    verify_readback,
)
from jo_watchdog.core.plan import first_line_of, header_line_of
from jo_watchdog.core.provenance import parse_forward_origin, source_block
from jo_watchdog.core.report import (
    forward_origin_from_raw,
    intake_link,
    render_capture_report,
    render_dry_run_plan,
    render_edit_after_capture,
    render_heartbeat,
    render_heartbeat_failed_alert,
    render_message_gap,
    render_outage_alert,
    render_stale_cache,
)
from jo_watchdog.core.resolve import mention_entities
from jo_watchdog.core.verify import MANILA, CaptureFacts, EarlierRow, Finding, verify
from jo_watchdog.ledger.db import Ledger, OutboxMessage, fmt_utc, telegram_key
from jo_watchdog.ledger.states import State

log = logging.getLogger("jo_watchdog.worker")

# A queued post the gateway has not sent after this long is logged as stuck (#113).
STUCK_AFTER = timedelta(hours=1)

# A failed reaction is retried inline after these delays, then counted.
REACTION_RETRY_SECONDS = (2, 5)
DEADMAN_CHECK_SECONDS = 30.0

CLASSIFICATION = "Classification"
NOTION_WRITE = "Notion write"
NOTION_READ_BACK = "Notion read-back"
NOTION_RECONCILIATION = "Notion reconciliation"
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


class StepFailure(Exception):
    """An external step failed; the message stays in its state and is retried (#25)."""

    def __init__(self, step: str, cause: BaseException):
        super().__init__(f"{step}: {type(cause).__name__}: {cause}")
        self.step = step
        self.cause = cause


class DeadmanExit(Exception):
    """The poller made no progress for too long; the process must exit non-zero."""


def _utcnow() -> datetime:
    return datetime.now(UTC)


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
        matcher: DeliverableMatcher | None = None,
        deliverables: DeliverableSource | None = None,
        clock: Callable[[], datetime] = _utcnow,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        deadman_interval: float = DEADMAN_CHECK_SECONDS,
    ):
        self.ledger = ledger
        self.telegram = telegram
        self.notion = notion
        self.classifier = classifier
        self.config = config
        self.caches = caches
        self.matcher = matcher
        self.deliverables = deliverables
        self.clock = clock
        self.sleep = sleep
        self.deadman_interval = deadman_interval
        self.stuck_warned: set[int] = set()  # outbox rows already warned about as stuck

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
                    alert=None if u.edited else self._gap_alert(u),
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
                alert=self._gap_alert(u),
            )

    def _gap_alert(self, u) -> OutboxMessage | None:
        """Warn when an intake-chat message skips ids the ledger never saw (#101).

        Message ids are one counter per chat, so observed posts in every topic count
        as seen. Unseen ids can belong to other topics or indicate possible loss.
        Must run before the update itself is recorded.
        """
        if not self.config.message_gap_alerts or u.chat_id != self.config.intake_chat_id:
            return None
        last_seen = self.ledger.last_seen_message_id(u.chat_id)
        if last_seen is None or u.message_id <= last_seen + 1:
            return None
        log.warning(
            "possible message loss in the intake chat: unseen ids %d to %d"
            " (last seen %d, now %d); gaps may be posts in other topics",
            last_seen + 1,
            u.message_id - 1,
            last_seen,
            u.message_id,
        )
        return OutboxMessage(
            kind="message_gap",
            chat_id=self.config.ops_chat_id,
            topic_id=self.config.ops_topic_id,
            text=render_message_gap(
                chat_id=u.chat_id,
                topic_id=self.config.intake_topic_id,
                last_seen=last_seen,
                message_id=u.message_id,
            ),
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
        now = self.clock()
        stamp = fmt_utc(now)
        # A row left in `writing` by a crash waits at least the first retry delay
        # before it is reconciled against Notion (the create may still be landing).
        settle = fmt_utc(now - timedelta(seconds=self.config.retry_schedule_seconds[0]))
        due: list[str] = []
        # Plans held while dry-run was on resume once it is off; `planned` is
        # before any external call, so resuming is safe.
        if not self.config.dry_run:
            due += self.ledger.due(State.PLANNED, stamp)
        due += self.ledger.due(State.WRITING, stamp, settle_before=settle)
        due += self.ledger.due(State.WRITTEN, stamp)
        due += self.ledger.due(State.RECEIVED, stamp)
        due += self.ledger.due(State.EXTRACTED, stamp)
        for key in due:
            await self.process(key)
            self._beat()

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
        trace = result.model_dump(exclude={"extraction", "retry_after"})
        call = {
            "provider": "deepseek",
            "purpose": "classify_extract",
            "masker_version": msg["masker_version"],
            **trace,
        }
        if result.extraction is None:
            # No repair call and no bare-row fallback: the message stays queued.
            failure = self.ledger.record_classifier_failure(
                key,
                call=call,
                schedule=self.config.retry_schedule_seconds,
                now=self.clock(),
                retry_after=result.retry_after,
                on_exhausted=self._outage_alert(msg, CLASSIFICATION),
            )
            if failure.exhausted:
                log.error("classifier %s for %s; retries exhausted", result.outcome, key)
            else:
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
        try:
            await self._process(key)
        except StepFailure as failure:
            self._record_step_failure(key, failure)

    def _outage_alert(self, msg: dict, step: str) -> Callable[[str, int], OutboxMessage]:
        """Builds the one owner-tagged alert, inside the failure's own transaction."""

        def build(error: str, attempts: int) -> OutboxMessage:
            return OutboxMessage(
                kind="outage_alert",
                chat_id=self.config.ops_chat_id,
                topic_id=self.config.ops_topic_id,
                text=render_outage_alert(
                    step=step,
                    intake_url=intake_link(
                        msg["chat_id"], self.config.intake_topic_id, msg["message_id"]
                    ),
                    error=error,
                    attempts=attempts,
                    given_up_at=self.clock(),
                    telegram_key=msg["telegram_key"],
                    owner_handle=self.config.owner_telegram_handle,
                ),
            )

        return build

    def _record_step_failure(self, key: str, failure: StepFailure) -> None:
        msg = self.ledger.get_message(key)
        if msg is None:
            return
        result = self.ledger.record_failure(
            key,
            error=f"{type(failure.cause).__name__}: {failure.cause}",
            schedule=self.config.retry_schedule_seconds,
            now=self.clock(),
            retry_after=getattr(failure.cause, "retry_after", None),
            on_exhausted=self._outage_alert(msg, failure.step),
        )
        if result.exhausted:
            log.error("%s for %s failed %d times; given up", failure.step, key, result.attempts)
        else:
            log.warning(
                "%s for %s failed (attempt %d); retry at %s",
                failure.step,
                key,
                result.attempts,
                result.next_attempt_at,
            )

    async def _process(self, key: str) -> None:
        msg = self.ledger.get_message(key)
        if msg is None:
            return
        if msg["state"] == State.WRITTEN.value:
            await self._after_written(msg)
            return
        if msg["state"] == State.WRITING.value:
            await self._resume_writing(msg)
            return
        if msg["state"] == State.PLANNED.value:
            # A plan held by dry-run: write exactly what was stored.
            if self.config.dry_run or not msg["plan_json"]:
                return
            plan = load_plan(msg["plan_json"])
        else:
            if msg["state"] == State.RECEIVED.value:
                if await self.classify(msg) is not State.EXTRACTED:
                    return
            elif msg["state"] != State.EXTRACTED.value:
                return
            if self.caches is not None and not self.caches.ready:
                # No People/Projects/Department snapshot has ever loaded: hold at
                # `extracted` (no attempt spent, no false unresolved finding).
                return

            # Plan and resolution are decided here, once, before any external call.
            plan = self._build_plan(msg)
            if plan is None:
                return
            dup_key = self._annotate_duplicate(msg, plan)
            match = await self._match(msg, plan)
            # State, plan and match records commit together: a crash cannot leave
            # `planned` empty, and a resumed plan never repeats the Jev call.
            if not self.ledger.plan_message(
                key,
                plan.model_dump(mode="json"),
                jo_key=dup_key,
                stage_results={"match": json.loads(match.stage_result_json())} if match else None,
                llm_calls=[c.model_dump() for c in match.llm_calls] if match else None,
            ):
                return
            if self.config.dry_run:
                self._report_dry_run(msg, plan)
                return

        # Move to `writing` before the create call (closes the crash window).
        if not self.ledger.mark_writing(key):
            return
        await self._create(msg, plan)

    def _build_plan(self, msg: dict) -> JobOrderPlan | None:
        """Plan one extracted message: the #21 planner fed by the #22 resolvers.

        Without caches nothing is resolved: no relations, no Department check
        and no shift deadlines.
        """
        key = msg["telegram_key"]
        stored = (self.ledger.stage_result(key, "extract") or {}).get("extraction")
        if not stored:
            log.error("no stored extraction for %s; cannot plan", key)
            return None
        extraction = Extraction.model_validate(stored)
        message = _raw_update(msg).get("message") or {}
        caches = self.caches
        aliases = caches.settings.department_aliases if caches is not None else {}

        people = project = department = None
        if caches is not None:
            header = parse_header(header_line_of(msg["text"]), aliases)
            if header is not None:
                project_raw, department_token = header.project, header.department
            else:
                project_raw, department_token = extraction.project_raw, extraction.department_raw
            people = caches.resolve_people(
                extraction.assignees_raw, extraction.cc_raw, mention_entities(message)
            )
            project = caches.resolve_project(project_raw)
            department = caches.resolve_department(department_token)

        plan = plan_job_order(
            extraction,
            telegram_key=key,
            raw_text=msg["text"],
            posted_at=_posted_at(msg, _raw_update(msg)) or _ledger_time(msg),
            source_block=self._source_block(msg),
            department_aliases=aliases,
            department_resolution=department,
            assignee_shifts=[(s.name, s.end_of_shift) for s in people.assignee_shifts]
            if people is not None
            else (),
        )
        if people is not None:
            apply_resolution(plan, people=people, project=project, department=department)
        return plan

    async def _match(self, msg: dict, plan: JobOrderPlan):
        """Run the #26 Deliverable match stage and fold its outcome into the plan.

        It never raises and never blocks the write: a matcher failure leaves
        Deliverable blank and adds one ``matcher_skipped`` finding. In shadow
        mode (the default) decisions and hints are kept but no relation is added.
        """
        if self.matcher is None or self.deliverables is None:
            return None
        project = None
        page_id = plan.resolution.project_page_id if plan.resolution else None
        name = self.caches.project_name(page_id) if self.caches is not None else None
        if page_id and name:
            project = ResolvedProject(page_id=page_id, name=name)
        stage = await run_match_stage(
            matcher=self.matcher,
            deliverables=self.deliverables,
            config=self.config,
            source_text=msg["masked_text"],
            project=project,
            sleep=self.sleep,
        )
        plan.properties = with_deliverable({"properties": plan.properties}, stage)["properties"]
        if stage.outcome == "skipped":
            plan.findings.append(PlanFinding(code="matcher_skipped"))
        for hint in stage.decision.hints if stage.decision else []:
            plan.findings.append(
                PlanFinding(
                    code="deliverable_hint",
                    params={"name": hint.name, "probability": hint.p_match},
                )
            )
        plan.match = json.loads(stage.stage_result_json())
        return stage

    async def _create(self, msg: dict, plan: JobOrderPlan) -> None:
        key = msg["telegram_key"]
        payload = build_job_order_payload(
            plan, data_source_id=self.config.job_orders_data_source_id
        )
        try:
            page_ref = await self.notion.create_page(payload)  # failure -> stays `writing`
        except Exception as exc:
            raise StepFailure(NOTION_WRITE, exc) from exc
        # The id is kept while still `writing`: a later read-back failure then
        # retries only the retrieve, never a second create.
        self.ledger.store_page_id(key, page_ref.id)
        await self._read_back(msg, plan, page_ref)

    async def _resume_writing(self, msg: dict) -> None:
        """Recover a row left in `writing` (crash or failed attempt).

        With a stored page id only the read-back is outstanding. Without one the
        create is uncertain, so ask Notion by `Telegram Key`: adopt the one match,
        create only when there is none, and report (never merge or delete) more
        than one.
        """
        key = msg["telegram_key"]
        if not msg["plan_json"]:
            return
        plan = load_plan(msg["plan_json"])
        if msg["notion_page_id"]:
            page_id = msg["notion_page_id"]
            await self._read_back(msg, plan, PageRef(id=page_id, url=page_url_for(page_id)))
            return
        try:
            matches = await self.notion.find_pages_by_telegram_key(
                self.config.job_orders_data_source_id, key
            )
        except Exception as exc:
            raise StepFailure(NOTION_RECONCILIATION, exc) from exc
        if not matches:
            await self._create(msg, plan)
            return
        if len(matches) > 1:
            self.ledger.record_stage_result(
                key, "reconcile", {"duplicate_urls": [m.url for m in matches]}
            )
        self.ledger.store_page_id(key, matches[0].id)
        await self._read_back(msg, plan, matches[0])

    async def _read_back(self, msg: dict, plan: JobOrderPlan, page_ref: PageRef) -> None:
        key = msg["telegram_key"]
        # Read back and compare; a mismatch is recorded, never rewritten.
        try:
            retrieved = await self.notion.retrieve_page(page_ref.id)
        except Exception as exc:
            raise StepFailure(NOTION_READ_BACK, exc) from exc
        fields = extract_readback_fields(retrieved)
        mismatches = verify_readback(
            plan,
            retrieved_name=fields["name"],
            retrieved_telegram_key=fields["telegram_key"],
            retrieved_dates=fields["dates"],
        )

        # Verification annotates, never gates: findings and their report are
        # committed in the same transaction that marks the row written.
        reconcile = self.ledger.stage_result(key, "reconcile") or {}
        facts = self._capture_facts(msg, plan, mismatches)
        facts.duplicate_key_urls = reconcile.get("duplicate_urls", [])
        findings = verify(facts, header_date_tolerance_days=self.config.header_date_tolerance_days)
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
        await self._after_written(self.ledger.get_message(key))

    async def _after_written(self, msg: dict) -> None:
        """The capture reaction, then `done`; both are independent of the outbox."""
        key = msg["telegram_key"]
        # The reaction fires only after a confirmed write and read-back. Its
        # failure never touches the outbox, and a failed flagged reaction is
        # never downgraded to the captured one.
        if msg["reaction_state"] != "set":
            # Chosen from the findings `mark_written` stored, so a resumed row
            # gets the same emoji as an uninterrupted one.
            flagged = json.loads(msg["findings_json"] or "[]") != []
            emoji = self.config.flagged_reaction if flagged else self.config.captured_reaction
            for delay in (0, *REACTION_RETRY_SECONDS):
                if delay:
                    await self.sleep(delay)
                try:
                    await self.telegram.set_reaction(
                        msg["chat_id"], msg["message_id"], emoji
                    )
                except Exception as exc:
                    log.warning("reaction failed for %s: %s", key, type(exc).__name__)
                else:
                    self.ledger.set_reaction_state(key, "set")
                    break
            else:
                self.ledger.set_reaction_state(key, "failed")
        self.ledger.mark_done(key)

    def _report_dry_run(self, msg: dict, plan: JobOrderPlan) -> None:
        """Dry-run stops at `planned`: post the masked plan to ops, write nothing."""
        text = render_dry_run_plan(
            plan=plan.model_dump(mode="json"),
            intake_url=intake_link(msg["chat_id"], self.config.intake_topic_id, msg["message_id"]),
        )
        self.ledger.enqueue_system(
            OutboxMessage(
                kind="dry_run_plan",
                chat_id=self.config.ops_chat_id,
                topic_id=self.config.ops_topic_id,
                text=text,
            ),
            key=msg["telegram_key"],
        )
        log.info("dry run: %s held at planned", msg["telegram_key"])

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

    @staticmethod
    def _jo_key(plan: JobOrderPlan) -> tuple[str | None, str | None]:
        """The content-duplicate key and the JO Number as posted, from the stored resolution.

        The key needs a resolved Project page and a parsed number; anything
        less returns no key, so reuse detection skips. The Department is not
        part of it: JO Numbers are one counter per project (#93).
        """
        if plan.resolution is None or plan.jo_number_int is None:
            return None, None
        resolved = plan.resolution
        key = jo_key(resolved.project_page_id, plan.jo_number_int)
        shown = plan.properties.get("JO Number", {}).get("rich_text", [])
        display = "".join(item["text"]["content"] for item in shown)
        return key, display or f"#{plan.jo_number_int}"

    def _annotate_duplicate(self, msg: dict, plan: JobOrderPlan) -> str | None:
        """Look for an earlier exact re-post or reused JO Number; return the jo_key.

        Without a complete key (D1) no content-duplicate lookup runs at all.

        A match never blocks or merges: the new row is still written, with an
        ``Earlier row:`` Watchdog Notes line and a finding stored in the plan.
        """
        key = msg["telegram_key"]
        dup_key, jo_number = self._jo_key(plan)
        if dup_key is None:
            return None
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
            add_earlier_row_line(plan, notes_line(match))
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
        ``plan.findings``.
        """
        raw = _raw_update(msg)
        stage = self.ledger.stage_result(msg["telegram_key"], "extract") or {}
        extraction = stage.get("extraction") or {}
        duplicate = plan.duplicate or {}
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
                for f in plan.findings
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
            # A retried row reuses its key, so a gateway queues the post once (#95).
            options = (
                {"idempotency_key": f"watchdog-outbox-{row['id']}"}
                if getattr(self.telegram, "accepts_idempotency_key", False)
                else {}
            )
            try:
                sent_id = await self.telegram.send_message(
                    row["chat_id"], row["topic_id"], row["text"], **options
                )
            except Exception as exc:
                state = self.ledger.outbox_failed_attempt(
                    row["id"],
                    str(exc),
                    retry_seconds=self.config.retry_schedule_seconds,
                    now=self.clock().timestamp(),
                    retry_after=getattr(exc, "retry_after", None),
                )
                log.warning(
                    "ops post outbox=%s kind=%s failed (%s): now %s",
                    row["id"],
                    row["kind"],
                    type(exc).__name__,
                    state,
                )
                continue
            if isinstance(sent_id, GatewayRef):
                # Queued at the gateway: the real Telegram ID comes from ``resolve_accepted``.
                self.ledger.outbox_accepted(row["id"], sent_id.ref)
                log.info(
                    "ops post outbox=%s kind=%s accepted by the gateway as ref %s",
                    row["id"],
                    row["kind"],
                    sent_id.ref,
                )
                continue
            self.ledger.outbox_sent(row["id"], sent_id)
            log.info(
                "ops post outbox=%s kind=%s sent as telegram message %s",
                row["id"],
                row["kind"],
                sent_id,
            )

    async def resolve_accepted(self) -> None:
        """Ask the gateway which Telegram message each queued post became (#109, #113).

        ``sent`` stores the real ID; ``queued`` leaves the row for a later cycle (and warns
        once it has waited over an hour). ``failed`` and unknown (404) mark the row failed
        and never re-send it. A lookup that errors is tried again next cycle.
        """
        for row in self.ledger.accepted_outbox():
            try:
                status = await self.telegram.notify_status(row["gateway_ref"])
            except Exception as exc:
                log.warning(
                    "could not resolve outbox=%s gateway ref %s (%s)",
                    row["id"],
                    row["gateway_ref"],
                    type(exc).__name__,
                )
                continue
            if status.state == "sent" and status.telegram_message_id is not None:
                self.stuck_warned.discard(row["id"])
                self.ledger.outbox_resolved(row["id"], status.telegram_message_id)
                log.info(
                    "ops post outbox=%s kind=%s sent as telegram message %s",
                    row["id"],
                    row["kind"],
                    status.telegram_message_id,
                )
            elif status.state in ("failed", "unknown"):
                self._gateway_post_failed(row, status.error or f"gateway reported {status.state}")
            else:
                self._warn_if_stuck(row)

    def _gateway_post_failed(self, row: dict, error: str) -> None:
        window = self.ledger.heartbeat_window(row["id"])
        alert = None
        if window is not None:
            alert = OutboxMessage(
                kind="outage_alert",
                chat_id=self.config.ops_chat_id,
                topic_id=self.config.ops_topic_id,
                text=render_heartbeat_failed_alert(
                    window_start=window[0],
                    window_end=window[1],
                    error=outbound_text(error).text,
                    owner_handle=self.config.owner_telegram_handle,
                ),
            )
        if self.ledger.outbox_gateway_failed(row["id"], error, alert=alert):
            self.stuck_warned.discard(row["id"])
            log.error(
                "ops post outbox=%s kind=%s failed at the gateway (ref %s): %s",
                row["id"],
                row["kind"],
                row["gateway_ref"],
                outbound_text(error).text,
            )

    def _warn_if_stuck(self, row: dict) -> None:
        """Warn once per row (per process) that the gateway still has not sent it."""
        if row["id"] in self.stuck_warned:
            return
        queued_at = datetime.strptime(row["created_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        age = self.clock() - queued_at
        if age >= STUCK_AFTER:
            self.stuck_warned.add(row["id"])
            log.warning(
                "ops post outbox=%s kind=%s is still unresolved at the gateway after %s (ref %s);"
                " not re-sending, it stays out of backlinks",
                row["id"],
                row["kind"],
                age,
                row["gateway_ref"],
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

    # -- liveness (#25) ----------------------------------------------------

    def _beat(self) -> None:
        """Stamp the poller's liveness; the deadman and health check read it."""
        self.ledger.stamp_alive(self.clock())

    def check_deadman(self) -> None:
        """Raise ``DeadmanExit`` when no progress was stamped for too long."""
        alive = self.ledger.poller_alive_at()
        limit = timedelta(minutes=self.config.deadman_stale_minutes)
        if alive is not None and self.clock() - alive > limit:
            raise DeadmanExit(
                f"poller made no progress since {alive.isoformat()}"
                f" (limit {self.config.deadman_stale_minutes} minutes)"
            )

    async def _deadman(self) -> None:
        while True:
            await asyncio.sleep(self.deadman_interval)
            self.check_deadman()

    # -- daily heartbeat (#25) -----------------------------------------------

    def enqueue_heartbeat_if_due(self) -> bool:
        """Queue the heartbeat for the latest 07:00 window, once per window."""
        start, end = heartbeat_window(
            self.clock(), self.config.heartbeat_time, ZoneInfo(self.config.timezone)
        )
        if self.ledger.heartbeat_exists(start):
            return False
        if not self.ledger.has_heartbeats():
            # First start of a new ledger: there is no history to report, so this
            # window is skipped and the first heartbeat follows at the next 07:00.
            self.ledger.skip_heartbeat(start, end)
            return False
        last = self.ledger.previous_heartbeat(start)
        text = render_heartbeat(
            window_start=start,
            window_end=end,
            stats=self.ledger.heartbeat_stats(start, end),
            previous=(
                datetime.fromisoformat(last["window_end"]),
                intake_link(last["chat_id"], last["topic_id"], last["telegram_message_id"]),
            )
            if last
            else None,
        )
        return self.ledger.enqueue_heartbeat(
            start,
            end,
            OutboxMessage(
                kind="heartbeat",
                chat_id=self.config.ops_chat_id,
                topic_id=self.config.ops_topic_id,
                text=text,
            ),
        )

    async def run_once(self) -> None:
        try:
            await self.refresh_caches()
            await self.poll_once()
            self._beat()
            await self.drain_once()
        finally:
            try:
                await self.resolve_accepted()
            except Exception:
                log.exception("could not resolve accepted ops posts")
            try:
                self.enqueue_heartbeat_if_due()
            except Exception:
                log.exception("could not queue the daily heartbeat")
            # A capture stuck on a failing dependency must not starve ops posts.
            await self.deliver_outbox()

    async def _loop(self) -> None:
        while True:
            try:
                await self.run_once()
            except Exception:
                log.exception("pipeline cycle failed; retrying next cycle")
            await asyncio.sleep(self.config.poll_interval)

    async def run(self) -> None:
        """Run the pipeline with a deadman beside it.

        If the poller stays stale for ``deadman_stale_minutes``, ``DeadmanExit``
        propagates so the process exits non-zero and the container restarts it.
        """
        self._beat()
        loop = asyncio.create_task(self._loop())
        deadman = asyncio.create_task(self._deadman())
        try:
            done, _ = await asyncio.wait({loop, deadman}, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            for task in (loop, deadman):
                task.cancel()
            await asyncio.gather(loop, deadman, return_exceptions=True)
