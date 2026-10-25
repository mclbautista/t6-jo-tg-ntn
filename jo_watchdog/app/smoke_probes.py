"""Telegram origin and reaction probes for ``smoke.py`` (#60).

Pure checks over supplied facts (recorded or captured fixtures, a ``getChat``
reaction list, attestations), so offline tests exercise them without a network.
A fixture that is not supplied is a gap, reported as SKIP, never a pass; actual
live evidence comes only from an authorized manual run with real forwards.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

from jo_watchdog.app.smoke_gate import FAIL, MANUAL, PASS, SKIP, Attestation, Result
from jo_watchdog.core.provenance import parse_forward_origin
from jo_watchdog.core.report import forward_origin_from_raw

ORIGIN_TYPES = ("user", "hidden_user", "chat", "channel")

# Standard emoji a bot may use as a reaction when ``getChat`` omits
# ``available_reactions`` (Bot API ReactionTypeEmoji). Variation selectors are ignored.
STANDARD_REACTIONS = frozenset(
    "👍 👎 ❤ 🔥 🥰 👏 😁 🤔 🤯 😱 🤬 😢 🎉 🤩 🤮 💩 🙏 👌 🕊 🤡 🥱 🥴 😍 🐳 ❤‍🔥 🌚 🌭 💯 🤣 ⚡ "
    "🍌 🏆 💔 🤨 😐 🍓 🍾 💋 🖕 😈 😴 😭 🤓 👻 👨‍💻 👀 🎃 🙈 😇 😨 🤝 ✍ 🤗 🫡 🎅 🎄 ☃ 💅 "
    "🤪 🗿 🆒 💘 🙉 🦄 😘 💊 🙊 😎 👾 🤷‍♂ 🤷 🤷‍♀ 😡".split()
)


def _bare(emoji: str) -> str:
    return emoji.replace("️", "")


def load_forward_fixtures(path: str | Path) -> dict[str, dict | None]:
    """Read ``{origin type: Bot API message or update of a real forward | null}``."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("forward fixtures must be a JSON object keyed by origin type")
    unknown = set(data) - set(ORIGIN_TYPES)
    if unknown:
        raise ValueError(f"unknown origin types: {', '.join(sorted(unknown))}")
    return data


def _expected_channel_link(origin: Mapping) -> str | None:
    chat = origin.get("chat") or {}
    message_id = origin.get("message_id")
    if not message_id:
        return None
    if chat.get("username"):
        return f"https://t.me/{chat['username']}/{message_id}"
    chat_id = str(chat.get("id", ""))
    if chat_id.startswith("-100"):
        return f"https://t.me/c/{chat_id[4:]}/{message_id}"
    return None


def _origin_of(fixture: Mapping) -> dict:
    message = fixture.get("message", fixture)
    origin = message.get("forward_origin") if isinstance(message, Mapping) else None
    return origin if isinstance(origin, dict) else {}


def check_origin_fixture(kind: str, fixture: Mapping) -> str | None:
    """A problem with one fixture of origin ``kind``, or None when it checks out.

    The channel link is compared with one built here from the raw chat and message
    id; every other origin must produce no link.
    """
    raw_origin = _origin_of(fixture)
    if raw_origin.get("type") != kind:
        return f"{kind}: fixture is a {raw_origin.get('type')!r} forward, not {kind!r}"
    if parse_forward_origin(fixture.get("message", fixture)) is None:
        return f"{kind}: provenance parser does not read this forward"
    origin = forward_origin_from_raw(dict(fixture))
    if origin is None or origin.type != kind:
        return f"{kind}: report parser reads it as {getattr(origin, 'type', None)!r}"
    if kind == "channel":
        expected = _expected_channel_link(raw_origin)
        if expected is None:
            return "channel: fixture lacks the chat id/username and message id of a real post"
        if origin.original_link != expected:
            return f"channel: link {origin.original_link!r}, want {expected!r}"
    elif origin.original_link is not None:
        return f"{kind}: unsupported origin must have no link, got {origin.original_link!r}"
    return None


def evaluate_forward_origins(fixtures: Mapping[str, Mapping | None] | None) -> Result:
    """All four origins checked; any unsupplied fixture is a gap, so no PASS."""
    fixtures = fixtures or {}
    problems, gaps, checked = [], [], []
    for kind in ORIGIN_TYPES:
        fixture = fixtures.get(kind)
        if not fixture:
            gaps.append(kind)
        elif problem := check_origin_fixture(kind, fixture):
            problems.append(problem)
        else:
            checked.append(kind)
    if problems:
        return Result(FAIL, "; ".join(problems))
    if gaps:
        done = f"checked {', '.join(checked)}; " if checked else ""
        return Result(SKIP, f"{done}no real forward fixture for: {', '.join(gaps)}")
    return Result(PASS, "user, hidden_user, chat and channel forwards read; only channel links")


def evaluate_channel_link(fixtures: Mapping[str, Mapping | None] | None) -> Result:
    """The real channel-origin link; the person opens it to see the original post."""
    fixture = (fixtures or {}).get("channel")
    if not fixture:
        return Result(SKIP, "no real channel forward fixture supplied")
    if problem := check_origin_fixture("channel", fixture):
        return Result(FAIL, problem)
    link = forward_origin_from_raw(dict(fixture)).original_link
    return Result(PASS, f"channel link {link}")


def evaluate_reactions(available: list | None, configured: str) -> tuple[Result, str]:
    """Judge ``configured`` against ``getChat``'s ``available_reactions``.

    ``available`` is the list of reaction types, or None when Telegram omits it.
    Returns the verdict and the line to print. An omitted list allows only the
    standard emoji; an empty list allows none.
    """
    if available is None:
        shown = "absent (all standard emoji allowed)"
        if _bare(configured) not in STANDARD_REACTIONS:
            return Result(FAIL, f"{configured} is not a standard reaction emoji"), shown
        return Result(PASS, f"{configured} is a standard emoji (no restriction)"), shown
    emoji = [r.emoji for r in available if getattr(r, "type", "") == "emoji"]
    shown = " ".join(emoji) or "(none)"
    if _bare(configured) not in {_bare(e) for e in emoji}:
        return Result(FAIL, f"captured_reaction {configured} is not in the list above"), shown
    return Result(PASS, f"{configured} is available"), shown


def _handle(value: str | None) -> str:
    return (value or "").strip().lstrip("@").casefold()


def evaluate_owner_handle(
    owner: str | None, leizette: str | None, attestation: Attestation | None
) -> Result:
    """MANUAL until a person attests; an attestation must name both configured handles."""
    handles = [h for h in (owner, leizette) if _handle(h)]
    ask = (
        f"confirm {owner} (owner) and {leizette} (Leizette) are the real handles; "
        "the Bot API cannot look a handle up. Record it with --attestations "
        "(scope must name both handles)"
    )
    if attestation is None:
        return Result(MANUAL, ask)
    named = f"{attestation.scope} {attestation.evidence}".casefold()
    unnamed = [h for h in handles if _handle(h) not in named]
    if unnamed:
        return Result(FAIL, f"attestation does not name {', '.join(unnamed)}: {attestation.scope}")
    return Result(MANUAL, ask)
