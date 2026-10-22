from datetime import UTC, datetime
from decimal import Decimal as D

import pytest
from hypothesis import given
from hypothesis import strategies as st

from oddtrue.allocation import (
    AllocationInput,
    Candidate,
    Check,
    Constraint,
    Rejection,
    RotationReason,
    TradeProposal,
    ceil_to_step,
    client_trade_capacity,
    common_step,
    final_allocation,
    floor_to_step,
    is_multiple_of,
    market_cap,
    plan_rotation,
    split_capacity,
    validate_trade,
)
from oddtrue.pricing import SymbolRules

RULES = SymbolRules(min_qty=D("0.001"), max_qty=D(100), step=D("0.001"), tick=D("0.01"), min_notional=D(5))


# ---- rounding ----------------------------------------
def test_floor_ceil_and_multiple() -> None:
    assert floor_to_step(D("1.2345"), D("0.01")) == D("1.23")
    assert ceil_to_step(D("1.2345"), D("0.01")) == D("1.24")
    assert ceil_to_step(D("1.23"), D("0.01")) == D("1.23")
    assert is_multiple_of(D("1.23"), D("0.01"))
    assert not is_multiple_of(D("1.234"), D("0.01"))


def test_rounding_input_validation() -> None:
    for bad in (
        lambda: floor_to_step(D(1), D(0)),
        lambda: floor_to_step(D(-1), D(1)),
        lambda: is_multiple_of(D(1), D(0)),
    ):
        with pytest.raises(ValueError):
            bad()
    with pytest.raises(ValueError):
        common_step()
    with pytest.raises(ValueError):
        common_step(D("0.1"), D(0))


def test_common_step() -> None:
    assert common_step(D("0.00001"), D("0.001")) == D("0.001")
    assert common_step(D("0.2"), D("0.3")) == D("0.6")
    assert common_step(D("0.5")) == D("0.5")


@given(
    st.decimals(min_value=0, max_value=D(10**6), places=6), st.sampled_from([D("0.001"), D("0.00001"), D("0.5"), D(1)])
)
def test_property_floor_invariants(value: D, step: D) -> None:
    f = floor_to_step(value, step)
    assert f <= value < f + step
    assert is_multiple_of(f, step)
    c = ceil_to_step(value, step)
    assert value <= c < value + step or c == value
    assert is_multiple_of(c, step)


# ---- capacity ----------------------------------------
def test_capacity_split_and_validation() -> None:
    cap = split_capacity(D(1000), D("0.95"), D("0.05"))
    assert cap.trade_usdt + cap.reserve_usdt == D(1000)
    with pytest.raises(ValueError):
        split_capacity(D(-1), D("0.95"), D("0.05"))
    with pytest.raises(ValueError):
        split_capacity(D(1), D("0.9"), D("0.2"))


def test_trade_capacity_uses_smaller_exchange() -> None:
    assert client_trade_capacity(D(500), D(300), D("0.95")) == D(285)
    assert client_trade_capacity(D(300), D(500), D("0.95")) == D(285)
    with pytest.raises(ValueError):
        client_trade_capacity(D(-1), D(1), D("0.95"))


def test_market_cap_rejects_negative() -> None:
    with pytest.raises(ValueError):
        market_cap(D(-1), D("0.9"))


# ---- final allocation ----------------------------------------
def _alloc_input(**over: object) -> AllocationInput:
    base: dict[str, object] = {
        "buy_price": D(100),
        "sell_price": D(101),
        "client_trade_capacity_usdt": D(475),
        "market_cap_qty": D(50),
        "available_capital_usdt": D(500),
        "borrowable_qty": D(50),
        "buy_rules": RULES,
        "sell_rules": RULES,
    }
    base.update(over)
    return AllocationInput(**base)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("over", "binding", "qty"),
    [
        ({}, Constraint.CLIENT_CAP, D("4.75")),
        ({"market_cap_qty": D("2.5")}, Constraint.MARKET_CAP, D("2.5")),
        ({"available_capital_usdt": D(100)}, Constraint.AVAILABLE_CAPITAL, D(1)),
        ({"borrowable_qty": D("0.7")}, Constraint.BORROWABLE, D("0.7")),
        (
            {"buy_rules": SymbolRules(D("0.001"), D("1.5"), D("0.001"), D("0.01"), D(5))},
            Constraint.EXCHANGE_MAX,
            D("1.5"),
        ),
    ],
)
def test_each_cap_can_bind(over: dict[str, object], binding: Constraint, qty: D) -> None:
    a = final_allocation(_alloc_input(**over))
    assert a.ok and a.binding is binding and a.qty == qty


def test_rounding_uses_step_valid_on_both_exchanges() -> None:
    coarse = SymbolRules(D("0.001"), D(100), D("0.003"), D("0.01"), D(5))
    a = final_allocation(_alloc_input(buy_rules=coarse, client_trade_capacity_usdt=D("100")))
    assert a.qty == D("0.999")  # raw 1.000; floor to lcm(0.003, 0.001) = 0.003 -> 0.999
    assert is_multiple_of(a.qty, D("0.003")) and is_multiple_of(a.qty, D("0.001"))


def test_rejections() -> None:
    assert final_allocation(_alloc_input(borrowable_qty=D(0))).rejection is Rejection.NO_CAPACITY
    assert final_allocation(_alloc_input(client_trade_capacity_usdt=D("0.05"))).rejection is Rejection.BELOW_MIN_QTY
    big_min = SymbolRules(D("0.001"), D(100), D("0.001"), D("0.01"), D(1000))
    r = final_allocation(_alloc_input(sell_rules=big_min))
    assert r.rejection is Rejection.BELOW_MIN_NOTIONAL and r.qty == D(0)
    with pytest.raises(ValueError):
        final_allocation(_alloc_input(buy_price=D(0)))


@given(
    capacity=st.decimals(min_value=0, max_value=D(10**5), places=2),
    market=st.decimals(min_value=0, max_value=D(1000), places=3),
    avail=st.decimals(min_value=0, max_value=D(10**5), places=2),
    borrow=st.decimals(min_value=0, max_value=D(1000), places=3),
    price=st.decimals(min_value=D("0.5"), max_value=D(10**5), places=2),
)
def test_property_allocation_never_exceeds_any_cap_and_conforms(
    capacity: D, market: D, avail: D, borrow: D, price: D
) -> None:
    inp = _alloc_input(
        buy_price=price,
        sell_price=price,
        client_trade_capacity_usdt=capacity,
        market_cap_qty=market,
        available_capital_usdt=avail,
        borrowable_qty=borrow,
    )
    a = final_allocation(inp)
    if a.ok:
        assert a.qty * price <= capacity
        assert a.qty <= market and a.qty <= borrow
        assert a.qty * price <= avail
        assert RULES.min_qty <= a.qty <= RULES.max_qty
        assert is_multiple_of(a.qty, RULES.step)
        assert a.qty * price >= RULES.min_notional
    else:
        assert a.qty == D(0)


# ---- rotation ----------------------------------------
T1 = datetime(2026, 1, 1, tzinfo=UTC)
T2 = datetime(2026, 1, 2, tzinfo=UTC)


def test_spec8_demand_100k_vs_capacity_45k() -> None:
    # Five clients each want 20,000 (as qty at price 1); capacity 45,000 -> only two fit.
    cands = [Candidate(f"c{i}", D(20000), T1) for i in range(5)]
    out = plan_rotation(cands, D(45000))
    included = [d for d in out if d.reason is RotationReason.INCLUDED]
    assert len(included) == 2
    assert sum(d.qty for d in out) <= D(45000)
    assert all(d.reason is RotationReason.SKIPPED_DOES_NOT_FIT for d in out[2:])


def test_longest_waiting_first_and_never_run_first() -> None:
    out = plan_rotation(
        [Candidate("b", D(10), T2), Candidate("a", D(10), T1), Candidate("z", D(10), None)],
        D(20),
    )
    assert [d.client_id for d in out] == ["z", "a", "b"]
    assert [d.reason for d in out] == [
        RotationReason.INCLUDED,
        RotationReason.INCLUDED,
        RotationReason.SKIPPED_DOES_NOT_FIT,
    ]


def test_size_fit_lets_smaller_later_client_in_and_tiebreak_by_id() -> None:
    out = plan_rotation([Candidate("b", D(5), T1), Candidate("a", D(30), T1), Candidate("c", D(8), T1)], D(15))
    assert [d.client_id for d in out] == ["a", "b", "c"]  # same time -> client_id order
    assert [d.reason for d in out] == [
        RotationReason.SKIPPED_DOES_NOT_FIT,
        RotationReason.INCLUDED,
        RotationReason.INCLUDED,
    ]
    assert [d.position for d in out] == [1, 2, 3]


def test_zero_allocation_and_validation() -> None:
    out = plan_rotation([Candidate("a", D(0), None)], D(10))
    assert out[0].reason is RotationReason.SKIPPED_NO_ALLOCATION
    with pytest.raises(ValueError):
        plan_rotation([], D(-1))
    with pytest.raises(ValueError):
        plan_rotation([Candidate("a", D(1), None), Candidate("a", D(1), None)], D(10))


@given(
    st.lists(st.tuples(st.integers(0, 50), st.integers(0, 3)), min_size=0, max_size=30),
    st.integers(0, 500),
)
def test_property_rotation_total_within_capacity_and_deterministic(rows: list[tuple[int, int]], cap: int) -> None:
    cands = [
        Candidate(f"c{i:03d}", D(q), None if d == 0 else datetime(2026, 1, d, tzinfo=UTC))
        for i, (q, d) in enumerate(rows)
    ]
    out = plan_rotation(cands, D(cap))
    assert sum((d.qty for d in out), D(0)) <= D(cap)
    assert out == plan_rotation(list(reversed(cands)), D(cap))  # input order never matters


# ---- validate_trade (master constraint) ----------------------------------------
def _proposal(**over: object) -> TradeProposal:
    base: dict[str, object] = {
        "qty": D("4.75"),
        "buy_price": D(100),
        "sell_price": D(101),
        "usable_buy": D(500),
        "usable_borrow": D(500),
        "trade_pct": D("0.95"),
        "executable_liquidity_qty": D(100),
        "market_pct": D("0.90"),
        "borrowable_qty": D(10),
        "buy_rules": RULES,
        "sell_rules": RULES,
        "expected_net_roi_pct": D("0.5"),
        "min_net_roi_pct": D("0.3"),
    }
    base.update(over)
    return TradeProposal(**base)  # type: ignore[arg-type]


def _checks(**over: object) -> list[Check]:
    return [f.check for f in validate_trade(_proposal(**over)).failures]


def test_valid_trade_passes() -> None:
    r = validate_trade(_proposal())
    assert r.passed and r.reason is None and r.failures == ()


@pytest.mark.parametrize(
    ("over", "expected"),
    [
        ({"qty": D(0)}, Check.QTY_NOT_POSITIVE),
        ({"qty": D("4.76")}, Check.CLIENT_ALLOCATION),
        ({"executable_liquidity_qty": D(5)}, Check.MARKET_EXECUTION),
        ({"borrowable_qty": D(4)}, Check.BORROW_QTY),
        (
            {
                "qty": D(101),
                "usable_buy": D(10**6),
                "usable_borrow": D(10**6),
                "executable_liquidity_qty": D(10**4),
                "borrowable_qty": D(10**4),
            },
            Check.MAX_QTY,
        ),
        ({"qty": D("0.0005")}, Check.MIN_QTY),
        ({"qty": D("1.0005")}, Check.LOT_SIZE),
        ({"qty": D("0.01")}, Check.MIN_NOTIONAL),
        ({"buy_limit_price": D("100.005")}, Check.PRICE_TICK),
        ({"sell_limit_price": D("101.005")}, Check.PRICE_TICK),
        ({"expected_net_roi_pct": D("0.29")}, Check.MIN_ROI),
        ({"recovery_interest_usdt": D(30), "reserve_usdt": D(25)}, Check.RECOVERY_INTEREST_RESERVE),
        ({"recovery_interest_usdt": D(1)}, Check.RECOVERY_INTEREST_RESERVE),  # reserve unknown -> fail safe
        ({"outstanding_loan_after_recovery": D("0.000001")}, Check.LOAN_NOT_ZERO),
    ],
)
def test_each_failure_is_reported(over: dict[str, object], expected: Check) -> None:
    assert expected in _checks(**over)


def test_boundary_values_pass() -> None:
    assert validate_trade(_proposal(expected_net_roi_pct=D("0.3"))).passed
    assert validate_trade(
        _proposal(recovery_interest_usdt=D(25), reserve_usdt=D(25), outstanding_loan_after_recovery=D(0))
    ).passed


def test_all_failures_listed_and_reason_is_first() -> None:
    r = validate_trade(_proposal(qty=D("4.76"), expected_net_roi_pct=D(0)))
    assert not r.passed
    assert {f.check for f in r.failures} >= {Check.CLIENT_ALLOCATION, Check.MIN_ROI}
    assert r.reason == r.failures[0]


@given(
    capacity=st.decimals(min_value=D(10), max_value=D(10**5), places=2),
    market=st.decimals(min_value=D(1), max_value=D(1000), places=3),
    borrow=st.decimals(min_value=D(1), max_value=D(1000), places=3),
)
def test_property_allocation_output_always_passes_validation(capacity: D, market: D, borrow: D) -> None:
    """The allocator and the master constraint must agree: anything allocated validates."""
    pct, mpct = D("0.95"), D("0.90")
    inp = _alloc_input(
        client_trade_capacity_usdt=client_trade_capacity(capacity, capacity, pct),
        market_cap_qty=market_cap(market, mpct),
        available_capital_usdt=capacity,
        borrowable_qty=borrow,
    )
    a = final_allocation(inp)
    if a.ok:
        r = validate_trade(
            _proposal(
                qty=a.qty,
                usable_buy=capacity,
                usable_borrow=capacity,
                executable_liquidity_qty=market,
                borrowable_qty=borrow,
            )
        )
        assert r.passed, r.failures
