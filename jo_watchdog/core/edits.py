"""Pure core: what to do with an ``edited_message`` update (#24 edit table).

Capture is one-way: an edit never changes the Notion row. Corrections need a
fresh Telegram message, which gets its own ``Telegram Key`` and row.
"""

from __future__ import annotations

from enum import StrEnum


class EditAction(StrEnum):
    # ``edits`` row with outcome ``reported`` + ``edit_after_capture`` message now.
    REPORT = "reported"
    # ``edits`` row with outcome ``pending_capture``; the row is written from the
    # original text and the same message is posted once it is captured.
    PENDING_CAPTURE = "pending_capture"
    # ``edits`` row only; not re-classified, not reported.
    IGNORED_MESSAGE = "ignored_message"
    # No ledger row: ``ignore_log`` row with reason ``edit_unknown_message``.
    UNKNOWN = "edit_unknown_message"


_CAPTURED = {"written", "done"}
_IN_PIPELINE = {"received", "extracted", "planned", "writing"}


def decide_edit(state: str | None, *, ignored: bool = False) -> EditAction:
    """Map the original message's ledger state to the edit action.

    ``state`` is the ``messages.state`` of the edited message, or ``None`` when
    it is not in ``messages``. ``ignored`` is true when it is in the ledger as
    an ignored message (``ignored`` state or an ``ignore_log`` row).
    """
    if ignored or state == "ignored":
        return EditAction.IGNORED_MESSAGE
    if state is None:
        return EditAction.UNKNOWN
    if state in _CAPTURED:
        return EditAction.REPORT
    if state in _IN_PIPELINE:
        return EditAction.PENDING_CAPTURE
    raise ValueError(f"unknown message state {state!r}")
