"""Pure core: the deterministic Job Order plan (#21).

Turns a validated extraction into the Job Order fields, in ``t6.domain.JobOrder``
terms, and the source-text body blocks, for every field that needs no live
relation resolution. The plan is stored before any write and replayed as-is:
``job_order_fields`` reads only the stored plan, never the extraction or the
clock. t6-core maps the fields to Notion (#134); nothing here names a Notion
property. Plans stored before #134 held Notion-shaped properties and are upgraded
on load by ``legacy_plan``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import date, datetime, time
from typing import Any, Protocol

from pydantic import BaseModel, Field
from t6.domain import JobOrderStatus

from jo_watchdog.core.deadline import MANILA, DeadlineSource, resolve_deadline
from jo_watchdog.core.extraction import is_subtitle_value
from jo_watchdog.core.findings import ASAP, Finding
from jo_watchdog.core.header import (
    canonical_name,
    normalize_department,
    parse_header,
    parse_number,
)
from jo_watchdog.core.masker import mask_text, outbound_text
from jo_watchdog.core.plan import header_line_of, split_blocks
from jo_watchdog.core.resolve import (
    DepartmentResolution,
    PeopleResolution,
    ProjectResolution,
)

ASAP_NOTE = "ASAP — deprioritize other JOs"

# Verbatim text fields, by Job Order field name; each is also an extraction field.
TEXT_FIELDS: tuple[str, ...] = (
    "title",
    "task",
    "purpose",
    "source",
    "platform",
    "link_settings",
    "watermark",
    "mode_of_transfer",
    "blocker",
    "pending",
)
STATUS_NEEDS_INTAKE_REVIEW = JobOrderStatus.NEEDS_INTAKE_REVIEW.value


class Extraction(Protocol):
    """The raw fields this slice reads from the #20 extraction result."""

    header_raw: str | None
    jo_number_raw: str | None
    department_raw: str | None
    title: str | None
    task: str | None
    purpose: str | None
    source: str | None
    platform: str | None
    link_settings: str | None
    watermark: str | None
    mode_of_transfer: str | None
    blocker: str | None
    pending: str | None
    notes: str | None
    deadline_raw: str | None
    subtitle: str | None


class UnresolvedMention(BaseModel):
    """A People mention the resolver left out of the relations."""

    raw: str
    role: str  # "assignee" | "cc"
    status: str  # "unresolved" | "ambiguous"


class PlanResolution(BaseModel):
    """What resolution decided, stored with the plan so a resume never resolves again."""

    project_page_id: str | None = None
    department: str | None = None
    assigned: list[str] = Field(default_factory=list)
    cc: list[str] = Field(default_factory=list)
    unresolved: list[UnresolvedMention] = Field(default_factory=list)


class JobOrderPlan(BaseModel):
    """The Job Order fields and body, stored at ``planned`` and replayed unchanged.

    ``fields`` holds ``t6.domain.JobOrder`` field values that survive JSON: text,
    ISO-8601 strings with the Manila offset for ``date`` and ``deadline``, and
    lists of page IDs for the relations.
    """

    telegram_key: str
    name: str
    fields: dict[str, Any]
    body_blocks: list[str] = Field(default_factory=list)
    jo_number_int: int | None = None
    deadline_source: DeadlineSource = "blank"
    findings: list[Finding] = Field(default_factory=list)
    # None when no caches are wired (nothing was resolved).
    resolution: PlanResolution | None = None
    # The duplicate decision made at plan time (#24), replayed unchanged:
    # ``{"code": "exact_repost" | "number_reuse", "url", "days", "jo_number"}``.
    duplicate: dict | None = None
    # The Deliverable match stage result (#26): candidate snapshot, per-candidate
    # probabilities and outcome. None when no matcher is wired.
    match: dict | None = None


def load_plan(stored: str | dict) -> JobOrderPlan:
    """Read a stored plan; plans stored before #134 (Notion-shaped) are upgraded."""
    data = json.loads(stored) if isinstance(stored, str) else dict(stored)
    if "fields" in data:
        return JobOrderPlan.model_validate(data)
    from jo_watchdog.core.legacy_plan import upgrade

    return JobOrderPlan.model_validate(upgrade(data))


def manila_iso(value: datetime) -> str:
    """ISO-8601 with the explicit ``+08:00`` offset."""
    return value.astimezone(MANILA).isoformat()


def stored_instant(value: str) -> date | datetime:
    """A stored ``date`` or ``deadline``: a datetime when it has a time, else a day."""
    return datetime.fromisoformat(value) if "T" in value else date.fromisoformat(value)


def _clean(value: str | None) -> str:
    """Trim a verbatim field and mask it; absent values stay blank."""
    if value is None:
        return ""
    return outbound_text(value.replace("\xa0", " ").strip()).text


def _verbatim(value: str | None) -> str:
    """Mask a verbatim field but keep its whitespace and NBSP; trimming only decides emptiness."""
    if value is None or not value.strip():
        return ""
    return outbound_text(value).text


def build_notes(poster_notes: str | None) -> str:
    """The coordinator's own notes, verbatim (masked), for the ``Notes`` column."""
    return _verbatim(poster_notes)


def build_watchdog_notes(deadline_raw: str | None, asap: bool) -> str:
    """What the watchdog adds, one line per item, for the ``Watchdog Notes`` column.

    ``Earlier row:`` lines are appended by #24 after these lines.
    """
    added: list[str] = []
    deadline = _clean(deadline_raw)
    if deadline:
        added.append(f"Deadline as posted: {deadline}")
    if asap:
        added.append(ASAP_NOTE)
    return "\n".join(added)


def plan_job_order(
    extraction: Extraction,
    *,
    telegram_key: str,
    raw_text: str,
    posted_at: datetime,
    source_block: str,
    department_aliases: Mapping[str, str],
    department_resolution: DepartmentResolution | None = None,
    assignee_shifts: Sequence[tuple[str, str | time | None]] = (),
) -> JobOrderPlan:
    """Build the deterministic plan for one Job Order.

    ``posted_at`` is the intake posting or forwarding time, never the forwarded
    original's time. ``source_block`` is the rendered provenance block (#24's
    ``provenance.source_block``); it is masked here like all source text.
    ``department_resolution`` and ``assignee_shifts`` come from the
    resolution slice (#22). The resolved live option spelling is authoritative
    for Department and the canonical Name; ``None`` means validation has not run.
    """
    # The posted first line is authoritative; the extracted header is never trusted over it.
    header_raw = header_line_of(raw_text)
    header = parse_header(header_raw, department_aliases)

    department: str | None = None
    if header is not None:
        department = header.department
    elif extraction.department_raw and extraction.department_raw.strip():
        department = normalize_department(extraction.department_raw, department_aliases)
    if department_resolution is not None:
        department = department_resolution.department

    digits = header.number if header is not None else parse_number(extraction.jo_number_raw)

    if department_resolution is not None and department is None:
        name_header = None
    else:
        name_header = header
    name = outbound_text(canonical_name(name_header, header_raw, department)).text

    deadline = resolve_deadline(extraction.deadline_raw, posted_at, assignee_shifts)
    asap = any(f.code == ASAP for f in deadline.findings)

    fields: dict[str, Any] = {
        "name": name,
        "telegram_key": telegram_key,
        "status": STATUS_NEEDS_INTAKE_REVIEW,
        "date": manila_iso(posted_at),
    }
    if digits is not None:
        fields["number"] = f"#{digits}"
    if department:
        fields["department"] = department
    if deadline.deadline is not None:
        fields["deadline"] = manila_iso(deadline.deadline)
    for field in TEXT_FIELDS:
        raw_value = getattr(extraction, field)
        value = _verbatim(raw_value) if field == "title" else _clean(raw_value)
        if value:
            fields[field] = value
    subtitle = _clean(extraction.subtitle)
    if subtitle and is_subtitle_value(subtitle):
        fields["subtitle"] = subtitle
    notes = build_notes(extraction.notes)
    if notes:
        fields["notes"] = notes
    watchdog_notes = build_watchdog_notes(extraction.deadline_raw, asap)
    if watchdog_notes:
        fields["watchdog_notes"] = watchdog_notes

    body = [
        *split_blocks(outbound_text(source_block).text),
        *split_blocks(outbound_text(raw_text).text),
    ]

    return JobOrderPlan(
        telegram_key=telegram_key,
        name=name,
        fields=fields,
        body_blocks=body,
        jo_number_int=int(digits) if digits is not None else None,
        deadline_source=deadline.source,
        findings=deadline.findings,
    )


def apply_resolution(
    plan: JobOrderPlan,
    *,
    people: PeopleResolution,
    project: ProjectResolution,
    department: DepartmentResolution,
) -> None:
    """Merge the #22 resolution into the plan: relations, findings and the stored decision.

    Only resolved page IDs are written; an unresolved Project leaves ``project_id`` out.
    """
    plan.fields["assigned_ids"] = list(people.assigned)
    plan.fields["cc_ids"] = list(people.cc)
    if project.page_id:
        plan.fields["project_id"] = project.page_id
    plan.resolution = PlanResolution(
        project_page_id=project.page_id,
        department=department.department,
        assigned=list(people.assigned),
        cc=list(people.cc),
        unresolved=[
            UnresolvedMention(raw=m.raw, role=m.role, status=m.status)
            for m in people.mentions
            if m.status != "resolved"
        ],
    )
    plan.findings.extend(
        Finding(code=f.code, params=dict(f.params))
        for f in (*people.findings, *project.findings, *department.findings)
    )


def job_order_fields(plan: JobOrderPlan) -> dict[str, Any]:
    """The fields for ``t6.notion.job_orders.JobOrders.create``, built only from the stored plan.

    Stored ISO dates become ``date`` or ``datetime`` values, and the body blocks
    are masked once more at this exit boundary and joined into ``source_text``.
    """
    fields: dict[str, Any] = dict(plan.fields)
    for name in ("date", "deadline"):
        if isinstance(fields.get(name), str):
            fields[name] = stored_instant(fields[name])
    if plan.body_blocks:
        fields["source_text"] = "\n".join(mask_text(block) for block in plan.body_blocks)
    return fields
