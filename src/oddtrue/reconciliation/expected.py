"""What the balances SHOULD be: the last known-good snapshot plus the effect of every settled leg since (pure).

The ledger holds the expectation; the exchange holds the truth (CLAUDE.md rule 7). Only balance-changing facts are
applied. Liabilities are not rebuilt from legs: every trade ends only after the exchange reports a zero loan, so for an
idle client the expected liability is simply the baseline's.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

from oddtrue.exchanges.models import base_asset
from oddtrue.ledger.safety import LegFact
from oddtrue.pricing.types import ZERO
from oddtrue.reconciliation import snapshot as snap
from oddtrue.reconciliation.snapshot import MARGIN, SPOT, Snapshot

USDT = "USDT"

# legs that buy coin with USDT / sell coin for USDT, and where (spot on A, margin on B)
_BUYS_ON_A = {"A_BUY"}
_SELLS_ON_A = {"A_UNWIND"}
_SELLS_ON_B = {"B_SELL"}
_BUYS_ON_B = {"B_COVER", "B_REPAY_BUY"}


def _fee_effects(out: Snapshot, exchange: str, section: str, leg: LegFact) -> None:
    if leg.fee > ZERO and leg.fee_asset is not None:
        snap.add(out, exchange, section, leg.fee_asset, -leg.fee)


def apply_legs(baseline: Snapshot, legs: Sequence[LegFact]) -> Snapshot:
    out = snap.copy(baseline)
    for leg in legs:
        coin = base_asset(leg.symbol)
        notional = leg.filled_qty * leg.avg_price
        filled = leg.filled_qty
        if leg.leg in _BUYS_ON_A | _BUYS_ON_B:
            ex, sec = (leg.buy_exchange, SPOT) if leg.leg in _BUYS_ON_A else (leg.borrow_exchange, MARGIN)
            if filled > ZERO:
                snap.add(out, ex, sec, USDT, -notional)
                snap.add(out, ex, sec, coin, filled)
                _fee_effects(out, ex, sec, leg)
        elif leg.leg in _SELLS_ON_A | _SELLS_ON_B:
            ex, sec = (leg.buy_exchange, SPOT) if leg.leg in _SELLS_ON_A else (leg.borrow_exchange, MARGIN)
            if filled > ZERO:
                snap.add(out, ex, sec, coin, -filled)
                snap.add(out, ex, sec, USDT, notional)
                _fee_effects(out, ex, sec, leg)
        elif leg.leg == "B_BORROW" and leg.status == "FILLED":
            snap.add(out, leg.borrow_exchange, MARGIN, coin, filled)
        elif leg.leg == "B_REPAY" and leg.status == "FILLED":
            snap.add(out, leg.borrow_exchange, MARGIN, coin, -filled)
    return out


def total_coin(snapshot: Snapshot, coin: str) -> Decimal:
    return sum((snap.get(snapshot, ex, sec, coin) for ex in snapshot for sec in (SPOT, MARGIN)), ZERO)
