"""Binance read-only adapter (Phase 5a): market data and account reads only.

Every order, borrow, repay and transfer method raises ``ReadOnlyViolation`` before any network code runs, and the
HTTP layer cannot send anything but an allowlisted GET (see ``http.py``). Endpoint details, weights and the
quirks below are recorded, with dates, in docs/exchanges/binance.md.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager
from decimal import Decimal
from typing import Any

from oddtrue.exchanges.base import ExchangeAdapter
from oddtrue.exchanges.binance.depth import NAME, DepthBook, parse_levels
from oddtrue.exchanges.binance.http import BinanceHttp, ReadOnlyViolation
from oddtrue.exchanges.errors import AuthError, ExchangeDown, Rejected
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

# REST depth only accepts these limits; weight by limit (docs/exchanges/binance.md, read 2026-10-22).
_DEPTH_WEIGHT = ((100, 5), (500, 25), (1000, 50), (5000, 250))
_DEPTH_LIMITS = (5, 10, 20, 50, 100, 500, 1000, 5000)
STREAM_URL = "wss://stream.binance.com:9443/ws"

# An async iterator of raw websocket text messages, closed on exit. The real one wraps the ``websockets`` package.
WsConnect = Callable[[str], AbstractAsyncContextManager[AsyncIterator[str | bytes]]]


def _dec(value: Any) -> Decimal:
    return Decimal(str(value))


def _depth_request(depth: int) -> tuple[int, int]:
    limit = next((n for n in _DEPTH_LIMITS if n >= depth), 5000)
    weight = next(w for top, w in _DEPTH_WEIGHT if limit <= top)
    return limit, weight


class BinanceReadOnlyAdapter(ExchangeAdapter):
    def __init__(
        self,
        http: BinanceHttp,
        *,
        clock: Callable[[], float],
        ws_connect: WsConnect | None = None,
    ) -> None:
        self._http = http
        self._clock = clock
        self._ws_connect = ws_connect

    @property
    def name(self) -> str:
        return NAME

    # ---- market data ----
    async def _snapshot(self, symbol: str, limit: int, weight: int) -> dict[str, Any]:
        data = await self._http.get(
            "/api/v3/depth",
            {"symbol": symbol, "limit": str(limit)},
            weight=weight,
            priority=Priority.REVALIDATION,
        )
        return dict(data)

    async def get_orderbook(self, symbol: str, depth: int) -> OrderBook:
        limit, weight = _depth_request(depth)
        snap = await self._snapshot(symbol, limit, weight)
        ts = self._clock()  # REST depth carries no exchange timestamp; local receive time bounds staleness
        return DepthBook(symbol, snap).top(depth, ts)

    async def stream_orderbook(self, symbol: str, depth: int) -> AsyncIterator[OrderBook]:
        if self._ws_connect is None:
            raise ExchangeDown("no websocket connector configured", exchange=NAME, code="no_ws")
        limit, weight = _depth_request(max(depth, 100))  # a deeper local book survives level churn
        url = f"{STREAM_URL}/{symbol.lower()}@depth@100ms"
        async with self._ws_connect(url) as messages:
            # Connect first so events are buffered while the snapshot is fetched.
            book = DepthBook(symbol, await self._snapshot(symbol, limit, weight))
            yield book.top(depth, self._clock())
            async for raw in messages:
                event = json.loads(raw)
                if event.get("e") == "depthUpdate" and book.apply(event):
                    yield book.top(depth, self._clock())

    async def get_fees(self, symbol: str) -> Fees:
        data = await self._http.get(
            "/api/v3/account/commission", {"symbol": symbol}, weight=20, priority=Priority.BACKGROUND, signed=True
        )
        std, tax = data["standardCommission"], data["taxCommission"]
        # Ignores the BNB discount on purpose: assuming it would understate cost for a client not paying in BNB.
        return Fees(maker=_dec(std["maker"]) + _dec(tax["maker"]), taker=_dec(std["taker"]) + _dec(tax["taker"]))

    async def get_symbol_rules(self, symbol: str) -> SymbolRules:
        data = await self._http.get("/api/v3/exchangeInfo", {"symbol": symbol}, weight=20, priority=Priority.BACKGROUND)
        entries = [s for s in data["symbols"] if s["symbol"] == symbol]
        if not entries:
            raise Rejected(f"unknown symbol {symbol}", exchange=NAME, code="unknown_symbol")
        info = entries[0]
        if info["status"] != "TRADING":
            raise Rejected(f"{symbol} is {info['status']}", exchange=NAME, code="symbol_not_trading")
        filters = {f["filterType"]: f for f in info["filters"]}
        try:
            lot, price = filters["LOT_SIZE"], filters["PRICE_FILTER"]
            notional = filters.get("NOTIONAL") or filters["MIN_NOTIONAL"]
            return SymbolRules(
                min_qty=_dec(lot["minQty"]),
                max_qty=_dec(lot["maxQty"]),
                step=_dec(lot["stepSize"]),
                tick=_dec(price["tickSize"]),
                min_notional=_dec(notional["minNotional"]),
            )
        except KeyError as exc:
            raise Rejected(f"{symbol} lacks filter {exc}", exchange=NAME, code="missing_filter") from None

    # ---- account ----
    async def _margin_account(self) -> dict[str, Any]:
        return dict(
            await self._http.get("/sapi/v1/margin/account", weight=10, priority=Priority.RECONCILIATION, signed=True)
        )

    async def get_balances(self, account: AccountType) -> list[Balance]:
        if account is AccountType.SPOT:
            data = await self._http.get(
                "/api/v3/account",
                {"omitZeroBalances": "true"},
                weight=20,
                priority=Priority.RECONCILIATION,
                signed=True,
            )
            rows = data["balances"]
        else:
            rows = (await self._margin_account())["userAssets"]
        return [
            Balance(asset=r["asset"], free=_dec(r["free"]), locked=_dec(r["locked"]))
            for r in rows
            if _dec(r["free"]) > ZERO or _dec(r["locked"]) > ZERO
        ]

    async def get_margin_account(self) -> MarginAccount:
        acct = await self._margin_account()
        px = await self._http.get(
            "/api/v3/ticker/price", {"symbol": "BTCUSDT"}, weight=4, priority=Priority.RECONCILIATION
        )
        btc = _dec(px["price"])
        liabilities = _dec(acct["totalLiabilityOfBtc"]) * btc
        # Binance reports 999 when nothing is borrowed; our model uses None.
        level = _dec(acct["marginLevel"]) if liabilities > ZERO else None
        return MarginAccount(
            equity_usdt=_dec(acct["totalNetAssetOfBtc"]) * btc, liabilities_usdt=liabilities, margin_level=level
        )

    async def get_borrowable(self, asset: str) -> Borrowable:
        data = await self._http.get(
            "/sapi/v1/margin/maxBorrowable", {"asset": asset}, weight=50, priority=Priority.REVALIDATION, signed=True
        )
        # `amount` is limited by system availability, `borrowLimit` by the account level: take the smaller.
        return Borrowable(asset=asset, qty=min(_dec(data["amount"]), _dec(data["borrowLimit"])))

    @staticmethod
    def _loan(row: dict[str, Any]) -> Loan:
        return Loan(asset=row["asset"], principal=_dec(row["borrowed"]), interest=_dec(row["interest"]))

    async def get_liabilities(self) -> list[Loan]:
        rows = (await self._margin_account())["userAssets"]
        return [loan for loan in map(self._loan, rows) if loan.outstanding > ZERO]

    async def get_loan(self, asset: str) -> Loan:
        rows = (await self._margin_account())["userAssets"]
        return next((self._loan(r) for r in rows if r["asset"] == asset), Loan(asset, ZERO, ZERO))

    async def get_borrow_rate(self, asset: str) -> BorrowRate:
        data = await self._http.get(
            "/sapi/v1/margin/next-hourly-interest-rate",
            {"assets": asset, "isIsolated": "FALSE"},
            weight=100,
            priority=Priority.BACKGROUND,
            signed=True,
        )
        rows = [r for r in data if r["asset"] == asset]
        if not rows:
            raise Rejected(f"no borrow rate for {asset}", exchange=NAME, code="no_rate")
        return BorrowRate(asset=asset, rate_per_hour=_dec(rows[0]["nextHourlyInterestRate"]))

    # ---- permissions ----
    async def check_permissions(self) -> Permissions:
        data = await self._http.get(
            "/sapi/v1/account/apiRestrictions", weight=1, priority=Priority.RECONCILIATION, signed=True
        )
        if not data["enableReading"]:
            raise AuthError("API key cannot read", exchange=NAME, code="no_read")
        # Binance has no separate borrow flag: borrowing needs `enableMargin`; trading is one combined flag.
        return Permissions(
            read=bool(data["enableReading"]),
            trade=bool(data["enableSpotAndMarginTrading"]),
            margin=bool(data["enableMargin"]),
            borrow=bool(data["enableMargin"]),
            withdraw=bool(data["enableWithdrawals"]),
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


__all__ = ["BinanceReadOnlyAdapter", "Level", "OrderBook", "parse_levels"]
