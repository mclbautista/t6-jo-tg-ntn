"""A fake clock and a restartable pipeline harness for the reliability tests (#25).

The ledger stamps rows with ``jo_watchdog.ledger.db._now``; the worker reads its
``clock``. ``Pipeline`` points both at one ``FakeClock``, so a test moves time
by calling ``clock.advance`` instead of sleeping.
"""

from __future__ import annotations

import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

from jo_watchdog.app.config import Config
from jo_watchdog.app.worker import Worker
from jo_watchdog.ledger.db import Ledger, fmt_utc
from tests.fakes import FakeClassifier, FakeNotion, FakeTelegram

KEY = "-100123:42"
OWNER = "owner"

# 2026-10-22 01:00 UTC is 09:00 in Manila, two hours after that day's 07:00 boundary.
START = datetime(2026, 10, 22, 1, 0, tzinfo=UTC)


class FakeClock:
    def __init__(self, start: datetime = START):
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta) -> None:
        self.now += timedelta(**delta)


async def no_sleep(_seconds: float) -> None:
    """Inline reaction retries wait 2s and 5s; tests record them instead."""


def make_config(**overrides) -> Config:
    base = dict(
        intake_chat_id=-100123,
        intake_topic_id=5,
        ops_chat_id=-100999,
        ops_topic_id=7,
        leizette_telegram_handle="leizette",
        owner_telegram_handle=OWNER,
        captured_reaction="✅",
        job_orders_data_source_id="ds-1",
    )
    base.update(overrides)
    return Config(**base)


class Pipeline:
    """A worker over a real SQLite ledger and fake adapters, restartable like a process."""

    def __init__(
        self,
        test,
        updates=(),
        *,
        telegram=None,
        notion=None,
        classifier=None,
        clock: FakeClock | None = None,
        caches=None,
        **config,
    ):
        self.tmp = tempfile.TemporaryDirectory()
        test.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "ledger.db"
        self.clock = clock or FakeClock()
        patcher = mock.patch("jo_watchdog.ledger.db._now", lambda: fmt_utc(self.clock()))
        patcher.start()
        test.addCleanup(patcher.stop)
        self.telegram = telegram or FakeTelegram(list(updates))
        self.notion = notion or FakeNotion()
        self.classifier = classifier or FakeClassifier()
        self.caches = caches
        self.config = make_config(**config)
        self.sleeps: list[float] = []
        self.ledger: Ledger | None = None
        self.start(test)

    async def _sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)

    def start(self, test=None) -> None:
        """Open the ledger and build a fresh worker, as a restarted process would."""
        if self.ledger is not None:
            self.ledger.close()
        self.ledger = Ledger(self.path)
        self.worker = self.make_worker(self.ledger)
        if test is not None:
            test.addCleanup(lambda: self.ledger.close())

    def make_worker(self, ledger: Ledger, **overrides) -> Worker:
        kwargs = dict(
            ledger=ledger,
            telegram=self.telegram,
            notion=self.notion,
            classifier=self.classifier,
            config=self.config,
            caches=self.caches,
            clock=self.clock,
            sleep=self._sleep,
            deadman_interval=0.01,
        )
        kwargs.update(overrides)
        return Worker(**kwargs)

    def outbox(self, kind: str | None = None) -> list[dict]:
        sql = "SELECT * FROM outbox" + (" WHERE kind = ?" if kind else "") + " ORDER BY id"
        rows = self.ledger._conn.execute(sql, (kind,) if kind else ())
        return [dict(r) for r in rows]

    def message(self, key: str = KEY) -> dict:
        return self.ledger.get_message(key)

    async def cycles(self, count: int, *, step_seconds: int = 0) -> None:
        for _ in range(count):
            await self.worker.run_once()
            self.clock.advance(seconds=step_seconds)


class Crash(BaseException):
    """A simulated process death: nothing in the worker may catch or handle it."""


async def exhaust(pipeline: Pipeline) -> None:
    """Fail five attempts in a row (every delay is <= 15 minutes)."""
    for _ in range(5):
        await pipeline.worker.run_once()
        pipeline.clock.advance(seconds=900)
