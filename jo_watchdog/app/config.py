"""Configuration: non-secret values from config.yaml, secrets from env.

A bad non-secret value fails boot (Pydantic validation) rather than failing a
message later. Secrets never appear in the config file or in logs.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, model_validator

from jo_watchdog.adapters.deepseek import DEFAULT_BASE_URL, DEFAULT_MODEL
from jo_watchdog.adapters.notion import NOTION_VERSION


class Config(BaseModel):
    """Validated runtime configuration."""

    # Telegram scope filter
    intake_chat_id: int
    intake_topic_id: int
    captured_reaction: str = "✅"

    # Polling
    poll_timeout: int = Field(default=50, ge=1, le=50)  # Telegram long-poll seconds
    poll_interval: float = Field(default=1.0, gt=0)

    # Notion
    job_orders_data_source_id: str
    notion_version: str = NOTION_VERSION

    # DeepSeek classify/extract
    deepseek_base_url: str = DEFAULT_BASE_URL
    deepseek_model: str = DEFAULT_MODEL  # the "latest" alias, no pin
    deepseek_timeout_seconds: float = Field(default=60, gt=0)

    # Ledger
    ledger_path: str = "./data/ledger.db"

    @model_validator(mode="after")
    def _require_intake_ids(self) -> "Config":
        if not self.intake_chat_id or not self.intake_topic_id:
            raise ValueError("intake_chat_id and intake_topic_id must be set")
        return self


def load_config(path: str | Path) -> Config:
    """Load non-secret config from a YAML file."""
    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    return Config(**data)


def telegram_token() -> str:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")
    return token


def notion_token() -> str:
    token = os.environ.get("NOTION_API_KEY", "")
    if not token:
        raise RuntimeError("NOTION_API_KEY is not set")
    return token


def deepseek_token() -> str:
    token = os.environ.get("DEEPSEEK_API_KEY", "")
    if not token:
        raise RuntimeError("DEEPSEEK_API_KEY is not set")
    return token
