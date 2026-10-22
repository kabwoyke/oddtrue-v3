"""Exchange-neutral data types. All money, price and quantity fields are Decimal."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from oddtrue.pricing.types import ZERO, Level, Side

QUOTE_ASSET = "USDT"


def base_asset(symbol: str) -> str:
    """Base coin of a USDT-quoted symbol such as BTCUSDT. Only USDT pairs are in scope (spec section 3)."""
    if not symbol.endswith(QUOTE_ASSET) or len(symbol) == len(QUOTE_ASSET):
        raise ValueError(f"not a USDT-quoted symbol: {symbol!r}")
    return symbol[: -len(QUOTE_ASSET)]


class AccountType(StrEnum):
    SPOT = "spot"
    MARGIN = "margin"


class TimeInForce(StrEnum):
    """Resting orders are never used by the engine: every order is protected IOC or FOK (CLAUDE.md rule 4)."""

    IOC = "ioc"
    FOK = "fok"


class OrderStatus(StrEnum):
    FILLED = "filled"
    PARTIALLY_FILLED = "partially_filled"  # IOC filled part, rest cancelled
    EXPIRED = "expired"  # IOC/FOK filled nothing
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class OrderBook:
    symbol: str
    bids: tuple[Level, ...]  # best (highest) first
    asks: tuple[Level, ...]  # best (lowest) first
    ts: float  # exchange timestamp, epoch seconds; used for staleness checks


@dataclass(frozen=True, slots=True)
class Fees:
    maker: Decimal
    taker: Decimal


@dataclass(frozen=True, slots=True)
class Balance:
    asset: str
    free: Decimal
    locked: Decimal = ZERO


@dataclass(frozen=True, slots=True)
class MarginAccount:
    equity_usdt: Decimal  # assets minus liabilities, valued in USDT
    liabilities_usdt: Decimal
    margin_level: Decimal | None  # assets / liabilities; None when there are no liabilities


@dataclass(frozen=True, slots=True)
class Borrowable:
    asset: str
    qty: Decimal


@dataclass(frozen=True, slots=True)
class BorrowRate:
    asset: str
    rate_per_hour: Decimal  # adapters normalise whatever the exchange reports to per-hour


@dataclass(frozen=True, slots=True)
class Loan:
    asset: str
    principal: Decimal
    interest: Decimal

    @property
    def outstanding(self) -> Decimal:
        return self.principal + self.interest


@dataclass(frozen=True, slots=True)
class OrderRequest:
    client_order_id: str
    symbol: str
    side: Side
    qty: Decimal
    price: Decimal  # always a protected limit price; there is no market order
    tif: TimeInForce
    account: AccountType


@dataclass(frozen=True, slots=True)
class OrderResult:
    client_order_id: str
    exchange_order_id: str
    symbol: str
    side: Side
    account: AccountType
    status: OrderStatus
    requested_qty: Decimal
    filled_qty: Decimal
    avg_price: Decimal  # 0 when nothing filled
    fee: Decimal
    fee_asset: str


@dataclass(frozen=True, slots=True)
class BorrowResult:
    client_id: str
    asset: str
    qty: Decimal


@dataclass(frozen=True, slots=True)
class RepayResult:
    client_id: str
    asset: str
    qty: Decimal
    remaining: Loan


@dataclass(frozen=True, slots=True)
class Permissions:
    read: bool
    trade: bool
    margin: bool
    borrow: bool
    withdraw: bool


@dataclass(frozen=True, slots=True)
class DepositAddress:
    asset: str
    network: str
    address: str


@dataclass(frozen=True, slots=True)
class WithdrawLimits:
    asset: str
    network: str
    fee: Decimal
    min_amount: Decimal


class TransferState(StrEnum):
    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class TransferStatus:
    client_id: str
    asset: str
    qty: Decimal
    state: TransferState
