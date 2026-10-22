"""Six-hourly People, Projects and Department caches, persisted in the ledger.

Every successful, non-empty refresh is written to ``cache_snapshot`` and becomes
the live snapshot. On restart the latest persisted snapshot loads before the
first live refresh, so a Notion outage at boot does not blank every relation. A
failed refresh writes nothing, keeps the previous snapshot, and reports a stale
cache only once it is older than ``cache_stale_alert_hours``.

If a kind has never loaded, ``ready`` is False and planning must hold (the
message stays put, no retry attempt is spent and no unresolved finding is made).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel

from jo_watchdog.adapters.protocols import NotionReader
from jo_watchdog.app.config import Config, resolve_settings
from jo_watchdog.core.resolve import (
    DepartmentResolution,
    MentionEntity,
    PeopleIndex,
    PeopleResolution,
    Person,
    ProjectEntry,
    ProjectResolution,
    resolve_department,
    resolve_people,
    resolve_project,
)
from jo_watchdog.ledger.db import Ledger

log = logging.getLogger("jo_watchdog.caches")

KINDS = ("people", "projects", "departments")
# Bump when the cached payload shape changes; older snapshots are then ignored.
FORMAT_VERSION = 1

# Live Notion property names (schema is the contract).
CONTACT_NAME = "Name"
CONTACT_USER_ID = "TelegramUserID"
CONTACT_HANDLE = "TelegramHandle"
CONTACT_ALIASES = "Aliases"
CONTACT_END_OF_SHIFT = "End of Shift"
PROJECT_NAME = "Name"
PROJECT_CODE = "Project Code"
JO_DEPARTMENT = "Department"


class StaleCache(BaseModel):
    """A cache older than the alert threshold after a failed refresh (rendered by #23)."""

    kind: str
    fetched_at: str
    hours_stale: float
    error: str


# -- Notion page parsing ----------------------------------------------------


def _plain(prop: dict[str, Any] | None) -> str:
    if not prop:
        return ""
    kind = prop.get("type")
    if kind in ("title", "rich_text"):
        return "".join(part.get("plain_text", "") for part in prop.get(kind) or []).strip()
    if kind == "select":
        return ((prop.get("select") or {}).get("name") or "").strip()
    return ""


def person_from_page(page: dict[str, Any]) -> Person:
    props = page.get("properties", {})
    return Person(
        page_id=page["id"],
        name=_plain(props.get(CONTACT_NAME)),
        telegram_user_id=_plain(props.get(CONTACT_USER_ID)),
        telegram_handle=_plain(props.get(CONTACT_HANDLE)),
        aliases=_plain(props.get(CONTACT_ALIASES)),
        end_of_shift=_plain(props.get(CONTACT_END_OF_SHIFT)),
    )


def project_from_page(page: dict[str, Any]) -> ProjectEntry:
    props = page.get("properties", {})
    return ProjectEntry(
        page_id=page["id"],
        name=_plain(props.get(PROJECT_NAME)),
        code=_plain(props.get(PROJECT_CODE)),
    )


def department_options(data_source: dict[str, Any]) -> list[str]:
    prop = data_source.get("properties", {}).get(JO_DEPARTMENT) or {}
    options = (prop.get("select") or {}).get("options") or []
    return [o["name"] for o in options if o.get("name")]


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


# -- the cache manager --------------------------------------------------------


class CacheManager:
    """Owns the live snapshots and their refresh schedule."""

    def __init__(
        self,
        *,
        ledger: Ledger,
        notion: NotionReader,
        config: Config,
        clock: Callable[[], datetime] = _utcnow,
    ):
        self.ledger = ledger
        self.notion = notion
        self.config = config
        self.settings = resolve_settings(config)
        self.clock = clock
        self.people: list[Person] | None = None
        self.projects: list[ProjectEntry] | None = None
        self.departments: list[str] | None = None
        self.fetched_at: dict[str, str] = {}
        self.last_attempt: datetime | None = None
        self._people_index: PeopleIndex | None = None
        self._stale_reported: set[str] = set()

    # -- loading -------------------------------------------------------------

    def load_persisted(self) -> None:
        """Load the latest persisted snapshot per kind (before the first live refresh)."""
        for kind in KINDS:
            snapshot = self.ledger.latest_cache_snapshot(kind)
            if snapshot is None or snapshot["format_version"] != FORMAT_VERSION:
                continue
            if not snapshot["payload"]:
                continue
            self._apply(kind, snapshot["payload"], snapshot["fetched_at"])

    def _apply(self, kind: str, payload: list, fetched_at: str) -> None:
        if kind == "people":
            self.people = [Person(**p) for p in payload]
            self._people_index = PeopleIndex(self.people, self.settings.honorifics)
        elif kind == "projects":
            self.projects = [ProjectEntry(**p) for p in payload]
        else:
            self.departments = list(payload)
        self.fetched_at[kind] = fetched_at

    async def _fetch(self, kind: str) -> list:
        if kind == "people":
            pages = await self.notion.query_all(self.config.contacts_data_source_id)
            return [person_from_page(p).model_dump() for p in pages]
        if kind == "projects":
            pages = await self.notion.query_all(self.config.projects_data_source_id)
            return [project_from_page(p).model_dump() for p in pages]
        schema = await self.notion.retrieve_data_source(self.config.job_orders_data_source_id)
        return department_options(schema)

    async def refresh(self) -> list[StaleCache]:
        """Refresh every kind; return stale-cache events for failed refreshes past the threshold."""
        now = self.clock()
        self.last_attempt = now
        stale: list[StaleCache] = []
        for kind in KINDS:
            try:
                payload = await self._fetch(kind)
                if not payload:
                    raise ValueError(f"{kind} refresh returned no items")
            except Exception as exc:  # any failure keeps the previous snapshot
                log.warning("cache refresh failed for %s at %s: %s", kind, _iso(now), exc)
                event = self._stale_event(kind, now, str(exc))
                if event is not None:
                    stale.append(event)
                continue
            fetched_at = _iso(now)
            self.ledger.save_cache_snapshot(kind, FORMAT_VERSION, payload, len(payload), fetched_at)
            self._apply(kind, payload, fetched_at)
            self._stale_reported.discard(kind)
        return stale

    def _stale_event(self, kind: str, now: datetime, error: str) -> StaleCache | None:
        fetched_at = self.fetched_at.get(kind)
        if fetched_at is None or kind in self._stale_reported:
            return None
        age = now - _parse_iso(fetched_at)
        if age <= timedelta(hours=self.config.cache_stale_alert_hours):
            return None
        self._stale_reported.add(kind)
        return StaleCache(
            kind=kind,
            fetched_at=fetched_at,
            hours_stale=round(age.total_seconds() / 3600, 1),
            error=error,
        )

    def refresh_due(self) -> bool:
        if self.last_attempt is None:
            return True
        return self.clock() - self.last_attempt >= timedelta(hours=self.config.cache_refresh_hours)

    async def refresh_if_due(self) -> list[StaleCache]:
        if not self.refresh_due():
            return []
        return await self.refresh()

    # -- resolution ------------------------------------------------------------

    @property
    def ready(self) -> bool:
        """True once every kind has a loaded snapshot; until then planning holds."""
        return (
            self.people is not None and self.projects is not None and self.departments is not None
        )

    def resolve_people(
        self,
        assignees_raw: str | None,
        cc_raw: str | None,
        entities: list[MentionEntity] | None = None,
    ) -> PeopleResolution:
        if self._people_index is None:
            raise RuntimeError("People cache has not loaded; hold planning")
        return resolve_people(
            assignees_raw, cc_raw, self._people_index, self.settings, entities or []
        )

    def resolve_project(self, project_raw: str | None) -> ProjectResolution:
        if self.projects is None:
            raise RuntimeError("Projects cache has not loaded; hold planning")
        return resolve_project(project_raw, self.projects)

    def resolve_department(self, token: str | None) -> DepartmentResolution:
        if self.departments is None:
            raise RuntimeError("Department options have not loaded; hold planning")
        return resolve_department(token, self.departments, self.settings.department_aliases)
