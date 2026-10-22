"""Every worked example in the spec (and CLAUDE.md section 13 Phase 1 exit) reproduced as a test."""

from decimal import Decimal as D

from oddtrue.allocation import (
    AllocationInput,
    client_trade_capacity,
    final_allocation,
    floor_to_step,
    market_cap,
    split_capacity,
)
from oddtrue.pricing import SymbolRules, interest_value_usdt


def test_spec2_500_splits_475_and_25() -> None:
    cap = split_capacity(D(500), D("0.95"), D("0.05"))
    assert (cap.trade_usdt, cap.reserve_usdt) == (D(475), D(25))


def test_spec6_50000_liquidity_gives_45000() -> None:
    assert market_cap(D(50000), D("0.90")) == D(45000)


def test_spec9_0_123456_btc_rounds_to_0_12345() -> None:
    assert floor_to_step(D("0.123456"), D("0.00001")) == D("0.12345")


def test_spec20_liability_is_principal_plus_interest() -> None:
    assert D("0.003000") + D("0.000020") == D("0.003020")


def test_spec21_interest_purchase_costs_2_usdt() -> None:
    assert interest_value_usdt(D("0.000020"), D(100000)) == D(2)


def test_spec9_rounded_allocation_stays_inside_caps() -> None:
    rules = SymbolRules(D("0.00001"), D(100), D("0.00001"), D("0.01"), D(5))
    # $500 -> $475 trade capacity at $100,000/BTC = 0.00475 BTC, well inside the other caps.
    alloc = final_allocation(
        AllocationInput(
            buy_price=D(100000),
            sell_price=D(100050),
            client_trade_capacity_usdt=client_trade_capacity(D(500), D(500), D("0.95")),
            market_cap_qty=market_cap(D(10), D("0.90")),
            available_capital_usdt=D(500),
            borrowable_qty=D(1),
            buy_rules=rules,
            sell_rules=rules,
        )
    )
    assert alloc.ok
    assert alloc.qty == D("0.00475")
    assert alloc.qty * D(100000) <= D(475)
