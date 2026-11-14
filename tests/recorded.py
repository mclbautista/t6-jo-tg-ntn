"""The recorded Notion fixtures, read through t6-core as the watchdog's cache entries."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
from t6.domain import Contact, Project
from t6.notion import NotionClient
from t6.notion.departments import get_departments
from t6.notion.reads import ContactReader, ProjectReader

from jo_watchdog.app.caches import person_from_contact, project_entry
from jo_watchdog.core.resolve import Person, ProjectEntry

NOTION = Path(__file__).parent / "fixtures" / "notion"


def load(name: str) -> dict:
    return json.loads((NOTION / name).read_text(encoding="utf-8"))


def _client(handler) -> NotionClient:
    return NotionClient("test-token", transport=httpx.MockTransport(handler))


def _pages(*names: str) -> NotionClient:
    pages = [load(name) for name in names]

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        index = 1 if body.get("start_cursor") and len(pages) > 1 else 0
        return httpx.Response(200, json=pages[index])

    return _client(handler)


def recorded_contacts() -> list[Contact]:
    with _pages("contacts_query_page1.json", "contacts_query_page2.json") as client:
        return list(ContactReader(client, "contacts").list())


def recorded_projects() -> list[Project]:
    with _pages("projects_query.json") as client:
        return list(ProjectReader(client, "projects").list())


def recorded_people() -> list[Person]:
    return [person_from_contact(c) for c in recorded_contacts()]


def recorded_project_entries() -> list[ProjectEntry]:
    return [project_entry(p) for p in recorded_projects()]


def recorded_departments() -> list[str]:
    schema = load("job_orders_data_source.json")
    with _client(lambda request: httpx.Response(200, json=schema)) as client:
        return [d.name for d in get_departments(client, "job-orders")]


class RecordedStudio:
    """Serves the recorded Contacts, Projects and Departments in place of ``T6Studio``."""

    async def contacts(self, data_source_id: str) -> list[Contact]:
        return await asyncio.to_thread(recorded_contacts)

    async def projects(self, data_source_id: str) -> list[Project]:
        return await asyncio.to_thread(recorded_projects)

    async def department_names(self, job_orders_data_source_id: str) -> list[str]:
        return await asyncio.to_thread(recorded_departments)

    async def query_project_deliverables(self, data_source_id: str, project_page_id: str):
        return []
