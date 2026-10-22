"""Bitget read-only adapter (Phase 5b): market data and account reads only.

Spot is the buy side, cross margin the borrow side. Every order, borrow, repay and transfer method raises
``ReadOnlyViolation`` before any network code runs. Endpoints, fields and open questions are recorded, with
dates, in docs/exchanges/bitget.md.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from decimal import Decimal
from typing import Any

from oddtrue.exchanges.base import ExchangeAdapter
from oddtrue.exchanges.bitget.http import NAME, BitgetHttp, ReadOnlyViolation
from oddtrue.exchanges.errors import ExchangeDown, Rejected
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
from oddtrue.pricing.types import ZERO, Level, SymbolRules
from oddtrue.ratelimit import Priority

MAX_DEPTH = 150  # documented maximum for the REST order book
NO_MAX_QTY = Decimal("1e20")  # Bitget reports 0 or an absurd figure when a symbol has no practical maximum
HOURS_PER_DAY = Decimal(24)
POLL_INTERVAL_S = 1.0  # interim: the Bitget websocket book is not built yet (see bitget.md)


def _dec(value: Any) -> Decimal:
    return Decimal(str(value))


def _levels(raw: list[list[str]]) -> tuple[Level, ...]:
    return tuple(Level(_dec(p), _dec(q)) for p, q, *_ in raw if _dec(q) > ZERO)


class BitgetReadOnlyAdapter(ExchangeAdapter):
    def __init__(
        self,
        http: BitgetHttp,
        *,
        clock: Callable[[], float],
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        poll_interval_s: float = POLL_INTERVAL_S,
    ) -> None:
        self._http = http
        self._clock = clock
        self._sleep = sleep
        self._poll = poll_interval_s

    @property
    def name(self) -> str:
        return NAME

    # ---- market data ----
    async def get_orderbook(self, symbol: str, depth: int) -> OrderBook:
        data = await self._http.get(
            "/api/v2/spot/market/orderbook",
            {"symbol": symbol, "type": "step0", "limit": str(min(depth, MAX_DEPTH))},
            ip_weight=1,
            priority=Priority.REVALIDATION,
        )
        return OrderBook(
            symbol=symbol,
            bids=_levels(data["bids"])[:depth],
            asks=_levels(data["asks"])[:depth],
            ts=int(data["ts"]) / 1000,  # matching-engine time, so staleness is measured against the exchange
        )

    async def stream_orderbook(self, symbol: str, depth: int) -> AsyncIterator[OrderBook]:
        """Interim: polls the REST book once per interval. A failed poll ends the stream (caller resyncs)."""
        last_ts = -1.0
        while True:
            book = await self.get_orderbook(symbol, depth)
            if book.ts != last_ts:  # unchanged matching-engine time means no new data
                last_ts = book.ts
                yield book
            await self._sleep(self._poll)

    async def _symbol(self, symbol: str) -> dict[str, Any]:
        data = await self._http.get(
            "/api/v2/spot/public/symbols", {"symbol": symbol}, ip_weight=2, priority=Priority.BACKGROUND
        )
        rows = [r for r in data if r["symbol"] == symbol]
        if not rows:
            raise Rejected(f"unknown symbol {symbol}", exchange=NAME, code="unknown_symbol")
        info: dict[str, Any] = rows[0]
        if info["status"] != "online":
            raise Rejected(f"{symbol} is {info['status']}", exchange=NAME, code="symbol_not_trading")
        return info

    async def get_fees(self, symbol: str) -> Fees:
        # The documented rates are the symbol defaults, "can be overridden by individual transaction fee": an
        # account-specific rate is not readable, so the default (usually the higher one) is used.
        info = await self._symbol(symbol)
        return Fees(maker=_dec(info["makerFeeRate"]), taker=_dec(info["takerFeeRate"]))

    async def get_symbol_rules(self, symbol: str) -> SymbolRules:
        info = await self._symbol(symbol)
        max_qty = _dec(info["maxTradeAmount"])
        return SymbolRules(
            min_qty=max(_dec(info["minTradeAmount"]), ZERO),  # documented obsolete; 0 means "see min notional"
            max_qty=max_qty if max_qty > ZERO else NO_MAX_QTY,
            step=Decimal(1).scaleb(-int(info["quantityPrecision"])),
            tick=Decimal(1).scaleb(-int(info["pricePrecision"])),
            min_notional=_dec(info["minTradeUSDT"]),
        )

    # ---- account ----
    async def get_balances(self, account: AccountType) -> list[Balance]:
        if account is AccountType.SPOT:
            rows = await self._http.get(
                "/api/v2/spot/account/assets",
                {"assetType": "hold_only"},
                ip_weight=0,
                priority=Priority.RECONCILIATION,
                signed=True,
            )
            out = [
                Balance(r["coin"].upper(), _dec(r["available"]), _dec(r["frozen"]) + _dec(r["locked"])) for r in rows
            ]
        else:
            rows = await self._cross_assets()
            out = [Balance(r["coin"].upper(), _dec(r["available"]), _dec(r["frozen"])) for r in rows]
        return [b for b in out if b.free > ZERO or b.locked > ZERO]

    async def _cross_assets(self) -> list[dict[str, Any]]:
        rows = await self._http.get(
            "/api/v2/margin/crossed/account/assets", ip_weight=0, priority=Priority.RECONCILIATION, signed=True
        )
        return [dict(r) for r in rows]

    async def _usdt_price(self, coin: str) -> Decimal:
        if coin == "USDT":
            return Decimal(1)
        rows = await self._http.get(
            "/api/v2/spot/market/tickers", {"symbol": f"{coin}USDT"}, ip_weight=2, priority=Priority.RECONCILIATION
        )
        if not rows or _dec(rows[0]["lastPr"]) <= ZERO:
            raise Rejected(f"no USDT price for {coin}", exchange=NAME, code="no_price")  # never guess a valuation
        return _dec(rows[0]["lastPr"])

    async def get_margin_account(self) -> MarginAccount:
        equity = liabilities = ZERO
        for r in await self._cross_assets():
            debt = _dec(r["borrow"]) + _dec(r["interest"])
            if _dec(r["net"]) == ZERO and debt == ZERO:
                continue
            price = await self._usdt_price(r["coin"].upper())
            equity += _dec(r["net"]) * price  # net already has borrow and interest taken off
            liabilities += debt * price
        level: Decimal | None = None
        if liabilities > ZERO:
            risk = await self._http.get(
                "/api/v2/margin/crossed/account/risk-rate", ip_weight=0, priority=Priority.RECONCILIATION, signed=True
            )
            level = _dec(risk["riskRateRatio"])
        return MarginAccount(equity_usdt=equity, liabilities_usdt=liabilities, margin_level=level)

    async def _rate_and_limit(self, asset: str) -> dict[str, Any]:
        rows = await self._http.get(
            "/api/v2/margin/crossed/interest-rate-and-limit",
            {"coin": asset},
            ip_weight=2,
            priority=Priority.REVALIDATION,
            signed=True,  # the exchange resolves the VIP tier from the caller
        )
        found = [r for r in rows if r["coin"].upper() == asset]
        if not found:
            raise Rejected(f"{asset} is not a cross margin coin", exchange=NAME, code="no_margin_coin")
        row: dict[str, Any] = found[0]
        return row

    async def get_borrowable(self, asset: str) -> Borrowable:
        # Both questions matter: the coin must be borrowable at all AND the account must have room (spec section 15).
        if not (await self._rate_and_limit(asset))["borrowable"]:
            return Borrowable(asset=asset, qty=ZERO)
        data = await self._http.get(
            "/api/v2/margin/crossed/account/max-borrowable-amount",
            {"coin": asset},
            ip_weight=0,
            priority=Priority.REVALIDATION,
            signed=True,
        )
        return Borrowable(asset=asset, qty=_dec(data["maxBorrowableAmount"]))

    @staticmethod
    def _loan(row: dict[str, Any]) -> Loan:
        return Loan(asset=row["coin"].upper(), principal=_dec(row["borrow"]), interest=_dec(row["interest"]))

    async def get_liabilities(self) -> list[Loan]:
        return [loan for loan in map(self._loan, await self._cross_assets()) if loan.outstanding > ZERO]

    async def get_loan(self, asset: str) -> Loan:
        return next(
            (self._loan(r) for r in await self._cross_assets() if r["coin"].upper() == asset), Loan(asset, ZERO, ZERO)
        )

    async def get_borrow_rate(self, asset: str) -> BorrowRate:
        # Top-level rate is the non-VIP (highest) one; VIP tiers are lower, so this never understates cost.
        daily = _dec((await self._rate_and_limit(asset))["dailyInterestRate"])
        return BorrowRate(asset=asset, rate_per_hour=daily / HOURS_PER_DAY)

    # ---- permissions ----
    async def check_permissions(self) -> Permissions:
        info = await self._http.get(
            "/api/v2/spot/account/info", ip_weight=0, priority=Priority.RECONCILIATION, signed=True, min_interval_s=1.0
        )
        granted = set(info["authorities"])
        # Bitget lists "...r" (read) and "...w" (write) codes; there is no separate borrow flag, borrowing is
        # margin-trade write. Reaching this point means the key could read the account.
        return Permissions(
            read=True,
            trade="stow" in granted,
            margin="smow" in granted,
            borrow="smow" in granted,
            withdraw="wwow" in granted,
        )

    # ---- everything below moves money or is a later phase: refused before any network code ----
    async def place_order(self, req: OrderRequest) -> OrderResult:
        raise ReadOnlyViolation("place_order")

    async def get_order(self, client_order_id: str, symbol: str, account: AccountType) -> OrderResult:
        raise ReadOnlyViolation("get_order")

    async def cancel_order(self, client_order_id: str, symbol: str, account: AccountType) -> OrderResult:
        raise ReadOnlyViolation("cancel_order")

    async def borrow(self, asset: str, qty: Decimal, client_id: str) -> BorrowResult:
        raise ReadOnlyViolation("borrow")

    async def repay(self, asset: str, qty: Decimal, client_id: str) -> RepayResult:
        raise ReadOnlyViolation("repay")

    async def get_deposit_address(self, asset: str, network: str) -> DepositAddress:
        raise ReadOnlyViolation("get_deposit_address")

    async def withdraw(self, asset: str, qty: Decimal, network: str, address: str, client_id: str) -> TransferStatus:
        raise ReadOnlyViolation("withdraw")

    async def get_transfer_status(self, client_id: str) -> TransferStatus:
        raise ReadOnlyViolation("get_transfer_status")

    async def get_withdraw_fee_and_min(self, asset: str, network: str) -> WithdrawLimits:
        raise ReadOnlyViolation("get_withdraw_fee_and_min")


__all__ = ["BitgetReadOnlyAdapter", "ExchangeDown"]
