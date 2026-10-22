"""validate_trade(): the master mathematical constraint (spec section 41) in exactly one place.

Every check runs (no early exit) so the result lists all failing reasons; ``reason`` is the first one.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from oddtrue.allocation.capacity import client_trade_capacity, market_cap
from oddtrue.pricing.profit import meets_min_roi
from oddtrue.pricing.rounding import is_multiple_of
from oddtrue.pricing.types import ZERO, SymbolRules


class Check(StrEnum):
    QTY_NOT_POSITIVE = "qty_not_positive"
    CLIENT_ALLOCATION = "client_allocation_exceeds_trade_capacity"
    MARKET_EXECUTION = "market_execution_exceeds_liquidity_cap"
    BORROW_QTY = "borrow_qty_exceeds_available"
    MAX_QTY = "qty_above_exchange_max"
    MIN_QTY = "qty_below_exchange_min"
    LOT_SIZE = "qty_not_on_lot_size"
    MIN_NOTIONAL = "notional_below_minimum"
    PRICE_TICK = "price_not_on_tick"
    MIN_ROI = "net_roi_below_minimum"
    RECOVERY_INTEREST_RESERVE = "recovery_interest_exceeds_reserve"
    LOAN_NOT_ZERO = "outstanding_loan_not_zero"
    REPAY_COIN = "repay_coin_unavailable_on_borrow_side"


@dataclass(frozen=True, slots=True)
class Failure:
    check: Check
    side: str  # "buy", "sell" or "-" when the check is not per-exchange
    detail: str


@dataclass(frozen=True, slots=True)
class TradeProposal:
    qty: Decimal
    buy_price: Decimal  # buy VWAP, used for capital and notional
    sell_price: Decimal  # sell VWAP, used for notional
    usable_buy: Decimal  # per-exchange usable capital (D-022)
    usable_borrow: Decimal
    trade_pct: Decimal
    executable_liquidity_qty: Decimal
    market_pct: Decimal
    borrowable_qty: Decimal
    buy_rules: SymbolRules
    sell_rules: SymbolRules
    expected_net_roi_pct: Decimal
    min_net_roi_pct: Decimal
    buy_limit_price: Decimal | None = None
    sell_limit_price: Decimal | None = None
    # Recovery-time terms (spec section 41, last two lines); leave None when not in recovery.
    recovery_interest_usdt: Decimal | None = None
    reserve_usdt: Decimal | None = None
    outstanding_loan_after_recovery: Decimal | None = None
    # Settlement feasibility (D-027): coin that must be on the borrow side to repay principal + estimated interest.
    repay_coin_needed: Decimal | None = None
    repay_coin_available: Decimal | None = None


@dataclass(frozen=True, slots=True)
class ValidationResult:
    failures: tuple[Failure, ...]

    @property
    def passed(self) -> bool:
        return not self.failures

    @property
    def reason(self) -> Failure | None:
        return self.failures[0] if self.failures else None


@dataclass(frozen=True, slots=True)
class RecoveryProposal:
    """The recovery-time terms of spec section 41, for use while a trade is already in RECOVERY."""

    recovery_interest_usdt: Decimal | None = None
    reserve_usdt: Decimal | None = None
    outstanding_loan_after_recovery: Decimal | None = None


def _recovery_failures(
    interest_usdt: Decimal | None, reserve_usdt: Decimal | None, outstanding_loan: Decimal | None
) -> list[Failure]:
    """Single implementation of the recovery terms; ``validate_trade`` and ``validate_recovery`` both use it."""
    fails: list[Failure] = []
    if interest_usdt is not None:
        reserve = reserve_usdt if reserve_usdt is not None else ZERO
        if interest_usdt > reserve:
            fails.append(
                Failure(Check.RECOVERY_INTEREST_RESERVE, "sell", f"interest={interest_usdt} > reserve={reserve}")
            )
    if outstanding_loan is not None and outstanding_loan != ZERO:
        fails.append(Failure(Check.LOAN_NOT_ZERO, "sell", f"loan={outstanding_loan}"))
    return fails


def validate_recovery(r: RecoveryProposal) -> ValidationResult:
    return ValidationResult(
        failures=tuple(_recovery_failures(r.recovery_interest_usdt, r.reserve_usdt, r.outstanding_loan_after_recovery))
    )


def validate_trade(t: TradeProposal) -> ValidationResult:
    fails: list[Failure] = []

    def fail(check: Check, side: str, detail: str) -> None:
        fails.append(Failure(check, side, detail))

    if t.qty <= ZERO:
        fail(Check.QTY_NOT_POSITIVE, "-", f"qty={t.qty}")

    capital = t.qty * t.buy_price
    capacity = client_trade_capacity(t.usable_buy, t.usable_borrow, t.trade_pct)
    if capital > capacity:
        fail(Check.CLIENT_ALLOCATION, "-", f"capital={capital} > capacity={capacity}")

    liquidity_cap = market_cap(t.executable_liquidity_qty, t.market_pct)
    if t.qty > liquidity_cap:
        fail(Check.MARKET_EXECUTION, "-", f"qty={t.qty} > cap={liquidity_cap}")

    if t.qty > t.borrowable_qty:
        fail(Check.BORROW_QTY, "sell", f"qty={t.qty} > borrowable={t.borrowable_qty}")

    for side, rules, price, limit in (
        ("buy", t.buy_rules, t.buy_price, t.buy_limit_price),
        ("sell", t.sell_rules, t.sell_price, t.sell_limit_price),
    ):
        if t.qty > rules.max_qty:
            fail(Check.MAX_QTY, side, f"qty={t.qty} > max={rules.max_qty}")
        if t.qty < rules.min_qty:
            fail(Check.MIN_QTY, side, f"qty={t.qty} < min={rules.min_qty}")
        if not is_multiple_of(t.qty, rules.step):
            fail(Check.LOT_SIZE, side, f"qty={t.qty} step={rules.step}")
        if t.qty * price < rules.min_notional:
            fail(Check.MIN_NOTIONAL, side, f"notional={t.qty * price} < min={rules.min_notional}")
        if limit is not None and not is_multiple_of(limit, rules.tick):
            fail(Check.PRICE_TICK, side, f"price={limit} tick={rules.tick}")

    if not meets_min_roi(t.expected_net_roi_pct, t.min_net_roi_pct):
        fail(Check.MIN_ROI, "-", f"roi={t.expected_net_roi_pct} < min={t.min_net_roi_pct}")

    if t.repay_coin_needed is not None:
        available = t.repay_coin_available if t.repay_coin_available is not None else ZERO
        if available < t.repay_coin_needed:
            fail(Check.REPAY_COIN, "sell", f"available={available} < needed={t.repay_coin_needed}")

    fails.extend(_recovery_failures(t.recovery_interest_usdt, t.reserve_usdt, t.outstanding_loan_after_recovery))

    return ValidationResult(failures=tuple(fails))
