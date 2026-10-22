"""Direct cross-exchange opportunity evaluation (CLAUDE.md section 7). Pure: no I/O, no clock.

One direction at a time: BUY on ``buy_exchange`` (spot, takes the asks), BORROW and SELL on ``sell_exchange``
(margin, takes the bids). Both directions are evaluated by the caller. The cost model is the same one the engine
uses at revalidation (``execution.engine._revalidate``): VWAP on both sides, taker fees on both legs, interest on
the borrowed quantity for the loan duration billed per started hour, net ROI on the buy notional. Detection is
market-level: it sizes to the market cap (90% of executable liquidity) and knows nothing about any client's
balances or borrow limits, so a result is "worth considering", never "will trade". ``validate_trade`` stays the
single judge before any money moves.

Triangular opportunities are a separate class and are not handled here.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from oddtrue.allocation.capacity import market_cap
from oddtrue.exchanges.models import Fees, OrderBook
from oddtrue.pricing.profit import (
    interest_estimate_coin,
    interest_value_usdt,
    meets_min_roi,
    net_profit,
    net_roi_pct,
)
from oddtrue.pricing.rounding import common_step, floor_to_step
from oddtrue.pricing.types import ZERO, Side, SymbolRules
from oddtrue.pricing.vwap import executable_liquidity, walk_book

SECONDS_PER_HOUR = Decimal(3600)


class Reject(StrEnum):
    NO_LIQUIDITY = "no_liquidity"
    NOT_CROSSED = "not_crossed"  # best sell price is not above best buy price
    BELOW_MIN_QTY = "below_min_qty"
    BELOW_MIN_NOTIONAL = "below_min_notional"
    BELOW_MIN_ROI = "below_min_roi"


@dataclass(frozen=True, slots=True)
class DetectionParams:
    max_slippage: Decimal
    min_net_roi_pct: Decimal
    market_pct: Decimal
    expected_loan_seconds: int


@dataclass(frozen=True, slots=True)
class Opportunity:
    symbol: str
    buy_exchange: str
    sell_exchange: str
    qty: Decimal
    buy_vwap: Decimal
    sell_vwap: Decimal
    gross: Decimal
    fees: Decimal
    interest: Decimal
    net: Decimal
    net_roi_pct: Decimal
    capital_used: Decimal
    executable_qty: Decimal  # before the market-share cap


@dataclass(frozen=True, slots=True)
class Rejection:
    symbol: str
    buy_exchange: str
    sell_exchange: str
    reason: Reject
    net_roi_pct: Decimal | None = None  # known for BELOW_MIN_ROI, to show how close it was


Verdict = Opportunity | Rejection


def evaluate(
    symbol: str,
    buy_exchange: str,
    sell_exchange: str,
    buy_book: OrderBook,
    sell_book: OrderBook,
    buy_rules: SymbolRules,
    sell_rules: SymbolRules,
    buy_fees: Fees,
    sell_fees: Fees,
    borrow_rate_per_hour: Decimal,
    params: DetectionParams,
) -> Verdict:
    def no(reason: Reject, roi: Decimal | None = None) -> Rejection:
        return Rejection(symbol, buy_exchange, sell_exchange, reason, roi)

    if not buy_book.asks or not sell_book.bids:
        return no(Reject.NO_LIQUIDITY)
    if sell_book.bids[0].price <= buy_book.asks[0].price:
        return no(Reject.NOT_CROSSED)

    executable = min(
        executable_liquidity(buy_book.asks, Side.BUY, params.max_slippage).qty,
        executable_liquidity(sell_book.bids, Side.SELL, params.max_slippage).qty,
    )
    step = common_step(buy_rules.step, sell_rules.step)
    qty = floor_to_step(market_cap(executable, params.market_pct), step)
    qty = min(qty, floor_to_step(min(buy_rules.max_qty, sell_rules.max_qty), step))
    if qty <= ZERO:
        return no(Reject.NO_LIQUIDITY)
    if qty < max(buy_rules.min_qty, sell_rules.min_qty):
        return no(Reject.BELOW_MIN_QTY)

    buy = walk_book(buy_book.asks, qty, Side.BUY)
    sell = walk_book(sell_book.bids, qty, Side.SELL)
    if buy.notional < buy_rules.min_notional or sell.notional < sell_rules.min_notional:
        return no(Reject.BELOW_MIN_NOTIONAL)

    hours = Decimal(max(1, math.ceil(Decimal(params.expected_loan_seconds) / SECONDS_PER_HOUR)))  # billed per hour
    interest_coin = interest_estimate_coin(qty, borrow_rate_per_hour, hours)
    interest = interest_value_usdt(interest_coin, sell.vwap)
    fees = buy.notional * buy_fees.taker + sell.notional * sell_fees.taker
    gross = sell.notional - buy.notional
    net = net_profit(gross, fees, interest, ZERO, ZERO)  # slippage is inside the VWAPs, not booked twice
    roi = net_roi_pct(net, buy.notional)
    if not meets_min_roi(roi, params.min_net_roi_pct):
        return no(Reject.BELOW_MIN_ROI, roi)
    return Opportunity(
        symbol=symbol,
        buy_exchange=buy_exchange,
        sell_exchange=sell_exchange,
        qty=qty,
        buy_vwap=buy.vwap,
        sell_vwap=sell.vwap,
        gross=gross,
        fees=fees,
        interest=interest,
        net=net,
        net_roi_pct=roi,
        capital_used=buy.notional,
        executable_qty=executable,
    )
