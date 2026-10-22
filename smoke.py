#!/usr/bin/env python3
"""Real-account smoke test for the walking skeleton.

Two modes:

- ``--notion-only`` (default): run the full capture pipeline against the REAL
  Notion Job Orders database with a synthetic in-scope message and a recording
  (non-sending) Telegram reaction. Creates one clearly-marked page, reads it
  back, compares it to the stored plan, and prints the page id/url for cleanup.
  Requires ``NOTION_API_KEY`` in the environment.

- ``--full``: poll one REAL Telegram intake message and run the pipeline against
  the real accounts. Requires ``TELEGRAM_BOT_TOKEN`` + a filled-in config.yaml
  (real intake chat/topic ids) and ``DEEPSEEK_API_KEY``.

This file is the manual real-account seam (test strategy #4) — never run in CI.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
import time
from pathlib import Path

from jo_watchdog.adapters.notion import NotionAdapter
from jo_watchdog.adapters.protocols import ClassifyResult, IncomingUpdate
from jo_watchdog.app.config import Config, load_config, notion_token
from jo_watchdog.core.extraction import EXTRACTED_FIELDS, PROMPT_TAG, Extraction
from jo_watchdog.app.worker import Worker
from jo_watchdog.ledger.db import Ledger


class RecordingTelegram:
    """Records the reaction instead of sending it (no token needed)."""

    def __init__(self):
        self.reactions: list[tuple[int, int, str]] = []
        self.updates: list[IncomingUpdate] = []

    async def get_updates(self, offset: int, timeout: int) -> list[IncomingUpdate]:
        batch = [u for u in self.updates if u.update_id >= offset]
        self.updates = []
        return batch

    async def set_reaction(self, chat_id: int, message_id: int, emoji: str) -> None:
        self.reactions.append((chat_id, message_id, emoji))


class AlwaysJoClassifier:
    """Stands in for DeepSeek in Notion-only mode (no DeepSeek key needed)."""

    async def classify(self, masked_text: str, origin_lines) -> ClassifyResult:
        extraction = Extraction(
            verdict="jo",
            confidence="high",
            reasons="Smoke test stub.",
            multiple_jo_suspected=False,
            **{field: None for field in EXTRACTED_FIELDS},
        )
        return ClassifyResult(
            outcome="ok", extraction=extraction, requested_model="stub", prompt_tag=PROMPT_TAG
        )


async def notion_only_smoke(config_path: str | None) -> int:
    config = (
        load_config(config_path)
        if config_path
        else Config(
            intake_chat_id=-1000000000000,
            intake_topic_id=1,
            captured_reaction="✅",
            job_orders_data_source_id="3d8fb4d5-9e59-8073-a0d0-000b77632947",
        )
    )

    synthetic = IncomingUpdate(
        update_id=1,
        chat_id=config.intake_chat_id,
        message_id=1,
        message_thread_id=config.intake_topic_id,
        text="WALKING SKELETON SMOKE TEST — delete this row",
        raw={
            "update_id": 1,
            "message": {
                "chat": {"id": config.intake_chat_id},
                "message_thread_id": config.intake_topic_id,
                "message_id": 1,
                "text": "WALKING SKELETON SMOKE TEST — delete this row",
            },
        },
    )

    with tempfile.TemporaryDirectory() as tmp:
        ledger = Ledger(Path(tmp) / "smoke.db")
        telegram = RecordingTelegram()
        telegram.updates.append(synthetic)
        notion = NotionAdapter(notion_token())
        worker = Worker(
            ledger=ledger,
            telegram=telegram,
            notion=notion,
            classifier=AlwaysJoClassifier(),
            config=config,
        )

        await worker.run_once()
        key = f"{synthetic.chat_id}:{synthetic.message_id}"
        msg = ledger.get_message(key)
        print(json.dumps({
            "state": msg["state"],
            "notion_page_id": msg["notion_page_id"],
            "readback_mismatch": msg["readback_mismatch"],
            "reaction_recorded": telegram.reactions,
            "plan": json.loads(msg["plan_json"]),
        }, indent=2, ensure_ascii=False))

        await notion.close()
        ledger.close()

    return 0


async def full_smoke(config_path: str) -> int:
    from aiogram import Bot

    from jo_watchdog.adapters.deepseek import DeepSeekClassifier
    from jo_watchdog.adapters.telegram import AiogramTelegramClient
    from jo_watchdog.app.config import deepseek_token, telegram_token

    config = load_config(config_path)
    ledger = Ledger(config.ledger_path)
    bot = Bot(token=telegram_token())
    telegram = AiogramTelegramClient(bot)
    notion = NotionAdapter(notion_token())
    classifier = DeepSeekClassifier(
        deepseek_token(),
        base_url=config.deepseek_base_url,
        model=config.deepseek_model,
        timeout_seconds=config.deepseek_timeout_seconds,
    )
    worker = Worker(
        ledger=ledger, telegram=telegram, notion=notion, classifier=classifier, config=config
    )
    try:
        await worker.run_once()  # one poll + drain cycle
    finally:
        await classifier.close()
        await notion.close()
        await bot.session.close()
        ledger.close()
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--full", action="store_true", help="poll one real Telegram message")
    parser.add_argument("--config", default=None, help="path to config.yaml (full mode)")
    args = parser.parse_args()

    if args.full:
        if not args.config:
            parser.error("--full requires --config")
        asyncio.run(full_smoke(args.config))
    else:
        asyncio.run(notion_only_smoke(args.config))


if __name__ == "__main__":
    main()
