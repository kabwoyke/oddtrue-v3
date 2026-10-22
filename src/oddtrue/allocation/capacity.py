"""Client trade capacity (95/5) and market liquidity cap (90%). Each rule is implemented once, here."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from oddtrue.pricing.types import ZERO

ONE = Decimal(1)


@dataclass(frozen=True, slots=True)
class Capacity:
    trade_usdt: Decimal
    reserve_usdt: Decimal


def split_capacity(usable: Decimal, trade_pct: Decimal, reserve_pct: Decimal) -> Capacity:
    """U -> (U x trade_pct, U x reserve_pct). Spec section 2: $500 -> $475 / $25."""
    if usable < ZERO:
        raise ValueError("usable capacity cannot be negative")
    if trade_pct + reserve_pct != ONE:
        raise ValueError("trade_pct + reserve_pct must equal 1")
    return Capacity(trade_usdt=usable * trade_pct, reserve_usdt=usable * reserve_pct)


def client_trade_capacity(usable_buy: Decimal, usable_borrow: Decimal, trade_pct: Decimal) -> Decimal:
    """``trade_capacity`` (D-022): trade_pct x min(per-exchange usable capital)."""
    if usable_buy < ZERO or usable_borrow < ZERO:
        raise ValueError("usable capacity cannot be negative")
    return min(usable_buy, usable_borrow) * trade_pct


def market_cap(executable: Decimal, market_pct: Decimal) -> Decimal:
    """Max share of executable liquidity we may consume. Works for qty or USDT. Spec 6: 50000 -> 45000."""
    if executable < ZERO:
        raise ValueError("executable liquidity cannot be negative")
    return executable * market_pct
