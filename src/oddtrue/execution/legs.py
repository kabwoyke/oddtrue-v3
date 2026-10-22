"""Leg runners: persist the idempotency key, send once, resolve ambiguity by querying, book from exchange figures.

Contract shared by every runner:
* The leg row (with its client order id) is written BEFORE the exchange call.
* A leg that already has a row is never sent again: it is resolved by querying the exchange (crash-and-resume).
* Nothing here retries a borrow, order or repay blindly.
"""

from __future__ import annotations

from decimal import Decimal

from oddtrue.exchanges.base import DUPLICATE_CLIENT_ID
from oddtrue.exchanges.errors import (
    AuthError,
    ExchangeDown,
    InsufficientFunds,
    InsufficientLiquidity,
    InvalidQuantity,
    RateLimited,
    Rejected,
    UnknownOutcome,
)
from oddtrue.exchanges.models import AccountType, OrderRequest, OrderResult, OrderStatus, TimeInForce
from oddtrue.execution.context import TradeCtx
from oddtrue.execution.deps import AlertKind
from oddtrue.execution.gateway import Unresolved
from oddtrue.ledger.models import EntryKind, LegStatus
from oddtrue.ledger.repository import Leg
from oddtrue.pricing.types import ZERO, Side

TERMINAL_LEG = frozenset({LegStatus.FILLED, LegStatus.PARTIALLY_FILLED, LegStatus.EXPIRED, LegStatus.REJECTED})

A_BUY = "A_BUY"
B_BORROW = "B_BORROW"
B_SELL = "B_SELL"
A_UNWIND = "A_UNWIND"
B_COVER = "B_COVER"
B_REPAY_BUY = "B_REPAY_BUY"
B_REPAY = "B_REPAY"

_STATUS = {
    OrderStatus.FILLED: LegStatus.FILLED,
    OrderStatus.PARTIALLY_FILLED: LegStatus.PARTIALLY_FILLED,
    OrderStatus.EXPIRED: LegStatus.EXPIRED,
    OrderStatus.REJECTED: LegStatus.REJECTED,
}


async def _find(ctx: TradeCtx, leg_name: str, attempt: int) -> Leg | None:
    for leg in await ctx.deps.ledger.legs(ctx.plan.client_id, ctx.plan.trade_id):
        if leg.leg == leg_name and leg.attempt == attempt:
            return leg
    return None


async def _save_order(ctx: TradeCtx, leg: Leg, res: OrderResult) -> Leg:
    return await ctx.deps.ledger.update_leg(
        ctx.plan.client_id,
        leg.client_order_id,
        _STATUS[res.status],
        exchange_order_id=res.exchange_order_id,
        filled_qty=res.filled_qty,
        avg_price=res.avg_price,
        fee=res.fee,
        fee_asset=res.fee_asset,
    )


async def _mark(ctx: TradeCtx, leg: Leg, status: LegStatus, reason: str) -> Leg:
    return await ctx.deps.ledger.update_leg(
        ctx.plan.client_id, leg.client_order_id, status, filled_qty=ZERO if status is LegStatus.REJECTED else None,
        meta={"reason": reason},
    )  # fmt: skip


async def resolve_open_order_leg(ctx: TradeCtx, leg: Leg, adapter_is_a: bool, account: AccountType) -> Leg:
    """Establish the truth about a leg whose outcome we do not know. Never re-sends. Raises ``Unresolved``."""
    adapter = ctx.a if adapter_is_a else ctx.b
    try:
        res = await ctx.gw.resolve_order(adapter, leg.client_order_id, ctx.plan.symbol, account)
    except Unresolved:
        await ctx.deps.ledger.update_leg(ctx.plan.client_id, leg.client_order_id, LegStatus.UNKNOWN)
        ctx.alert(AlertKind.UNKNOWN_OUTCOME_UNRESOLVED, f"{leg.leg} {leg.client_order_id}")
        raise
    if res is None:
        return await _mark(ctx, leg, LegStatus.REJECTED, "never_placed")
    return await _save_order(ctx, leg, res)


async def run_order_leg(
    ctx: TradeCtx,
    *,
    leg_name: str,
    attempt: int,
    on_a: bool,
    side: Side,
    qty: Decimal,
    price: Decimal,
    account: AccountType,
    tif: TimeInForce = TimeInForce.IOC,
) -> Leg:
    """Place one protected limit order (never a market order) and return the leg in a settled state."""
    existing = await _find(ctx, leg_name, attempt)
    if existing is not None and existing.status in TERMINAL_LEG:
        return existing
    if existing is not None:  # a previous run got at least as far as writing the intent: query, do not re-send
        return await resolve_open_order_leg(ctx, existing, on_a, account)

    led, cid = ctx.deps.ledger, ctx.plan.client_id
    leg = await led.record_leg_intent(
        cid, ctx.plan.trade_id, leg_name, attempt, qty, meta={"limit_price": str(price), "side": side.value}
    )
    ctx.cp(f"intent:{leg_name}:{attempt}")
    adapter = ctx.a if on_a else ctx.b
    req = OrderRequest(leg.client_order_id, ctx.plan.symbol, side, qty, price, tif, account)
    leg = await led.update_leg(cid, leg.client_order_id, LegStatus.SENT)
    try:
        res = await ctx.gw.guard("place_order", lambda: adapter.place_order(req), leg.client_order_id)
    except (UnknownOutcome, RateLimited, ExchangeDown):
        ctx.cp(f"sent:{leg_name}:{attempt}")
        return await resolve_open_order_leg(ctx, leg, on_a, account)
    except Rejected as exc:
        if exc.code == DUPLICATE_CLIENT_ID:
            return await resolve_open_order_leg(ctx, leg, on_a, account)
        return await _mark(ctx, leg, LegStatus.REJECTED, f"rejected:{exc.code or exc}")
    except (InsufficientFunds, InsufficientLiquidity, InvalidQuantity) as exc:
        return await _mark(ctx, leg, LegStatus.REJECTED, f"{type(exc).__name__}:{exc}")
    except AuthError as exc:
        ctx.alert(AlertKind.AUTH_INCIDENT, f"{leg_name}: {exc}")
        return await _mark(ctx, leg, LegStatus.REJECTED, "auth_error")
    ctx.cp(f"sent:{leg_name}:{attempt}")
    leg = await _save_order(ctx, leg, res)
    ctx.cp(f"saved:{leg_name}:{attempt}")
    return leg


# ---- borrow ----------------------------------------


async def run_borrow(ctx: TradeCtx, qty: Decimal) -> Leg:
    """Borrow ``qty`` on B once. The loan row (principal before) must already exist (written before sending)."""
    led, cid = ctx.deps.ledger, ctx.plan.client_id
    existing = await _find(ctx, B_BORROW, 1)
    if existing is not None and existing.status in TERMINAL_LEG:
        return existing
    loan_row = next(iter(await led.loans(cid, ctx.plan.trade_id)))
    if existing is not None:
        return await _resolve_borrow(ctx, existing, qty, loan_row.principal_before, loan_row.id)

    leg = await led.record_leg_intent(cid, ctx.plan.trade_id, B_BORROW, 1, qty)
    ctx.cp("intent:B_BORROW:1")
    leg = await led.update_leg(cid, leg.client_order_id, LegStatus.SENT)
    try:
        await ctx.gw.guard("borrow", lambda: ctx.b.borrow(ctx.coin, qty, leg.client_order_id), leg.client_order_id)
    except (UnknownOutcome, RateLimited, ExchangeDown):
        ctx.cp("sent:B_BORROW:1")
        return await _resolve_borrow(ctx, leg, qty, loan_row.principal_before, loan_row.id)
    except Rejected as exc:
        if exc.code == DUPLICATE_CLIENT_ID:
            return await _resolve_borrow(ctx, leg, qty, loan_row.principal_before, loan_row.id)
        return await _mark(ctx, leg, LegStatus.REJECTED, f"rejected:{exc.code or exc}")
    except (InsufficientFunds, InsufficientLiquidity, InvalidQuantity) as exc:
        return await _mark(ctx, leg, LegStatus.REJECTED, f"{type(exc).__name__}:{exc}")
    except AuthError as exc:
        ctx.alert(AlertKind.AUTH_INCIDENT, f"borrow: {exc}")
        return await _mark(ctx, leg, LegStatus.REJECTED, "auth_error")
    ctx.cp("sent:B_BORROW:1")
    await led.set_loan_borrowed(cid, loan_row.id, qty)
    leg = await led.update_leg(cid, leg.client_order_id, LegStatus.FILLED, filled_qty=qty)
    ctx.cp("saved:B_BORROW:1")
    return leg


async def _resolve_borrow(ctx: TradeCtx, leg: Leg, qty: Decimal, principal_before: Decimal, loan_id: int) -> Leg:
    """There is no query-by-id for a borrow, so compare the exchange's loan principal with what it was before."""
    led, cid = ctx.deps.ledger, ctx.plan.client_id
    attempts = ctx.deps.risk.unknown_resolve_attempts
    for i in range(attempts):
        try:
            loan = await ctx.gw.guard("get_loan", lambda: ctx.b.get_loan(ctx.coin))
        except (UnknownOutcome, RateLimited, ExchangeDown):
            loan = None
        if loan is not None:
            borrowed = loan.principal - principal_before
            if borrowed > ZERO:
                await led.set_loan_borrowed(cid, loan_id, borrowed)
                return await led.update_leg(cid, leg.client_order_id, LegStatus.FILLED, filled_qty=borrowed)
        if i < attempts - 1:
            await ctx.deps.clock.sleep(float(ctx.deps.risk.unknown_resolve_backoff_s))
    if loan is None:
        await led.update_leg(cid, leg.client_order_id, LegStatus.UNKNOWN)
        ctx.alert(AlertKind.UNKNOWN_OUTCOME_UNRESOLVED, f"B_BORROW {leg.client_order_id}")
        raise Unresolved("borrow outcome unknown")
    # The loan has not appeared. Treat as not borrowed; the trade cannot finish until the exchange confirms
    # a zero loan, so a late-appearing borrow is still caught (and repaid) before DONE.
    return await _mark(ctx, leg, LegStatus.REJECTED, "no_loan_found")


# ---- repay ----------------------------------------


def _outstanding_before(leg: Leg) -> Decimal:
    """The loan outstanding when this repay was prepared, persisted with the leg (never re-derived afterwards)."""
    return Decimal(leg.meta["outstanding_before"])


async def run_repay(
    ctx: TradeCtx, attempt: int, qty: Decimal, outstanding_before: Decimal, interest_before: Decimal
) -> Leg:
    """Repay ``qty`` once (id derived from trade + leg + attempt). Ambiguity is resolved by re-reading the loan."""
    led, cid = ctx.deps.ledger, ctx.plan.client_id
    existing = await _find(ctx, B_REPAY, attempt)
    if existing is not None and existing.status in TERMINAL_LEG:
        return existing
    if existing is not None:
        return await _resolve_repay(ctx, existing, existing.requested_qty, _outstanding_before(existing))
    leg = await led.record_leg_intent(
        cid,
        ctx.plan.trade_id,
        B_REPAY,
        attempt,
        qty,
        meta={"interest_before": str(interest_before), "outstanding_before": str(outstanding_before)},
    )
    ctx.cp(f"intent:B_REPAY:{attempt}")
    leg = await led.update_leg(cid, leg.client_order_id, LegStatus.SENT)
    try:
        await ctx.gw.guard("repay", lambda: ctx.b.repay(ctx.coin, qty, leg.client_order_id), leg.client_order_id)
    except (UnknownOutcome, RateLimited, ExchangeDown):
        ctx.cp(f"sent:B_REPAY:{attempt}")
        return await _resolve_repay(ctx, leg, qty, outstanding_before)
    except Rejected as exc:
        if exc.code == DUPLICATE_CLIENT_ID:
            return await _resolve_repay(ctx, leg, qty, outstanding_before)
        return await _mark(ctx, leg, LegStatus.REJECTED, f"rejected:{exc.code or exc}")
    except (InsufficientFunds, InvalidQuantity) as exc:
        return await _mark(ctx, leg, LegStatus.REJECTED, f"{type(exc).__name__}:{exc}")
    except AuthError as exc:
        ctx.alert(AlertKind.AUTH_INCIDENT, f"repay: {exc}")
        return await _mark(ctx, leg, LegStatus.REJECTED, "auth_error")
    ctx.cp(f"sent:B_REPAY:{attempt}")
    leg = await led.update_leg(cid, leg.client_order_id, LegStatus.FILLED, filled_qty=qty)
    ctx.cp(f"saved:B_REPAY:{attempt}")
    return leg


async def _resolve_repay(ctx: TradeCtx, leg: Leg, qty: Decimal, outstanding_before: Decimal) -> Leg:
    led, cid = ctx.deps.ledger, ctx.plan.client_id
    attempts = ctx.deps.risk.unknown_resolve_attempts
    loan = None
    for i in range(attempts):
        try:
            loan = await ctx.gw.guard("get_loan", lambda: ctx.b.get_loan(ctx.coin))
        except (UnknownOutcome, RateLimited, ExchangeDown):
            loan = None
        if loan is not None:
            # Interest keeps accruing, so "applied" means the outstanding fell by roughly the repaid amount.
            if loan.outstanding <= outstanding_before - qty / 2:
                return await led.update_leg(cid, leg.client_order_id, LegStatus.FILLED, filled_qty=qty)
        if i < attempts - 1:
            await ctx.deps.clock.sleep(float(ctx.deps.risk.unknown_resolve_backoff_s))
    if loan is None:
        await led.update_leg(cid, leg.client_order_id, LegStatus.UNKNOWN)
        ctx.alert(AlertKind.UNKNOWN_OUTCOME_UNRESOLVED, f"B_REPAY {leg.client_order_id}")
        raise Unresolved("repay outcome unknown")
    return await _mark(ctx, leg, LegStatus.REJECTED, "repay_not_applied")


# ---- resolving everything left open after a crash or timeout ----------------------------------------

_ORDER_LEGS = {A_BUY: (True, AccountType.SPOT), A_UNWIND: (True, AccountType.SPOT)}


async def resolve_all_open_legs(ctx: TradeCtx) -> None:
    """Query the exchange for every leg not yet in a terminal state. Raises ``Unresolved`` if any stays unknown."""
    led, cid = ctx.deps.ledger, ctx.plan.client_id
    for leg in await led.legs(cid, ctx.plan.trade_id):
        if leg.status in TERMINAL_LEG:
            continue
        if leg.leg == B_BORROW:
            loan_row = next(iter(await led.loans(cid, ctx.plan.trade_id)))
            await _resolve_borrow(ctx, leg, leg.requested_qty, loan_row.principal_before, loan_row.id)
        elif leg.leg == B_REPAY:
            await _resolve_repay(ctx, leg, leg.requested_qty, _outstanding_before(leg))
        else:
            on_a, account = _ORDER_LEGS.get(leg.leg, (False, AccountType.MARGIN))
            await resolve_open_order_leg(ctx, leg, on_a, account)


# ---- booking fills into the ledger (idempotent) ----------------------------------------


async def book_all_legs(ctx: TradeCtx) -> None:
    """Write ledger entries for every settled leg, once each (D-028). Safe to call any number of times."""
    led, cid, tid = ctx.deps.ledger, ctx.plan.client_id, ctx.plan.trade_id
    legs = await led.legs(cid, tid)
    purchased_net = ZERO  # coin bought only to cover a repay shortfall, so far
    for leg in legs:
        filled = leg.filled_qty or ZERO
        price = leg.avg_price or ZERO
        if leg.leg in (A_BUY, B_COVER, B_REPAY_BUY, A_UNWIND, B_SELL) and filled > ZERO:
            notional = filled * price
            if leg.leg in (A_BUY, B_COVER):
                await led.record_entry_once(cid, tid, leg, EntryKind.BUY_COST, notional, asset=ctx.coin, qty=filled)
            elif leg.leg in (A_UNWIND, B_SELL):
                await led.record_entry_once(
                    cid, tid, leg, EntryKind.SELL_PROCEEDS, notional, asset=ctx.coin, qty=filled
                )
            else:
                await led.record_entry_once(
                    cid, tid, leg, EntryKind.RECOVERY_COST, notional, asset=ctx.coin, qty=filled,
                    note="repay shortfall purchase",
                )  # fmt: skip
            fee = leg.fee or ZERO
            if fee > ZERO:
                fee_usdt = fee * price if leg.fee_asset == ctx.coin else fee
                await led.record_entry_once(cid, tid, leg, EntryKind.FEE, fee_usdt, asset=leg.fee_asset)
            if leg.leg == B_REPAY_BUY:
                purchased_net += filled - (fee if leg.fee_asset == ctx.coin else ZERO)
        elif leg.leg == B_REPAY and leg.status is LegStatus.FILLED:
            interest_before = Decimal(leg.meta.get("interest_before", "0"))
            paid_interest = min(interest_before, filled)
            from_wallet = max(paid_interest - purchased_net, ZERO)
            sell_price = next((x.avg_price for x in legs if x.leg == B_SELL and x.avg_price), None)
            value_price = sell_price or ctx.plan.sell_vwap or ZERO
            await led.record_entry_once(
                cid, tid, leg, EntryKind.INTEREST, from_wallet * value_price, asset=ctx.coin, qty=from_wallet
            )
    ctx.cp("entries_booked")
