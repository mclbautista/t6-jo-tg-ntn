"""The studio's Notion data through t6-core: reads, and Job Order writes.

t6-core returns and takes ``t6.domain`` entities and knows the Notion property
and option IDs; nothing here names a Notion property. It sits on t6-core's
blocking client, which owns a private event loop and cannot run inside the
worker's loop, so every call runs on one dedicated thread.

A write is never retried here. t6-core reads the page back and returns the saved
Job Order, ``Rejected`` (nothing written) or ``Ambiguous`` (it may have been); the
worker's ledger decides what to do next.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from typing import Any, TypeVar

import httpx
from t6.domain import Contact, JobOrder, Project
from t6.notion import NotionClient
from t6.notion.departments import get_departments
from t6.notion.job_orders import JobOrders
from t6.notion.reads import ContactReader, DeliverableReader, ProjectReader
from t6.notion.writes import Ambiguous, Rejected

from jo_watchdog.core.matching import Candidate

T = TypeVar("T")


class T6Studio:
    """``StudioReader``, ``DeliverableSource`` and ``JobOrderStore`` over t6-core."""

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

    async def create(
        self, data_source_id: str, fields: Mapping[str, Any]
    ) -> JobOrder | Rejected | Ambiguous:
        """Create a Job Order from domain ``fields`` (with ``source_text`` for the body)."""
        return await self._run(lambda: JobOrders(self._client, data_source_id).create(fields))

    async def get(self, data_source_id: str, page_id: str) -> JobOrder:
        return await self._run(lambda: JobOrders(self._client, data_source_id).get(page_id))

    async def find_all_by_telegram_key(
        self, data_source_id: str, telegram_key: str
    ) -> list[JobOrder]:
        """Job Orders with this Telegram Key, oldest first (write recovery only)."""
        found = await self._run(
            lambda: JobOrders(self._client, data_source_id).find_all_by_telegram_key(telegram_key)
        )
        return list(found)

    async def close(self) -> None:
        await self._run(self._client.close)
        self._pool.shutdown(wait=False)
