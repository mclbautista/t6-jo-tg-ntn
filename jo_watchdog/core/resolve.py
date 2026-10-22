"""Pure core: resolve raw People, Project and Department mentions to Notion values.

The extractor only proposes text; this module decides identity against the
cached Contacts, Projects and Department lists (resolve, don't generate). It has
no I/O: callers pass the loaded snapshots in, so every rule is testable offline.

Precision beats recall. Anything ambiguous stays blank and is reported as a
finding (code plus parameters, rendered later by the ops reporter); nothing is
backfilled later.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import time

from pydantic import BaseModel, Field
from rapidfuzz import fuzz

DEFAULT_HONORIFICS = ("ate", "kuya", "sir")
FLEXIBLE_SHIFT = "Flexible / Variable"

# Mention separators: commas, slashes, ampersands, " and ", and line breaks.
_SPLIT_RE = re.compile(r"\s*(?:,|/|&|\n|\band\b)\s*", re.IGNORECASE)
_HEDGE_RE = re.compile(r"\b(?:but|maybe|or|baka)\b", re.IGNORECASE)
_CONDITION_RE = re.compile(r"\b(?:if|kung)\b.*$", re.IGNORECASE | re.DOTALL)
_LEADING_FOR_RE = re.compile(r"^for\s+", re.IGNORECASE)
_COURTESY_TAIL_RE = re.compile(
    r"(?:[\s,!.]+|^)(?:thank\w*|ty(?:vm+)?|you+\w*|u|po)[\s,!.]*$", re.IGNORECASE
)
_HANDLE_RE = re.compile(r"@([A-Za-z0-9_]{3,})")
_SHIFT_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*([AaPp][Mm])\s*$")


# -- snapshot models ---------------------------------------------------------


class Person(BaseModel):
    """One Contacts page, as cached."""

    page_id: str
    name: str
    telegram_user_id: str = ""
    telegram_handle: str = ""
    aliases: str = ""
    end_of_shift: str = ""


class ProjectEntry(BaseModel):
    """One Projects page, as cached."""

    page_id: str
    name: str
    code: str = ""


class Finding(BaseModel):
    """A finding code plus its parameters; the ops reporter renders the line."""

    code: str
    params: dict[str, str] = Field(default_factory=dict)


class ResolveSettings(BaseModel):
    """The resolution knobs from config."""

    fuzzy_min_score: float = 90
    fuzzy_min_lead: float = 8
    honorifics: tuple[str, ...] = DEFAULT_HONORIFICS
    department_aliases: dict[str, str] = Field(default_factory=dict)
    leizette_contact_page_id: str | None = None


class MentionEntity(BaseModel):
    """A Telegram ``mention`` or ``text_mention`` entity from the message.

    ``text`` is the entity's text as it appears in the message (``@handle`` for
    a mention, the display name for a text_mention).
    """

    text: str
    user_id: str | None = None
    handle: str | None = None


class MentionResult(BaseModel):
    """How one raw mention resolved."""

    raw: str
    role: str  # "assignee" | "cc"
    status: str  # "resolved" | "unresolved" | "ambiguous"
    method: str | None = None  # "user_id" | "handle" | "alias" | "fuzzy"
    page_id: str | None = None
    contact_name: str | None = None


class AssigneeShift(BaseModel):
    """A resolved assignee's End of Shift, as handed to the deadline resolver."""

    page_id: str
    name: str
    end_of_shift: str  # the raw select value: "10:00 PM", "Flexible / Variable" or ""

    @property
    def shift_time(self) -> time | None:
        return parse_end_of_shift(self.end_of_shift)


class PeopleResolution(BaseModel):
    """Relation values and findings for Assigned and cc."""

    assigned: list[str] = Field(default_factory=list)
    cc: list[str] = Field(default_factory=list)
    mentions: list[MentionResult] = Field(default_factory=list)
    assignee_shifts: list[AssigneeShift] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)


class ProjectResolution(BaseModel):
    page_id: str | None = None
    code: str | None = None
    findings: list[Finding] = Field(default_factory=list)


class DepartmentResolution(BaseModel):
    """``department`` is the exact live option name, or None when it failed validation."""

    department: str | None = None
    token: str | None = None
    findings: list[Finding] = Field(default_factory=list)

    @property
    def valid(self) -> bool:
        return self.department is not None


# -- normalization -------------------------------------------------------------


def normalize_name(text: str, honorifics: Iterable[str] = DEFAULT_HONORIFICS) -> str:
    """Casefold, trim, collapse whitespace, and drop honorific words."""
    drop = {h.casefold() for h in honorifics}
    words = [w for w in text.casefold().split() if w.strip(".") not in drop]
    return " ".join(words)


def normalize_handle(handle: str) -> str:
    return handle.strip().lstrip("@").casefold()


def split_aliases(aliases: str, honorifics: Iterable[str] = DEFAULT_HONORIFICS) -> list[str]:
    """Contacts ``Aliases`` is comma-separated; each entry is normalized like a mention."""
    out = []
    for part in aliases.split(","):
        norm = normalize_name(part, honorifics)
        if norm:
            out.append(norm)
    return out


def parse_end_of_shift(value: str | None) -> time | None:
    """Parse a Contacts ``End of Shift`` option (``10:00 PM``); None when blank or flexible."""
    if not value or value.strip() == FLEXIBLE_SHIFT:
        return None
    m = _SHIFT_RE.match(value)
    if not m:
        return None
    hour, minute, meridiem = int(m.group(1)) % 12, int(m.group(2)), m.group(3).lower()
    if meridiem == "pm":
        hour += 12
    return time(hour, minute)


def _strip_courtesy(segment: str) -> str:
    """Drop a leading ``for`` and trailing thanks (``thank youuu``, ``tyvm``, ``po``)."""
    text = _LEADING_FOR_RE.sub("", segment.strip())
    while True:
        m = _COURTESY_TAIL_RE.search(text)
        if not m or m.start() == m.end():
            return text.strip()
        text = text[: m.start()].strip()


def split_mentions(raw: str | None) -> list[str]:
    """Split an ``assignees_raw``/``cc_raw`` value into individual mentions.

    A trailing condition (``if abutin the next day``, ``kung sakaling ...``) is
    dropped, ``@handles`` are split from the names around them, and a hedged
    mention (``Piolo but maybe Majo``) is kept whole so it is reported as one
    ambiguous mention, quoted as written.
    """
    if not raw or not raw.strip():
        return []
    text = _CONDITION_RE.sub("", raw)
    out = []
    for part in _SPLIT_RE.split(text):
        part = _strip_courtesy(part)
        if not part:
            continue
        if _HEDGE_RE.search(part):
            out.append(part)
            continue
        out.extend(_split_handles(part))
    return out


def _split_handles(part: str) -> list[str]:
    """``Piolo @raedsss`` -> ``Piolo``, ``@raedsss``; ``ate @debz38`` stays whole."""
    pieces: list[str] = []
    last = 0
    for m in _HANDLE_RE.finditer(part):
        before = part[last : m.start()].strip()
        if before and normalize_name(before):
            pieces.append(before)
        elif before:
            # Only an honorific before the handle: keep it attached (``ate @debz38``).
            pieces.append(f"{before} {m.group(0)}")
            last = m.end()
            continue
        pieces.append(m.group(0))
        last = m.end()
    tail = part[last:].strip()
    if tail:
        pieces.append(tail)
    return [p for p in (_strip_courtesy(p) for p in pieces) if p]


# -- people --------------------------------------------------------------------


class PeopleIndex:
    """Lookup tables over a People snapshot. Collisions map to several page ids."""

    def __init__(self, people: Sequence[Person], honorifics: Iterable[str] = DEFAULT_HONORIFICS):
        self.honorifics = tuple(honorifics)
        self.people = {p.page_id: p for p in people}
        self.by_user_id: dict[str, set[str]] = {}
        self.by_handle: dict[str, set[str]] = {}
        self.by_alias: dict[str, set[str]] = {}
        self.titles: list[tuple[str, str]] = []  # (normalized title, page id)
        for p in people:
            if p.telegram_user_id.strip():
                self.by_user_id.setdefault(p.telegram_user_id.strip(), set()).add(p.page_id)
            if normalize_handle(p.telegram_handle):
                self.by_handle.setdefault(normalize_handle(p.telegram_handle), set()).add(p.page_id)
            for alias in split_aliases(p.aliases, self.honorifics):
                self.by_alias.setdefault(alias, set()).add(p.page_id)
            title = normalize_name(p.name, self.honorifics)
            if title:
                self.titles.append((title, p.page_id))


def _entity_for(segment: str, entities: Sequence[MentionEntity]) -> MentionEntity | None:
    folded = segment.casefold()
    for entity in entities:
        if entity.text and entity.text.casefold() in folded:
            return entity
    return None


def _from_ids(raw: str, role: str, ids: set[str], method: str, index: PeopleIndex):
    if len(ids) == 1:
        page_id = next(iter(ids))
        return MentionResult(
            raw=raw,
            role=role,
            status="resolved",
            method=method,
            page_id=page_id,
            contact_name=index.people[page_id].name,
        )
    return MentionResult(raw=raw, role=role, status="ambiguous", method=method)


def resolve_mention(
    raw: str,
    role: str,
    index: PeopleIndex,
    settings: ResolveSettings,
    entities: Sequence[MentionEntity] = (),
) -> MentionResult:
    """Resolve one mention: TelegramUserID, TelegramHandle, Aliases, then guarded fuzzy."""
    if _HEDGE_RE.search(raw):
        return MentionResult(raw=raw, role=role, status="ambiguous")

    entity = _entity_for(raw, entities)

    # 1. TelegramUserID from a mention/text_mention entity.
    if entity and entity.user_id and entity.user_id in index.by_user_id:
        return _from_ids(raw, role, index.by_user_id[entity.user_id], "user_id", index)

    # 2. TelegramHandle, from the entity or an @handle in the text.
    handle = None
    if entity and entity.handle:
        handle = normalize_handle(entity.handle)
    else:
        m = _HANDLE_RE.search(raw)
        if m:
            handle = normalize_handle(m.group(1))
    if handle and handle in index.by_handle:
        return _from_ids(raw, role, index.by_handle[handle], "handle", index)

    # 3. Contacts Aliases (exact, after normalization; a collision is unresolved).
    norm = normalize_name(raw, settings.honorifics)
    if norm in index.by_alias:
        return _from_ids(raw, role, index.by_alias[norm], "alias", index)

    # An @handle mention never falls through to fuzzy matching.
    if handle or norm.startswith("@") or (entity and entity.user_id):
        return MentionResult(raw=raw, role=role, status="unresolved")

    # 4. Guarded fuzzy match against Contacts titles.
    return _fuzzy(raw, role, norm, index, settings)


def _fuzzy(
    raw: str, role: str, norm: str, index: PeopleIndex, settings: ResolveSettings
) -> MentionResult:
    if not norm:
        return MentionResult(raw=raw, role=role, status="unresolved")
    best: dict[str, float] = {}
    for title, page_id in index.titles:
        score = fuzz.WRatio(norm, title)
        if score > best.get(page_id, -1):
            best[page_id] = score
    ranked = sorted(best.items(), key=lambda kv: kv[1], reverse=True)
    if not ranked:
        return MentionResult(raw=raw, role=role, status="unresolved")
    top_id, top = ranked[0]
    runner_up = ranked[1][1] if len(ranked) > 1 else 0.0
    if top >= settings.fuzzy_min_score and top - runner_up >= settings.fuzzy_min_lead:
        return MentionResult(
            raw=raw,
            role=role,
            status="resolved",
            method="fuzzy",
            page_id=top_id,
            contact_name=index.people[top_id].name,
        )
    return MentionResult(raw=raw, role=role, status="unresolved")


def _mention_finding(result: MentionResult) -> Finding | None:
    if result.status == "resolved" and result.method == "fuzzy":
        return Finding(
            code="fuzzy_person",
            params={"raw": result.raw, "role": result.role, "contact": result.contact_name or ""},
        )
    if result.status == "unresolved":
        return Finding(code="unresolved_person", params={"raw": result.raw, "role": result.role})
    if result.status == "ambiguous":
        return Finding(code="ambiguous_person", params={"raw": result.raw, "role": result.role})
    return None


def _split_unresolved_names(
    result: MentionResult, index: PeopleIndex, settings: ResolveSettings
) -> list[MentionResult]:
    """``Jade Majo`` (names with no separator) splits only if every word is an exact alias."""
    if result.status != "unresolved" or "@" in result.raw:
        return [result]
    words = [w for w in result.raw.split() if normalize_name(w, settings.honorifics)]
    if len(words) < 2:
        return [result]
    parts = []
    for word in words:
        ids = index.by_alias.get(normalize_name(word, settings.honorifics))
        if not ids or len(ids) != 1:
            return [result]
        parts.append(_from_ids(word, result.role, ids, "alias", index))
    return parts


def _add_unique(ids: list[str], page_id: str) -> None:
    if page_id not in ids:
        ids.append(page_id)


def resolve_people(
    assignees_raw: str | None,
    cc_raw: str | None,
    people: Sequence[Person] | PeopleIndex,
    settings: ResolveSettings,
    entities: Sequence[MentionEntity] = (),
) -> PeopleResolution:
    """Resolve Assigned and cc. Leizette is always in cc; Coordinator stays blank."""
    index = people if isinstance(people, PeopleIndex) else PeopleIndex(people, settings.honorifics)
    out = PeopleResolution()
    for role, raw in (("assignee", assignees_raw), ("cc", cc_raw)):
        for mention in split_mentions(raw):
            result = resolve_mention(mention, role, index, settings, entities)
            for part in _split_unresolved_names(result, index, settings):
                out.mentions.append(part)
                finding = _mention_finding(part)
                if finding:
                    out.findings.append(finding)
                if part.status != "resolved" or part.page_id is None:
                    continue
                if role == "assignee":
                    _add_unique(out.assigned, part.page_id)
                else:
                    _add_unique(out.cc, part.page_id)

    if settings.leizette_contact_page_id:
        _add_unique(out.cc, settings.leizette_contact_page_id)

    for page_id in out.assigned:
        person = index.people[page_id]
        out.assignee_shifts.append(
            AssigneeShift(page_id=page_id, name=person.name, end_of_shift=person.end_of_shift)
        )
    return out


# -- projects ------------------------------------------------------------------


def resolve_project(project_raw: str | None, projects: Sequence[ProjectEntry]) -> ProjectResolution:
    """Match known Project Codes as whole tokens, case-insensitively, longest first.

    Zero matches, more than one distinct code, or a code shared by several
    Projects pages all leave Projects blank with an ``unresolved_project`` finding.
    """
    if not project_raw or not project_raw.strip():
        return ProjectResolution()

    by_code: dict[str, set[str]] = {}
    spelled: dict[str, str] = {}
    for p in projects:
        code = p.code.strip()
        if code:
            by_code.setdefault(code.casefold(), set()).add(p.page_id)
            spelled.setdefault(code.casefold(), code)

    remaining = project_raw
    matched: list[str] = []
    for code in sorted(by_code, key=len, reverse=True):
        pattern = re.compile(rf"(?<![\w]){re.escape(code)}(?![\w])", re.IGNORECASE)
        if pattern.search(remaining):
            matched.append(code)
            # Blank the span so a shorter code inside it cannot match again.
            remaining = pattern.sub(lambda m: " " * len(m.group(0)), remaining)

    unresolved = Finding(code="unresolved_project", params={"raw": project_raw.strip()})
    if len(matched) != 1:
        return ProjectResolution(findings=[unresolved])
    ids = by_code[matched[0]]
    if len(ids) != 1:
        return ProjectResolution(findings=[unresolved])
    return ProjectResolution(page_id=next(iter(ids)), code=spelled[matched[0]])


# -- departments ----------------------------------------------------------------


def normalize_department_token(token: str, aliases: Mapping[str, str]) -> str:
    """Apply the configured header alias map, then uppercase."""
    stripped = " ".join(token.split())
    folded_aliases = {k.strip().casefold(): v for k, v in aliases.items()}
    return folded_aliases.get(stripped.casefold(), stripped).upper()


def resolve_department(
    token: str | None, options: Sequence[str], aliases: Mapping[str, str]
) -> DepartmentResolution:
    """Validate a header department token against the live ``Department`` options."""
    if not token or not token.strip():
        return DepartmentResolution()
    normalized = normalize_department_token(token, aliases)
    live = {" ".join(o.split()).casefold(): o for o in options}
    match = live.get(normalized.casefold())
    if match is None:
        return DepartmentResolution(
            token=token.strip(),
            findings=[Finding(code="unknown_department", params={"token": token.strip()})],
        )
    return DepartmentResolution(department=match, token=token.strip())


def relation_properties(people: PeopleResolution, project: ProjectResolution) -> dict[str, dict]:
    """Notion relation values for Assigned, cc and Projects.

    Only resolved page ids are written; an unresolved Project leaves Projects
    empty. ``🗂️ Coordinator`` is never set in v1.
    """
    return {
        "Assigned": {"relation": [{"id": page_id} for page_id in people.assigned]},
        "cc": {"relation": [{"id": page_id} for page_id in people.cc]},
        "Projects": {"relation": [{"id": project.page_id}] if project.page_id else []},
    }
