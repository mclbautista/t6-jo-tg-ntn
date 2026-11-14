"""Studio reads through t6-core: Contacts, Projects, Departments and a Project's Deliverables.

t6-core's readers return ``t6.domain`` entities and know the Notion property IDs;
nothing here names a Notion property. They sit on t6-core's blocking client, which
owns a private event loop and cannot run inside the worker's loop, so every call
runs on one dedicated thread.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import TypeVar

import httpx
from t6.domain import Contact, Project
from t6.notion import NotionClient
from t6.notion.departments import get_departments
from t6.notion.reads import ContactReader, DeliverableReader, ProjectReader

from jo_watchdog.core.matching import Candidate

T = TypeVar("T")


class T6Studio:
    """The studio's Notion reads through t6-core: ``StudioReader`` and ``DeliverableSource``."""

    def __init__(
        self,
        token: str,
        *,
        notion_version: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        options = {"notion_version": notion_version} if notion_version else {}
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="t6-notion")
        self._client = NotionClient(token, transport=transport, **options)

    async def _run(self, call: Callable[[], T]) -> T:
        return await asyncio.get_running_loop().run_in_executor(self._pool, call)

    async def contacts(self, data_source_id: str) -> list[Contact]:
        return list(await self._run(lambda: ContactReader(self._client, data_source_id).list()))

    async def projects(self, data_source_id: str) -> list[Project]:
        return list(await self._run(lambda: ProjectReader(self._client, data_source_id).list()))

    async def department_names(self, job_orders_data_source_id: str) -> list[str]:
        found = await self._run(lambda: get_departments(self._client, job_orders_data_source_id))
        return [department.name for department in found]

    async def query_project_deliverables(
        self, data_source_id: str, project_page_id: str
    ) -> list[Candidate]:
        """All Deliverables related to the Project, with no Status or Class filter (#26)."""
        found = await self._run(
            lambda: DeliverableReader(self._client, data_source_id).for_project(project_page_id)
        )
        return [Candidate(page_id=d.page_id, name=d.name) for d in found]

    async def close(self) -> None:
        await self._run(self._client.close)
        self._pool.shutdown(wait=False)
