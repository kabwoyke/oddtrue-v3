"""Shared value types for pure pricing/allocation math. All money, price and quantity values are Decimal."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

ZERO = Decimal(0)


class Side(StrEnum):
    """BUY consumes asks (ascending price); SELL consumes bids (descending price)."""

    BUY = "buy"
    SELL = "sell"


@dataclass(frozen=True, slots=True)
class Level:
    price: Decimal
    qty: Decimal

    def __post_init__(self) -> None:
        if self.price <= ZERO or self.qty <= ZERO:
            raise ValueError("level price and qty must be positive")


@dataclass(frozen=True, slots=True)
class SymbolRules:
    """Exchange order rules for one symbol (spec section 9)."""

    min_qty: Decimal
    max_qty: Decimal
    step: Decimal
    tick: Decimal
    min_notional: Decimal

    def __post_init__(self) -> None:
        if self.step <= ZERO or self.tick <= ZERO:
            raise ValueError("step and tick must be positive")
        if self.min_qty < ZERO or self.min_notional < ZERO:
            raise ValueError("min_qty and min_notional cannot be negative")
        if self.max_qty < self.min_qty:
            raise ValueError("max_qty cannot be below min_qty")
