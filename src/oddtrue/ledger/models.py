"""Ledger value types. Entry amounts are non-negative magnitudes whose KIND carries the sign, except ADJUSTMENT."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from oddtrue.pricing.types import ZERO


class EntryKind(StrEnum):
    BUY_COST = "BUY_COST"
    SELL_PROCEEDS = "SELL_PROCEEDS"
    FEE = "FEE"
    INTEREST = "INTEREST"
    SLIPPAGE = "SLIPPAGE"
    RECOVERY_COST = "RECOVERY_COST"  # B interest purchase, recovery fees, other recovery costs (not the A unwind)
    TRANSFER_COST = "TRANSFER_COST"
    REBALANCE_COST = "REBALANCE_COST"
    ADJUSTMENT = "ADJUSTMENT"  # signed correction: positive credits the client, negative debits


class Outcome(StrEnum):
    SUCCESS = "SUCCESS"
    RECOVERED = "RECOVERED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class LegStatus(StrEnum):
    INTENT = "INTENT"  # persisted before sending
    SENT = "SENT"
    FILLED = "FILLED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    EXPIRED = "EXPIRED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"  # UnknownOutcome: must be reconciled


@dataclass(frozen=True, slots=True)
class Entry:
    id: int
    client_id: str
    trade_id: str | None
    kind: EntryKind
    amount_usdt: Decimal
    asset: str | None
    qty: Decimal | None
    note: str | None


@dataclass(frozen=True, slots=True)
class TradeResult:
    """Derived from ledger entries only (spec sections 32-33). Actual figures, never estimates."""

    trade_id: str
    client_id: str
    starting_usdt: Decimal
    allocated_95: Decimal
    reserve_5: Decimal
    capital_used: Decimal
    qty_bought: Decimal
    qty_sold: Decimal
    buy_vwap: Decimal | None
    sell_vwap: Decimal | None
    buy_cost: Decimal
    sell_proceeds: Decimal
    gross: Decimal
    fees: Decimal
    interest: Decimal
    slippage: Decimal
    recovery: Decimal
    transfer: Decimal
    rebalance: Decimal
    adjustments: Decimal
    net: Decimal
    realized_roi_pct: Decimal
    ending_balance: Decimal
    outcome: Outcome | None


def net_from_parts(
    gross: Decimal,
    fees: Decimal,
    interest: Decimal,
    slippage: Decimal,
    recovery: Decimal,
    transfer: Decimal,
    rebalance: Decimal,
    adjustments: Decimal = ZERO,
) -> Decimal:
    """Spec 33: net = sell proceeds - buy cost - fees - interest - slippage - recovery/transfer costs."""
    return gross - fees - interest - slippage - recovery - transfer - rebalance + adjustments
