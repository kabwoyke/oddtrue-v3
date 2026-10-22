"""Final per-client allocation (spec section 7): min of all caps, floored to lot size, then re-checked."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from oddtrue.pricing.rounding import common_step, floor_to_step
from oddtrue.pricing.types import ZERO, SymbolRules


class Constraint(StrEnum):
    CLIENT_CAP = "client_cap"
    MARKET_CAP = "market_cap"
    AVAILABLE_CAPITAL = "available_capital"
    BORROWABLE = "borrowable"
    EXCHANGE_MAX = "exchange_max"


class Rejection(StrEnum):
    NO_CAPACITY = "no_capacity"
    BELOW_MIN_QTY = "below_min_qty"
    BELOW_MIN_NOTIONAL = "below_min_notional"
    ABOVE_CAP_AFTER_ROUNDING = "above_cap_after_rounding"


@dataclass(frozen=True, slots=True)
class AllocationInput:
    buy_price: Decimal
    sell_price: Decimal
    client_trade_capacity_usdt: Decimal
    market_cap_qty: Decimal
    available_capital_usdt: Decimal
    borrowable_qty: Decimal
    buy_rules: SymbolRules
    sell_rules: SymbolRules


@dataclass(frozen=True, slots=True)
class Allocation:
    qty: Decimal
    binding: Constraint
    rejection: Rejection | None

    @property
    def ok(self) -> bool:
        return self.rejection is None


def final_allocation(inp: AllocationInput) -> Allocation:
    """Return the order quantity, or qty=0 with the reason it cannot trade.

    Quantity is floored to a step valid on BOTH exchanges, then min qty, min notional and every cap are
    re-checked on the rounded number (rounding must never push a trade over a cap or under a minimum).
    """
    if inp.buy_price <= ZERO or inp.sell_price <= ZERO:
        raise ValueError("prices must be positive")

    caps: tuple[tuple[Constraint, Decimal], ...] = (
        (Constraint.CLIENT_CAP, inp.client_trade_capacity_usdt / inp.buy_price),
        (Constraint.MARKET_CAP, inp.market_cap_qty),
        (Constraint.AVAILABLE_CAPITAL, inp.available_capital_usdt / inp.buy_price),
        (Constraint.BORROWABLE, inp.borrowable_qty),
        (Constraint.EXCHANGE_MAX, min(inp.buy_rules.max_qty, inp.sell_rules.max_qty)),
    )
    binding, raw = min(caps, key=lambda c: c[1])
    if raw <= ZERO:
        return Allocation(qty=ZERO, binding=binding, rejection=Rejection.NO_CAPACITY)

    step = common_step(inp.buy_rules.step, inp.sell_rules.step)
    qty = floor_to_step(raw, step)

    if qty <= ZERO or qty < max(inp.buy_rules.min_qty, inp.sell_rules.min_qty):
        return Allocation(qty=ZERO, binding=binding, rejection=Rejection.BELOW_MIN_QTY)
    if qty * inp.buy_price < inp.buy_rules.min_notional or qty * inp.sell_price < inp.sell_rules.min_notional:
        return Allocation(qty=ZERO, binding=binding, rejection=Rejection.BELOW_MIN_NOTIONAL)
    if any(qty > limit for _, limit in caps):  # pragma: no cover - unreachable by construction; kept as a guard
        return Allocation(qty=ZERO, binding=binding, rejection=Rejection.ABOVE_CAP_AFTER_ROUNDING)
    return Allocation(qty=qty, binding=binding, rejection=None)
