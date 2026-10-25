"""Production adapter and worker construction, shared by ``run`` and ``smoke.py``.

The smoke check must exercise the same wiring the service runs: caches, the Jev
matcher and the Deliverable source all present. Both entry points build their
worker here, and a caller replaces individual adapters (for example a recording
Telegram) rather than assembling a worker by hand.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from jo_watchdog.app.caches import CacheManager
from jo_watchdog.app.config import (
    Config,
    deepseek_token,
    notion_token,
    openrouter_token,
    telegram_token,
)
from jo_watchdog.app.worker import Worker
from jo_watchdog.ledger.db import Ledger


@dataclass
class Adapters:
    """The four external adapters; ``closers`` release whatever the real ones opened."""

    telegram: object
    notion: object
    classifier: object
    matcher: object | None
    closers: list = field(default_factory=list)

    def with_(self, **replacements) -> Adapters:
        """A copy with some adapters swapped; the swapped-out real ones stay closable."""
        return replace(self, **replacements)

    async def close(self) -> None:
        for closer in self.closers:
            await closer()


def build_adapters(config: Config) -> Adapters:
    """The real aiogram, httpx, DeepSeek and Jev adapters, from the environment's secrets."""
    from aiogram import Bot

    from jo_watchdog.adapters.deepseek import DeepSeekClassifier
    from jo_watchdog.adapters.jev import JevMatcher
    from jo_watchdog.adapters.notion import NotionAdapter
    from jo_watchdog.adapters.telegram import AiogramTelegramClient

    bot = Bot(token=telegram_token())
    notion = NotionAdapter(notion_token())
    classifier = DeepSeekClassifier(
        deepseek_token(),
        base_url=config.deepseek_base_url,
        model=config.deepseek_model,
        timeout_seconds=config.deepseek_timeout_seconds,
    )
    matcher = JevMatcher(
        openrouter_token(),
        url=config.jev_url,
        model=config.jev_model,
        timeout=config.jev_timeout_seconds,
    )

    async def close_bot() -> None:
        await bot.session.close()

    return Adapters(
        telegram=AiogramTelegramClient(bot),
        notion=notion,
        classifier=classifier,
        matcher=matcher,
        closers=[notion.close, classifier.close, matcher.close, close_bot],
    )


def build_worker(config: Config, ledger: Ledger, adapters: Adapters, **worker_options) -> Worker:
    """The production worker: persisted caches loaded, Notion also the Deliverable source."""
    caches = CacheManager(ledger=ledger, notion=adapters.notion, config=config)
    caches.load_persisted()  # last good snapshots first, then the live refresh
    return Worker(
        ledger=ledger,
        telegram=adapters.telegram,
        notion=adapters.notion,
        classifier=adapters.classifier,
        config=config,
        caches=caches,
        matcher=adapters.matcher,
        deliverables=adapters.notion,
        **worker_options,
    )


def missing_wiring(worker: Worker) -> list[str]:
    """Names of the production collaborators this worker lacks (empty when fully wired)."""
    return [
        name
        for name in ("caches", "matcher", "deliverables")
        if getattr(worker, name) is None
    ]
