"""Profit, fee, interest and ROI math (spec section 13). Estimates only: booked figures come from the exchange."""

from __future__ import annotations

from collections.abc import Iterable
from decimal import Decimal

from oddtrue.pricing.types import ZERO, Side

HUNDRED = Decimal(100)


def buy_cost(qty: Decimal, buy_vwap: Decimal) -> Decimal:
    return qty * buy_vwap


def sell_proceeds(qty: Decimal, sell_vwap: Decimal) -> Decimal:
    return qty * sell_vwap


def gross_profit(proceeds: Decimal, cost: Decimal) -> Decimal:
    return proceeds - cost


def trading_fees(legs: Iterable[tuple[Decimal, Decimal]]) -> Decimal:
    """Sum of executed_value * fee_rate over (executed_value, fee_rate) pairs."""
    total = ZERO
    for value, rate in legs:
        if value < ZERO or rate < ZERO:
            raise ValueError("fee value and rate cannot be negative")
        total += value * rate
    return total


def interest_estimate_coin(borrowed_qty: Decimal, rate_per_hour: Decimal, duration_hours: Decimal) -> Decimal:
    """Interest in the borrowed coin: borrowed * rate * duration. Actual interest comes from the exchange."""
    if borrowed_qty < ZERO or rate_per_hour < ZERO or duration_hours < ZERO:
        raise ValueError("borrowed qty, rate and duration cannot be negative")
    return borrowed_qty * rate_per_hour * duration_hours


def interest_value_usdt(interest_coin: Decimal, price: Decimal) -> Decimal:
    """USDT needed to buy ``interest_coin`` at ``price`` (spec section 21: 0.00002 BTC x 100000 = 2)."""
    return interest_coin * price


def slippage_cost(reference_price: Decimal, fill_price: Decimal, qty: Decimal, side: Side) -> Decimal:
    """Adverse move versus a reference price, never negative. Favourable slippage is not booked as profit.

    Use only when gross profit was computed at reference prices; gross computed from VWAP already contains it.
    """
    adverse = fill_price - reference_price if side is Side.BUY else reference_price - fill_price
    return max(adverse, ZERO) * qty


def net_profit(gross: Decimal, fees: Decimal, interest: Decimal, slippage: Decimal, other: Decimal) -> Decimal:
    return gross - fees - interest - slippage - other


def net_roi_pct(net: Decimal, capital_used: Decimal) -> Decimal:
    if capital_used <= ZERO:
        raise ValueError("capital_used must be positive")
    return net / capital_used * HUNDRED


def meets_min_roi(expected_net_roi_pct: Decimal, min_net_roi_pct: Decimal) -> bool:
    return expected_net_roi_pct >= min_net_roi_pct
