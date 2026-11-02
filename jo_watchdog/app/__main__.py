"""Entry point: ``python -m jo_watchdog.app [run|healthcheck|retry] --config config.yaml``.

``run`` (the default) builds the real adapters (aiogram Telegram, httpx Notion)
and the ledger, then runs the worker loop. Secrets come from the environment
(``TELEGRAM_BOT_TOKEN`` or, with ``telegram_mode: gateway``, ``WATCHDOG_SECRET``;
``NOTION_API_KEY``, ``DEEPSEEK_API_KEY``, ``OPENROUTER_API_KEY``); non-secrets come from the
YAML config.

``retry <telegram_key>`` (or ``retry --all-exhausted``) makes exhausted messages
eligible again without touching their stored plan.

``reset-offset --to N`` sets the polling offset; used once at the gateway cutover (#95),
when the cursor moves from Telegram update ids to the gateway's sequence numbers.

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
from jo_watchdog.app.config import (
    load_config,
    missing_secrets,
    require_live_keys,
)
from jo_watchdog.app.logging_config import configure_logging
from jo_watchdog.app.wiring import build_adapters, build_worker
from jo_watchdog.app.worker import DeadmanExit
from jo_watchdog.ledger.db import Ledger

_TELEGRAM_KEY = re.compile(r"-?\d+:\d+")


async def run(config_path: str) -> None:
    config = load_config(config_path)
    require_live_keys(config)
    missing = missing_secrets(config.telegram_mode)
    if missing:
        raise RuntimeError(f"missing secrets in the environment: {', '.join(missing)}")
    configure_logging(log_path=Path(config.ledger_path).with_name("jo-watchdog.jsonl"))
    ledger = Ledger(config.ledger_path)

    adapters = build_adapters(config)
    worker = build_worker(config, ledger, adapters)

    try:
        await worker.run()
    finally:
        await adapters.close()
        ledger.close()


def run_reset_offset(config_path: str, to: int) -> int:
    """Set the polling offset (once, at the gateway cutover); run with the watchdog stopped."""
    config = load_config(config_path)
    configure_logging(log_path=Path(config.ledger_path).with_name("jo-watchdog.jsonl"))
    ledger = Ledger(config.ledger_path)
    try:
        previous = ledger.reset_offset(to)
    finally:
        ledger.close()
    logging.getLogger("jo_watchdog.reset_offset").info(
        "offset reset from %s to %s by %s", previous, to, getpass.getuser()
    )
    print(f"offset reset from {previous} to {to}")
    return 0


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
        choices=["run", "healthcheck", "retry", "reset-offset"],
        help="run the watchdog (default), check the poller's liveness, retry exhausted "
        "messages, or reset the polling offset",
    )
    parser.add_argument(
        "--to", type=int, help="for reset-offset: the offset to set (0 at the gateway cutover)"
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
    if args.command == "reset-offset":
        if args.to is None or args.to < 0:
            parser.error("reset-offset needs --to N (N >= 0)")
        return run_reset_offset(args.config, args.to)
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
