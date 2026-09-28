"""Pure core: the credential-masking seam.

The walking skeleton ships an identity masker. Ticket #2 (credential-safe
capture) replaces this with the deterministic masker that redacts labeled
credentials (``pw``/``pass``/``password``/``passcode``/``PIN`` followed by
``:`` or ``=``) everywhere text leaves the ledger. Keeping this as a single
function means every outbound path already routes through one rule.
"""

from __future__ import annotations


def mask_text(text: str) -> str:
    """Return ``text`` with credentials masked. Identity in v0."""
    return text


def mask_blocks(blocks: list[str]) -> list[str]:
    """Apply :func:`mask_text` to each block."""
    return [mask_text(b) for b in blocks]
