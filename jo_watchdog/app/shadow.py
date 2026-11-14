"""Shadow comparison of this app's lookups against t6-core's (#136, #137).

The app's own Project and person resolution still decides. This module runs
t6-core's `Lookup` beside it, over the same snapshots, and reports where the two
disagree. A difference carries only kinds and outcomes, never a name, a raw
mention or a page ID, so it is safe to log. It never raises into planning.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass

from t6.domain import Contact, Project
from t6.lookup import Index, Lookup

from jo_watchdog.core.extraction import EXTRACTED_FIELDS, Extraction, normalize_ws
from jo_watchdog.core.resolve import (
    MentionEntity,
    MentionResult,
    PeopleResolution,
    ProjectResolution,
)

log = logging.getLogger(__name__)

_TOKEN = re.compile(r"[\w-]+")


@dataclass(frozen=True, slots=True)
class Difference:
    """One place the two lookups disagree. Plain words only."""

    kind: str  # "project" | "person"
    legacy: str
    core: str

    def line(self) -> str:
        return f"shadow_difference kind={self.kind} legacy={self.legacy} core={self.core}"


def _legacy_project_outcome(resolution: ProjectResolution) -> str:
    return "resolved" if resolution.page_id else "unresolved"


def _core_project(lookup: Lookup, raw: str | None, known_codes: set[str]) -> tuple[str, str | None]:
    """The outcome of t6-core's lookup for the Project Code in `raw`, and the page it picked."""
    codes = {t.casefold() for t in _TOKEN.findall(raw or "")} & known_codes
    if len(codes) != 1:
        return "unresolved", None
    found = lookup.project(next(iter(codes)))
    if found.entity is not None:
        return "resolved", found.entity.page_id
    return (found.reason or "review"), None


def compare_project(
    raw: str | None, legacy: ProjectResolution, projects: Sequence[Project]
) -> list[Difference]:
    """Compare the app's Project resolution with t6-core's rotation-aware one."""
    if not raw or not raw.strip():
        return []
    known = {p.project_code.casefold() for p in projects if p.project_code}
    outcome, page_id = _core_project(Lookup(Index(projects=tuple(projects))), raw, known)
    legacy_outcome = _legacy_project_outcome(legacy)
    if outcome == "resolved" and legacy_outcome == "resolved":
        outcome = "resolved" if page_id == legacy.page_id else "resolved_other"
    if outcome == legacy_outcome:
        return []
    return [Difference("project", legacy_outcome, outcome)]


def _core_mention(lookup: Lookup, part: MentionResult, entities: Sequence[MentionEntity]) -> str:
    """The outcome and target of t6-core's lookup for one mention, as a plain word."""
    text = part.raw.strip()
    entity = next((e for e in entities if e.text and e.text.casefold() in text.casefold()), None)
    if entity and entity.user_id:
        text = entity.user_id
    elif entity and entity.handle:
        text = f"@{entity.handle}"
    found = lookup.contact(text)
    if found.entity is not None:
        same = part.page_id in (None, found.entity.page_id)
        return "resolved" if same else "resolved_other"
    return "unresolved" if found.method == "none" or not found.needs_review else "review"


def compare_people(
    people: PeopleResolution, entities: Sequence[MentionEntity], contacts: Sequence[Contact]
) -> list[Difference]:
    """Compare each mention's resolution with t6-core's contact policy.

    Hedged mentions stay ambiguous in the app, so they are not compared.
    """
    lookup = Lookup(Index(contacts=tuple(contacts)))
    out: list[Difference] = []
    for part in people.mentions:
        if part.status == "ambiguous":
            continue
        core = _core_mention(lookup, part, entities)
        expected = "resolved" if part.status == "resolved" else "unresolved"
        if core == expected:
            continue
        if expected == "unresolved" and core == "review":
            continue  # t6-core offers candidates for a person; the app leaves it unresolved
        out.append(Difference("person", part.status, core))
    return out


def compare_extraction(unmasked: Extraction, masked: Extraction) -> list[Difference]:
    """Where extraction from masked text (names restored) differs from the unmasked one.

    A difference names the field only, never its value.
    """
    out = [
        Difference("extraction", field, "masked_differs")
        for field in ("verdict", "multiple_jo_suspected", *EXTRACTED_FIELDS)
        if not _same(getattr(unmasked, field, None), getattr(masked, field, None))
    ]
    return out


def _same(a: object, b: object) -> bool:
    if isinstance(a, str) or isinstance(b, str) or a is None or b is None:
        return normalize_ws(a if isinstance(a, str) else None) == normalize_ws(
            b if isinstance(b, str) else None
        )
    return a == b


def log_differences(differences: Sequence[Difference]) -> None:
    for difference in differences:
        log.info(difference.line())
