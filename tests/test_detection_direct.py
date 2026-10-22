"""Phase 5: pure direct-opportunity evaluation, against hand-computed figures."""

from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from oddtrue.detection.direct import DetectionParams, Opportunity, Reject, Rejection, Verdict, evaluate
from oddtrue.exchanges.models import Fees, OrderBook
from oddtrue.pricing.types import Level, SymbolRules

D = Decimal
PARAMS = DetectionParams(
    max_slippage=D("0.002"), min_net_roi_pct=D("0.30"), market_pct=D("0.90"), expected_loan_seconds=60
)
RULES = SymbolRules(min_qty=D("0.001"), max_qty=D(1000), step=D("0.001"), tick=D("0.01"), min_notional=D(5))
FEES = Fees(maker=D("0.001"), taker=D("0.001"))
RATE = D("0.00001")  # per hour


def book(bids: list[tuple[str, str]], asks: list[tuple[str, str]]) -> OrderBook:
    return OrderBook(
        "BTCUSDT", tuple(Level(D(p), D(q)) for p, q in bids), tuple(Level(D(p), D(q)) for p, q in asks), 0.0
    )


def run(
    buy: OrderBook,
    sell: OrderBook,
    *,
    params: DetectionParams = PARAMS,
    rules_b: SymbolRules = RULES,
    rules_a: SymbolRules = RULES,
    fees: Fees = FEES,
    rate: Decimal = RATE,
) -> Verdict:
    return evaluate("BTCUSDT", "binance", "bitget", buy, sell, rules_a, rules_b, fees, fees, rate, params)


def test_hand_computed_opportunity() -> None:
    # buy 10 @ 100 on A, sell 10 @ 101 on B. Market share 90% -> qty 9.
    v = run(book([("99", "1")], [("100", "10")]), book([("101", "10")], [("102", "1")]))
    assert isinstance(v, Opportunity)
    assert v.qty == D(9) and v.executable_qty == D(10)
    assert (v.buy_vwap, v.sell_vwap) == (D(100), D(101))
    assert v.capital_used == D(900)
    assert v.gross == D(9)  # 909 - 900
    assert v.fees == D("1.809")  # 900*0.001 + 909*0.001
    assert v.interest == D("0.00909")  # 9 BTC * 0.00001/h * 1 billed hour * 101
    assert v.net == D("7.18191")
    assert v.net_roi_pct == D("0.7979900")  # 7.18191 / 900 * 100


def test_multi_level_vwap_is_used_not_best_price() -> None:
    v = run(book([], [("100", "5"), ("100.1", "5")]), book([("101", "5"), ("100.9", "5")], []))
    assert isinstance(v, Opportunity)
    assert v.qty == D(9)
    assert v.buy_vwap == (D(500) + D("100.1") * 4) / 9
    assert v.sell_vwap == (D(505) + D("100.9") * 4) / 9


def test_not_crossed() -> None:
    v = run(book([], [("101", "10")]), book([("100", "10")], []))
    assert v == Rejection("BTCUSDT", "binance", "bitget", Reject.NOT_CROSSED)
    equal = run(book([], [("100", "10")]), book([("100", "10")], []))
    assert isinstance(equal, Rejection) and equal.reason is Reject.NOT_CROSSED


def test_empty_books_and_dust() -> None:
    empty_buy = run(book([], []), book([("101", "10")], []))
    empty_sell = run(book([], [("100", "10")]), book([], []))
    assert isinstance(empty_buy, Rejection) and empty_buy.reason is Reject.NO_LIQUIDITY
    assert isinstance(empty_sell, Rejection) and empty_sell.reason is Reject.NO_LIQUIDITY
    dust = run(book([], [("100", "0.0005")]), book([("101", "0.0005")], []))
    assert isinstance(dust, Rejection) and dust.reason is Reject.NO_LIQUIDITY  # floors to zero at the 0.001 step


def test_below_min_qty_and_notional() -> None:
    big_min = SymbolRules(D("0.01"), D(1000), D("0.001"), D("0.01"), D(5))
    v = run(book([], [("100", "0.005")]), book([("101", "0.005")], []), rules_b=big_min)
    assert isinstance(v, Rejection) and v.reason is Reject.BELOW_MIN_QTY
    v = run(book([], [("100", "0.01")]), book([("101", "0.01")], []))  # qty 0.009 -> notional 0.9 < 5
    assert isinstance(v, Rejection) and v.reason is Reject.BELOW_MIN_NOTIONAL


def test_below_min_roi_reports_the_roi() -> None:
    v = run(book([], [("100", "10")]), book([("100.2", "10")], []))
    assert isinstance(v, Rejection) and v.reason is Reject.BELOW_MIN_ROI
    assert v.net_roi_pct is not None and v.net_roi_pct < PARAMS.min_net_roi_pct


def test_quantity_is_clamped_to_the_exchange_maximum_and_common_step() -> None:
    small_max = SymbolRules(D("0.001"), D("2.5"), D("0.5"), D("0.01"), D(5))
    v = run(book([], [("100", "10")]), book([("101", "10")], []), rules_a=small_max)
    assert isinstance(v, Opportunity)
    assert v.qty == D("2.5")  # step common to 0.5 and 0.001 is 0.5; 9 -> capped at the 2.5 maximum


def test_interest_is_billed_per_started_hour() -> None:
    long_loan = DetectionParams(D("0.002"), D("0.30"), D("0.90"), 3601)  # 2 started hours
    base = run(book([], [("100", "10")]), book([("101", "10")], []))
    longer = run(book([], [("100", "10")]), book([("101", "10")], []), params=long_loan)
    assert isinstance(base, Opportunity) and isinstance(longer, Opportunity)
    assert longer.interest == base.interest * 2


@given(st.integers(1, 50), st.integers(1, 500))
def test_never_sizes_beyond_the_market_cap(qty_a: int, qty_b: int) -> None:
    v = run(book([], [("100", str(qty_a))]), book([("101", str(qty_b))], []))
    if isinstance(v, Opportunity):
        assert v.qty <= min(D(qty_a), D(qty_b)) * PARAMS.market_pct
        assert v.qty % D("0.001") == 0
        assert v.net_roi_pct >= PARAMS.min_net_roi_pct


def test_rejection_reasons_are_distinct_strings() -> None:
    assert len({r.value for r in Reject}) == len(Reject)
    with pytest.raises(AttributeError):
        Rejection("s", "a", "b", Reject.NO_LIQUIDITY).reason = Reject.NOT_CROSSED  # type: ignore[misc]
