"""Pure core: the deterministic Job Order plan (#21).

Turns a validated extraction into the exact Notion properties and body blocks
for every Job Order field that needs no live relation resolution. The plan is
stored before any write and replayed as-is: ``build_job_order_payload`` reads
only the stored plan, never the extraction or the clock.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import datetime, time
from typing import Any, Protocol

from pydantic import BaseModel, Field

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
from jo_watchdog.core.payload import STATUS_NEEDS_INTAKE_REVIEW
from jo_watchdog.core.plan import BLOCK_LIMIT, first_line_of, split_blocks
from jo_watchdog.core.resolve import (
    DepartmentResolution,
    PeopleResolution,
    ProjectResolution,
    relation_properties,
)

WATCHDOG_MARKER = "— added by the watchdog —"
ASAP_NOTE = "ASAP — deprioritize other JOs"

# Exact live property names, including the trailing space on `Blocker `.
TEXT_PROPERTIES: dict[str, str] = {
    "title": "Title",
    "task": "Task",
    "purpose": "Purpose",
    "source": "Source / Source Drive",
    "platform": "Platform",
    "link_settings": "Link Settings",
    "watermark": "Watermark",
    "mode_of_transfer": "Mode of Transfer",
    "blocker": "Blocker ",
    "pending": "Pending",
}


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
    """The exact Notion payload pieces, stored at ``planned`` and replayed unchanged."""

    telegram_key: str
    name: str
    properties: dict[str, Any]
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
    """Read a stored plan; a pre-#39 skeleton plan (no ``properties``) is upgraded in place."""
    data = json.loads(stored) if isinstance(stored, str) else dict(stored)
    if "properties" in data:
        return JobOrderPlan.model_validate(data)
    properties: dict[str, Any] = {
        "Name": {"title": rich_text(data["name"])},
        "Telegram Key": {"rich_text": rich_text(data["telegram_key"])},
        "Status": {"status": {"name": STATUS_NEEDS_INTAKE_REVIEW}},
    }
    if data.get("notes"):
        properties["Notes"] = {"rich_text": rich_text(data["notes"])}
    return JobOrderPlan(
        telegram_key=data["telegram_key"],
        name=data["name"],
        properties=properties,
        body_blocks=data.get("body_blocks", []),
        duplicate=data.get("duplicate"),
    )


def manila_iso(value: datetime) -> str:
    """ISO-8601 with the explicit ``+08:00`` offset."""
    return value.astimezone(MANILA).isoformat()


def notion_date(value: datetime) -> dict[str, Any]:
    """A Notion Date value for ``value``: the instant at its Manila UTC offset, no ``time_zone``.

    Notion rejects a ``time_zone`` next to a non-zero offset (#83), and with a
    ``time_zone`` it reads the wall clock of ``start`` in that zone and ignores the
    offset: a UTC start came back eight hours early (#85). A bare offset keeps the
    exact instant, which is what the read-back compares.
    """
    return {"date": {"start": manila_iso(value)}}


def _instant_date(date: dict[str, Any]) -> dict[str, Any]:
    """Drop ``time_zone`` from a stored date, keeping each instant at its Manila offset."""
    if "time_zone" not in date:
        return date
    fixed = {k: v for k, v in date.items() if k != "time_zone"}
    for edge in ("start", "end"):
        value = date.get(edge)
        if not isinstance(value, str):
            continue
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            continue
        if parsed.tzinfo is not None:
            fixed[edge] = manila_iso(parsed)
    return fixed


def normalize_date_properties(properties: dict[str, Any]) -> dict[str, Any]:
    """Make every Date property Notion-safe, including ones in plans stored earlier.

    Plans stored by earlier builds carry ``time_zone`` (with ``+08:00`` before #83, with
    UTC after it). Each instant is kept and re-expressed at the Manila offset without
    ``time_zone``, so a stuck row is replanned by replay and no second page is made.
    """
    return {
        name: (
            {**prop, "date": _instant_date(prop["date"])}
            if isinstance(prop, dict) and isinstance(prop.get("date"), dict)
            else prop
        )
        for name, prop in properties.items()
    }


def rich_text(value: str) -> list[dict]:
    """Split a text value into Notion rich-text items of at most 2000 characters."""
    return [
        {"type": "text", "text": {"content": value[i : i + BLOCK_LIMIT]}}
        for i in range(0, len(value), BLOCK_LIMIT)
    ]


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


def build_notes(poster_notes: str | None, deadline_raw: str | None, asap: bool) -> str:
    """Poster notes verbatim, then the watchdog block when it has anything to add.

    ``Earlier row:`` lines are appended by #24 after this block.
    """
    notes = _verbatim(poster_notes)
    added: list[str] = []
    deadline = _clean(deadline_raw)
    if deadline:
        added.append(f"Deadline as posted: {deadline}")
    if asap:
        added.append(ASAP_NOTE)
    if not added:
        return notes
    block = "\n".join([WATCHDOG_MARKER, *added])
    return f"{notes}\n\n{block}" if notes else block


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
    header_raw = first_line_of(raw_text)
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

    properties: dict[str, Any] = {
        "Name": {"title": rich_text(name)},
        "Telegram Key": {"rich_text": rich_text(telegram_key)},
        "Status": {"status": {"name": STATUS_NEEDS_INTAKE_REVIEW}},
        "Date": notion_date(posted_at),
    }
    if digits is not None:
        properties["JO Number"] = {"rich_text": rich_text(f"#{digits}")}
    if department:
        properties["Department"] = {"select": {"name": department}}
    if deadline.deadline is not None:
        properties["Deadline"] = notion_date(deadline.deadline)
    for field, prop in TEXT_PROPERTIES.items():
        raw_value = getattr(extraction, field)
        value = _verbatim(raw_value) if field == "title" else _clean(raw_value)
        if value:
            properties[prop] = {"rich_text": rich_text(value)}
    subtitle = _clean(extraction.subtitle)
    if subtitle and is_subtitle_value(subtitle):
        properties["Subtitle"] = {"rich_text": rich_text(subtitle)}
    notes = build_notes(extraction.notes, extraction.deadline_raw, asap)
    if notes:
        properties["Notes"] = {"rich_text": rich_text(notes)}

    body = [
        *split_blocks(outbound_text(source_block).text),
        *split_blocks(outbound_text(raw_text).text),
    ]

    return JobOrderPlan(
        telegram_key=telegram_key,
        name=name,
        properties=properties,
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
    """Merge the #22 resolution into the plan: relations, findings and the stored decision."""
    plan.properties.update(relation_properties(people, project))
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


def build_job_order_payload(plan: JobOrderPlan, *, data_source_id: str) -> dict:
    """The exact create-page payload, built only from the stored plan."""
    children = [
        {
            "object": "block",
            "type": "paragraph",
            "paragraph": {"rich_text": rich_text(mask_text(block))},
        }
        for block in plan.body_blocks
    ]
    return {
        "parent": {"data_source_id": data_source_id},
        "properties": normalize_date_properties(plan.properties),
        "children": children,
    }
