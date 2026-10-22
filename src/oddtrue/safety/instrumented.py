"""An ``ExchangeAdapter`` decorator that measures every call and feeds exchange health.

Wrap each client's adapters with ``instrument(...)`` and hand the result to the engine: API error rates and latency per
exchange (CLAUDE.md section 12) and the automatic per-exchange halt come for free, with no change to engine code.
It never changes a result or an error: whatever the inner adapter returns or raises, the caller sees.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from decimal import Decimal
from typing import TypeVar

from oddtrue.exchanges.base import ExchangeAdapter
from oddtrue.exchanges.errors import ExchangeDown, RateLimited, UnknownOutcome
from oddtrue.exchanges.models import (
    AccountType,
    Balance,
    Borrowable,
    BorrowRate,
    BorrowResult,
    DepositAddress,
    Fees,
    Loan,
    MarginAccount,
    OrderBook,
    OrderRequest,
    OrderResult,
    Permissions,
    RepayResult,
    TransferStatus,
    WithdrawLimits,
)
from oddtrue.execution.deps import Clock
from oddtrue.observability import metrics as m
from oddtrue.observability.metrics import Metrics
from oddtrue.pricing.types import SymbolRules
from oddtrue.safety.health import ExchangeHealth

T = TypeVar("T")


def _unhealthy(exc: Exception) -> bool:
    """Errors that say the EXCHANGE is in trouble (not the request, not the credentials)."""
    return isinstance(exc, ExchangeDown | UnknownOutcome) or (isinstance(exc, RateLimited) and exc.banned)


class InstrumentedAdapter(ExchangeAdapter):
    def __init__(self, inner: ExchangeAdapter, metrics: Metrics, clock: Clock, health: ExchangeHealth | None = None):
        self._inner, self._metrics, self._clock, self._health = inner, metrics, clock, health

    @property
    def name(self) -> str:
        return self._inner.name

    async def _call(self, op: str, fn: Callable[[], Awaitable[T]]) -> T:
        started = self._clock.now
        try:
            result = await fn()
        except Exception as exc:
            self._metrics.inc(m.EXCHANGE_CALLS, exchange=self.name, op=op, result="error")
            self._metrics.inc(m.EXCHANGE_ERRORS, exchange=self.name, error=type(exc).__name__)
            self._metrics.observe(m.EXCHANGE_LATENCY, max(self._clock.now - started, 0.0), exchange=self.name)
            if self._health is not None and _unhealthy(exc):
                await self._health.record_failure(self.name, type(exc).__name__)
            raise
        self._metrics.inc(m.EXCHANGE_CALLS, exchange=self.name, op=op, result="ok")
        self._metrics.observe(m.EXCHANGE_LATENCY, max(self._clock.now - started, 0.0), exchange=self.name)
        if self._health is not None:
            self._health.record_success(self.name)
        return result

    # ---- market data ----------------------------------------
    async def get_orderbook(self, symbol: str, depth: int) -> OrderBook:
        return await self._call("get_orderbook", lambda: self._inner.get_orderbook(symbol, depth))

    async def stream_orderbook(self, symbol: str, depth: int) -> AsyncIterator[OrderBook]:
        async for book in self._inner.stream_orderbook(symbol, depth):
            yield book

    async def get_fees(self, symbol: str) -> Fees:
        return await self._call("get_fees", lambda: self._inner.get_fees(symbol))

    async def get_symbol_rules(self, symbol: str) -> SymbolRules:
        return await self._call("get_symbol_rules", lambda: self._inner.get_symbol_rules(symbol))

    # ---- account ----------------------------------------
    async def get_balances(self, account: AccountType) -> list[Balance]:
        return await self._call("get_balances", lambda: self._inner.get_balances(account))

    async def get_margin_account(self) -> MarginAccount:
        return await self._call("get_margin_account", self._inner.get_margin_account)

    async def get_borrowable(self, asset: str) -> Borrowable:
        return await self._call("get_borrowable", lambda: self._inner.get_borrowable(asset))

    async def get_liabilities(self) -> list[Loan]:
        return await self._call("get_liabilities", self._inner.get_liabilities)

    async def get_borrow_rate(self, asset: str) -> BorrowRate:
        return await self._call("get_borrow_rate", lambda: self._inner.get_borrow_rate(asset))

    # ---- orders ----------------------------------------
    async def place_order(self, req: OrderRequest) -> OrderResult:
        return await self._call("place_order", lambda: self._inner.place_order(req))

    async def get_order(self, client_order_id: str, symbol: str, account: AccountType) -> OrderResult:
        return await self._call("get_order", lambda: self._inner.get_order(client_order_id, symbol, account))

    async def cancel_order(self, client_order_id: str, symbol: str, account: AccountType) -> OrderResult:
        return await self._call("cancel_order", lambda: self._inner.cancel_order(client_order_id, symbol, account))

    # ---- margin ----------------------------------------
    async def borrow(self, asset: str, qty: Decimal, client_id: str) -> BorrowResult:
        return await self._call("borrow", lambda: self._inner.borrow(asset, qty, client_id))

    async def repay(self, asset: str, qty: Decimal, client_id: str) -> RepayResult:
        return await self._call("repay", lambda: self._inner.repay(asset, qty, client_id))

    async def get_loan(self, asset: str) -> Loan:
        return await self._call("get_loan", lambda: self._inner.get_loan(asset))

    # ---- transfers ----------------------------------------
    async def get_deposit_address(self, asset: str, network: str) -> DepositAddress:
        return await self._call("get_deposit_address", lambda: self._inner.get_deposit_address(asset, network))

    async def withdraw(self, asset: str, qty: Decimal, network: str, address: str, client_id: str) -> TransferStatus:
        return await self._call("withdraw", lambda: self._inner.withdraw(asset, qty, network, address, client_id))

    async def get_transfer_status(self, client_id: str) -> TransferStatus:
        return await self._call("get_transfer_status", lambda: self._inner.get_transfer_status(client_id))

    async def get_withdraw_fee_and_min(self, asset: str, network: str) -> WithdrawLimits:
        return await self._call(
            "get_withdraw_fee_and_min", lambda: self._inner.get_withdraw_fee_and_min(asset, network)
        )

    # ---- permissions ----------------------------------------
    async def check_permissions(self) -> Permissions:
        return await self._call("check_permissions", self._inner.check_permissions)


def instrument(
    venues: Mapping[str, ExchangeAdapter], metrics: Metrics, clock: Clock, health: ExchangeHealth | None = None
) -> dict[str, ExchangeAdapter]:
    return {name: InstrumentedAdapter(a, metrics, clock, health) for name, a in venues.items()}
