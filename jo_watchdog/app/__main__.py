"""Entry point: ``python -m jo_watchdog.app --config config.yaml``.

Builds the real adapters (aiogram Telegram, httpx Notion) and the ledger, then
runs the worker loop. Secrets come from the environment (``TELEGRAM_BOT_TOKEN``,
``NOTION_API_KEY``); non-secrets come from the YAML config.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from jo_watchdog.app.config import load_config, notion_token, telegram_token
from jo_watchdog.app.logging_config import configure_logging
from jo_watchdog.app.worker import Worker
from jo_watchdog.ledger.db import Ledger


async def run(config_path: str) -> None:
    config = load_config(config_path)
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


def main() -> None:
    parser = argparse.ArgumentParser(description="JO Telegram Watchdog")
    parser.add_argument("--config", default="config.yaml", help="path to config.yaml")
    args = parser.parse_args()
    try:
        asyncio.run(run(args.config))
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
