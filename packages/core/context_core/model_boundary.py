"""Which model calls may leave the firm (ADR-009).

A language model either runs inside the firm's own boundary ("internal", for example a model served
on the firm's own infrastructure) or is reached over a third-party API ("external", for example the
Gemini API). Only public-side material may be sent to an external model. Anything private-side or
above the wall, and anything whose wall side is unknown, stays with internal models or with the
deterministic offline path.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Literal

from context_core.models import Claim, ConflictSet, Entity, KnowledgeItem, WallSide

ModelBoundary = Literal["internal", "external"]

PUBLIC = WallSide.public_side.value


def _wall_of(item: Any) -> str | None:
    if isinstance(item, Claim):
        ws = item.acl.wall_side
    elif isinstance(item, (Entity, KnowledgeItem)):
        ws = item.wall_side
    elif isinstance(item, ConflictSet):
        walls = {_wall_of(c) for c in item.claims}
        if not item.claims or len(item.claims) != len(item.claim_ids):
            return None  # some claims are not visible here, so the conflict's side is unknown
        return PUBLIC if walls == {PUBLIC} else WallSide.private_side.value
    elif isinstance(item, (WallSide, str)):
        ws = item
    else:
        return None
    return ws.value if isinstance(ws, WallSide) else str(ws)


def external_model_allowed(items: Iterable[Any]) -> bool:
    """True only when every item is known to be public-side.

    Fails closed: an item of unknown type or without a wall side blocks external models.
    """
    return all(_wall_of(item) == PUBLIC for item in items)


def model_allowed(boundary: ModelBoundary, items: Iterable[Any]) -> bool:
    """Internal models may see anything the caller may see; external models only public-side items."""
    if boundary == "internal":
        return True
    return external_model_allowed(items)
