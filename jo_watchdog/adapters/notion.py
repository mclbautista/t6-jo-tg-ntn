"""Live-schema probes for the operator smoke tools (``smoke.py``), over t6-core's client.

The worker never uses this: Job Orders and the studio's reads go through t6-core
(``t6_studio``). The smoke tools probe the live Notion schema by property name,
which is their job, so they need page-level calls. They go through t6-core's
``AsyncNotionClient`` rather than a client of this repo's own.
"""

from __future__ import annotations

from typing import Any

import httpx
from pydantic import BaseModel, Field
from t6.notion import NOTION_VERSION, AsyncNotionClient, NotionError

__all__ = ["NOTION_VERSION", "NotionAdapter", "NotionError", "PageRef", "QueryRequest", "QueryResponse"]


class PageRef(BaseModel):
    """The reference Notion returns after creating a page."""

    id: str
    url: str


class QueryRequest(BaseModel):
    """A data-source query: at most ``page_size`` rows, optionally filtered."""

    page_size: int = Field(default=100, ge=1, le=100)
    filter: dict[str, Any] | None = None


class QueryResponse(BaseModel):
    results: list[dict[str, Any]]


class NotionAdapter:
    """Page-level Notion calls for the smoke tools."""

    def __init__(
        self,
        token: str,
        *,
        notion_version: str = NOTION_VERSION,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self._client = AsyncNotionClient(token, notion_version=notion_version, transport=transport)

    async def close(self) -> None:
        await self._client.aclose()

    async def create_page(self, payload: dict) -> PageRef:
        data = await self._client.create_page(payload)
        return PageRef(id=data["id"], url=data["url"])

    async def retrieve_page(self, page_id: str) -> dict:
        return await self._client.retrieve_page(page_id)

    async def retrieve_data_source(self, data_source_id: str) -> dict:
        return await self._client.retrieve_data_source(data_source_id)

    async def query_data_source(
        self, data_source_id: str, request: QueryRequest | None = None
    ) -> QueryResponse:
        request = request or QueryRequest()
        rows: list[dict[str, Any]] = []
        async for row in self._client.iter_data_source(
            data_source_id, filter=request.filter, page_size=request.page_size
        ):
            rows.append(row)
            if len(rows) >= request.page_size:
                break
        return QueryResponse(results=rows)
