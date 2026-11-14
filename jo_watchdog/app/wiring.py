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
    gateway_secret,
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
    studio: object
    classifier: object
    matcher: object | None
    masked_extractor: object | None = None
    closers: list = field(default_factory=list)

    def with_(self, **replacements) -> Adapters:
        """A copy with some adapters swapped; the swapped-out real ones stay closable."""
        return replace(self, **replacements)

    async def close(self) -> None:
        for closer in self.closers:
            await closer()


def build_adapters(config: Config) -> Adapters:
    """The real aiogram, httpx, DeepSeek and Jev adapters, from the environment's secrets."""
    from jo_watchdog.adapters.deepseek import DeepSeekClassifier
    from jo_watchdog.adapters.jev import JevMatcher
    from jo_watchdog.adapters.t6_studio import T6Studio

    studio = T6Studio(notion_token(), notion_version=config.notion_version)
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

    masked_extractor = None
    if config.llm_shadow_masking:
        from jo_watchdog.adapters.t6_llm import MaskedExtractor

        masked_extractor = MaskedExtractor(
            deepseek_token(),
            base_url=config.deepseek_base_url,
            model=config.deepseek_model,
            timeout_seconds=config.deepseek_timeout_seconds,
        )

    telegram, close_telegram = _build_telegram(config)
    return Adapters(
        telegram=telegram,
        notion=studio,
        studio=studio,
        classifier=classifier,
        matcher=matcher,
        masked_extractor=masked_extractor,
        closers=[
            studio.close,
            classifier.close,
            matcher.close,
            close_telegram,
            *([masked_extractor.close] if masked_extractor else []),
        ],
    )


def _build_telegram(config: Config):
    """The Telegram client for ``telegram_mode`` and the coroutine that closes it."""
    if config.telegram_mode == "gateway":
        from jo_watchdog.adapters.gateway import GatewayTelegramClient

        client = GatewayTelegramClient(config.gateway_url, gateway_secret())
        return client, client.close

    from aiogram import Bot

    from jo_watchdog.adapters.telegram import AiogramTelegramClient

    bot = Bot(token=telegram_token())

    async def close_bot() -> None:
        await bot.session.close()

    return AiogramTelegramClient(bot), close_bot


def build_worker(config: Config, ledger: Ledger, adapters: Adapters, **worker_options) -> Worker:
    """The production worker: persisted caches loaded; t6-core supplies caches and candidates."""
    caches = CacheManager(ledger=ledger, studio=adapters.studio, config=config)
    caches.load_persisted()  # last good snapshots first, then the live refresh
    return Worker(
        ledger=ledger,
        telegram=adapters.telegram,
        notion=adapters.notion,
        classifier=adapters.classifier,
        config=config,
        caches=caches,
        matcher=adapters.matcher,
        masked_extractor=adapters.masked_extractor,
        deliverables=adapters.studio,
        **worker_options,
    )


def missing_wiring(worker: Worker) -> list[str]:
    """Names of the production collaborators this worker lacks (empty when fully wired)."""
    return [
        name
        for name in ("caches", "matcher", "deliverables")
        if getattr(worker, name) is None
    ]
