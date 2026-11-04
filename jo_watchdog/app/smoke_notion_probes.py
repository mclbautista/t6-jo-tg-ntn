"""Notion schema and relation probes for ``smoke.py`` (#59).

The assertions and the probe logic live here so offline tests can exercise them
against recorded schemas and fakes. Nothing here runs in CI against Notion, and a
probe that could not observe its behavior says so (MANUAL) rather than passing.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping

from jo_watchdog.adapters.notion import NotionError, QueryRequest
from jo_watchdog.app.smoke_gate import FAIL, MANUAL, PASS, Result
from jo_watchdog.core.plan import NOTES_PROPERTY, WATCHDOG_NOTES_PROPERTY

STATUS_OPTION = "Needs Intake Review"
MAX_RETRY_AFTER_REQUESTS = 100
MAX_RETRY_AFTER_WAIT = 60.0

_TEXT = {"rich_text"}
# Exact live property names (note the trailing space in "Blocker " and the emoji
# in the Coordinator and Deliverables Projects names). ``None`` means the design
# fixes the name but not the type, so only presence is asserted.
EXPECTED_PROPERTIES: dict[str, dict[str, set[str] | None]] = {
    "Job Orders": {
        "Name": {"title"},
        "JO Number": _TEXT, "Title": _TEXT, "Subtitle": _TEXT, "Task": _TEXT, "Purpose": _TEXT,
        "Source / Source Drive": _TEXT, "Platform": _TEXT, "Link Settings": _TEXT,
        "Watermark": _TEXT, "Mode of Transfer": _TEXT, "Blocker ": _TEXT, "Pending": _TEXT,
        NOTES_PROPERTY: _TEXT, WATCHDOG_NOTES_PROPERTY: _TEXT, "Telegram Key": _TEXT,
        "Date": {"date"}, "Deadline": {"date"},
        "Department": {"select"},
        "Status": {"status", "select"},
        "Projects": {"relation"}, "Assigned": {"relation"}, "cc": {"relation"},
        "🗂️ Coordinator": {"relation"}, "Deliverable": {"relation"},
    },
    "Contacts": {
        "Name": {"title"}, "TelegramUserID": None, "TelegramHandle": None,
        "Aliases": None, "End of Shift": None,
    },
    "Projects": {"Name": {"title"}, "Project Code": {"select"}},
    "Deliverables": {
        "Name": {"title"}, "🗺️ Projects": {"relation"}, "Job Orders": {"relation"},
        "Status": None, "Class": None, "Playdate": None, "Deadline": None,
    },
}

# (data source, property) -> label of the data source the relation must target.
EXPECTED_RELATIONS = {
    ("Job Orders", "Projects"): "Projects",
    ("Job Orders", "Assigned"): "Contacts",
    ("Job Orders", "cc"): "Contacts",
    ("Job Orders", "🗂️ Coordinator"): "Contacts",
    ("Job Orders", "Deliverable"): "Deliverables",
    ("Deliverables", "🗺️ Projects"): "Projects",
    ("Deliverables", "Job Orders"): "Job Orders",
}


def _same(a: str, b: str) -> bool:
    return a.replace("-", "") == b.replace("-", "")


def check_schemas(schemas: Mapping[str, dict], ids: Mapping[str, str]) -> list[str]:
    """Problems found in the four live schemas; an empty list means they match.

    ``schemas`` and ``ids`` are keyed by data-source label (Job Orders, Contacts,
    Projects, Deliverables). A label with no schema is itself a problem.
    """
    problems: list[str] = []
    for label, expected in EXPECTED_PROPERTIES.items():
        if label not in schemas:
            problems.append(f"{label}: schema not read")
            continue
        props = schemas[label]["properties"]
        for name, kinds in expected.items():
            if name not in props:
                problems.append(f"{label}.{name!r} is missing")
            elif kinds and props[name]["type"] not in kinds:
                problems.append(
                    f"{label}.{name!r} is {props[name]['type']}, want {' or '.join(sorted(kinds))}"
                )
    for (label, name), target in EXPECTED_RELATIONS.items():
        prop = schemas.get(label, {}).get("properties", {}).get(name)
        if prop is None or prop["type"] != "relation":
            continue  # already reported above
        if not _same(prop["relation"].get("data_source_id", ""), ids[target]):
            problems.append(f"{label}.{name!r} does not relate to {target}")
    problems += _reciprocal_problems(schemas)
    status = schemas.get("Job Orders", {}).get("properties", {}).get("Status")
    if status:
        options = [o["name"] for o in status.get(status["type"], {}).get("options", [])]
        if STATUS_OPTION not in options:
            problems.append(f"Job Orders.'Status' has no {STATUS_OPTION!r} option")
    return problems


def _reciprocal_problems(schemas: Mapping[str, dict]) -> list[str]:
    deliverable = schemas.get("Job Orders", {}).get("properties", {}).get("Deliverable")
    if not deliverable or deliverable["type"] != "relation":
        return []
    relation = deliverable["relation"]
    if relation.get("type") != "dual_property":
        return [f"Job Orders.'Deliverable' is one-way ({relation.get('type')}), want reciprocal"]
    synced = relation["dual_property"].get("synced_property_name")
    if synced not in schemas.get("Deliverables", {}).get("properties", {}):
        return [f"Job Orders.'Deliverable' syncs to Deliverables.{synced!r}, which does not exist"]
    return []


async def probe_reciprocal_relation(
    notion,
    *,
    job_orders_ds: str,
    relation_name: str,
    reciprocal_name: str,
    deliverable_id: str,
    status_option: str = STATUS_OPTION,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    polls: int = 6,
) -> Result:
    """Write a Job Order -> Deliverable relation, then read it from both sides.

    Creates one marked Job Order (the integration cannot archive it); the URL is
    in every result so it can be deleted by hand.
    """
    payload = {
        "parent": {"data_source_id": job_orders_ds},
        "properties": {
            "Name": {"title": [{"text": {"content": "SMOKE TEST — delete this row (relation)"}}]},
            "Status": {"status": {"name": status_option}},
            relation_name: {"relation": [{"id": deliverable_id}]},
        },
    }
    try:
        ref = await notion.create_page(payload)
    except NotionError as exc:
        return Result(FAIL, f"relation write rejected ({exc.status} {exc.code})")
    cleanup = f"delete the row {ref.url} by hand"
    reciprocal = contains = False
    for attempt in range(polls):
        deliverable = await notion.retrieve_page(deliverable_id)
        side = deliverable["properties"].get(reciprocal_name, {})
        reciprocal = any(r["id"] == ref.id for r in side.get("relation", []))
        page = await notion.query_data_source(
            job_orders_ds,
            QueryRequest(
                page_size=5,
                filter={"property": relation_name, "relation": {"contains": deliverable_id}},
            ),
        )
        contains = any(r["id"] == ref.id for r in page.results)
        if reciprocal and contains:
            return Result(PASS, f"Deliverable.{reciprocal_name!r} and relation_contains saw "
                                f"the new row (attempt {attempt + 1}); {cleanup}")
        await sleep(2.0)
    seen = f"reciprocal side {'saw' if reciprocal else 'did not see'} it, relation_contains " \
           f"{'saw' if contains else 'did not see'} it"
    return Result(FAIL, f"{seen} after {polls} polls; {cleanup}")


async def probe_retry_after(
    notion,
    data_source_id: str,
    *,
    max_requests: int = 40,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> Result:
    """Send a bounded burst of cheap reads and report how Notion's 429 behaves.

    Stops at the first 429, honours its ``Retry-After`` (capped), and confirms a
    request succeeds afterwards. If no 429 appears within the bound the result is
    MANUAL: a limit that was not observed is never reported as handled.
    """
    bound = min(max_requests, MAX_RETRY_AFTER_REQUESTS)
    for sent in range(1, bound + 1):
        try:
            await notion.retrieve_data_source(data_source_id)
        except NotionError as exc:
            if exc.status != 429:
                return Result(FAIL, f"request {sent}: {exc.status} {exc.code}")
            if exc.retry_after is None:
                return Result(MANUAL, f"429 after {sent} requests but no usable Retry-After "
                                      "header; check the adapter's fallback by hand")
            await sleep(min(exc.retry_after, MAX_RETRY_AFTER_WAIT))
            try:
                await notion.retrieve_data_source(data_source_id)
            except NotionError as again:
                return Result(FAIL, f"still {again.status} {again.code} after waiting "
                                    f"{exc.retry_after}s")
            return Result(PASS, f"429 after {sent} requests, Retry-After={exc.retry_after}s "
                                "honoured, next request succeeded")
    return Result(MANUAL, f"429 not observed in {bound} requests; the limit was not exercised")
