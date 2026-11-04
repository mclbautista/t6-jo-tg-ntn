"""Entry point: ``python -m jo_watchdog.app [run|healthcheck|retry] --config config.yaml``.

``run`` (the default) builds the real adapters (aiogram Telegram, httpx Notion)
and the ledger, then runs the worker loop. Secrets come from the environment
(``TELEGRAM_BOT_TOKEN`` or, with ``telegram_mode: gateway``, ``WATCHDOG_SECRET``;
``NOTION_API_KEY``, ``DEEPSEEK_API_KEY``, ``OPENROUTER_API_KEY``); non-secrets come from the
YAML config.

``retry <telegram_key>`` (or ``retry --all-exhausted``) makes exhausted messages
eligible again without touching their stored plan.

``ingest --json FILE|-`` (or ``ingest --text ... --message-id N``) backfills an
intake message that never reached the watchdog (#101): it is queued as ``received``
and the running watchdog captures it through the normal pipeline.

``reset-offset --to N`` sets the polling offset; used once at the gateway cutover (#95),
when the cursor moves from Telegram update ids to the gateway's sequence numbers.

``healthcheck`` exits 0 when the poller's liveness stamp is fresh and 1
otherwise; the container's ``HEALTHCHECK`` runs it.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import logging
import re
import sqlite3
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from jo_watchdog.app import healthcheck
from jo_watchdog.app.config import (
    load_config,
    missing_secrets,
    require_live_keys,
)
from jo_watchdog.app.logging_config import configure_logging
from jo_watchdog.app.wiring import build_adapters, build_worker
from jo_watchdog.app.worker import DeadmanExit, posted_at_from_raw
from jo_watchdog.core.backfill import (
    Backfill,
    BackfillError,
    from_payload,
    from_text,
    scope_problem,
)
from jo_watchdog.core.payload import page_url_for
from jo_watchdog.core.plan import first_line_of
from jo_watchdog.ledger.db import Ledger, fmt_utc
from jo_watchdog.ledger.states import State

_TELEGRAM_KEY = re.compile(r"-?\d+:\d+")
_MANILA = ZoneInfo("Asia/Manila")
INGEST_POLL_SECONDS = 2.0


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


def _unix_date(value: str) -> int:
    """``--date``: unix seconds or ISO-8601; a time without an offset is Manila time."""
    if value.lstrip("-").isdigit():
        return int(value)
    when = datetime.fromisoformat(value)
    if when.tzinfo is None:
        when = when.replace(tzinfo=_MANILA)
    return int(when.timestamp())


def _read_backfill(args, config, *, by: str, at: str) -> Backfill:
    if args.json is not None:
        source = sys.stdin.read() if args.json == "-" else Path(args.json).read_text("utf-8")
        try:
            payload = json.loads(source)
        except ValueError as exc:
            raise BackfillError(f"not JSON: {exc}") from exc
        return from_payload(payload, by=by, at=at)
    return from_text(
        args.text,
        message_id=args.message_id,
        chat_id=config.intake_chat_id if args.chat_id is None else args.chat_id,
        topic_id=config.intake_topic_id,
        from_user=args.from_user,
        date=None if args.date is None else _unix_date(args.date),
        by=by,
        at=at,
    )


def _wait_for_capture(ledger: Ledger, key: str, wait: float) -> dict:
    """Re-read the row until the worker finishes with it or ``wait`` seconds pass."""
    deadline = time.monotonic() + wait
    while True:
        row = ledger.get_message(key) or {}
        finished = row.get("state") in (State.DONE.value, State.IGNORED.value)
        if finished or row.get("exhausted_at") or time.monotonic() >= deadline:
            return row
        time.sleep(INGEST_POLL_SECONDS)


def run_ingest(config_path: str, args) -> int:
    """Queue a missed intake message for capture; the running watchdog does the rest."""
    config = load_config(config_path)
    configure_logging(log_path=Path(config.ledger_path).with_name("jo-watchdog.jsonl"))
    who = getpass.getuser()
    try:
        backfill = _read_backfill(args, config, by=who, at=fmt_utc(datetime.now(UTC)))
    except (BackfillError, OSError, ValueError) as exc:
        print(f"ingest: {exc}", file=sys.stderr)
        return 2
    problem = scope_problem(
        backfill, intake_chat_id=config.intake_chat_id, intake_topic_id=config.intake_topic_id
    )
    if problem:
        print(f"ingest: not an intake message: {problem}", file=sys.stderr)
        return 2
    ledger = Ledger(config.ledger_path)
    try:
        try:
            key = ledger.ingest_message(
                backfill.chat_id,
                backfill.message_id,
                json.dumps(backfill.raw),
                backfill.text,
                first_line_of(backfill.text),
                posted_at=posted_at_from_raw(backfill.raw),
            )
        except sqlite3.IntegrityError:
            existing = ledger.get_message(f"{backfill.chat_id}:{backfill.message_id}") or {}
            print(
                f"ingest: {backfill.chat_id}:{backfill.message_id} is already in the ledger"
                f" (state {existing.get('state')}); nothing queued",
                file=sys.stderr,
            )
            return 1
        logging.getLogger("jo_watchdog.ingest").info("backfill of %s requested by %s", key, who)
        print(f"queued {key}; the running watchdog captures it on its next cycle")
        if args.wait:
            row = _wait_for_capture(ledger, key, args.wait)
            line = f"{key}: {row.get('state')}"
            if row.get("notion_page_id"):
                line += f" {page_url_for(row['notion_page_id'])}"
            if row.get("exhausted_at"):
                line += f" (retries exhausted: {row.get('last_error')})"
            elif row.get("state") not in (State.DONE.value, State.IGNORED.value):
                line = (
                    f"{key}: still {row.get('state')} after {args.wait:g}s;"
                    " is the watchdog running?"
                )
            print(line)
    finally:
        ledger.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="JO Telegram Watchdog")
    parser.add_argument(
        "command",
        nargs="?",
        default="run",
        choices=["run", "healthcheck", "retry", "reset-offset", "ingest"],
        help="run the watchdog (default), check the poller's liveness, retry exhausted "
        "messages, reset the polling offset, or backfill a missed intake message",
    )
    parser.add_argument(
        "--to", type=int, help="for reset-offset: the offset to set (0 at the gateway cutover)"
    )
    parser.add_argument("telegram_key", nargs="?", help="for retry: the message's Telegram Key")
    parser.add_argument(
        "--all-exhausted", action="store_true", help="for retry: every exhausted message"
    )
    ingest = parser.add_argument_group("ingest (backfill a missed intake message)")
    ingest.add_argument(
        "--json", help="a Telegram update, Telegram message or gateway item as JSON; - for stdin"
    )
    ingest.add_argument("--text", help="the message text, when the original JSON is not at hand")
    ingest.add_argument("--message-id", type=int, help="with --text: the Telegram message id")
    ingest.add_argument("--chat-id", type=int, help="with --text: defaults to intake_chat_id")
    ingest.add_argument("--from-user", help="with --text: the sender's @username or user id")
    ingest.add_argument(
        "--date", help="with --text: when it was posted (ISO-8601, Manila if no offset)"
    )
    ingest.add_argument(
        "--wait", type=float, default=0, help="seconds to wait for the capture to finish"
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
    if args.command == "ingest":
        if (args.json is None) == (args.text is None):
            parser.error("ingest needs exactly one of --json FILE|- or --text TEXT")
        if args.text is not None and args.message_id is None:
            parser.error("ingest --text needs --message-id")
        return run_ingest(args.config, args)
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
