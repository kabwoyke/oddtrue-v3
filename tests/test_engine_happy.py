"""Successful trade and every pre-check cancellation (CLAUDE.md section 6; journey steps 7-9)."""

from __future__ import annotations

from decimal import Decimal as D

import pytest

from oddtrue.exchanges.fake import Fault, FaultKind, Op
from oddtrue.exchanges.models import Permissions
from oddtrue.execution.deps import AlertKind
from oddtrue.execution.engine import RunResult, StartRefused
from oddtrue.execution.states import TradeState
from oddtrue.ledger.models import LegStatus, Outcome
from tests.world import MARGIN, SPOT, World


async def test_successful_trade_end_to_end(world: World) -> None:
    result = await world.engine().start(world.plan())
    assert result == RunResult(TradeState.DONE, Outcome.SUCCESS, None)

    history = [to for _, to, _ in await world.ledger.state_history("john", "t1")]
    assert history == [
        "ALLOCATED", "PRECHECK_B_LIQUIDITY", "PRECHECK_BORROW", "REVALIDATED", "EXECUTING",
        "SETTLING", "RECONCILING", "REBALANCE_EVAL", "DONE",
    ]  # fmt: skip

    legs = {x.leg: x for x in await world.ledger.legs("john", "t1")}
    assert set(legs) == {"A_BUY", "B_BORROW", "B_SELL", "B_REPAY"}
    assert all(x.status is LegStatus.FILLED for x in legs.values())
    assert len({x.client_order_id for x in legs.values()}) == 4

    # the exchange, not our ledger, says the loan is zero
    assert (await world.acc_b.get_loan("BTC")).outstanding == 0
    assert all(row.verified_zero_at is not None for row in await world.ledger.loans("john", "t1"))

    # hand-computed: buy 0.004 @ 100000, sell @ 100900, 0.1% taker fees, one billed hour of interest:
    # 0.004 BTC x 0.0001/h = 0.0000004 BTC, valued at the 100900 sell price
    r = await world.ledger.trade_result("john", "t1")
    assert (r.buy_cost, r.sell_proceeds, r.gross) == (D(400), D("403.6"), D("3.6"))
    assert r.fees == D("0.4") + D("0.4036") and r.interest == D("0.04036") and r.recovery == 0
    assert r.net == D("2.7964") - D("0.04036") and r.realized_roi_pct == r.net / 400 * 100
    assert r.outcome is Outcome.SUCCESS and r.buy_vwap == D(100000) and r.sell_vwap == D(100900)
    assert await world.ledger.client_net_total("john") == r.net

    # balances: coin moved from B inventory to A (D-027); USDT as booked
    assert world.acc_a.balance(SPOT, "BTC") == D("0.003996") and world.acc_a.balance(SPOT, "USDT") == D(600)
    assert world.acc_b.balance(MARGIN, "BTC") == D("0.01") - D("0.004") - D("0.0000004")
    assert world.acc_b.balance(MARGIN, "USDT") == D("500") + D("403.6") - D("0.4036")
    assert world.alerts.alerts == []


async def test_interest_is_booked_from_the_exchange_reported_figure(world: World) -> None:
    world.b.set_borrow("BTC", D("0.0005"), D(100))  # 0.05% per hour
    result = await world.engine().start(world.plan())
    assert result.outcome is Outcome.SUCCESS
    repay = next(x for x in await world.ledger.legs("john", "t1") if x.leg == "B_REPAY")
    interest_coin = D(repay.meta["interest_before"])
    assert interest_coin == D("0.000002")  # as reported by the exchange, not our estimate
    assert repay.filled_qty == D("0.004") + interest_coin
    r = await world.ledger.trade_result("john", "t1")
    assert r.interest == interest_coin * D("100900")
    assert (await world.acc_b.get_loan("BTC")).outstanding == 0


async def test_second_trade_for_same_client_is_refused_while_one_is_running(world: World) -> None:
    await world.ledger.create_trade_limited(world.plan("t0").new_trade_row(), world.plan("t0").to_json(), 1)
    with pytest.raises(StartRefused):
        await world.engine().start(world.plan("t1"))
    assert await world.ledger.legs("john", "t1") == []


async def _cancelled(
    world: World, why: str, plan_over: dict[str, object] | None = None, inventory: D = D("0.01")
) -> None:
    result = await world.engine().start(world.plan())
    assert result.state is TradeState.CANCELLED and result.outcome is Outcome.CANCELLED
    reason = (await world.ledger.state_history("john", "t1"))[-1][2] or ""
    assert why in reason, reason
    # cancelled means nothing was sent: no legs, no loans, no ledger entries, balances untouched
    assert await world.ledger.legs("john", "t1") == [] and await world.ledger.loans("john", "t1") == []
    assert await world.ledger.entries("john") == []
    assert world.acc_a.balance(SPOT, "USDT") == D(1000) and world.acc_b.balance(MARGIN, "BTC") == inventory


async def test_cancel_when_borrow_side_book_is_stale(world: World) -> None:
    world.b.add_fault(Op.GET_ORDERBOOK, Fault(FaultKind.STALE_BOOK, age_s=60))
    await _cancelled(world, "borrow_side_book_stale")


async def test_cancel_when_sell_liquidity_within_slippage_is_too_thin(world: World) -> None:
    world.set_books(depth="0.003")  # 0.003 per level < 0.004 wanted, and the 2nd level is outside max slippage
    world.b.set_book("BTCUSDT", [(D(100900), D("0.003")), (D(100000), D(10))], [(D(100910), D(10))])
    await _cancelled(world, "insufficient_sell_liquidity")


async def test_cancel_when_not_enough_to_borrow(world: World) -> None:
    world.b.set_borrow("BTC", D("0.0001"), D("0.001"))
    await _cancelled(world, "borrowable_too_low")


async def test_cancel_when_a_loan_for_the_coin_already_exists(world: World) -> None:
    await world.acc_b.borrow("BTC", D("0.001"), "manual-loan")
    result = await world.engine().start(world.plan())
    assert result.state is TradeState.CANCELLED
    assert AlertKind.LOAN_PRESENT_BEFORE_TRADE in world.alerts.kinds()
    assert await world.ledger.legs("john", "t1") == []


async def test_cancel_when_repay_coin_is_not_pre_held_on_the_borrow_side(world: World) -> None:
    world.acc_b.drift(MARGIN, "BTC", D("-0.01"))
    await _cancelled(world, "repay_coin_unavailable_on_borrow_side", inventory=D(0))


async def test_cancel_when_net_roi_is_below_minimum(world: World) -> None:
    world.set_books(bid_b="100050")  # gap too small to survive fees
    await _cancelled(world, "net_roi_below_minimum")


async def test_cancel_when_revalidation_finds_the_edge_gone_after_prechecks(world: World) -> None:
    flipped: list[str] = []

    def move_market(name: str) -> None:
        if name == "state:PRECHECK_BORROW" and not flipped:
            flipped.append(name)
            world.set_books(bid_b="100050")  # the opportunity vanishes between pre-check and revalidation

    result = await world.engine(checkpoint=move_market).start(world.plan())
    assert result.state is TradeState.CANCELLED
    assert "net_roi_below_minimum" in ((await world.ledger.state_history("john", "t1"))[-1][2] or "")
    assert await world.ledger.legs("john", "t1") == []


async def test_cancel_when_entitlement_gate_blocks(world: World) -> None:
    class Lapsed:
        async def blocked_reason(self, client_id: str) -> str | None:
            return "entitlement_lapsed"

    result = await world.engine(gate=Lapsed()).start(world.plan())
    assert result.state is TradeState.CANCELLED
    assert (await world.ledger.state_history("john", "t1"))[-1][2] == "gate:entitlement_lapsed"


async def test_cancel_when_keys_lack_margin_or_borrow_permission(world: World) -> None:
    world.acc_b.permissions = Permissions(read=True, trade=True, margin=True, borrow=False, withdraw=False)
    await _cancelled(world, "borrow_side_missing_permission")


async def test_cancel_when_buy_side_cannot_trade(world: World) -> None:
    world.acc_a.permissions = Permissions(read=True, trade=False, margin=False, borrow=False, withdraw=False)
    await _cancelled(world, "buy_side_no_trade_permission")


async def test_cancel_when_the_exchange_is_unreachable_before_anything_is_sent(world: World) -> None:
    world.b.add_fault(Op.GET_ORDERBOOK, Fault(FaultKind.EXCHANGE_DOWN, times=3))
    await _cancelled(world, "precheck_read_failed")


async def test_cancel_when_buy_side_book_cannot_fill_the_quantity(world: World) -> None:
    world.a.set_book("BTCUSDT", [(D(99990), D(1))], [(D(100000), D("0.001"))])
    result = await world.engine().start(world.plan())
    assert result.state is TradeState.CANCELLED
    assert await world.ledger.legs("john", "t1") == []


async def test_cancel_when_buy_side_book_is_stale_at_revalidation(world: World) -> None:
    world.a.add_fault(Op.GET_ORDERBOOK, Fault(FaultKind.STALE_BOOK, age_s=60))  # A is only read at revalidation
    result = await world.engine().start(world.plan())
    assert result.state is TradeState.CANCELLED
    assert "book_stale_at_revalidation" in ((await world.ledger.state_history("john", "t1"))[-1][2] or "")
    assert await world.ledger.legs("john", "t1") == []


async def test_cancel_when_sizing_exceeds_actual_funds(world: World) -> None:
    world.acc_a.drift(SPOT, "USDT", D(-900))  # only 100 USDT left on the buy side
    result = await world.engine().start(world.plan())
    assert result.state is TradeState.CANCELLED
    assert "client_allocation_exceeds_trade_capacity" in ((await world.ledger.state_history("john", "t1"))[-1][2] or "")


async def test_limits_are_protected_prices_not_market(world: World) -> None:
    await world.engine().start(world.plan())
    plan = await world.ledger.get_plan("john", "t1")
    # 0.2% max slippage from best: buy limit rounds DOWN from 100200, sell limit rounds UP from 100698.2
    assert D(plan["buy_limit"]) == D("100200.00") and D(plan["sell_limit"]) == D("100698.20")
    assert D(plan["expected_net_roi_pct"]) > D("0.30")
