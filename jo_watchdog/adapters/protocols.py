"""Adapter contracts, expressed as plain models and Protocols.

The worker depends only on these shapes, so tests substitute fake adapters and
the real adapters stay behind the seam (aiogram/httpx are never imported by the
worker). ``IncomingUpdate`` is the normalized shape the real Telegram adapter
maps an aiogram ``Update`` into.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel

from jo_watchdog.core.extraction import Extraction
from jo_watchdog.core.matching import Candidate, MatchResult


class IncomingUpdate(BaseModel):
    """A normalized Telegram update, decoupled from aiogram's model tree."""

    update_id: int
    chat_id: int
    message_id: int
    message_thread_id: int | None = None
    text: str = ""
    raw: dict[str, Any]
    # An ``edited_message`` update (#24): ``raw`` holds ``edited_message``, and
    # ``edit_date`` is Telegram's unix time of the edit.
    edited: bool = False
    edit_date: int | None = None


@dataclass(frozen=True)
class GatewayRef:
    """What ``send_message`` returns when the post is only queued (gateway mode, #109).

    A ref is the gateway's queue handle, not a Telegram message id: the worker stores it
    in its own column and waits for the gateway to report the real id. A client that
    returns a plain ``int`` has posted straight to Telegram and returned the real id.
    """

    ref: int


@dataclass(frozen=True)
class NotifyStatus:
    """The gateway's answer for one ref: ``queued`` or ``sent`` (with the real id)."""

    state: str
    telegram_message_id: int | None = None


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
    retry_after: float | None = None  # seconds, from the provider's Retry-After header


@runtime_checkable
class TelegramClient(Protocol):
    async def get_updates(self, offset: int, timeout: int) -> list[IncomingUpdate]: ...

    async def set_reaction(self, chat_id: int, message_id: int, emoji: str) -> None: ...

    async def send_message(self, chat_id: int, topic_id: int, text: str) -> int:
        """Post Telegram-HTML ``text`` to a forum topic; return the new message id.

        A client that sets ``accepts_idempotency_key`` (the gateway client, #95) also
        takes ``idempotency_key=`` and returns a ``GatewayRef`` (the gateway's
        ``message_ref``, not a Telegram id) and offers ``notify_status(ref)``.
        """
        ...


@runtime_checkable
class NotionClient(Protocol):
    async def create_page(self, payload: dict) -> PageRef: ...

    async def retrieve_page(self, page_id: str) -> dict: ...

    async def find_pages_by_telegram_key(
        self, data_source_id: str, telegram_key: str
    ) -> list[PageRef]:
        """Pages whose ``Telegram Key`` equals ``telegram_key``, oldest first.

        Reserved for recovering from an uncertain write; normal success reads
        back by id.
        """
        ...


@runtime_checkable
class NotionReader(Protocol):
    """The read side the six-hour caches need."""

    async def query_all(self, data_source_id: str) -> list[dict[str, Any]]: ...

    async def retrieve_data_source(self, data_source_id: str) -> dict: ...


@runtime_checkable
class Classifier(Protocol):
    async def classify(
        self, masked_text: str, origin_lines: list[str] | None
    ) -> ClassifyResult: ...


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
