"""Fake adapters for whole-pipeline tests (real SQLite, fake I/O)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from t6.domain import JobOrder
from t6.notion.writes import Ambiguous

from jo_watchdog.adapters.protocols import ClassifyResult, IncomingUpdate
from jo_watchdog.core.extraction import EXTRACTED_FIELDS, PROMPT_TAG, Extraction

# 2026-09-28 03:00:00 UTC = 11:00 in Manila.
POSTED_AT_UNIX = 1790564400


def update(
    update_id: int,
    *,
    chat_id: int = -100123,
    topic_id: int = 5,
    message_id: int = 1,
    text: str = "first line",
    extra: dict | None = None,
) -> IncomingUpdate:
    message = {"chat": {"id": chat_id}, "date": POSTED_AT_UNIX, "text": text, **(extra or {})}
    return IncomingUpdate(
        update_id=update_id,
        chat_id=chat_id,
        message_id=message_id,
        message_thread_id=topic_id,
        text=text,
        raw={"update_id": update_id, "message": message},
    )


def extraction(verdict: str = "jo", **fields) -> Extraction:
    base = {field: None for field in EXTRACTED_FIELDS}
    base.update(
        verdict=verdict,
        confidence="high",
        reasons=f"Looks like {verdict}.",
        multiple_jo_suspected=False,
    )
    base.update(fields)
    return Extraction(**base)


def ok_result(verdict: str = "jo", **fields) -> ClassifyResult:
    return ClassifyResult(
        outcome="ok",
        extraction=extraction(verdict, **fields),
        requested_model="deepseek-chat",
        returned_model="deepseek-chat-v9",
        prompt_tag=PROMPT_TAG,
        input_tokens=1200,
        output_tokens=300,
        latency_ms=850,
    )


def failed_result(outcome: str = "timeout") -> ClassifyResult:
    return ClassifyResult(
        outcome=outcome,
        error=f"{outcome} happened",
        requested_model="deepseek-chat",
        prompt_tag=PROMPT_TAG,
        latency_ms=60000,
    )


class FakeClassifier:
    """Returns scripted results in order (the last one repeats); records inputs."""

    def __init__(self, *results: ClassifyResult):
        self.results = list(results) or [ok_result()]
        self.calls: list[tuple[str, list[str] | None]] = []

    async def classify(self, masked_text, origin_lines=None) -> ClassifyResult:
        self.calls.append((masked_text, origin_lines))
        if len(self.results) > 1:
            return self.results.pop(0)
        return self.results[0]


class FakeTelegram:
    """Returns queued updates; records reactions and ops posts."""

    def __init__(self, updates=None, *, fail_reaction: bool = False, fail_send: int = 0):
        self.updates = list(updates or [])
        self.reactions: list[tuple[int, int, str]] = []
        self.sent: list[tuple[int, int, str]] = []
        self.get_updates_calls: list[int] = []
        self.fail_reaction = fail_reaction
        self.fail_send = fail_send  # fail this many send_message calls first

    async def get_updates(self, offset: int, timeout: int) -> list[IncomingUpdate]:
        self.get_updates_calls.append(offset)
        # Emit queued updates whose id >= offset, then clear the queue so the
        # same batch is not re-emitted on the next poll.
        batch = [u for u in self.updates if u.update_id >= offset]
        self.updates = []
        return batch

    async def set_reaction(self, chat_id: int, message_id: int, emoji: str) -> None:
        if self.fail_reaction:
            raise RuntimeError("reaction not allowed")
        self.reactions.append((chat_id, message_id, emoji))

    async def send_message(self, chat_id: int, topic_id: int, text: str) -> int:
        if self.fail_send:
            self.fail_send -= 1
            raise RuntimeError("telegram send failed")
        self.sent.append((chat_id, topic_id, text))
        return 9000 + len(self.sent)


class FakeNotion:
    """Stands in for t6-core's Job Order writes: saves what it is sent and reads it back."""

    def __init__(
        self,
        *,
        fail_create: bool = False,
        wrong_name: bool = False,
        fail_retrieve: int = 0,
        fail_find: int = 0,
        create_error: Exception | None = None,
        ambiguous: Ambiguous | None = None,
    ):
        self.fail_create = fail_create
        self.wrong_name = wrong_name  # the page reads back with another name: `Ambiguous` mismatch
        self.fail_retrieve = fail_retrieve  # fail this many reads: the create's own, then `get`
        self.fail_find = fail_find  # fail this many Telegram-Key queries first
        self.find_calls = 0
        self.create_calls = 0
        self.create_error = create_error  # raised instead of the default failure
        self.ambiguous = ambiguous  # returned (after saving the page) instead of the JobOrder
        self.created: list[dict] = []  # the fields each create was sent
        self.pages: dict[str, JobOrder] = {}
        self._counter = 0

    async def create(self, data_source_id: str, fields: Mapping[str, Any]):
        self.create_calls += 1
        if self.fail_create:
            raise self.create_error or RuntimeError("notion create failed")
        self._counter += 1
        page_id = f"page-{self._counter}"
        name = "WRONG NAME" if self.wrong_name else fields["name"]
        saved = JobOrder(
            page_id=page_id,
            name=name,
            telegram_key=fields["telegram_key"],
            source_text=fields.get("source_text"),
        )
        self.pages[page_id] = saved
        self.created.append(dict(fields))
        if self.ambiguous is not None:
            return replace(self.ambiguous, page_id=self.ambiguous.page_id or page_id)
        if self.wrong_name:
            return Ambiguous("mismatch", "saved values differ for: name", page_id, ("name",))
        if self.fail_retrieve:  # t6-core's own read-back failing is the first failed read
            self.fail_retrieve -= 1
            return Ambiguous("readback_failed", "read-back failed", page_id)
        return saved

    async def get(self, data_source_id: str, page_id: str) -> JobOrder:
        if self.fail_retrieve:
            self.fail_retrieve -= 1
            raise RuntimeError("notion retrieve failed")
        return self.pages[page_id]

    async def find_all_by_telegram_key(self, data_source_id: str, telegram_key: str):
        self.find_calls += 1
        if self.fail_find:
            self.fail_find -= 1
            raise RuntimeError("notion query failed")
        return [page for page in self.pages.values() if page.telegram_key == telegram_key]
