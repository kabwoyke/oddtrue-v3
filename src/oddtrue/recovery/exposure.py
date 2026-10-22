"""What is still open after the legs ran, derived from persisted legs only (no memory, so it survives a crash)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from oddtrue.execution.legs import A_BUY, A_UNWIND, B_BORROW, B_COVER, B_SELL
from oddtrue.ledger.repository import Leg
from oddtrue.pricing.types import ZERO


@dataclass(frozen=True, slots=True)
class Exposure:
    qa_gross: Decimal  # coin bought on A (before fees)
    held_a: Decimal  # coin from this trade still held on A (net of coin fees, minus unwinds)
    qb_sold: Decimal  # coin sold on B
    borrowed: Decimal
    unwound: Decimal  # coin already sold back on A
    cover_net: Decimal  # coin already bought back on B (net of coin fees)
    sell_on_a: Decimal  # surplus coin on A that must be sold (unrounded)
    cover_on_b: Decimal  # net coin still short on B that must be bought (unrounded)

    @property
    def clean(self) -> bool:
        return self.sell_on_a == ZERO and self.cover_on_b == ZERO


def _sum(legs: Sequence[Leg], name: str) -> Decimal:
    return sum((x.filled_qty or ZERO for x in legs if x.leg == name), ZERO)


def _coin_fee(legs: Sequence[Leg], name: str, coin: str) -> Decimal:
    return sum((x.fee or ZERO for x in legs if x.leg == name and x.fee_asset == coin), ZERO)


def compute_exposure(legs: Sequence[Leg], coin: str) -> Exposure:
    qa = _sum(legs, A_BUY)
    unwound = _sum(legs, A_UNWIND)
    qb = _sum(legs, B_SELL)
    cover_net = _sum(legs, B_COVER) - _coin_fee(legs, B_COVER, coin)
    held_a = qa - _coin_fee(legs, A_BUY, coin) - unwound
    # Matching is on gross filled quantity: the small coin fee on A is a cost, not an exposure to hedge.
    sell_on_a = max(min(qa - qb - unwound, held_a), ZERO)
    cover_on_b = max(qb - qa - cover_net, ZERO)
    return Exposure(qa, held_a, qb, _sum(legs, B_BORROW), unwound, cover_net, sell_on_a, cover_on_b)
