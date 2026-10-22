"""Deterministic exchange simulator for tests and paper mode (CLAUDE.md sections 4A, 11).

``FakeVenue`` is one exchange: shared order books, rules, fees, borrow pool, rate limits, fault plan and a
fake clock. ``FakeVenue.account(client_id)`` returns a ``FakeAccount`` -- an ``ExchangeAdapter`` bound to one
client -- so 300 simulated clients all trade into the same books.

Nothing here sleeps for real: latency advances the fake clock only.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from decimal import ROUND_UP, Decimal
from enum import StrEnum

from oddtrue.exchanges.base import DUPLICATE_CLIENT_ID, ORDER_NOT_FOUND, ExchangeAdapter
from oddtrue.exchanges.errors import (
    AuthError,
    ExchangeDown,
    InsufficientFunds,
    InvalidQuantity,
    RateLimited,
    Rejected,
    StreamDropped,
    UnknownOutcome,
)
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
    OrderStatus,
    Permissions,
    RepayResult,
    TransferState,
    TransferStatus,
    WithdrawLimits,
    base_asset,
)
from oddtrue.pricing.rounding import floor_to_step, is_multiple_of
from oddtrue.pricing.types import ZERO, Level, Side, SymbolRules

_Q8 = Decimal("1e-8")
_HOUR = 3600.0
USDT = "USDT"


def _q8(x: Decimal) -> Decimal:
    """Fees and interest round UP at 8 decimals, as exchanges do (never in our favour)."""
    return x.quantize(_Q8, rounding=ROUND_UP)


class FakeClock:
    def __init__(self, start: float = 1_000_000.0) -> None:
        self.now = start

    def advance(self, seconds: float) -> None:
        self.now += seconds

    async def sleep(self, seconds: float) -> None:
        self.advance(seconds)
        await asyncio.sleep(0)


class Op(StrEnum):
    GET_ORDERBOOK = "get_orderbook"
    STREAM_ORDERBOOK = "stream_orderbook"
    GET_FEES = "get_fees"
    GET_SYMBOL_RULES = "get_symbol_rules"
    GET_BALANCES = "get_balances"
    GET_MARGIN_ACCOUNT = "get_margin_account"
    GET_BORROWABLE = "get_borrowable"
    GET_LIABILITIES = "get_liabilities"
    GET_BORROW_RATE = "get_borrow_rate"
    PLACE_ORDER = "place_order"
    GET_ORDER = "get_order"
    CANCEL_ORDER = "cancel_order"
    BORROW = "borrow"
    REPAY = "repay"
    GET_LOAN = "get_loan"
    GET_DEPOSIT_ADDRESS = "get_deposit_address"
    WITHDRAW = "withdraw"
    GET_TRANSFER_STATUS = "get_transfer_status"
    GET_WITHDRAW_FEE = "get_withdraw_fee_and_min"
    CHECK_PERMISSIONS = "check_permissions"


class FaultKind(StrEnum):
    TIMEOUT_NOT_APPLIED = "timeout_not_applied"  # UnknownOutcome, the action did NOT happen
    TIMEOUT_APPLIED = "timeout_applied"  # UnknownOutcome, but the action DID happen ("timeout that fills")
    REJECT = "reject"
    RATE_LIMIT = "rate_limit"
    EXCHANGE_DOWN = "exchange_down"
    AUTH_ERROR = "auth_error"
    HANG_NOT_APPLIED = "hang_not_applied"  # never returns (caller's hard timeout must fire); action did NOT happen
    HANG_APPLIED = "hang_applied"  # applies the action, then never returns
    PARTIAL_FILL = "partial_fill"  # IOC orders only: fill ``fraction`` of the quantity
    STALE_BOOK = "stale_book"  # get_orderbook returns a snapshot ``age_s`` seconds old


@dataclass(slots=True)
class Fault:
    kind: FaultKind
    times: int = 1
    client_id: str | None = None  # None = any client
    fraction: Decimal = Decimal("0.5")
    age_s: float = 60.0
    code: str = "injected"


@dataclass(frozen=True, slots=True)
class RateLimitConfig:
    window_s: float
    ip_budget: int  # shared by every client (shared egress IP)
    client_budget: int
    ban_after_violations: int
    ban_s: float
    weights: dict[Op, int] = field(default_factory=dict)  # default weight 1


@dataclass(slots=True)
class _Book:
    bids: list[list[Decimal]]  # [price, qty], best first
    asks: list[list[Decimal]]
    ts: float


@dataclass(slots=True)
class _LoanState:
    principal: Decimal = ZERO
    interest: Decimal = ZERO
    opened_ts: float | None = None  # when the principal went from 0 to >0
    hours_charged: int = 0


_DROP = object()


class FakeVenue:
    def __init__(self, name: str, clock: FakeClock | None = None) -> None:
        self.name = name
        self.clock = clock or FakeClock()
        self.latency_s = 0.0
        self.rate_limit: RateLimitConfig | None = None
        self.max_leverage = Decimal(3)  # total liabilities <= equity * (max_leverage - 1)
        self.fees = Fees(maker=Decimal("0.001"), taker=Decimal("0.001"))
        self.withdraw_limits = WithdrawLimits(asset="", network="", fee=Decimal("0.0005"), min_amount=Decimal("0.001"))
        self._books: dict[str, _Book] = {}
        self._rules: dict[str, SymbolRules] = {}
        self._borrow_rate: dict[str, Decimal] = {}
        self._borrow_pool: dict[str, Decimal] = {}
        self._accounts: dict[str, FakeAccount] = {}
        self._faults: list[tuple[Op, Fault]] = []
        self._subs: dict[str, list[asyncio.Queue[object]]] = {}
        self._window = -1
        self._ip_used = 0
        self._client_used: dict[str, int] = {}
        self._violations = 0
        self._banned_until = 0.0

    # ---- configuration ----------------------------------------
    def set_rules(self, symbol: str, rules: SymbolRules) -> None:
        self._rules[symbol] = rules

    def set_book(self, symbol: str, bids: list[tuple[Decimal, Decimal]], asks: list[tuple[Decimal, Decimal]]) -> None:
        self._books[symbol] = _Book([[p, q] for p, q in bids], [[p, q] for p, q in asks], self.clock.now)
        self._publish(symbol)

    def set_borrow(self, asset: str, rate_per_hour: Decimal, pool: Decimal) -> None:
        self._borrow_rate[asset] = rate_per_hour
        self._borrow_pool[asset] = pool

    def add_fault(self, op: Op, fault: Fault) -> None:
        self._faults.append((op, fault))

    def clear_faults(self) -> None:
        self._faults.clear()

    def account(self, client_id: str, permissions: Permissions | None = None) -> FakeAccount:
        if client_id not in self._accounts:
            perms = permissions or Permissions(read=True, trade=True, margin=True, borrow=True, withdraw=False)
            self._accounts[client_id] = FakeAccount(self, client_id, perms)
        return self._accounts[client_id]

    # ---- streams ----------------------------------------
    def drop_streams(self) -> None:
        """Simulate a websocket drop: every subscriber's stream raises StreamDropped."""
        for queues in self._subs.values():
            for q in queues:
                q.put_nowait(_DROP)

    def _publish(self, symbol: str) -> None:
        for q in self._subs.get(symbol, []):
            q.put_nowait(self._snapshot(symbol, 1_000_000))

    # ---- internals shared with accounts ----------------------------------------
    def _snapshot(self, symbol: str, depth: int, age_s: float = 0.0) -> OrderBook:
        book = self._books.get(symbol)
        if book is None:
            raise Rejected(f"unknown symbol {symbol}", exchange=self.name, code="unknown_symbol")
        return OrderBook(
            symbol=symbol,
            bids=tuple(Level(p, q) for p, q in book.bids[:depth]),
            asks=tuple(Level(p, q) for p, q in book.asks[:depth]),
            ts=self.clock.now - age_s,
        )

    def _rules_for(self, symbol: str) -> SymbolRules:
        try:
            return self._rules[symbol]
        except KeyError:
            raise Rejected(f"unknown symbol {symbol}", exchange=self.name, code="unknown_symbol") from None

    def mid_price(self, asset: str) -> Decimal:
        book = self._books.get(asset + USDT)
        if book is None or not book.bids or not book.asks:
            raise Rejected(f"no price for {asset}", exchange=self.name, code="no_price")
        return (book.bids[0][0] + book.asks[0][0]) / 2

    def _take_fault(self, op: Op, client_id: str) -> Fault | None:
        for i, (fop, fault) in enumerate(self._faults):
            if fop is op and fault.times > 0 and fault.client_id in (None, client_id):
                fault.times -= 1
                if fault.times == 0:
                    del self._faults[i]
                return fault
        return None

    def _charge(self, client_id: str, op: Op) -> None:
        cfg = self.rate_limit
        if cfg is None:
            return
        now = self.clock.now
        if now < self._banned_until:
            raise RateLimited(
                "banned", retry_after_s=Decimal(str(self._banned_until - now)), banned=True, exchange=self.name
            )
        window = int(now // cfg.window_s)
        if window != self._window:
            self._window, self._ip_used, self._client_used = window, 0, {}
        weight = cfg.weights.get(op, 1)
        used = self._client_used.get(client_id, 0)
        if self._ip_used + weight > cfg.ip_budget or used + weight > cfg.client_budget:
            self._violations += 1
            banned = self._violations >= cfg.ban_after_violations
            if banned:
                self._banned_until = now + cfg.ban_s
            retry = Decimal(str((window + 1) * cfg.window_s - now))
            raise RateLimited("rate limited", retry_after_s=retry, banned=banned, exchange=self.name)
        self._ip_used += weight
        self._client_used[client_id] = used + weight

    async def _enter(self, op: Op, client_id: str, key: str | None = None) -> Fault | None:
        """Latency, rate limit, then fault handling. Returns a fault the operation must apply itself."""
        if self.latency_s:
            await self.clock.sleep(self.latency_s)
        self._charge(client_id, op)
        fault = self._take_fault(op, client_id)
        if fault is None:
            return None
        k = fault.kind
        if k is FaultKind.REJECT:
            raise Rejected("injected reject", exchange=self.name, code=fault.code)
        if k is FaultKind.RATE_LIMIT:
            raise RateLimited("injected", retry_after_s=Decimal(1), exchange=self.name)
        if k is FaultKind.EXCHANGE_DOWN:
            raise ExchangeDown("injected", exchange=self.name)
        if k is FaultKind.AUTH_ERROR:
            raise AuthError("injected", exchange=self.name)
        if k is FaultKind.TIMEOUT_NOT_APPLIED:
            raise UnknownOutcome(operation=op.value, client_id=key, exchange=self.name)
        if k is FaultKind.HANG_NOT_APPLIED:
            await asyncio.Event().wait()
        return fault  # TIMEOUT_APPLIED, HANG_APPLIED, PARTIAL_FILL, STALE_BOOK are handled by the caller

    async def _post(self, fault: Fault | None, op: Op, key: str | None) -> None:
        if fault is None:
            return
        if fault.kind is FaultKind.TIMEOUT_APPLIED:
            raise UnknownOutcome(operation=op.value, client_id=key, exchange=self.name)
        if fault.kind is FaultKind.HANG_APPLIED:
            await asyncio.Event().wait()


class FakeAccount(ExchangeAdapter):
    def __init__(self, venue: FakeVenue, client_id: str, permissions: Permissions) -> None:
        self._v = venue
        self.client_id = client_id
        self.permissions = permissions
        self._bal: dict[AccountType, dict[str, Decimal]] = {AccountType.SPOT: {}, AccountType.MARGIN: {}}
        self._loans: dict[str, _LoanState] = {}
        self._orders: dict[str, OrderResult] = {}
        self._borrow_ids: dict[str, BorrowResult] = {}
        self._repay_ids: dict[str, RepayResult] = {}
        self._transfers: dict[str, TransferStatus] = {}
        self.duplicate_rejections = 0  # a correct engine never re-sends a persisted id: tests assert this stays 0

    @property
    def name(self) -> str:
        return self._v.name

    # ---- test helpers (not part of the adapter interface) ----------------------------------------
    def fund(self, account: AccountType, asset: str, qty: Decimal) -> None:
        self._bal[account][asset] = self._bal[account].get(asset, ZERO) + qty

    def balance(self, account: AccountType, asset: str) -> Decimal:
        return self._bal[account].get(asset, ZERO)

    drift = fund  # balance drift = an unexplained change in a balance; negative qty removes funds

    # ---- guards ----------------------------------------
    def _need(self, flag: bool, what: str) -> None:
        if not flag:
            raise AuthError(f"missing permission: {what}", exchange=self.name, code="permission")

    def _accrue(self, asset: str) -> _LoanState:
        """Interest is charged per started hour (the first hour the moment the loan opens), never continuously.

        This is an assumption about the exchanges (to be verified in Phase 5): it matches hourly billing and keeps
        "loan == 0" stable between the final repay and its verification.
        """
        st = self._loans.setdefault(asset, _LoanState())
        if st.principal > 0 and st.opened_ts is not None:
            elapsed_h = (self._v.clock.now - st.opened_ts) / _HOUR
            due = max(1, math.ceil(elapsed_h))
            rate = self._v._borrow_rate.get(asset, ZERO)
            for _ in range(due - st.hours_charged):
                st.interest += _q8(st.principal * rate)
            st.hours_charged = max(st.hours_charged, due)
        return st

    # ---- market data ----------------------------------------
    async def get_orderbook(self, symbol: str, depth: int) -> OrderBook:
        fault = await self._v._enter(Op.GET_ORDERBOOK, self.client_id)
        age = fault.age_s if fault is not None and fault.kind is FaultKind.STALE_BOOK else 0.0
        return self._v._snapshot(symbol, depth, age)

    async def stream_orderbook(self, symbol: str, depth: int) -> AsyncIterator[OrderBook]:
        await self._v._enter(Op.STREAM_ORDERBOOK, self.client_id)
        queue: asyncio.Queue[object] = asyncio.Queue()
        self._v._subs.setdefault(symbol, []).append(queue)
        try:
            yield self._v._snapshot(symbol, depth)
            while True:
                item = await queue.get()
                if item is _DROP:
                    raise StreamDropped("websocket dropped", exchange=self.name)
                if not isinstance(item, OrderBook):  # pragma: no cover - queue only carries books or _DROP
                    raise TypeError("unexpected stream item")
                yield OrderBook(item.symbol, item.bids[:depth], item.asks[:depth], item.ts)
        finally:
            self._v._subs[symbol].remove(queue)

    async def get_fees(self, symbol: str) -> Fees:
        await self._v._enter(Op.GET_FEES, self.client_id)
        return self._v.fees

    async def get_symbol_rules(self, symbol: str) -> SymbolRules:
        await self._v._enter(Op.GET_SYMBOL_RULES, self.client_id)
        return self._v._rules_for(symbol)

    # ---- account ----------------------------------------
    async def get_balances(self, account: AccountType) -> list[Balance]:
        await self._v._enter(Op.GET_BALANCES, self.client_id)
        return [Balance(a, q) for a, q in sorted(self._bal[account].items())]

    def _margin_totals(self) -> tuple[Decimal, Decimal]:
        assets = ZERO
        for asset, qty in self._bal[AccountType.MARGIN].items():
            assets += qty if asset == USDT else qty * self._v.mid_price(asset)
        liabilities = ZERO
        for asset in list(self._loans):
            st = self._accrue(asset)
            liabilities += (st.principal + st.interest) * self._v.mid_price(asset)
        return assets, liabilities

    async def get_margin_account(self) -> MarginAccount:
        await self._v._enter(Op.GET_MARGIN_ACCOUNT, self.client_id)
        assets, liabilities = self._margin_totals()
        level = assets / liabilities if liabilities > 0 else None
        return MarginAccount(equity_usdt=assets - liabilities, liabilities_usdt=liabilities, margin_level=level)

    async def get_borrowable(self, asset: str) -> Borrowable:
        await self._v._enter(Op.GET_BORROWABLE, self.client_id)
        return Borrowable(asset, self._borrowable(asset))

    def _borrowable(self, asset: str) -> Decimal:
        assets, liabilities = self._margin_totals()
        headroom = (assets - liabilities) * (self._v.max_leverage - 1) - liabilities
        by_collateral = max(headroom, ZERO) / self._v.mid_price(asset)
        return min(self._v._borrow_pool.get(asset, ZERO), by_collateral)

    async def get_liabilities(self) -> list[Loan]:
        await self._v._enter(Op.GET_LIABILITIES, self.client_id)
        out = []
        for asset in sorted(self._loans):
            st = self._accrue(asset)
            if st.principal + st.interest > 0:
                out.append(Loan(asset, st.principal, st.interest))
        return out

    async def get_borrow_rate(self, asset: str) -> BorrowRate:
        await self._v._enter(Op.GET_BORROW_RATE, self.client_id)
        return BorrowRate(asset, self._v._borrow_rate.get(asset, ZERO))

    # ---- orders ----------------------------------------
    async def place_order(self, req: OrderRequest) -> OrderResult:
        fault = await self._v._enter(Op.PLACE_ORDER, self.client_id, req.client_order_id)
        if req.client_order_id in self._orders:
            self.duplicate_rejections += 1
            raise Rejected("duplicate client order id", exchange=self.name, code=DUPLICATE_CLIENT_ID)
        self._need(self.permissions.trade, "trade")
        if req.account is AccountType.MARGIN:
            self._need(self.permissions.margin, "margin")
        rules = self._v._rules_for(req.symbol)
        self._validate_order(req, rules)
        coin = base_asset(req.symbol)
        wallet = self._bal[req.account]

        if req.side is Side.BUY and wallet.get(USDT, ZERO) < req.qty * req.price:
            raise InsufficientFunds("not enough USDT", exchange=self.name)
        if req.side is Side.SELL and wallet.get(coin, ZERO) < req.qty:
            raise InsufficientFunds(f"not enough {coin}", exchange=self.name)

        target = req.qty
        if fault is not None and fault.kind is FaultKind.PARTIAL_FILL and req.tif.value == "ioc":
            target = floor_to_step(req.qty * fault.fraction, rules.step)
        plan, filled, notional = self._plan(req.symbol, req.side, target, req.price)
        if req.tif.value == "fok" and filled < req.qty:
            plan, filled, notional = [], ZERO, ZERO
        for level, take in plan:
            level[1] -= take
        self._compact(req.symbol)

        taker = self._v.fees.taker
        if req.side is Side.BUY:
            fee, fee_asset = _q8(filled * taker), coin  # fee comes out of the coin received
            wallet[USDT] = wallet.get(USDT, ZERO) - notional
            wallet[coin] = wallet.get(coin, ZERO) + filled - fee
        else:
            fee, fee_asset = _q8(notional * taker), USDT  # fee comes out of the USDT received
            wallet[coin] = wallet.get(coin, ZERO) - filled
            wallet[USDT] = wallet.get(USDT, ZERO) + notional - fee

        status = (
            OrderStatus.EXPIRED
            if filled == 0
            else OrderStatus.FILLED
            if filled == req.qty
            else OrderStatus.PARTIALLY_FILLED
        )
        result = OrderResult(
            client_order_id=req.client_order_id,
            exchange_order_id=f"{self.name}-{self.client_id}-{len(self._orders) + 1}",
            symbol=req.symbol,
            side=req.side,
            account=req.account,
            status=status,
            requested_qty=req.qty,
            filled_qty=filled,
            avg_price=notional / filled if filled else ZERO,
            fee=fee,
            fee_asset=fee_asset,
        )
        self._orders[req.client_order_id] = result
        await self._v._post(fault, Op.PLACE_ORDER, req.client_order_id)
        return result

    def _validate_order(self, req: OrderRequest, rules: SymbolRules) -> None:
        if req.qty <= 0 or req.price <= 0:
            raise InvalidQuantity("qty and price must be positive", exchange=self.name)
        if req.qty < rules.min_qty or req.qty > rules.max_qty:
            raise InvalidQuantity("qty outside min/max", exchange=self.name)
        if not is_multiple_of(req.qty, rules.step):
            raise InvalidQuantity("qty not on step", exchange=self.name)
        if not is_multiple_of(req.price, rules.tick):
            raise InvalidQuantity("price not on tick", exchange=self.name)
        if req.qty * req.price < rules.min_notional:
            raise InvalidQuantity("below min notional", exchange=self.name)

    def _plan(
        self, symbol: str, side: Side, qty: Decimal, limit: Decimal
    ) -> tuple[list[tuple[list[Decimal], Decimal]], Decimal, Decimal]:
        book = self._v._books[symbol]
        levels = book.asks if side is Side.BUY else book.bids
        plan: list[tuple[list[Decimal], Decimal]] = []
        remaining, notional = qty, ZERO
        for level in levels:
            price, avail = level
            if remaining == 0 or (side is Side.BUY and price > limit) or (side is Side.SELL and price < limit):
                break
            take = min(avail, remaining)
            plan.append((level, take))
            notional += price * take
            remaining -= take
        return plan, qty - remaining, notional

    def _compact(self, symbol: str) -> None:
        book = self._v._books[symbol]
        book.bids[:] = [lv for lv in book.bids if lv[1] > 0]
        book.asks[:] = [lv for lv in book.asks if lv[1] > 0]

    async def get_order(self, client_order_id: str, symbol: str, account: AccountType) -> OrderResult:
        fault = await self._v._enter(Op.GET_ORDER, self.client_id, client_order_id)
        order = self._orders.get(client_order_id)
        if order is None or order.symbol != symbol or order.account is not account:
            raise Rejected("order not found", exchange=self.name, code=ORDER_NOT_FOUND)
        await self._v._post(fault, Op.GET_ORDER, client_order_id)
        return order

    async def cancel_order(self, client_order_id: str, symbol: str, account: AccountType) -> OrderResult:
        await self._v._enter(Op.CANCEL_ORDER, self.client_id, client_order_id)
        order = self._orders.get(client_order_id)
        if order is None or order.symbol != symbol or order.account is not account:
            raise Rejected("order not found", exchange=self.name, code=ORDER_NOT_FOUND)
        return order  # IOC/FOK orders are already terminal

    # ---- margin ----------------------------------------
    async def borrow(self, asset: str, qty: Decimal, client_id: str) -> BorrowResult:
        fault = await self._v._enter(Op.BORROW, self.client_id, client_id)
        if client_id in self._borrow_ids:
            self.duplicate_rejections += 1
            raise Rejected("duplicate client id", exchange=self.name, code=DUPLICATE_CLIENT_ID)
        self._need(self.permissions.margin and self.permissions.borrow, "margin borrow")
        if qty <= 0:
            raise InvalidQuantity("borrow qty must be positive", exchange=self.name)
        if qty > self._borrowable(asset):
            raise Rejected("exceeds borrowable", exchange=self.name, code="borrow_limit")
        st = self._accrue(asset)
        if st.principal == ZERO:
            st.opened_ts, st.hours_charged = self._v.clock.now, 0
            st.principal = qty
            self._accrue(asset)  # the first hour is charged at once
        else:
            st.principal += qty
            st.interest += _q8(qty * self._v._borrow_rate.get(asset, ZERO))  # top-up pays the current hour
        self._v._borrow_pool[asset] = self._v._borrow_pool.get(asset, ZERO) - qty
        margin = self._bal[AccountType.MARGIN]
        margin[asset] = margin.get(asset, ZERO) + qty
        result = BorrowResult(client_id, asset, qty)
        self._borrow_ids[client_id] = result
        await self._v._post(fault, Op.BORROW, client_id)
        return result

    async def repay(self, asset: str, qty: Decimal, client_id: str) -> RepayResult:
        fault = await self._v._enter(Op.REPAY, self.client_id, client_id)
        if client_id in self._repay_ids:
            self.duplicate_rejections += 1
            raise Rejected("duplicate client id", exchange=self.name, code=DUPLICATE_CLIENT_ID)
        self._need(self.permissions.margin, "margin")
        st = self._accrue(asset)
        if qty <= 0 or qty > st.principal + st.interest:
            raise InvalidQuantity("repay qty must be positive and within the outstanding loan", exchange=self.name)
        margin = self._bal[AccountType.MARGIN]
        if margin.get(asset, ZERO) < qty:
            raise InsufficientFunds(f"not enough {asset} to repay", exchange=self.name)
        margin[asset] -= qty
        to_interest = min(qty, st.interest)
        st.interest -= to_interest
        to_principal = qty - to_interest
        st.principal -= to_principal
        if st.principal == ZERO:
            st.opened_ts, st.hours_charged = None, 0
        self._v._borrow_pool[asset] = self._v._borrow_pool.get(asset, ZERO) + to_principal
        result = RepayResult(client_id, asset, qty, Loan(asset, st.principal, st.interest))
        self._repay_ids[client_id] = result
        await self._v._post(fault, Op.REPAY, client_id)
        return result

    async def get_loan(self, asset: str) -> Loan:
        await self._v._enter(Op.GET_LOAN, self.client_id)
        st = self._accrue(asset)
        return Loan(asset, st.principal, st.interest)

    # ---- transfers ----------------------------------------
    async def get_deposit_address(self, asset: str, network: str) -> DepositAddress:
        await self._v._enter(Op.GET_DEPOSIT_ADDRESS, self.client_id)
        return DepositAddress(asset, network, f"fake-{self.name}-{self.client_id}-{asset}-{network}")

    async def withdraw(self, asset: str, qty: Decimal, network: str, address: str, client_id: str) -> TransferStatus:
        fault = await self._v._enter(Op.WITHDRAW, self.client_id, client_id)
        self._need(self.permissions.withdraw, "withdraw")
        if client_id in self._transfers:
            raise Rejected("duplicate client id", exchange=self.name, code=DUPLICATE_CLIENT_ID)
        spot = self._bal[AccountType.SPOT]
        if spot.get(asset, ZERO) < qty:
            raise InsufficientFunds(f"not enough {asset}", exchange=self.name)
        spot[asset] -= qty
        status = TransferStatus(client_id, asset, qty, TransferState.COMPLETED)
        self._transfers[client_id] = status
        await self._v._post(fault, Op.WITHDRAW, client_id)
        return status

    async def get_transfer_status(self, client_id: str) -> TransferStatus:
        await self._v._enter(Op.GET_TRANSFER_STATUS, self.client_id, client_id)
        try:
            return self._transfers[client_id]
        except KeyError:
            raise Rejected("transfer not found", exchange=self.name, code=ORDER_NOT_FOUND) from None

    async def get_withdraw_fee_and_min(self, asset: str, network: str) -> WithdrawLimits:
        await self._v._enter(Op.GET_WITHDRAW_FEE, self.client_id)
        wl = self._v.withdraw_limits
        return WithdrawLimits(asset, network, wl.fee, wl.min_amount)

    async def check_permissions(self) -> Permissions:
        await self._v._enter(Op.CHECK_PERMISSIONS, self.client_id)
        return self.permissions
