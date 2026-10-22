"""Recovery and settlement (CLAUDE.md section 6): fix exposures, repay in full, verify loan == 0 from the exchange.

The same repay stage serves a clean SETTLING and a RECOVERY. Everything is re-derived from persisted legs and fresh
exchange reads on every pass, so a crash at any point resumes by simply running the handler again.
"""

from __future__ import annotations

from decimal import Decimal

from oddtrue.allocation import RecoveryProposal, validate_recovery
from oddtrue.execution.context import TradeCtx
from oddtrue.execution.deps import AlertKind
from oddtrue.execution.legs import (
    A_UNWIND,
    B_COVER,
    B_REPAY,
    B_REPAY_BUY,
    book_all_legs,
    resolve_all_open_legs,
    run_repay,
)
from oddtrue.execution.states import TradeState
from oddtrue.execution.steps import Hold, Move, Step
from oddtrue.ledger.models import EntryKind
from oddtrue.pricing.types import ZERO
from oddtrue.recovery.exposure import Exposure, compute_exposure
from oddtrue.recovery.ladder import BOOK_DEPTH, attempts_so_far, buy_coin_on_b, buy_qty_for, sell_coin_on_a


async def _exposure(ctx: TradeCtx) -> Exposure:
    legs = await ctx.deps.ledger.legs(ctx.plan.client_id, ctx.plan.trade_id)
    return compute_exposure(legs, ctx.coin)


async def fix_exposures(ctx: TradeCtx) -> Hold | None:
    """Close the naked short first (unbounded risk), then sell surplus coin on A. None means both are closed."""
    exp = await _exposure(ctx)
    if exp.cover_on_b > ZERO:
        if not await attempts_so_far(ctx, B_COVER):
            ctx.alert(AlertKind.NAKED_POSITION, f"short {exp.cover_on_b} {ctx.coin} on {ctx.plan.borrow_exchange}")

        async def short() -> Decimal:
            await book_all_legs(ctx)
            return (await _exposure(ctx)).cover_on_b

        if not await buy_coin_on_b(ctx, B_COVER, short):
            ctx.alert(
                AlertKind.COVER_ESCALATION_EXHAUSTED,
                f"short still open after {len(ctx.deps.risk.recovery_slippage_ladder)} rungs",
            )
            return Hold("naked_short_not_covered")
    exp = await _exposure(ctx)
    if exp.sell_on_a > ZERO:

        async def surplus() -> Decimal:
            await book_all_legs(ctx)
            return (await _exposure(ctx)).sell_on_a

        if not await sell_coin_on_a(ctx, surplus):
            ctx.alert(AlertKind.COVER_ESCALATION_EXHAUSTED, f"surplus {A_UNWIND} not sold after full ladder")
            return Hold("surplus_not_sold")
    return None


async def _reserve_left(ctx: TradeCtx) -> Decimal:
    entries = await ctx.deps.ledger.entries(ctx.plan.client_id, ctx.plan.trade_id)
    spent = sum((e.amount_usdt for e in entries if e.kind is EntryKind.RECOVERY_COST), ZERO)
    return ctx.plan.reserve_5 - spent


async def _fund_shortfall(ctx: TradeCtx) -> Hold | None:
    """Buy only the coin missing for the repayment (spec: interest only), within the 5% reserve."""
    rules = await ctx.rules(ctx.plan.borrow_exchange)
    book = await ctx.gw.read("get_orderbook", lambda: ctx.b.get_orderbook(ctx.plan.symbol, BOOK_DEPTH))
    if not book.asks:
        return Hold("no_ask_to_price_shortfall")
    best_ask = book.asks[0].price

    async def missing() -> Decimal:
        await book_all_legs(ctx)
        loan = await ctx.gw.read("get_loan", lambda: ctx.b.get_loan(ctx.coin))
        return max(loan.outstanding - await ctx.coin_free_on_b(), ZERO)

    need = await missing()
    if need <= ZERO:
        return None
    cost = buy_qty_for(need, best_ask, rules, (await ctx.fees(ctx.plan.borrow_exchange)).taker) * best_ask
    verdict = validate_recovery(RecoveryProposal(recovery_interest_usdt=cost, reserve_usdt=await _reserve_left(ctx)))
    if not verdict.passed:
        ctx.alert(AlertKind.RESERVE_INSUFFICIENT, f"shortfall {need} {ctx.coin} ~ {cost} USDT: {verdict.reason}")
        return Hold("reserve_insufficient")
    if not await buy_coin_on_b(ctx, B_REPAY_BUY, missing):
        return Hold("shortfall_not_funded")
    return None


async def repay_stage(ctx: TradeCtx) -> Hold | None:
    """Repay principal + interest in full and verify with the exchange that the loan is zero."""
    led, cid, tid = ctx.deps.ledger, ctx.plan.client_id, ctx.plan.trade_id
    rows = await led.loans(cid, tid)
    if not rows:
        return None  # no borrow was ever attempted
    for _ in range(ctx.deps.risk.repay_attempts):
        loan = await ctx.gw.read("get_loan", lambda: ctx.b.get_loan(ctx.coin))
        if loan.outstanding == ZERO:
            break
        if await ctx.coin_free_on_b() < loan.outstanding:
            hold = await _fund_shortfall(ctx)
            if hold is not None:
                return hold
            continue  # re-read: interest kept accruing while we bought
        attempt = len(await attempts_so_far(ctx, B_REPAY)) + 1
        await run_repay(ctx, attempt, loan.outstanding, loan.outstanding, loan.interest)
        await book_all_legs(ctx)
    final = await ctx.gw.read("get_loan", lambda: ctx.b.get_loan(ctx.coin))
    if final.outstanding != ZERO:
        return Hold(f"loan_not_zero:{final.outstanding}")
    for row in rows:
        await led.verify_loan_zero(cid, row.id, at=ctx.now())
    ctx.cp("loan_verified")
    return None


async def _overdue(ctx: TradeCtx) -> bool:
    entered = await ctx.deps.ledger.state_entered_at(ctx.plan.client_id, ctx.plan.trade_id, TradeState.RECOVERY.value)
    return entered is not None and (ctx.now() - entered).total_seconds() > ctx.deps.risk.recovery_timeout_s


async def recovery_handler(ctx: TradeCtx) -> Step:
    await resolve_all_open_legs(ctx)
    await book_all_legs(ctx)
    hold = await fix_exposures(ctx)
    if hold is None:
        hold = await repay_stage(ctx)
    if hold is not None:
        if await _overdue(ctx):
            ctx.alert(AlertKind.LOAN_NOT_ZERO_AFTER_RECOVERY_TIMEOUT, hold.reason)
        return hold
    await book_all_legs(ctx)
    ctx.plan = ctx.plan.with_(outcome="RECOVERED")
    await ctx.deps.ledger.set_plan(ctx.plan.client_id, ctx.plan.trade_id, ctx.plan.to_json())
    return Move(TradeState.RECONCILING, "recovered_loan_verified_zero")


async def settling_handler(ctx: TradeCtx) -> Step:
    """Both legs filled: repay from the borrow side's pre-held inventory (D-027) and verify loan == 0."""
    await resolve_all_open_legs(ctx)
    await book_all_legs(ctx)
    if not (await _exposure(ctx)).clean:
        return Move(TradeState.RECOVERY, "exposure_found_while_settling")
    hold = await repay_stage(ctx)
    if hold is not None:
        return Move(TradeState.RECOVERY, f"settlement_incomplete:{hold.reason}")
    await book_all_legs(ctx)
    ctx.plan = ctx.plan.with_(outcome="SUCCESS")
    await ctx.deps.ledger.set_plan(ctx.plan.client_id, ctx.plan.trade_id, ctx.plan.to_json())
    return Move(TradeState.RECONCILING, "settled_loan_verified_zero")


__all__ = ["fix_exposures", "recovery_handler", "repay_stage", "settling_handler"]
