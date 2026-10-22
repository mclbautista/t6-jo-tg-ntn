"""Configuration: non-secret values from config.yaml, secrets from env.

A bad non-secret value fails boot (Pydantic validation) rather than failing a
message later. Secrets never appear in the config file or in logs.

Every key in the configuration reference (issue #27) is declared here. A few
keys with no sensible default (page ids, handles) are optional on the model so
tests and partial tools can build a ``Config``, and ``require_live_keys`` makes
them mandatory when the service boots.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from jo_watchdog.adapters.deepseek import DEFAULT_BASE_URL, DEFAULT_MODEL
from jo_watchdog.adapters.jev import JEV_MODEL, JEV_URL
from jo_watchdog.adapters.notion import NOTION_VERSION
from jo_watchdog.core.resolve import ResolveSettings

_HANDLE_RE = re.compile(r"^@?[A-Za-z0-9_]{5,32}$")
_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

# Keys the running service cannot do without; checked by ``require_live_keys``.
LIVE_REQUIRED_KEYS = (
    "leizette_contact_page_id",
    "leizette_telegram_handle",
    "owner_telegram_handle",
)

SECRET_KEYS = (
    "TELEGRAM_BOT_TOKEN",
    "NOTION_API_KEY",
    "DEEPSEEK_API_KEY",
    "OPENROUTER_API_KEY",
)


class Config(BaseModel):
    """Validated runtime configuration."""

    model_config = ConfigDict(extra="forbid")

    # Telegram scope filter
    intake_chat_id: int
    intake_topic_id: int
    captured_reaction: str = "✅"

    # Ops topic (#23): where capture reports and system messages go
    ops_chat_id: int
    ops_topic_id: int
    leizette_telegram_handle: str | None = None
    header_date_tolerance_days: int = Field(default=1, ge=0)

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

    # Live resolution caches (#22)
    contacts_data_source_id: str = "77b2a701-71db-4e88-a694-37f07303e77e"
    projects_data_source_id: str = "cc1dc02a-8f32-4c97-81e4-dc1e08d7f134"
    leizette_contact_page_id: str | None = None
    cache_refresh_hours: float = Field(default=6, gt=0)
    cache_stale_alert_hours: float = Field(default=24, gt=0)
    fuzzy_min_score: float = Field(default=90, ge=0, le=100)
    fuzzy_min_lead: float = Field(default=8, ge=0, le=100)
    honorifics: list[str] = Field(default_factory=lambda: ["ate", "kuya", "sir"])

    # Ledger
    ledger_path: str = "./data/ledger.db"

    # Planning: header Department token -> Department option (validated live in #22)
    department_aliases: dict[str, str] = Field(default_factory=lambda: {"TO": "TOC"})

    # Reliability and heartbeat (#25)
    owner_telegram_handle: str | None = None
    retry_schedule_seconds: list[int] = Field(default_factory=lambda: [30, 120, 300, 600, 900])
    deadman_stale_minutes: int = Field(default=10, gt=0)
    heartbeat_time: str = "07:00"
    timezone: str = "Asia/Manila"

    # Deliverable matching (Jev via OpenRouter, #26)
    deliverables_data_source_id: str = "0a4ff084-3138-429c-8dbe-4d802aded74f"
    jev_url: str = JEV_URL
    jev_model: str = JEV_MODEL
    jev_timeout_seconds: float = Field(default=30, gt=0)
    t_link: float = Field(default=0.90, ge=0, le=1)
    t_hint: float = Field(default=0.50, ge=0, le=1)
    deliverable_link_enabled: bool = False

    # Rollback control (#27)
    dry_run: bool = False

    @field_validator("captured_reaction")
    @classmethod
    def _non_empty_reaction(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("captured_reaction must be an emoji")
        return value

    @field_validator("leizette_telegram_handle", "owner_telegram_handle")
    @classmethod
    def _telegram_handle(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        value = value.strip()
        if not _HANDLE_RE.match(value):
            raise ValueError(f"not a Telegram handle: {value!r}")
        return value if value.startswith("@") else f"@{value}"

    @field_validator("heartbeat_time")
    @classmethod
    def _hhmm(cls, value: str) -> str:
        if not _HHMM_RE.match(value):
            raise ValueError("heartbeat_time must be HH:MM (24-hour)")
        return value

    @field_validator("timezone")
    @classmethod
    def _known_zone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown timezone: {value!r}") from exc
        return value

    @field_validator("retry_schedule_seconds")
    @classmethod
    def _retry_schedule(cls, value: list[int]) -> list[int]:
        if not value or any(step <= 0 for step in value):
            raise ValueError("retry_schedule_seconds must be a non-empty list of positive ints")
        return value

    @field_validator("honorifics")
    @classmethod
    def _honorifics(cls, value: list[str]) -> list[str]:
        return [h.strip().casefold() for h in value if h.strip()]

    @field_validator("department_aliases")
    @classmethod
    def _department_aliases(cls, value: dict[str, str]) -> dict[str, str]:
        if any(not k.strip() or not v.strip() for k, v in value.items()):
            raise ValueError("department_aliases keys and values must be non-empty")
        return value

    @model_validator(mode="after")
    def _cross_field(self) -> Config:
        if not self.intake_chat_id or not self.intake_topic_id:
            raise ValueError("intake_chat_id and intake_topic_id must be set")
        if not self.ops_chat_id or not self.ops_topic_id:
            raise ValueError("ops_chat_id and ops_topic_id must be set")
        if self.t_hint > self.t_link:
            raise ValueError("t_hint must not exceed t_link")
        if self.cache_stale_alert_hours < self.cache_refresh_hours:
            raise ValueError("cache_stale_alert_hours must be at least cache_refresh_hours")
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


def require_live_keys(config: Config) -> None:
    """Fail boot when a key the running service needs is missing."""
    missing = [key for key in LIVE_REQUIRED_KEYS if getattr(config, key) in (None, "", 0)]
    if missing:
        raise ValueError(f"config.yaml is missing required keys: {', '.join(missing)}")


def load_config(path: str | Path) -> Config:
    """Load non-secret config from a YAML file."""
    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    return Config(**data)


def _secret(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise RuntimeError(f"{name} is not set")
    return value


def missing_secrets() -> list[str]:
    """Names of the secrets in ``SECRET_KEYS`` that are unset."""
    return [name for name in SECRET_KEYS if not os.environ.get(name)]


def telegram_token() -> str:
    return _secret("TELEGRAM_BOT_TOKEN")


def notion_token() -> str:
    return _secret("NOTION_API_KEY")


def deepseek_token() -> str:
    return _secret("DEEPSEEK_API_KEY")


def openrouter_token() -> str:
    return _secret("OPENROUTER_API_KEY")
