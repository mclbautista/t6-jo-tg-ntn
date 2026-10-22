"""Adapter contracts, expressed as plain models and Protocols.

The worker depends only on these shapes, so tests substitute fake adapters and
the real adapters stay behind the seam (aiogram/httpx are never imported by the
worker). ``IncomingUpdate`` is the normalized shape the real Telegram adapter
maps an aiogram ``Update`` into.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel

from jo_watchdog.core.matching import Candidate, MatchResult


class IncomingUpdate(BaseModel):
    """A normalized Telegram update, decoupled from aiogram's model tree."""

    update_id: int
    chat_id: int
    message_id: int
    message_thread_id: int | None = None
    text: str = ""
    raw: dict[str, Any]


class PageRef(BaseModel):
    """The reference Notion returns after creating a page."""

    id: str
    url: str


@runtime_checkable
class TelegramClient(Protocol):
    async def get_updates(self, offset: int, timeout: int) -> list[IncomingUpdate]: ...

    async def set_reaction(self, chat_id: int, message_id: int, emoji: str) -> None: ...


@runtime_checkable
class NotionClient(Protocol):
    async def create_page(self, payload: dict) -> PageRef: ...

    async def retrieve_page(self, page_id: str) -> dict: ...


@runtime_checkable
class DeliverableMatcher(Protocol):
    """Answers one match/maybe/no decision per candidate (#26)."""

    async def match(
        self, state: str, project_name: str, candidates: list[Candidate]
    ) -> MatchResult: ...


@runtime_checkable
class DeliverableSource(Protocol):
    """Live, project-scoped Deliverable candidates (#26)."""

    async def query_project_deliverables(
        self, data_source_id: str, project_page_id: str
    ) -> list[Candidate]: ...
