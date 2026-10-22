"""Protected recovery orders with escalating slippage (D-026). Never a market order.

Each rung is a NEW attempt (new client order id) priced from a fresh order book. The ladder is bounded: after the
last rung the trade holds in RECOVERY, alerts, and waits for a human or a later pass.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from decimal import Decimal

from oddtrue.exchanges.models import AccountType, OrderBook
from oddtrue.execution.context import TradeCtx
from oddtrue.execution.legs import run_order_leg
from oddtrue.ledger.models import LegStatus
from oddtrue.ledger.repository import Leg
from oddtrue.pricing.protected import protected_limit_price
from oddtrue.pricing.rounding import ceil_to_step, floor_to_step
from oddtrue.pricing.types import ZERO, Side, SymbolRules

BOOK_DEPTH = 20
_PLACED_BUT_NOT_DONE = (LegStatus.EXPIRED, LegStatus.PARTIALLY_FILLED)


async def attempts_so_far(ctx: TradeCtx, leg_name: str) -> list[Leg]:
    return [x for x in await ctx.deps.ledger.legs(ctx.plan.client_id, ctx.plan.trade_id) if x.leg == leg_name]


def _rung(prior: list[Leg], ladder_len: int) -> int:
    """Escalate only after an order that actually reached the book without finishing the job."""
    return min(sum(1 for x in prior if x.status in _PLACED_BUT_NOT_DONE), ladder_len - 1)


async def _book(ctx: TradeCtx, on_a: bool) -> OrderBook:
    adapter = ctx.a if on_a else ctx.b
    return await ctx.gw.read("get_orderbook", lambda: adapter.get_orderbook(ctx.plan.symbol, BOOK_DEPTH))


async def sell_coin_on_a(ctx: TradeCtx, remaining: Callable[[], Awaitable[Decimal]]) -> bool:
    """Sell surplus coin on A. ``remaining`` re-derives the quantity left. True when nothing sellable remains."""
    return await _ladder(ctx, "A_UNWIND", True, Side.SELL, AccountType.SPOT, remaining)


async def buy_coin_on_b(ctx: TradeCtx, leg_name: str, remaining: Callable[[], Awaitable[Decimal]]) -> bool:
    """Buy coin on B (cover a short or fund a repay shortfall). True when nothing is left to buy."""
    return await _ladder(ctx, leg_name, False, Side.BUY, AccountType.MARGIN, remaining)


def buy_qty_for(need_net: Decimal, best_ask: Decimal, rules: SymbolRules, taker: Decimal) -> Decimal:
    """Smallest legal buy that nets at least ``need_net`` coin: grossed up for the coin-denominated fee, on the step,
    and not below the exchange's minimum quantity or minimum notional."""
    qty = ceil_to_step(need_net / (1 - taker), rules.step)
    return max(qty, rules.min_qty, ceil_to_step(rules.min_notional / best_ask, rules.step))


def _fits(qty: Decimal, price: Decimal, rules: SymbolRules) -> bool:
    return qty >= rules.min_qty and qty * price >= rules.min_notional


async def _ladder(
    ctx: TradeCtx,
    leg_name: str,
    on_a: bool,
    side: Side,
    account: AccountType,
    remaining: Callable[[], Awaitable[Decimal]],
) -> bool:
    ladder = ctx.deps.risk.recovery_slippage_ladder
    exchange = ctx.plan.buy_exchange if on_a else ctx.plan.borrow_exchange
    rules = await ctx.rules(exchange)
    taker = (await ctx.fees(exchange)).taker
    for _ in range(len(ladder) + 1):
        need = await remaining()
        if need <= ZERO:
            return True
        book = await _book(ctx, on_a)
        levels = book.bids if side is Side.SELL else book.asks
        if not levels:
            return False  # nothing to price against; hold rather than guess
        best = levels[0].price
        if side is Side.SELL:
            qty = floor_to_step(need, rules.step)
            if not _fits(qty, best, rules):
                ctx.alert_dust(leg_name, need)
                return True  # below the exchange minimum: cannot be sold, left as dust
        else:
            qty = buy_qty_for(need, best, rules, taker)
        prior = await attempts_so_far(ctx, leg_name)
        rung = _rung(prior, len(ladder))
        price = protected_limit_price(best, ladder[rung], rules.tick, side)
        await run_order_leg(
            ctx, leg_name=leg_name, attempt=len(prior) + 1, on_a=on_a, side=side, qty=qty, price=price, account=account
        )
    return (await remaining()) <= ZERO
