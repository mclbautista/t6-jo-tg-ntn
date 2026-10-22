"""Container health check: ``python -m jo_watchdog.app healthcheck``.

Healthy means the poller stamped ``runtime_state.poller_alive_at`` (migration 7,
issue #25) within the last ``MAX_AGE_SECONDS``. The ledger is opened read-only
so the check never migrates, locks, or creates the database.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

MAX_AGE_SECONDS = 120


def check(ledger_path: str | Path, *, now: datetime | None = None) -> tuple[bool, str]:
    """Return ``(healthy, reason)`` for the ledger at ``ledger_path``."""
    path = Path(ledger_path)
    if not path.exists():
        return False, f"ledger not found at {path}"
    now = now or datetime.now(UTC)
    try:
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        try:
            row = conn.execute(
                "SELECT value FROM runtime_state WHERE key = 'poller_alive_at'"
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return False, f"cannot read runtime_state: {exc}"
    if row is None:
        return False, "poller has not stamped poller_alive_at yet"
    try:
        stamped = datetime.fromisoformat(row[0])
    except ValueError:
        return False, f"unparseable poller_alive_at: {row[0]!r}"
    if stamped.tzinfo is None:
        stamped = stamped.replace(tzinfo=UTC)
    age = (now - stamped).total_seconds()
    if age > MAX_AGE_SECONDS:
        return False, f"poller_alive_at is {age:.0f}s old (limit {MAX_AGE_SECONDS}s)"
    return True, f"poller alive {age:.0f}s ago"
