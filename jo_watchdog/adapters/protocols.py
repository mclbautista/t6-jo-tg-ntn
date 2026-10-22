"""Adapter contracts, expressed as plain models and Protocols.

The worker depends only on these shapes, so tests substitute fake adapters and
the real adapters stay behind the seam (aiogram/httpx are never imported by the
worker). ``IncomingUpdate`` is the normalized shape the real Telegram adapter
maps an aiogram ``Update`` into.
"""

from __future__ import annotations

from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel

from jo_watchdog.core.extraction import Extraction


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


ClassifierOutcome = Literal[
    "ok", "invalid_json", "schema_violation", "empty_content", "timeout", "api_error"
]


class ClassifyResult(BaseModel):
    """One classify/extract call: the validated result or its failure class, plus trace."""

    outcome: ClassifierOutcome
    extraction: Extraction | None = None
    error: str | None = None
    requested_model: str
    returned_model: str | None = None
    prompt_tag: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    latency_ms: int | None = None


@runtime_checkable
class TelegramClient(Protocol):
    async def get_updates(self, offset: int, timeout: int) -> list[IncomingUpdate]: ...

    async def set_reaction(self, chat_id: int, message_id: int, emoji: str) -> None: ...


@runtime_checkable
class NotionClient(Protocol):
    async def create_page(self, payload: dict) -> PageRef: ...

    async def retrieve_page(self, page_id: str) -> dict: ...


@runtime_checkable
class Classifier(Protocol):
    async def classify(
        self, masked_text: str, origin_lines: list[str] | None
    ) -> ClassifyResult: ...
