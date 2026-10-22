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
    """Mask, then HTML-escape, one source-derived value."""
    return html.escape(outbound_text(str(value)).text, quote=False)


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
                f" for this project+department, {_days(p['days'])}"
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
            names = p.get("names") or []
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
