"""Client rotation (spec section 8, D-023): longest-waiting-first with size fit. Pure: time is an input."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from oddtrue.pricing.types import ZERO


class RotationReason(StrEnum):
    INCLUDED = "included"
    SKIPPED_DOES_NOT_FIT = "skipped_does_not_fit"
    SKIPPED_NO_ALLOCATION = "skipped_no_allocation"


@dataclass(frozen=True, slots=True)
class Candidate:
    client_id: str
    qty: Decimal  # the client's final allocation (already lot-size rounded)
    last_participated: datetime | None  # None = has never participated


@dataclass(frozen=True, slots=True)
class RotationDecision:
    client_id: str
    position: int  # 1-based place in the queue this round
    qty: Decimal  # quantity granted (0 when skipped)
    reason: RotationReason


def plan_rotation(candidates: Iterable[Candidate], market_capacity_qty: Decimal) -> tuple[RotationDecision, ...]:
    """Walk the queue (never-participated first, then oldest participation, then client_id) and include every
    client whose allocation fits the remaining market capacity.

    A skipped client is not a failed client: nothing is changed for it, so it keeps its place next round.
    Total granted quantity never exceeds ``market_capacity_qty``.
    """
    if market_capacity_qty < ZERO:
        raise ValueError("market capacity cannot be negative")
    pool = list(candidates)
    never = sorted((c for c in pool if c.last_participated is None), key=lambda c: c.client_id)
    seen = sorted(
        (c for c in pool if c.last_participated is not None),
        key=lambda c: (c.last_participated, c.client_id),
    )
    ordered = never + seen
    ids = [c.client_id for c in ordered]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate client_id in rotation candidates")

    remaining = market_capacity_qty
    decisions: list[RotationDecision] = []
    for position, cand in enumerate(ordered, start=1):
        if cand.qty <= ZERO:
            decisions.append(RotationDecision(cand.client_id, position, ZERO, RotationReason.SKIPPED_NO_ALLOCATION))
        elif cand.qty <= remaining:
            remaining -= cand.qty
            decisions.append(RotationDecision(cand.client_id, position, cand.qty, RotationReason.INCLUDED))
        else:
            decisions.append(RotationDecision(cand.client_id, position, ZERO, RotationReason.SKIPPED_DOES_NOT_FIT))
    return tuple(decisions)
