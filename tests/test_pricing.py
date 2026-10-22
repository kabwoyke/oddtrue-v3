from decimal import Decimal as D

import pytest
from hypothesis import given
from hypothesis import strategies as st

from oddtrue.pricing import (
    InsufficientDepth,
    Level,
    Side,
    SymbolRules,
    buy_cost,
    executable_liquidity,
    gross_profit,
    interest_estimate_coin,
    meets_min_roi,
    net_profit,
    net_roi_pct,
    protected_limit_price,
    sell_proceeds,
    slippage_cost,
    slippage_limit_price,
    trading_fees,
    walk_book,
)

ASKS = [Level(D(100), D(1)), Level(D(101), D(2)), Level(D(103), D(5))]
BIDS = [Level(D(100), D(1)), Level(D(99), D(2)), Level(D(95), D(5))]


def test_vwap_hand_computed_buy() -> None:
    fill = walk_book(ASKS, D(2), Side.BUY)  # 1@100 + 1@101
    assert fill.vwap == D("100.5")
    assert (fill.notional, fill.best_price, fill.worst_price) == (D(201), D(100), D(101))


def test_vwap_hand_computed_sell() -> None:
    fill = walk_book(BIDS, D(3), Side.SELL)  # 1@100 + 2@99
    assert fill.notional == D(298)
    assert fill.vwap == D(298) / D(3)


def test_vwap_single_level_equals_price() -> None:
    assert walk_book(ASKS, D("0.5"), Side.BUY).vwap == D(100)


def test_insufficient_depth_raises() -> None:
    with pytest.raises(InsufficientDepth):
        walk_book(ASKS, D(9), Side.BUY)


def test_nonpositive_qty_and_bad_levels_rejected() -> None:
    with pytest.raises(ValueError):
        walk_book(ASKS, D(0), Side.BUY)
    with pytest.raises(ValueError):
        Level(D(0), D(1))
    with pytest.raises(ValueError):
        Level(D(1), D(-1))


def test_unsorted_book_rejected() -> None:
    with pytest.raises(ValueError):
        walk_book(list(reversed(ASKS)), D(1), Side.BUY)
    with pytest.raises(ValueError):
        executable_liquidity(list(reversed(BIDS)), Side.SELL, D("0.01"))


def test_executable_liquidity_within_slippage() -> None:
    # limit = 100 * 1.01 = 101 -> includes 100 and 101 levels only
    liq = executable_liquidity(ASKS, Side.BUY, D("0.01"))
    assert (liq.qty, liq.notional) == (D(3), D(100 + 202))
    # bids: limit = 100 * 0.99 = 99
    liq = executable_liquidity(BIDS, Side.SELL, D("0.01"))
    assert (liq.qty, liq.notional) == (D(3), D(100 + 198))


def test_executable_liquidity_zero_slippage_is_top_level_and_empty_book() -> None:
    assert executable_liquidity(ASKS, Side.BUY, D(0)).qty == D(1)
    assert executable_liquidity([], Side.BUY, D("0.01")).qty == D(0)


def test_slippage_limit_bounds() -> None:
    assert slippage_limit_price(D(100), D("0.01"), Side.BUY) == D("101.00")
    assert slippage_limit_price(D(100), D("0.01"), Side.SELL) == D("99.00")
    with pytest.raises(ValueError):
        slippage_limit_price(D(100), D(1), Side.BUY)
    with pytest.raises(ValueError):
        slippage_limit_price(D(100), D("-0.1"), Side.BUY)


def test_profit_chain_hand_computed() -> None:
    cost = buy_cost(D(2), D(100))  # 200
    proceeds = sell_proceeds(D(2), D(102))  # 204
    gross = gross_profit(proceeds, cost)  # 4
    fees = trading_fees([(cost, D("0.001")), (proceeds, D("0.001"))])  # 0.2 + 0.204
    assert gross == D(4)
    assert fees == D("0.404")
    interest = interest_estimate_coin(D(2), D("0.00001"), D(1)) * D(102)  # 0.00002 coin * 102
    assert interest == D("0.00204")
    net = net_profit(gross, fees, interest, D(0), D(0))
    assert net == D("3.59396")
    assert net_roi_pct(net, cost) == D("1.79698")
    assert meets_min_roi(D("1.79698"), D("1.5"))
    assert not meets_min_roi(D("0.29"), D("0.30"))
    assert meets_min_roi(D("0.30"), D("0.30"))


def test_profit_input_validation() -> None:
    with pytest.raises(ValueError):
        trading_fees([(D(-1), D("0.1"))])
    with pytest.raises(ValueError):
        interest_estimate_coin(D(1), D(-1), D(1))
    with pytest.raises(ValueError):
        net_roi_pct(D(1), D(0))


def test_slippage_cost_only_adverse() -> None:
    assert slippage_cost(D(100), D(101), D(2), Side.BUY) == D(2)
    assert slippage_cost(D(100), D(99), D(2), Side.BUY) == D(0)  # favourable not booked
    assert slippage_cost(D(100), D(99), D(2), Side.SELL) == D(2)
    assert slippage_cost(D(100), D(101), D(2), Side.SELL) == D(0)


def test_protected_price_rounds_protectively() -> None:
    # SELL floor 100*(1-0.005)=99.5 -> tick 1 rounds UP to 100 (never worse than allowed)
    assert protected_limit_price(D(100), D("0.005"), D(1), Side.SELL) == D(100)
    # BUY ceiling 100*(1.005)=100.5 -> tick 1 rounds DOWN to 100
    assert protected_limit_price(D(100), D("0.005"), D(1), Side.BUY) == D(100)
    assert protected_limit_price(D(100), D("0.0025"), D("0.01"), Side.SELL) == D("99.75")


def test_symbol_rules_validation() -> None:
    with pytest.raises(ValueError):
        SymbolRules(D(1), D(10), D(0), D("0.1"), D(5))
    with pytest.raises(ValueError):
        SymbolRules(D(-1), D(10), D("0.1"), D("0.1"), D(5))
    with pytest.raises(ValueError):
        SymbolRules(D(5), D(1), D("0.1"), D("0.1"), D(5))


level_st = st.builds(
    Level,
    price=st.decimals(min_value=D("0.01"), max_value=D(100000), places=2),
    qty=st.decimals(min_value=D("0.001"), max_value=D(100), places=3),
)


@given(st.lists(level_st, min_size=1, max_size=15), st.data())
def test_property_vwap_between_best_and_worst_touched(levels: list[Level], data: st.DataObject) -> None:
    side = data.draw(st.sampled_from([Side.BUY, Side.SELL]))
    book = sorted(levels, key=lambda lv: lv.price, reverse=side is Side.SELL)
    total = sum((lv.qty for lv in book), D(0))
    qty = data.draw(st.decimals(min_value=D("0.001"), max_value=total, places=3))
    fill = walk_book(book, qty, side)
    lo, hi = sorted((fill.best_price, fill.worst_price))
    assert lo <= fill.vwap <= hi
    assert fill.notional == fill.vwap * qty or abs(fill.notional - fill.vwap * qty) < D("1e-20")


@given(st.lists(level_st, min_size=1, max_size=15), st.decimals(min_value=0, max_value=D("0.5"), places=3))
def test_property_executable_liquidity_never_exceeds_book_and_grows_with_slippage(levels: list[Level], slip: D) -> None:
    book = sorted(levels, key=lambda lv: lv.price)
    liq = executable_liquidity(book, Side.BUY, slip)
    wider = executable_liquidity(book, Side.BUY, slip + D("0.1"))
    assert D(0) < liq.qty <= wider.qty <= sum((lv.qty for lv in book), D(0))
