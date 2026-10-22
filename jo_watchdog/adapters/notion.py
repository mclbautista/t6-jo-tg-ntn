"""Hand-written Notion adapter over httpx, pinned to a Notion-Version.

Only the endpoints the watchdog needs are wrapped, with Pydantic request/response
models. The adapter is pagination-ready: query responses carry ``has_more`` and
``next_cursor`` and the caller passes ``start_cursor`` through.
"""

from __future__ import annotations

from typing import Any

import httpx
from pydantic import BaseModel, Field

from jo_watchdog.adapters.protocols import PageRef
from jo_watchdog.core.matching import Candidate

NOTION_VERSION = "2026-03-11"
BASE_URL = "https://api.notion.com/v1"

# Live Deliverables schema names (deliverable-matching research).
DELIVERABLE_PROJECTS_PROPERTY = "🗺️ Projects"
DELIVERABLE_TITLE_PROPERTY = "Name"


class NotionError(Exception):
    """A Notion API error (status + code + message)."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(f"Notion {status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message


class QueryRequest(BaseModel):
    """A data-source query request; pagination-ready."""

    page_size: int = Field(default=100, ge=1, le=100)
    start_cursor: str | None = None
    filter: dict[str, Any] | None = None


class QueryResponse(BaseModel):
    """A data-source query response page."""

    results: list[dict[str, Any]]
    has_more: bool = False
    next_cursor: str | None = None


class NotionAdapter:
    """Async REST client for the pinned Notion API version."""

    def __init__(
        self,
        token: str,
        *,
        base_url: str = BASE_URL,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers={
                "Authorization": f"Bearer {token}",
                "Notion-Version": NOTION_VERSION,
                "Content-Type": "application/json",
            },
            timeout=timeout,
            transport=transport,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        resp = await self._client.request(method, path, json=body)
        if resp.status_code >= 400:
            data = resp.json()
            raise NotionError(
                resp.status_code,
                data.get("code", "unknown"),
                data.get("message", resp.text),
            )
        return resp.json()

    async def create_page(self, payload: dict) -> PageRef:
        """Create a page from the exact payload built by ``core.payload``."""
        data = await self._request("POST", "/pages", payload)
        return PageRef(id=data["id"], url=data["url"])

    async def retrieve_page(self, page_id: str) -> dict:
        """Retrieve a page by id (the normal read-back path)."""
        return await self._request("GET", f"/pages/{page_id}")

    async def query_data_source(
        self,
        data_source_id: str,
        request: QueryRequest,
    ) -> QueryResponse:
        """Query a data source; follow ``next_cursor`` while ``has_more``."""
        body: dict[str, Any] = {"page_size": request.page_size}
        if request.start_cursor:
            body["start_cursor"] = request.start_cursor
        if request.filter is not None:
            body["filter"] = request.filter
        data = await self._request("POST", f"/data_sources/{data_source_id}/query", body)
        return QueryResponse(
            results=data.get("results", []),
            has_more=data.get("has_more", False),
            next_cursor=data.get("next_cursor"),
        )

    async def query_project_deliverables(
        self, data_source_id: str, project_page_id: str
    ) -> list[Candidate]:
        """All Deliverables whose ``🗺️ Projects`` relation contains the Project.

        A live, fully paginated query with no Status or Class filter (#26).
        """
        candidates: list[Candidate] = []
        request = QueryRequest(
            filter={
                "property": DELIVERABLE_PROJECTS_PROPERTY,
                "relation": {"contains": project_page_id},
            }
        )
        while True:
            page = await self.query_data_source(data_source_id, request)
            candidates.extend(_deliverable_candidate(row) for row in page.results)
            if not page.has_more or not page.next_cursor:
                return candidates
            request = request.model_copy(update={"start_cursor": page.next_cursor})


def _deliverable_candidate(row: dict) -> Candidate:
    title = row.get("properties", {}).get(DELIVERABLE_TITLE_PROPERTY, {}).get("title") or []
    name = "".join(
        item.get("plain_text") or item.get("text", {}).get("content", "") for item in title
    )
    return Candidate(page_id=row["id"], name=name)
