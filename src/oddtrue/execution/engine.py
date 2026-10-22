"""The trade state machine (CLAUDE.md section 6). Persisted state, resumable at every step.

``advance`` reads the trade's state from the database, runs that state's handler, and records the move. It holds no
state of its own, so after a crash any worker resumes a trade by calling ``advance`` again: each handler re-derives
what to do from the database and the exchanges and never re-sends an action that already has a persisted leg.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from oddtrue.allocation import TradeProposal, validate_trade
from oddtrue.exchanges.base import ExchangeAdapter
from oddtrue.exchanges.errors import AuthError, ExchangeDown, ExchangeError, RateLimited, UnknownOutcome
from oddtrue.exchanges.models import AccountType, OrderResult, TimeInForce
from oddtrue.execution.context import TradeCtx
from oddtrue.execution.deps import AlertKind, EngineDeps, to_datetime
from oddtrue.execution.gateway import Gateway, Unresolved
from oddtrue.execution.legs import (
    A_BUY,
    B_BORROW,
    B_SELL,
    book_all_legs,
    resolve_all_open_legs,
    run_borrow,
    run_order_leg,
)
from oddtrue.execution.plan import TradePlan
from oddtrue.execution.states import TERMINAL, TradeState, is_legal
from oddtrue.execution.steps import Hold, Move, Step
from oddtrue.ledger.models import LegStatus, Outcome
from oddtrue.ledger.repository import ConcurrencyLimit, StateConflict
from oddtrue.pricing import (
    InsufficientDepth,
    executable_liquidity,
    interest_estimate_coin,
    interest_value_usdt,
    net_profit,
    net_roi_pct,
    protected_limit_price,
    walk_book,
)
from oddtrue.pricing.types import ZERO, Side
from oddtrue.recovery.exposure import compute_exposure
from oddtrue.recovery.recovery import recovery_handler, settling_handler

BOOK_DEPTH = 50
_NO_MONEY_AT_RISK = frozenset(
    {TradeState.ALLOCATED, TradeState.PRECHECK_B_LIQUIDITY, TradeState.PRECHECK_BORROW, TradeState.REVALIDATED}
)
_SECONDS_PER_HOUR = Decimal(3600)


@dataclass(frozen=True, slots=True)
class RunResult:
    state: TradeState
    outcome: Outcome | None
    held: str | None  # why the trade is parked (None when it reached a terminal state)

    @property
    def finished(self) -> bool:
        return self.state in TERMINAL


class StartRefused(Exception):
    pass


Handler = Callable[[TradeCtx], Awaitable[Step]]


class TradeEngine:
    def __init__(self, deps: EngineDeps) -> None:
        self._d = deps
        self._handlers: dict[TradeState, Handler] = {
            TradeState.ALLOCATED: self._allocated,
            TradeState.PRECHECK_B_LIQUIDITY: self._precheck_liquidity,
            TradeState.PRECHECK_BORROW: self._precheck_borrow,
            TradeState.REVALIDATED: self._revalidate,
            TradeState.EXECUTING: self._executing,
            TradeState.SETTLING: settling_handler,
            TradeState.RECOVERY: recovery_handler,
            TradeState.RECONCILING: self._reconciling,
            TradeState.REBALANCE_EVAL: self._rebalance_eval,
        }

    # ---- public API ----------------------------------------
    async def start(self, plan: TradePlan) -> RunResult:
        """Persist the allocated trade (refusing if the client already has one running), then drive it."""
        try:
            await self._d.ledger.create_trade_limited(
                plan.new_trade_row(), plan.to_json(), self._d.max_unfinished_per_client, at=_at(self._d)
            )
        except ConcurrencyLimit as exc:
            raise StartRefused(str(exc)) from exc
        self._d.checkpoint("created")
        return await self.advance(plan.client_id, plan.trade_id)

    async def resume_all(self) -> list[RunResult]:
        """After a restart: reload every unfinished trade and continue it."""
        return [await self.advance(t.client_id, t.id) for t in await self._d.ledger.unfinished_trades()]

    async def advance(self, client_id: str, trade_id: str) -> RunResult:
        led = self._d.ledger
        plan = TradePlan.from_json(await led.get_plan(client_id, trade_id))
        ctx = TradeCtx(self._d, plan, Gateway(self._d))
        while True:
            row = await led.get_trade(client_id, trade_id)
            state = TradeState(row.state)
            if state in TERMINAL:
                outcome = row.outcome or await self._finish(ctx, state)
                return RunResult(state, outcome, None)
            try:
                step = await self._handlers[state](ctx)
            except Unresolved as exc:
                return RunResult(state, None, f"unresolved:{exc.what}")
            except AuthError as exc:
                ctx.alert(AlertKind.AUTH_INCIDENT, str(exc))
                if state not in _NO_MONEY_AT_RISK:
                    return RunResult(state, None, "auth_error")
                step = Move(TradeState.CANCELLED, "auth_error")  # nothing sent yet: stop, do not park the client
            except (ExchangeDown, RateLimited, UnknownOutcome) as exc:
                ctx.alert(AlertKind.EXCHANGE_DOWN_IN_FLIGHT, f"{state.value}: {type(exc).__name__}")
                return RunResult(state, None, f"exchange_unavailable:{type(exc).__name__}")
            except ExchangeError as exc:  # anything else the adapters normalise: park the trade, never crash it
                ctx.alert(
                    AlertKind.UNKNOWN_OUTCOME_UNRESOLVED, f"{state.value}: unexpected {type(exc).__name__}: {exc}"
                )
                return RunResult(state, None, f"exchange_error:{type(exc).__name__}")
            if isinstance(step, Hold):
                return RunResult(state, None, step.reason)
            if not is_legal(state, step.to):
                raise RuntimeError(f"illegal transition {state} -> {step.to}")
            if step.to is TradeState.CANCELLED:
                ctx.plan = ctx.plan.with_(outcome=Outcome.CANCELLED.value)
                await led.set_plan(client_id, trade_id, ctx.plan.to_json())
            try:
                await led.transition(client_id, trade_id, state.value, step.to.value, step.reason, at=ctx.now())
            except StateConflict:
                continue  # another worker moved it; reload and decide again
            ctx.cp(f"state:{step.to.value}")

    async def _finish(self, ctx: TradeCtx, state: TradeState) -> Outcome:
        plan = TradePlan.from_json(await ctx.deps.ledger.get_plan(ctx.plan.client_id, ctx.plan.trade_id))
        outcome = Outcome(plan.outcome or (Outcome.CANCELLED.value if state is TradeState.CANCELLED else "FAILED"))
        await ctx.deps.ledger.finish_trade(ctx.plan.client_id, ctx.plan.trade_id, outcome, at=ctx.now())
        ctx.cp("finished")
        return outcome

    # ---- pre-checks (no money at risk: any failure cancels) ----------------------------------------
    async def _allocated(self, ctx: TradeCtx) -> Step:
        return Move(TradeState.PRECHECK_B_LIQUIDITY, "allocated")

    def _fresh(self, ctx: TradeCtx, ts: float) -> bool:
        return Decimal(str(ctx.deps.clock.now - ts)) <= ctx.deps.risk.max_book_age_s

    async def _precheck_liquidity(self, ctx: TradeCtx) -> Step:
        """Do not borrow unless B's sell liquidity is confirmed within max slippage (CLAUDE.md rule 4)."""
        try:
            book = await ctx.gw.read("get_orderbook", lambda: ctx.b.get_orderbook(ctx.plan.symbol, BOOK_DEPTH))
        except Unresolved as exc:
            return Move(TradeState.CANCELLED, f"precheck_read_failed:{exc.what}")
        if not self._fresh(ctx, book.ts):
            return Move(TradeState.CANCELLED, "borrow_side_book_stale")
        liquidity = executable_liquidity(book.bids, Side.SELL, ctx.deps.risk.max_slippage)
        if liquidity.qty < ctx.plan.qty:
            return Move(TradeState.CANCELLED, f"insufficient_sell_liquidity:{liquidity.qty}<{ctx.plan.qty}")
        return Move(TradeState.PRECHECK_BORROW, "sell_liquidity_ok")

    async def _precheck_borrow(self, ctx: TradeCtx) -> Step:
        try:
            perms_a = await ctx.gw.read("check_permissions", ctx.a.check_permissions)
            perms_b = await ctx.gw.read("check_permissions", ctx.b.check_permissions)
            if not perms_a.trade:
                return Move(TradeState.CANCELLED, "buy_side_no_trade_permission")
            if not (perms_b.trade and perms_b.margin and perms_b.borrow):
                return Move(TradeState.CANCELLED, "borrow_side_missing_permission")
            loan = await ctx.gw.read("get_loan", lambda: ctx.b.get_loan(ctx.coin))
            if loan.outstanding > ZERO:
                ctx.alert(AlertKind.LOAN_PRESENT_BEFORE_TRADE, f"{loan.outstanding} {ctx.coin} already owed")
                return Move(TradeState.CANCELLED, "existing_loan_for_coin")
            borrowable = await ctx.gw.read("get_borrowable", lambda: ctx.b.get_borrowable(ctx.coin))
        except Unresolved as exc:
            return Move(TradeState.CANCELLED, f"precheck_read_failed:{exc.what}")
        if borrowable.qty < ctx.plan.qty:
            return Move(TradeState.CANCELLED, f"borrowable_too_low:{borrowable.qty}<{ctx.plan.qty}")
        return Move(TradeState.REVALIDATED, "borrow_ok")

    async def _revalidate(self, ctx: TradeCtx) -> Step:
        """Final check from fresh data immediately before sending anything. validate_trade() is the single judge."""
        p, risk = ctx.plan, ctx.deps.risk
        blocked = await ctx.deps.gate.blocked_reason(p.client_id)
        if blocked is not None:
            return Move(TradeState.CANCELLED, f"gate:{blocked}")
        try:
            book_a = await ctx.gw.read("get_orderbook", lambda: ctx.a.get_orderbook(p.symbol, BOOK_DEPTH))
            book_b = await ctx.gw.read("get_orderbook", lambda: ctx.b.get_orderbook(p.symbol, BOOK_DEPTH))
            rules_a, rules_b = await ctx.rules(p.buy_exchange), await ctx.rules(p.borrow_exchange)
            fees_a, fees_b = await ctx.fees(p.buy_exchange), await ctx.fees(p.borrow_exchange)
            rate = await ctx.gw.read("get_borrow_rate", lambda: ctx.b.get_borrow_rate(ctx.coin))
            borrowable = await ctx.gw.read("get_borrowable", lambda: ctx.b.get_borrowable(ctx.coin))
            spot = await ctx.gw.read("get_balances", lambda: ctx.a.get_balances(AccountType.SPOT))
            margin = await ctx.gw.read("get_margin_account", ctx.b.get_margin_account)
            repay_coin = await ctx.coin_free_on_b()
        except Unresolved as exc:
            return Move(TradeState.CANCELLED, f"revalidation_read_failed:{exc.what}")
        if not (self._fresh(ctx, book_a.ts) and self._fresh(ctx, book_b.ts)):
            return Move(TradeState.CANCELLED, "book_stale_at_revalidation")
        try:
            buy = walk_book(book_a.asks, p.qty, Side.BUY)
            sell = walk_book(book_b.bids, p.qty, Side.SELL)
        except InsufficientDepth as exc:
            return Move(TradeState.CANCELLED, f"insufficient_depth:{exc}")

        hours = Decimal(max(1, math.ceil(Decimal(risk.expected_loan_seconds) / _SECONDS_PER_HOUR)))  # billed per hour
        interest_coin = interest_estimate_coin(p.qty, rate.rate_per_hour, hours)
        fees = buy.notional * fees_a.taker + sell.notional * fees_b.taker
        net = net_profit(sell.notional - buy.notional, fees, interest_value_usdt(interest_coin, sell.vwap), ZERO, ZERO)
        roi = net_roi_pct(net, buy.notional)
        buy_limit = protected_limit_price(buy.best_price, risk.max_slippage, rules_a.tick, Side.BUY)
        sell_limit = protected_limit_price(sell.best_price, risk.max_slippage, rules_b.tick, Side.SELL)
        liq = min(
            executable_liquidity(book_a.asks, Side.BUY, risk.max_slippage).qty,
            executable_liquidity(book_b.bids, Side.SELL, risk.max_slippage).qty,
        )
        usdt_free = next((x.free for x in spot if x.asset == "USDT"), ZERO)
        verdict = validate_trade(
            TradeProposal(
                qty=p.qty,
                buy_price=buy.vwap,
                sell_price=sell.vwap,
                usable_buy=min(p.usable_buy, usdt_free),
                usable_borrow=min(p.usable_borrow, margin.equity_usdt),
                trade_pct=risk.client_trade_pct,
                executable_liquidity_qty=liq,
                market_pct=risk.market_liquidity_pct,
                borrowable_qty=borrowable.qty,
                buy_rules=rules_a,
                sell_rules=rules_b,
                expected_net_roi_pct=roi,
                min_net_roi_pct=risk.min_net_roi,
                buy_limit_price=buy_limit,
                sell_limit_price=sell_limit,
                repay_coin_needed=p.qty + interest_coin,
                repay_coin_available=repay_coin,
            )
        )
        if not verdict.passed:
            why = ",".join(f"{f.check.value}[{f.side}]" for f in verdict.failures)
            return Move(TradeState.CANCELLED, f"validate_trade_failed:{why}")
        ctx.plan = p.with_(
            buy_vwap=buy.vwap, sell_vwap=sell.vwap, buy_limit=buy_limit, sell_limit=sell_limit, expected_net_roi_pct=roi
        )
        await ctx.deps.ledger.set_plan(p.client_id, p.trade_id, ctx.plan.to_json())
        return Move(TradeState.EXECUTING, "revalidated")

    # ---- execution ----------------------------------------
    async def _executing(self, ctx: TradeCtx) -> Step:
        """A buy and B (borrow -> protected sell) run concurrently under one hard time limit."""
        p, led = ctx.plan, ctx.deps.ledger
        if p.buy_limit is None or p.sell_limit is None:
            raise RuntimeError("EXECUTING without revalidated limit prices")
        existing = {x.leg for x in await led.legs(p.client_id, p.trade_id)}
        # A trade found already part-way through EXECUTING was interrupted (crash, restart). Its revalidation may be
        # minutes old, so it NEVER sends a new entry order: it resolves what was sent and recovers the rest.
        resumed = bool(existing) or bool(await led.loans(p.client_id, p.trade_id))
        if not await led.loans(p.client_id, p.trade_id):
            before = await ctx.gw.read("get_loan", lambda: ctx.b.get_loan(ctx.coin))
            await led.open_loan(p.client_id, p.trade_id, p.borrow_exchange, ctx.coin, before.principal)
            ctx.cp("loan_opened")

        async def leg_a() -> None:
            if resumed and A_BUY not in existing:
                return
            try:
                await run_order_leg(
                    ctx, leg_name=A_BUY, attempt=1, on_a=True, side=Side.BUY, qty=p.qty,
                    price=p.buy_limit or ZERO, account=AccountType.SPOT, tif=TimeInForce.IOC,
                )  # fmt: skip
            except (Unresolved, ExchangeError):
                pass  # the leg row records what is known; resolve_all_open_legs below decides

        async def leg_b() -> None:
            if resumed and B_BORROW not in existing:
                return
            try:
                borrow = await run_borrow(ctx, p.qty)
                if resumed and B_SELL not in existing:
                    return
                if borrow.status is LegStatus.FILLED and (borrow.filled_qty or ZERO) > ZERO:
                    await run_order_leg(
                        ctx, leg_name=B_SELL, attempt=1, on_a=False, side=Side.SELL, qty=borrow.filled_qty or ZERO,
                        price=p.sell_limit or ZERO, account=AccountType.MARGIN, tif=TimeInForce.IOC,
                    )  # fmt: skip
            except (Unresolved, ExchangeError):
                pass

        try:
            async with asyncio.timeout(ctx.deps.risk.execution_timeout_s), asyncio.TaskGroup() as tg:
                tg.create_task(leg_a())
                tg.create_task(leg_b())
        except TimeoutError:
            ctx.alert(AlertKind.UNKNOWN_OUTCOME_UNRESOLVED, "execution time limit hit; reconciling legs", page=False)

        await resolve_all_open_legs(ctx)
        await book_all_legs(ctx)
        return await self._after_execution(ctx)

    async def _after_execution(self, ctx: TradeCtx) -> Step:
        p, led = ctx.plan, ctx.deps.ledger
        legs = await led.legs(p.client_id, p.trade_id)
        exp = compute_exposure(legs, ctx.coin)
        by = {x.leg: x for x in legs}
        a_full = A_BUY in by and by[A_BUY].status is LegStatus.FILLED and (by[A_BUY].filled_qty or ZERO) == p.qty
        b_full = B_SELL in by and by[B_SELL].status is LegStatus.FILLED and (by[B_SELL].filled_qty or ZERO) == p.qty
        if a_full and b_full and exp.borrowed == p.qty:
            return Move(TradeState.SETTLING, "both_legs_filled")
        if exp.qa_gross == ZERO and exp.qb_sold == ZERO and exp.borrowed == ZERO:
            loan = await ctx.gw.read("get_loan", lambda: ctx.b.get_loan(ctx.coin))
            if loan.outstanding == ZERO:  # nothing happened anywhere: record a failed trade with no cost
                for row in await led.loans(p.client_id, p.trade_id):
                    await led.verify_loan_zero(p.client_id, row.id, at=ctx.now())
                ctx.plan = p.with_(outcome=Outcome.FAILED.value)
                await led.set_plan(p.client_id, p.trade_id, ctx.plan.to_json())
                return Move(TradeState.RECONCILING, "no_fills_no_loan")
        return Move(TradeState.RECOVERY, _why_recovery(a_full, b_full, exp.qa_gross, exp.qb_sold, exp.borrowed))

    # ---- reconciliation (Phase 3: legs and loan; full balance reconciliation and freezing is Phase 4) -----
    async def _reconciling(self, ctx: TradeCtx) -> Step:
        p, led = ctx.plan, ctx.deps.ledger
        await resolve_all_open_legs(ctx)
        await book_all_legs(ctx)
        for leg in await led.legs(p.client_id, p.trade_id):
            if leg.exchange_order_id is None or leg.leg in (B_BORROW,):
                continue
            on_a = leg.leg in (A_BUY, "A_UNWIND")
            account = AccountType.SPOT if on_a else AccountType.MARGIN
            adapter = ctx.a if on_a else ctx.b
            seen = await _query_order(ctx, adapter, leg.client_order_id, account)
            if seen.filled_qty != (leg.filled_qty or ZERO):
                ctx.alert(
                    AlertKind.RECONCILIATION_MISMATCH,
                    f"{leg.leg}: ledger {leg.filled_qty} vs exchange {seen.filled_qty}",
                )
                return Hold("reconciliation_mismatch")
        rows = await led.loans(p.client_id, p.trade_id)
        if rows:
            loan = await ctx.gw.read("get_loan", lambda: ctx.b.get_loan(ctx.coin))
            if loan.outstanding != ZERO:
                return Move(TradeState.RECOVERY, f"loan_not_zero_at_reconcile:{loan.outstanding}")
            for row in rows:
                await led.verify_loan_zero(p.client_id, row.id, at=ctx.now())
        await led.trade_result(p.client_id, p.trade_id)  # derivable from entries alone
        return Move(TradeState.REBALANCE_EVAL, "reconciled")

    async def _rebalance_eval(self, ctx: TradeCtx) -> Step:
        await ctx.deps.rebalance.evaluate(ctx.plan.client_id, ctx.plan.trade_id)
        return Move(TradeState.DONE, "done")


def _why_recovery(a_full: bool, b_full: bool, qa: Decimal, qb: Decimal, borrowed: Decimal) -> str:
    return f"legs_incomplete:a_full={a_full},b_full={b_full},bought={qa},sold={qb},borrowed={borrowed}"


def _at(deps: EngineDeps) -> datetime:
    return to_datetime(deps.clock.now)


async def _query_order(ctx: TradeCtx, adapter: ExchangeAdapter, coid: str, account: AccountType) -> OrderResult:
    return await ctx.gw.read("get_order", lambda: adapter.get_order(coid, ctx.plan.symbol, account))
