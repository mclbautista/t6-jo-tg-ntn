"""Entry point: ``python -m jo_watchdog.app [run|healthcheck] --config config.yaml``.

``run`` (the default) builds the real adapters (aiogram Telegram, httpx Notion)
and the ledger, then runs the worker loop. Secrets come from the environment
(``TELEGRAM_BOT_TOKEN``, ``NOTION_API_KEY``, ``DEEPSEEK_API_KEY``,
``OPENROUTER_API_KEY``); non-secrets come from the YAML config.

``healthcheck`` exits 0 when the poller's liveness stamp is fresh and 1
otherwise; the container's ``HEALTHCHECK`` runs it.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from jo_watchdog.app import healthcheck
from jo_watchdog.app.config import (
    load_config,
    missing_secrets,
    notion_token,
    require_live_keys,
    telegram_token,
)
from jo_watchdog.app.logging_config import configure_logging
from jo_watchdog.app.worker import Worker
from jo_watchdog.ledger.db import Ledger


async def run(config_path: str) -> None:
    config = load_config(config_path)
    require_live_keys(config)
    missing = missing_secrets()
    if missing:
        raise RuntimeError(f"missing secrets in the environment: {', '.join(missing)}")
    configure_logging(log_path=Path(config.ledger_path).with_name("jo-watchdog.jsonl"))
    ledger = Ledger(config.ledger_path)

    from aiogram import Bot

    from jo_watchdog.adapters.notion import NotionAdapter
    from jo_watchdog.adapters.telegram import AiogramTelegramClient

    bot = Bot(token=telegram_token())
    telegram = AiogramTelegramClient(bot)
    notion = NotionAdapter(notion_token())

    worker = Worker(ledger=ledger, telegram=telegram, notion=notion, config=config)

    try:
        await worker.run()
    finally:
        await notion.close()
        await bot.session.close()
        ledger.close()


def run_healthcheck(config_path: str) -> int:
    try:
        config = load_config(config_path)
    except Exception as exc:  # a broken config is an unhealthy container
        print(f"unhealthy: bad config: {exc}", file=sys.stderr)
        return 1
    healthy, reason = healthcheck.check(config.ledger_path)
    print(f"{'healthy' if healthy else 'unhealthy'}: {reason}")
    return 0 if healthy else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="JO Telegram Watchdog")
    parser.add_argument(
        "command",
        nargs="?",
        default="run",
        choices=["run", "healthcheck"],
        help="run the watchdog (default) or check the poller's liveness",
    )
    parser.add_argument("--config", default="config.yaml", help="path to config.yaml")
    args = parser.parse_args(argv)
    if args.command == "healthcheck":
        return run_healthcheck(args.config)
    try:
        asyncio.run(run(args.config))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
