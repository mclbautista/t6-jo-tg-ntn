"""Fake adapters for whole-pipeline tests (real SQLite, fake I/O)."""

from __future__ import annotations

from jo_watchdog.adapters.protocols import ClassifyResult, IncomingUpdate, PageRef
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
    """Returns queued updates; records reactions."""

    def __init__(self, updates=None):
        self.updates = list(updates or [])
        self.reactions: list[tuple[int, int, str]] = []
        self.get_updates_calls: list[int] = []

    async def get_updates(self, offset: int, timeout: int) -> list[IncomingUpdate]:
        self.get_updates_calls.append(offset)
        # Emit queued updates whose id >= offset, then clear the queue so the
        # same batch is not re-emitted on the next poll.
        batch = [u for u in self.updates if u.update_id >= offset]
        self.updates = []
        return batch

    async def set_reaction(self, chat_id: int, message_id: int, emoji: str) -> None:
        self.reactions.append((chat_id, message_id, emoji))


class FakeNotion:
    """Echoes written properties back so read-back comparison can run."""

    def __init__(self, *, fail_create: bool = False, wrong_name: bool = False):
        self.fail_create = fail_create
        self.wrong_name = wrong_name
        self.created: list[dict] = []
        self.pages: dict[str, dict] = {}
        self._counter = 0

    async def create_page(self, payload: dict):
        if self.fail_create:
            raise RuntimeError("notion create failed")
        self._counter += 1
        page_id = f"page-{self._counter}"
        name = payload["properties"]["Name"]["title"][0]["text"]["content"]
        key = payload["properties"]["Telegram Key"]["rich_text"][0]["text"]["content"]
        if self.wrong_name:
            name = "WRONG NAME"
        self.pages[page_id] = {
            "id": page_id,
            "url": f"https://notion.example/{page_id}",
            "properties": {
                "Name": {"title": [{"plain_text": name}]},
                "Telegram Key": {"rich_text": [{"plain_text": key}]},
            },
        }
        self.created.append(payload)
        return PageRef(id=page_id, url=f"https://notion.example/{page_id}")

    async def retrieve_page(self, page_id: str) -> dict:
        return self.pages[page_id]
