"""Pure core: the locked ops-topic message formats (#23).

Messages are rendered as Telegram HTML (``parse_mode="HTML"``): the wording is
exactly the #23 templates, with ``[text](URL)`` written as ``<a href>`` and
backticks as ``<code>``, so only ``& < >`` ever need escaping.

Every source-derived field passes through the shared masker before it is
escaped; template text and URLs built here are not masked. The full raw
message is never part of a report.
"""

from __future__ import annotations

import html
from datetime import date, datetime
from typing import Any

from jo_watchdog.core.masker import outbound_text
from jo_watchdog.core.verify import MANILA, Finding, ForwardOrigin, tags_leizette

FLAGGED_HEADER = "⚠️ Flagged on capture"
EDITED_HEADER = "✏️ Edited after capture, row not updated"
STALE_HEADER = "⚠️ Notion cache is stale"

_ORIGIN_WORDS = {"hidden_user": "hidden user", "chat": "group", "channel": "channel"}


def _field(value: Any) -> str:
    """Mask, flatten to one line, then HTML-escape, one source-derived value.

    Masking runs first so a multiline credential assignment is still seen
    whole; only the rendered copy is flattened, never a stored field.
    """
    masked = outbound_text(str(value)).text
    return html.escape(" ".join(masked.split()), quote=False)


def _link(text: str, url: str) -> str:
    return f'<a href="{html.escape(url, quote=True)}">{html.escape(text, quote=False)}</a>'


def _days(n: int) -> str:
    return f"{n} day ago" if n == 1 else f"{n} days ago"


def _role(role: str) -> str:
    return "cc" if role == "cc" else "Assignee"


def _post_date(iso: str) -> str:
    d = date.fromisoformat(iso)
    return f"{d.strftime('%a, %b')} {d.day}"


def finding_line(f: Finding) -> str:
    """The single report line for one finding, worded exactly as in #23."""
    p = f.params
    match f.code:
        case "unsure":
            return f"Classifier unsure: {_field(p.get('reasons', ''))}"
        case "multiple_jo":
            return (
                "Looks like more than one JO in this message, split by hand: "
                f"{_field(p.get('reasons', ''))}"
            )
        case "unresolved_person":
            return f'{_role(p["role"])} "{_field(p["raw"])}" didn\'t match a contact'
        case "ambiguous_person":
            return f'{_role(p["role"])} "{_field(p["raw"])}" matches more than one contact'
        case "unresolved_project":
            return f'Project mention "{_field(p["raw"])}" didn\'t match any known Project Code'
        case "unknown_department":
            return f'Department "{_field(p["token"])}" isn\'t a Notion option'
        case "fuzzy_person":
            return f'Matched by name: "{_field(p["raw"])}" → {_field(p["contact"])}'
        case "exact_repost":
            return f"Exact re-post of {_link('an earlier row', p['url'])}, {_days(p['days'])}"
        case "number_reuse":
            return (
                f"{_field(p['jo_number'])} already used on {_link('an earlier row', p['url'])}"
                f" for this project, {_days(p['days'])}"
            )
        case "forward_origin":
            word = _ORIGIN_WORDS.get(p["origin"], p["origin"])
            return f"Forwarded from a {word}: {_field(p.get('name', ''))}"
        case "title_blank":
            return "Title is blank"
        case "task_blank":
            return "Task is blank"
        case "deadline_past":
            return "Deadline is before the message was posted"
        case "deadline_unparsed":
            return f'Deadline "{_field(p["raw"])}" couldn\'t be turned into a date'
        case "asap":
            return "ASAP — high priority"
        case "shift_fallback":
            # #21 records `defaulted` names, or a `reason` when none resolved.
            names = p.get("names") or p.get("defaulted") or []
            if names:
                who = ", ".join(_field(n) for n in names)
                return f"Deadline defaulted to 23:59: {who} have no fixed End of Shift"
            return "Deadline defaulted to 23:59: no assignee resolved"
        case "header_date_mismatch":
            return (
                f'Header date ("{_field(p["raw"])}") doesn\'t match the actual post date'
                f" ({_post_date(p['posted'])})"
            )
        case "deliverable_hint":
            return (
                f"Possible Deliverable: {_field(p['name'])}"
                f" (match probability {float(p['probability']):.2f})"
            )
        case "matcher_skipped":
            return "Deliverable match skipped: matcher unavailable"
        case "readback_mismatch":
            props = ", ".join(_field(x) for x in p.get("properties", []))
            return f"Notion row differs from the plan: {props}"
        case "duplicate_key":
            rows = ", ".join(_link("row", u) for u in p.get("urls", []))
            return f"More than one Notion row has this Telegram Key: {rows}"
    raise ValueError(f"unknown finding code {f.code!r}")


def handle_mention(handle: str | None) -> str | None:
    """``@handle`` from a configured handle written with or without the ``@``."""
    if not handle or not handle.strip().lstrip("@"):
        return None
    return "@" + html.escape(handle.strip().lstrip("@"), quote=False)


def intake_link(chat_id: int, topic_id: int, message_id: int) -> str:
    """``https://t.me/c/<chat id without -100>/<topic id>/<message id>``."""
    internal = str(chat_id)
    internal = internal[4:] if internal.startswith("-100") else internal.lstrip("-")
    return f"https://t.me/c/{internal}/{topic_id}/{message_id}"


def channel_link(chat: dict, message_id: int | None) -> str | None:
    """Link to an original channel post, or None without enough metadata."""
    if not message_id:
        return None
    if chat.get("username"):
        return f"https://t.me/{chat['username']}/{message_id}"
    chat_id = chat.get("id")
    if chat_id is None:
        return None
    internal = str(chat_id)
    if not internal.startswith("-100"):
        return None
    return f"https://t.me/c/{internal[4:]}/{message_id}"


def forward_origin_from_raw(raw: dict) -> ForwardOrigin | None:
    """Read Bot API ``forward_origin`` from a stored update (or bare message).

    Only a ``channel`` origin carries both a chat and a message id, so it is the
    only one that ever gets an original-message link.
    """
    message = raw.get("message", raw) if isinstance(raw, dict) else {}
    origin = (message or {}).get("forward_origin")
    if not isinstance(origin, dict) or not origin.get("type"):
        return None
    kind = origin["type"]
    name = ""
    link = None
    if kind == "user":
        user = origin.get("sender_user") or {}
        name = " ".join(x for x in (user.get("first_name"), user.get("last_name")) if x)
    elif kind == "hidden_user":
        name = origin.get("sender_user_name") or ""
    elif kind in ("chat", "channel"):
        chat = origin.get("sender_chat" if kind == "chat" else "chat") or {}
        name = chat.get("title") or ""
        if origin.get("author_signature"):
            name = f"{name} — {origin['author_signature']}" if name else origin["author_signature"]
        if kind == "channel":
            link = channel_link(chat, origin.get("message_id"))
    return ForwardOrigin(type=kind, name=name, original_link=link)


def render_capture_report(
    *,
    name: str,
    findings: list[Finding],
    page_url: str,
    intake_url: str,
    original_url: str | None = None,
    leizette_handle: str | None = None,
) -> str | None:
    """The one combined report for a capture, or None when nothing triggered."""
    if not findings:
        return None
    lines = [FLAGGED_HEADER, f"<code>{_field(name)}</code>", ""]
    lines += [finding_line(f) for f in findings]
    lines += ["", _link("Notion row", page_url), _link("Telegram message", intake_url)]
    if original_url:
        lines.append(_link("Original message", original_url))
    mention = handle_mention(leizette_handle) if tags_leizette(findings) else None
    if mention:
        lines += ["", mention]
    return "\n".join(lines)


def render_edit_after_capture(*, name: str, page_url: str, intake_url: str) -> str:
    return "\n".join(
        [
            EDITED_HEADER,
            f"<code>{_field(name)}</code>",
            "",
            _link("Notion row", page_url),
            _link("Telegram message", intake_url),
        ]
    )


def render_stale_cache(
    *, cache: str, last_refreshed: datetime, now: datetime, error: str | None
) -> str:
    """``cache`` is ``People``, ``Projects`` or ``Departments``."""
    local = last_refreshed.astimezone(MANILA)
    hours = int((now - last_refreshed).total_seconds() // 3600)
    stamp = f"{local.strftime('%b')} {local.day} {local.strftime('%H:%M')}"
    return "\n".join(
        [
            STALE_HEADER,
            f"{_field(cache)}: last refreshed {stamp} ({hours} hours ago)",
            f"Last error: {_field(error or 'unknown')}",
        ]
    )


DRY_RUN_HEADER = "🧪 Dry run — planned, not written to Notion"


def _plain_property(prop: dict) -> str:
    """A Notion property value as the text a person would read."""
    if "title" in prop or "rich_text" in prop:
        items = prop.get("title") or prop.get("rich_text") or []
        return "".join(item["text"]["content"] for item in items)
    if "select" in prop or "status" in prop:
        return (prop.get("select") or prop.get("status") or {}).get("name", "")
    if "date" in prop:
        return (prop["date"] or {}).get("start", "")
    if "relation" in prop:
        return ", ".join(item["id"] for item in prop["relation"])
    return str(prop)


def render_dry_run_plan(*, plan: dict, intake_url: str) -> str:
    """The masked stored plan, one line per planned property (#27)."""
    lines = [DRY_RUN_HEADER, f"<code>{_field(plan.get('name', ''))}</code>", ""]
    for key, value in plan.items():
        if key in ("name", "telegram_key", "body_blocks") or value in (None, "", [], {}):
            continue
        if key == "properties":
            for prop, raw in value.items():
                text = _plain_property(raw)
                if prop not in ("Name", "Telegram Key") and text:
                    lines.append(f"{_field(prop)}: {_field(text)}")
            continue
        lines.append(f"{_field(key)}: {_field(value)}")
    lines.append(f"Body: {len(plan.get('body_blocks') or [])} block(s)")
    lines += ["", _link("Telegram message", intake_url)]
    return "\n".join(lines)


def render_outage_alert(
    *,
    step: str,
    intake_url: str,
    error: str,
    attempts: int,
    given_up_at: datetime,
    telegram_key: str,
    owner_handle: str | None,
) -> str:
    """The one owner-tagged alert after retry exhaustion (#25).

    ``step`` is ``Classification``, ``Notion write``, ``Notion read-back`` or
    ``Notion reconciliation``.
    """
    local = given_up_at.astimezone(MANILA)
    lines = [
        f"🔴 {_field(step)} failed after {attempts} attempts",
        "Message still queued, not lost",
        "",
        f"Telegram message: {_link('link', intake_url)}",
        f"Last error: {_field(error)} (attempt {attempts} of {attempts},"
        f" given up at {local.strftime('%H:%M')})",
        f"Will not retry automatically — run <code>retry {_field(telegram_key)}</code>"
        " after fixing the cause",
    ]
    mention = handle_mention(owner_handle)
    if mention:
        lines += ["", mention]
    return "\n".join(lines)


def render_heartbeat_failed_alert(
    *,
    window_start: datetime,
    window_end: datetime,
    error: str,
    owner_handle: str | None,
) -> str:
    """The one owner-tagged alert when the gateway reports a heartbeat as not delivered (#113)."""
    lines = [
        "🔴 Daily heartbeat was not delivered",
        f"Window: {_stamp(window_start)} → {_stamp(window_end)}",
        f"Gateway error: {_field(error)}",
        "Not re-sent automatically; the next heartbeat links the last delivered one",
    ]
    mention = handle_mention(owner_handle)
    if mention:
        lines += ["", mention]
    return "\n".join(lines)


def _stamp(when: datetime) -> str:
    local = when.astimezone(MANILA)
    return f"{local.strftime('%b')} {local.day} {local.strftime('%H:%M')}"


def render_heartbeat(
    *,
    window_start: datetime,
    window_end: datetime,
    stats: dict,
    previous: tuple[datetime, str] | None = None,
) -> str:
    """The daily heartbeat for ``[window_start, window_end)`` (#25), from ledger stats.

    ``previous`` is the (sent date, URL) of the last heartbeat that was posted.
    """
    d = stats["deliverables"]
    lines = [f"📋 Daily heartbeat — {_stamp(window_start)} → {_stamp(window_end)}"]
    if previous:
        sent, url = previous
        local = sent.astimezone(MANILA)
        lines.append("↩︎ " + _link(f"Previous heartbeat ({local.strftime('%b')} {local.day})", url))
    lines += [
        "",
        f"Captured: {stats['captured']}",
        f"Flagged: {stats['flagged']}",
        f"Ignored (not a JO): {stats['ignored']}",
        f"No-text forwards: {stats['no_text']}",
        f"Passwords withheld: {stats['passwords_withheld']}",
        f"Reaction failed: {stats['reaction_failed']}",
        "",
        f"Deliverables linked: {d['linked']}",
        f"Deliverables hinted: {d['hinted']}",
        f"Deliverables none: {d['none']}",
        f"Deliverables skipped: {d['skipped']}",
        "",
        f"Queue: {stats['stuck']} stuck, {stats['pending_retry']} pending retry,"
        f" {stats['ops_failed']} ops posts failed",
    ]
    for old, new in stats["models"].values():
        lines.append(f"Model changed: {_field(old)} → {_field(new)}")
    if stats["ignored_first_lines"]:
        lines += ["", "Ignored, first lines:"]
        lines += [f'"{_field(line)}"' for line in stats["ignored_first_lines"]]
    return "\n".join(lines)


GAP_HEADER = "⚠️ Possibly missed intake messages"
# A long gap lists only the first few message links; the range covers the rest.
GAP_LINK_LIMIT = 5


def render_message_gap(*, chat_id: int, topic_id: int, last_seen: int, message_id: int) -> str:
    """The ops alert when intake chat message ids jump from ``last_seen`` to ``message_id`` (#101).

    Ids are one counter for the whole chat, so the missing ones may also be posts in
    other topics the watchdog is never sent; the alert only says they might be lost.
    """
    first, last = last_seen + 1, message_id - 1
    count = last - first + 1
    span = f"{first}" if count == 1 else f"{first}–{last}"
    links = " · ".join(
        _link(str(missing), intake_link(chat_id, topic_id, missing))
        for missing in range(first, min(last, first + GAP_LINK_LIMIT - 1) + 1)
    )
    if count > GAP_LINK_LIMIT:
        links += " · …"
    noun = "message" if count == 1 else "messages"
    return "\n".join(
        [
            GAP_HEADER,
            f"{count} {noun} may not have reached the watchdog: id {span}"
            f" (last seen {last_seen}, now {message_id})",
            "Ids are shared across the chat; these may be posts in other topics."
            " A gap alone does not confirm lost intake messages.",
            f"Open: {links}",
            "If one is a Job Order, backfill it with <code>ingest</code> (see the README).",
        ]
    )
