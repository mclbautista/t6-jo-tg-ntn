"""Pipeline states enforced by the ledger schema."""

from __future__ import annotations

from enum import Enum


class State(str, Enum):
    """The pipeline state machine.

    ``extracted`` holds a validated DeepSeek result; ``ignored`` is terminal for
    ``not_jo`` and ``no_text`` messages, which also get an ``ignore_log`` row.
    """

    RECEIVED = "received"
    EXTRACTED = "extracted"
    PLANNED = "planned"
    WRITING = "writing"
    WRITTEN = "written"
    DONE = "done"
    IGNORED = "ignored"


# Transitions allowed by the state machine.
_TRANSITIONS = {
    State.RECEIVED: {State.EXTRACTED, State.IGNORED},
    State.EXTRACTED: {State.PLANNED},
    State.PLANNED: {State.WRITING},
    State.WRITING: {State.WRITTEN},
    State.WRITTEN: {State.DONE},
}


def can_transition(from_state: State | str, to_state: State | str) -> bool:
    """Return True if ``to_state`` may follow ``from_state``."""
    f = State(from_state)
    t = State(to_state)
    return t in _TRANSITIONS.get(f, set())
