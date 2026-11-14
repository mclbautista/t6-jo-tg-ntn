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
from typing import Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)
from t6.domain.job_order_rules import DEPARTMENT_ALIASES
from t6.domain.studio_people import standing_cc

from jo_watchdog.adapters.deepseek import DEFAULT_BASE_URL, DEFAULT_MODEL
from jo_watchdog.adapters.jev import JEV_MODEL, JEV_URL
from jo_watchdog.adapters.notion import NOTION_VERSION
from jo_watchdog.core.resolve import ResolveSettings

_HANDLE_RE = re.compile(r"^@?[A-Za-z0-9_]{5,32}$")
_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_NOTION_ID_RE = re.compile(
    r"^[0-9a-fA-F]{32}$|^[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$"
)
_NOTION_VERSION_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Notion ids a live service must hold real, well-formed values for.
_NOTION_ID_KEYS = (
    "job_orders_data_source_id",
    "contacts_data_source_id",
    "projects_data_source_id",
    "deliverables_data_source_id",
    "standing_cc_contact_page_id",
)
_URL_KEYS = ("deepseek_base_url", "jev_url")
_NON_EMPTY_KEYS = ("deepseek_model", "jev_model", "ledger_path")

# Keys the running service cannot do without; checked by ``require_live_keys``.
LIVE_REQUIRED_KEYS = (
    "standing_cc_contact_page_id",
    "standing_cc_telegram_handle",
    "owner_telegram_handle",
)

SECRET_KEYS = (
    "TELEGRAM_BOT_TOKEN",
    "NOTION_API_KEY",
    "DEEPSEEK_API_KEY",
    "OPENROUTER_API_KEY",
)

# Gateway mode (#95): the module secret replaces the Telegram token.
GATEWAY_SECRET_KEY = "WATCHDOG_SECRET"


class Config(BaseModel):
    """Validated runtime configuration."""

    model_config = ConfigDict(extra="forbid")

    # Telegram scope filter
    intake_chat_id: int
    intake_topic_id: int
    captured_reaction: str = "👍"
    flagged_reaction: str = "🤔"

    # Ops topic (#23): where capture reports and system messages go
    ops_chat_id: int
    ops_topic_id: int
    standing_cc_telegram_handle: str | None = None
    header_date_tolerance_days: int = Field(default=1, ge=0)
    # #101: alert ops when intake chat message ids jump (messages that never arrived)
    message_gap_alerts: bool = True

    # Telegram transport (#95): ``direct`` polls Telegram with the bot token; ``gateway``
    # reads and posts through the Sixto gateway with ``WATCHDOG_SECRET`` instead.
    telegram_mode: Literal["direct", "gateway"] = "direct"
    gateway_url: str | None = None

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
    standing_cc_contact_page_id: str | None = None
    cache_refresh_hours: float = Field(default=6, gt=0)
    cache_stale_alert_hours: float = Field(default=24, gt=0)
    fuzzy_min_score: float = Field(default=90, ge=0, le=100)
    fuzzy_min_lead: float = Field(default=8, ge=0, le=100)
    honorifics: list[str] = Field(default_factory=lambda: ["ate", "kuya", "sir"])

    # Ledger
    ledger_path: str = "./data/ledger.db"

    # Planning: header Department token -> Department option (validated live in #22)
    department_aliases: dict[str, str] = Field(default_factory=lambda: dict(DEPARTMENT_ALIASES))

    # Reliability and heartbeat (#25)
    owner_telegram_handle: str | None = None
    retry_schedule_seconds: list[int] = Field(default_factory=lambda: [30, 120, 300, 600])
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

    # Shadow lookups against t6-core (#136, #137)
    project_registry_path: str = ""

    # Rollback control (#27)
    dry_run: bool = False

    @field_validator("captured_reaction", "flagged_reaction")
    @classmethod
    def _non_empty_reaction(cls, value: str, info: ValidationInfo) -> str:
        if not value.strip():
            raise ValueError(f"{info.field_name} must be an emoji")
        return value

    @field_validator("standing_cc_telegram_handle", "owner_telegram_handle")
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
        if self.telegram_mode == "gateway":
            parts = urlsplit(self.gateway_url or "")
            if parts.scheme not in ("http", "https") or not parts.netloc:
                raise ValueError("gateway_url must be an http(s) URL when telegram_mode is gateway")
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
        standing_cc_contact_page_id=standing_cc() or config.standing_cc_contact_page_id,
    )


def _production_problems(config: Config) -> list[str]:
    """Why ``config`` is not fit to boot against live providers.

    Messages name the key and the rule, never the value.
    """
    problems = [
        f"{key} is missing"
        for key in LIVE_REQUIRED_KEYS
        if getattr(config, key) in (None, "", 0)
    ]
    for key in _NOTION_ID_KEYS:
        value = getattr(config, key)
        if value and not _NOTION_ID_RE.match(value.strip()):
            problems.append(f"{key} is not a Notion id (32 hex characters, dashes optional)")
        elif not value and key not in LIVE_REQUIRED_KEYS:
            problems.append(f"{key} must not be empty")
    for key in _URL_KEYS:
        parts = urlsplit(getattr(config, key).strip())
        if parts.scheme not in ("http", "https") or not parts.hostname:
            problems.append(f"{key} must be an http(s) URL with a host")
    for key in _NON_EMPTY_KEYS:
        if not getattr(config, key).strip():
            problems.append(f"{key} must not be empty")
    if not _NOTION_VERSION_RE.match(config.notion_version):
        problems.append("notion_version must look like YYYY-MM-DD")
    for key in ("intake_topic_id", "ops_topic_id"):
        if getattr(config, key) <= 0:
            problems.append(f"{key} must be a positive topic id")
    # Chat ids are not topic ids: supergroups are legitimately negative, so only
    # zero is rejected (the model already refuses it).
    return problems


def require_live_keys(config: Config) -> None:
    """Fail boot when the config is unfit to run against live providers.

    Checks structure only, so the example file passes it; ``placeholder_keys``
    separately flags the FILL IN values that must be replaced.
    """
    problems = _production_problems(config)
    if problems:
        raise ValueError("config.yaml is not valid for production: " + "; ".join(problems))


def placeholder_keys(config: Config) -> list[str]:
    """Keys still holding the example file's FILL IN values (not production-ready)."""
    placeholders = []
    for key in ("standing_cc_contact_page_id", "standing_cc_telegram_handle", "owner_telegram_handle"):
        if str(getattr(config, key)).startswith(("00000000", "@standing_cc_handle", "@owner_handle")):
            placeholders.append(key)
    if config.intake_chat_id == -1000000000000:
        placeholders.append("intake_chat_id")
    if config.ops_chat_id == -1000000000001:
        placeholders.append("ops_chat_id")
    return placeholders


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


def required_secrets(telegram_mode: str = "direct") -> tuple[str, ...]:
    """The secrets the given mode needs: gateway mode swaps the bot token for the module secret."""
    if telegram_mode == "gateway":
        return tuple(
            GATEWAY_SECRET_KEY if name == "TELEGRAM_BOT_TOKEN" else name for name in SECRET_KEYS
        )
    return SECRET_KEYS


def missing_secrets(telegram_mode: str = "direct") -> list[str]:
    """Names of the secrets the mode needs (``SECRET_KEYS`` by default) that are unset."""
    return [name for name in required_secrets(telegram_mode) if not os.environ.get(name)]


def telegram_token() -> str:
    return _secret("TELEGRAM_BOT_TOKEN")


def gateway_secret() -> str:
    return _secret(GATEWAY_SECRET_KEY)


def notion_token() -> str:
    return _secret("NOTION_API_KEY")


def deepseek_token() -> str:
    return _secret("DEEPSEEK_API_KEY")


def openrouter_token() -> str:
    return _secret("OPENROUTER_API_KEY")
