"""Pipeline states enforced by the ledger schema."""

from __future__ import annotations

from enum import Enum


class State(str, Enum):
    """The pipeline state machine.

    Walking-skeleton subset. Ticket #3 adds ``extracted`` (DeepSeek output)
    between ``received`` and ``planned`` via a forward-only migration.
    """

    RECEIVED = "received"
    PLANNED = "planned"
    WRITING = "writing"
    WRITTEN = "written"
    DONE = "done"


# Ordered transitions allowed by the state machine.
_TRANSITIONS = {
    State.RECEIVED: State.PLANNED,
    State.PLANNED: State.WRITING,
    State.WRITING: State.WRITTEN,
    State.WRITTEN: State.DONE,
}


def can_transition(from_state: State | str, to_state: State | str) -> bool:
    """Return True if ``to_state`` is the next state after ``from_state``."""
    f = State(from_state)
    t = State(to_state)
    return _TRANSITIONS.get(f) is t
