"""Order-book walking: VWAP and executable liquidity (spec sections 5 and 6)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from itertools import pairwise

from oddtrue.pricing.types import ZERO, Level, Side


class InsufficientDepth(ValueError):
    """The book cannot fill the requested quantity."""


@dataclass(frozen=True, slots=True)
class Fill:
    qty: Decimal
    notional: Decimal
    vwap: Decimal
    best_price: Decimal
    worst_price: Decimal


@dataclass(frozen=True, slots=True)
class Liquidity:
    qty: Decimal
    notional: Decimal


def _check_sorted(levels: Sequence[Level], side: Side) -> None:
    for prev, cur in pairwise(levels):
        if (side is Side.BUY and cur.price < prev.price) or (side is Side.SELL and cur.price > prev.price):
            raise ValueError(f"book levels not sorted best-first for {side.value}")


def walk_book(levels: Sequence[Level], qty: Decimal, side: Side) -> Fill:
    """Fill ``qty`` from best-first ``levels`` and return VWAP = sum(price*qty) / sum(qty)."""
    if qty <= ZERO:
        raise ValueError("qty must be positive")
    _check_sorted(levels, side)

    remaining = qty
    notional = ZERO
    worst = ZERO
    for level in levels:
        take = min(level.qty, remaining)
        notional += level.price * take
        worst = level.price
        remaining -= take
        if remaining == ZERO:
            return Fill(qty=qty, notional=notional, vwap=notional / qty, best_price=levels[0].price, worst_price=worst)
    raise InsufficientDepth(f"book holds {qty - remaining}, need {qty}")


def slippage_limit_price(best_price: Decimal, max_slippage: Decimal, side: Side) -> Decimal:
    """Worst acceptable price: best*(1+s) when buying, best*(1-s) when selling."""
    if max_slippage < ZERO or max_slippage >= 1:
        raise ValueError("max_slippage must be in [0, 1)")
    return best_price * (1 + max_slippage) if side is Side.BUY else best_price * (1 - max_slippage)


def executable_liquidity(levels: Sequence[Level], side: Side, max_slippage: Decimal) -> Liquidity:
    """Depth available at prices no worse than best price +/- ``max_slippage``."""
    _check_sorted(levels, side)
    if not levels:
        return Liquidity(qty=ZERO, notional=ZERO)
    limit = slippage_limit_price(levels[0].price, max_slippage, side)
    qty = ZERO
    notional = ZERO
    for level in levels:
        if (side is Side.BUY and level.price > limit) or (side is Side.SELL and level.price < limit):
            break
        qty += level.qty
        notional += level.price * level.qty
    return Liquidity(qty=qty, notional=notional)
