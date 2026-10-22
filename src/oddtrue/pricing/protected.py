"""Protected limit price: the computed max-worse price for IOC/FOK orders (spec section 15, never market)."""

from __future__ import annotations

from decimal import Decimal

from oddtrue.pricing.rounding import ceil_to_step, floor_to_step
from oddtrue.pricing.types import Side
from oddtrue.pricing.vwap import slippage_limit_price


def protected_limit_price(reference_price: Decimal, max_slippage: Decimal, tick: Decimal, side: Side) -> Decimal:
    """Worst price we accept. Rounded onto the tick in the protective direction.

    SELL: floor is (1-s)*ref, rounded UP so we never accept worse than allowed.
    BUY: ceiling is (1+s)*ref, rounded DOWN for the same reason.
    """
    raw = slippage_limit_price(reference_price, max_slippage, side)
    return ceil_to_step(raw, tick) if side is Side.SELL else floor_to_step(raw, tick)
