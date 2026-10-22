"""Entry point: ``python -m jo_watchdog.app [run|healthcheck|retry] --config config.yaml``.

``run`` (the default) builds the real adapters (aiogram Telegram, httpx Notion)
and the ledger, then runs the worker loop. Secrets come from the environment
(``TELEGRAM_BOT_TOKEN``, ``NOTION_API_KEY``, ``DEEPSEEK_API_KEY``,
``OPENROUTER_API_KEY``); non-secrets come from the YAML config.

``retry <telegram_key>`` (or ``retry --all-exhausted``) makes exhausted messages
eligible again without touching their stored plan.

``healthcheck`` exits 0 when the poller's liveness stamp is fresh and 1
otherwise; the container's ``HEALTHCHECK`` runs it.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

from jo_watchdog.app import healthcheck
from jo_watchdog.app.caches import CacheManager
from jo_watchdog.app.config import (
    deepseek_token,
    load_config,
    missing_secrets,
    notion_token,
    require_live_keys,
    telegram_token,
)
from jo_watchdog.app.logging_config import configure_logging
from jo_watchdog.app.worker import DeadmanExit, Worker
from jo_watchdog.ledger.db import Ledger

_TELEGRAM_KEY = re.compile(r"-?\d+:\d+")


async def run(config_path: str) -> None:
    config = load_config(config_path)
    require_live_keys(config)
    missing = missing_secrets()
    if missing:
        raise RuntimeError(f"missing secrets in the environment: {', '.join(missing)}")
    configure_logging(log_path=Path(config.ledger_path).with_name("jo-watchdog.jsonl"))
    ledger = Ledger(config.ledger_path)

    from aiogram import Bot

    from jo_watchdog.adapters.deepseek import DeepSeekClassifier
    from jo_watchdog.adapters.notion import NotionAdapter
    from jo_watchdog.adapters.telegram import AiogramTelegramClient

    bot = Bot(token=telegram_token())
    telegram = AiogramTelegramClient(bot)
    notion = NotionAdapter(notion_token())
    classifier = DeepSeekClassifier(
        deepseek_token(),
        base_url=config.deepseek_base_url,
        model=config.deepseek_model,
        timeout_seconds=config.deepseek_timeout_seconds,
    )

    caches = CacheManager(ledger=ledger, notion=notion, config=config)
    caches.load_persisted()  # last good snapshots first, then the live refresh
    worker = Worker(
        ledger=ledger,
        telegram=telegram,
        notion=notion,
        classifier=classifier,
        config=config,
        caches=caches,
    )

    try:
        await worker.run()
    finally:
        await notion.close()
        await classifier.close()
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


def run_retry(config_path: str, telegram_key: str | None, all_exhausted: bool) -> int:
    """Make exhausted messages eligible again; the stored plan is never changed."""
    if bool(telegram_key) == all_exhausted:
        print("usage: retry <telegram_key> | retry --all-exhausted", file=sys.stderr)
        return 2
    config = load_config(config_path)
    configure_logging(log_path=Path(config.ledger_path).with_name("jo-watchdog.jsonl"))
    ledger = Ledger(config.ledger_path)
    try:
        now = datetime.now(UTC)
        if all_exhausted:
            keys = ledger.exhausted_keys()
            count = ledger.retry_all_exhausted(now=now)
        else:
            keys = [telegram_key] if ledger.retry(telegram_key, now=now) else []
            count = len(keys)
    finally:
        ledger.close()
    who = getpass.getuser()
    for key in keys:
        logging.getLogger("jo_watchdog.retry").info("retry of %s requested by %s", key, who)
    print(f"{count} message(s) made eligible for retry")
    return 0 if count or all_exhausted else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="JO Telegram Watchdog")
    parser.add_argument(
        "command",
        nargs="?",
        default="run",
        choices=["run", "healthcheck", "retry"],
        help="run the watchdog (default), check the poller's liveness, or retry exhausted messages",
    )
    parser.add_argument("telegram_key", nargs="?", help="for retry: the message's Telegram Key")
    parser.add_argument(
        "--all-exhausted", action="store_true", help="for retry: every exhausted message"
    )
    parser.add_argument("--config", default="config.yaml", help="path to config.yaml")
    args, extra = parser.parse_known_args(argv)
    # Group chat ids are negative, so a Telegram Key like ``-100123:42`` looks like
    # a flag to argparse; take it as the key instead of rejecting it.
    keys = [arg for arg in extra if _TELEGRAM_KEY.fullmatch(arg)]
    if keys != extra or len(keys) > 1 or (keys and args.telegram_key):
        parser.error(f"unrecognized arguments: {' '.join(extra)}")
    if keys:
        args.telegram_key = keys[0]
    if args.command == "healthcheck":
        return run_healthcheck(args.config)
    if args.command == "retry":
        return run_retry(args.config, args.telegram_key, args.all_exhausted)
    try:
        asyncio.run(run(args.config))
    except KeyboardInterrupt:
        pass
    except DeadmanExit as exc:
        # Non-zero, so the container's restart policy brings the process back.
        print(f"deadman: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
