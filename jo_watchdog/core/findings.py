"""Pure core: findings recorded while planning, rendered later for ops.

A finding is a machine code plus parameters. Planning only records them; the
wording of each ops report belongs to verification and reporting (#23).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

# Codes recorded by deadline resolution (#21).
DEADLINE_PAST = "deadline_past"
DEADLINE_UNPARSED = "deadline_unparsed"
SHIFT_FALLBACK = "shift_fallback"
ASAP = "asap"


class Finding(BaseModel):
    code: str
    params: dict[str, Any] = Field(default_factory=dict)
