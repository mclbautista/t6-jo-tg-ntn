"""Smoke results: per-message capture verification and the full-verification gate.

``smoke.py`` is manual and never runs in CI, but the logic that decides what its
results mean lives here so offline tests can exercise it: which ledger facts prove
one named message was captured, and when a run may be called fully verified.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from jo_watchdog.ledger.db import Ledger

PASS, FAIL, SKIP, MANUAL, ATTESTED = "PASS", "FAIL", "SKIP", "MANUAL", "ATTESTED"

# Steps a full verification does not need: both are opt-in probes with side effects.
OPTIONAL_STEPS = frozenset({"unknown_select", "live_telegram"})


@dataclass
class Result:
    status: str  # PASS | FAIL | SKIP | MANUAL | ATTESTED
    detail: str = ""


# -- one message's capture ------------------------------------------------------


async def verify_capture(
    ledger: Ledger,
    key: str,
    *,
    notion,
    data_source_id: str,
    expect_dry_run: bool = False,
    recorded_reactions: list[tuple[int, int, str]] | None = None,
) -> Result:
    """Prove that the message named ``key`` (not the latest row) was fully captured.

    Checks its classification, stored plan, Notion write and read-back, the outbox
    rows linked to it, and ``reaction_state``. ``recorded_reactions`` is given when
    Telegram was a recording stand-in, to confirm the reaction reached it too.
    """
    msg = ledger.get_message(key)
    if msg is None:
        return Result(FAIL, f"{key} is not in the ledger")
    extract = (ledger.stage_result(key, "extract") or {}).get("extraction") or {}
    if extract.get("verdict") != "jo":
        return Result(FAIL, f"{key} classified {extract.get('verdict')!r}, want 'jo'")
    try:
        plan = json.loads(msg["plan_json"] or "null")
    except ValueError:
        plan = None
    if not isinstance(plan, dict) or plan.get("telegram_key") != key:
        return Result(FAIL, f"{key} has no stored plan for itself")

    outbox = ledger._conn.execute(
        "SELECT kind, state, telegram_message_id FROM outbox WHERE message_id = ?", (msg["id"],)
    ).fetchall()
    if expect_dry_run:
        if msg["state"] != "planned" or msg["notion_page_id"]:
            return Result(FAIL, f"{key} is {msg['state']}, want held at 'planned' with no page")
        posts = [r for r in outbox if r["kind"] == "dry_run_plan"]
        if len(posts) != 1 or posts[0]["state"] != "sent":
            got = [dict(r) for r in outbox]
            return Result(FAIL, f"{key}: want one sent dry_run_plan, got {got}")
        return Result(PASS, f"{key} planned and held; dry-run plan delivered")

    if msg["state"] != "done":
        detail = f"{key} stopped at {msg['state']}"
        if msg.get("last_error"):
            detail += f"; last error (masked): {msg['last_error']}"
        return Result(FAIL, detail)
    if msg["readback_mismatch"]:
        return Result(FAIL, f"{key} read-back mismatch: {msg['readback_mismatch']}")
    if not msg["notion_page_id"]:
        return Result(FAIL, f"{key} has no Notion page id")
    page = await notion.get(data_source_id, msg["notion_page_id"])
    stored_key = page.telegram_key
    if stored_key != key:
        return Result(FAIL, f"Notion page carries Telegram Key {stored_key!r}, want {key!r}")
    reports = [r for r in outbox if r["kind"] == "capture_report"]
    undelivered = any(r["state"] != "sent" or r["telegram_message_id"] is None for r in reports)
    if not reports or undelivered:
        return Result(FAIL, f"{key}: capture report not delivered: {[dict(r) for r in outbox]}")
    if msg["reaction_state"] != "set":
        return Result(FAIL, f"{key} reaction_state is {msg['reaction_state']!r}, want 'set'")
    if recorded_reactions is not None and not any(
        (chat, mid) == (msg["chat_id"], msg["message_id"]) for chat, mid, _ in recorded_reactions
    ):
        return Result(FAIL, f"{key}: no reaction reached Telegram")
    return Result(
        PASS, f"{key} classified, planned, written to {msg['notion_page_id']}, reported, reacted"
    )


# -- manual attestations --------------------------------------------------------


@dataclass(frozen=True)
class Attestation:
    step: str
    actor: str
    at: str
    scope: str
    evidence: str


def load_attestations(path: str | Path, known_steps: set[str]) -> dict[str, Attestation]:
    """Read a JSON list of ``{step, actor, at, scope, evidence}``; every field is required."""
    entries = json.loads(Path(path).read_text(encoding="utf-8"))
    attestations: dict[str, Attestation] = {}
    for entry in entries:
        fields = ("step", "actor", "at", "scope", "evidence")
        missing = [f for f in fields if not str(entry.get(f, "")).strip()]
        if missing:
            raise ValueError(f"attestation {entry.get('step', '?')!r} lacks {', '.join(missing)}")
        if entry["step"] not in known_steps:
            raise ValueError(f"attestation for unknown step {entry['step']!r}")
        datetime.fromisoformat(entry["at"])  # the time must be real, not free text
        attestations[entry["step"]] = Attestation(**{f: str(entry[f]) for f in fields})
    return attestations


def attest(result: Result, attestation: Attestation | None) -> Result:
    """A human's attestation settles a step the script could not: SKIP or MANUAL only."""
    if attestation is None or result.status not in (SKIP, MANUAL):
        return result
    return Result(
        ATTESTED,
        f"by {attestation.actor} at {attestation.at}; scope: {attestation.scope}; "
        f"evidence: {attestation.evidence}",
    )


# -- the gate -------------------------------------------------------------------


@dataclass
class Verdict:
    label: str  # FULL | PARTIAL | INCOMPLETE | FAILED
    exit_code: int
    reasons: list[str]


def verdict(results: Mapping[str, Result], all_steps: list[str], *, selected_only: bool) -> Verdict:
    """Full verification needs every required step to PASS or be attested; nothing less."""
    failed = [n for n, r in results.items() if r.status == FAIL]
    if failed:
        return Verdict("FAILED", 1, [f"failed: {', '.join(failed)}"])
    required = [n for n in all_steps if n not in OPTIONAL_STEPS]
    if selected_only:
        return Verdict("PARTIAL", 3, ["ran a subset with --only; not a full verification"])
    open_steps = [
        f"{n} ({results[n].status if n in results else 'not run'})"
        for n in required
        if n not in results or results[n].status not in (PASS, ATTESTED)
    ]
    if open_steps:
        return Verdict("INCOMPLETE", 3, [f"required but not passed: {', '.join(open_steps)}"])
    return Verdict("FULL", 0, [])
