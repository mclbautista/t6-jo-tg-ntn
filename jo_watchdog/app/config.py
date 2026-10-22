"""Configuration: non-secret values from config.yaml, secrets from env.

A bad non-secret value fails boot (Pydantic validation) rather than failing a
message later. Secrets never appear in the config file or in logs.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, model_validator

from jo_watchdog.adapters.notion import NOTION_VERSION
from jo_watchdog.core.resolve import ResolveSettings


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

    # Live resolution caches (#22)
    contacts_data_source_id: str = "77b2a701-71db-4e88-a694-37f07303e77e"
    projects_data_source_id: str = "cc1dc02a-8f32-4c97-81e4-dc1e08d7f134"
    leizette_contact_page_id: str | None = None
    cache_refresh_hours: float = Field(default=6, gt=0)
    cache_stale_alert_hours: float = Field(default=24, gt=0)
    fuzzy_min_score: float = Field(default=90, ge=0, le=100)
    fuzzy_min_lead: float = Field(default=8, ge=0, le=100)
    department_aliases: dict[str, str] = Field(default_factory=lambda: {"TO": "TOC"})
    honorifics: list[str] = Field(default_factory=lambda: ["ate", "kuya", "sir"])

    # Ledger
    ledger_path: str = "./data/ledger.db"

    @model_validator(mode="after")
    def _require_intake_ids(self) -> "Config":
        if not self.intake_chat_id or not self.intake_topic_id:
            raise ValueError("intake_chat_id and intake_topic_id must be set")
        return self


def resolve_settings(config: Config) -> ResolveSettings:
    """The pure resolver's knobs, taken from the validated config."""
    return ResolveSettings(
        fuzzy_min_score=config.fuzzy_min_score,
        fuzzy_min_lead=config.fuzzy_min_lead,
        honorifics=tuple(config.honorifics),
        department_aliases=config.department_aliases,
        leizette_contact_page_id=config.leizette_contact_page_id,
    )


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
