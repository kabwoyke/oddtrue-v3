"""Edge paths of the engine and recovery: error mappings, unresolved outcomes, time limits, guards, isolation."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import replace
from decimal import Decimal as D
from typing import Any

import pytest

from oddtrue.exchanges.base import DUPLICATE_CLIENT_ID
from oddtrue.exchanges.errors import ExchangeDown, InsufficientFunds, InvalidQuantity, Rejected
from oddtrue.exchanges.fake import Fault, FaultKind, Op
from oddtrue.exchanges.models import OrderRequest, TimeInForce
from oddtrue.execution.context import TradeCtx
from oddtrue.execution.deps import AlertKind
from oddtrue.execution.engine import RunResult
from oddtrue.execution.gateway import Gateway
from oddtrue.execution.plan import TradePlan
from oddtrue.execution.states import TradeState
from oddtrue.execution.steps import Move
from oddtrue.ids import client_order_id
from oddtrue.ledger.models import EntryKind, LegStatus, Outcome
from oddtrue.pricing.types import Side
from oddtrue.recovery.recovery import repay_stage
from tests.test_engine_recovery import assert_loan_closed, drain_inventory, legs_by_name
from tests.world import MARGIN, QTY, SPOT, World, build_world

Hook = Callable[[str], None]


def on(name: str, action: Callable[[], None]) -> Hook:
    fired: list[int] = []

    def hook(seen: str) -> None:
        if seen == name and not fired:
            fired.append(1)
            action()

    return hook


# ---- borrow error mapping ----------------------------------------


async def test_borrow_duplicate_rejection_is_resolved_by_the_loan_and_never_resent(world: World) -> None:
    world.b.add_fault(Op.BORROW, Fault(FaultKind.REJECT, code=DUPLICATE_CLIENT_ID))
    result = await world.engine().start(world.plan())
    assert result.outcome is Outcome.RECOVERED
    assert (await legs_by_name(world))["B_BORROW"] == [LegStatus.REJECTED]
    await assert_loan_closed(world)


async def test_borrow_auth_error_is_an_incident_and_the_a_position_is_unwound(world: World) -> None:
    world.b.add_fault(Op.BORROW, Fault(FaultKind.AUTH_ERROR))
    result = await world.engine().start(world.plan())
    assert result.outcome is Outcome.RECOVERED and AlertKind.AUTH_INCIDENT in world.alerts.kinds()
    await assert_loan_closed(world)


@pytest.mark.parametrize("error", [InsufficientFunds("no collateral"), InvalidQuantity("bad qty")])
async def test_borrow_definitive_errors_are_recorded_as_not_borrowed(
    world: World, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    async def refuse(*_: Any, **__: Any) -> None:
        raise error

    monkeypatch.setattr(world.acc_b, "borrow", refuse)
    result = await world.engine().start(world.plan())
    assert result.outcome is Outcome.RECOVERED
    borrow = next(x for x in await world.ledger.legs("john", "t1") if x.leg == "B_BORROW")
    assert borrow.status is LegStatus.REJECTED and type(error).__name__ in borrow.meta["reason"]
    await assert_loan_closed(world)


async def test_unresolvable_borrow_holds_then_resolves_on_resume(world: World) -> None:
    world.b.add_fault(Op.BORROW, Fault(FaultKind.TIMEOUT_NOT_APPLIED))
    outage = on("loan_opened", lambda: world.b.add_fault(Op.GET_LOAN, Fault(FaultKind.EXCHANGE_DOWN, times=6)))
    result = await world.engine(checkpoint=outage).start(world.plan())
    assert result.state is TradeState.EXECUTING and (result.held or "").startswith("unresolved")
    assert (await legs_by_name(world))["B_BORROW"] == [LegStatus.UNKNOWN]
    assert AlertKind.UNKNOWN_OUTCOME_UNRESOLVED in world.alerts.kinds()
    done = await world.engine().advance("john", "t1")  # the exchange answers: no loan was ever created
    assert done.outcome is Outcome.RECOVERED
    await assert_loan_closed(world)


# ---- repay error mapping ----------------------------------------


async def test_repay_duplicate_rejection_is_checked_against_the_loan_then_repaid_again(world: World) -> None:
    world.b.add_fault(Op.REPAY, Fault(FaultKind.REJECT, code=DUPLICATE_CLIENT_ID))
    result = await world.engine().start(world.plan())
    assert result.outcome is Outcome.SUCCESS
    assert (await legs_by_name(world))["B_REPAY"] == [LegStatus.REJECTED, LegStatus.FILLED]
    await assert_loan_closed(world)


async def test_repay_auth_error_is_an_incident_then_repaid(world: World) -> None:
    world.b.add_fault(Op.REPAY, Fault(FaultKind.AUTH_ERROR))
    result = await world.engine().start(world.plan())
    assert result.outcome is Outcome.SUCCESS and AlertKind.AUTH_INCIDENT in world.alerts.kinds()
    assert (await legs_by_name(world))["B_REPAY"] == [LegStatus.REJECTED, LegStatus.FILLED]


@pytest.mark.parametrize("error", [InsufficientFunds("no coin"), InvalidQuantity("bad qty")])
async def test_repay_definitive_errors_are_recorded_then_retried_under_a_new_id(
    world: World, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    real, calls = world.acc_b.repay, []

    async def flaky(asset: str, qty: D, client_id: str) -> Any:
        calls.append(client_id)
        if len(calls) == 1:
            raise error
        return await real(asset, qty, client_id)

    monkeypatch.setattr(world.acc_b, "repay", flaky)
    result = await world.engine().start(world.plan())
    assert result.outcome is Outcome.SUCCESS and len(set(calls)) == 2
    await assert_loan_closed(world)


async def test_unresolvable_repay_holds_and_never_claims_the_loan_closed(world: World) -> None:
    """Regression: an interrupted repay is judged against the outstanding figure persisted with the leg."""
    world.b.add_fault(Op.REPAY, Fault(FaultKind.TIMEOUT_NOT_APPLIED))
    outage = on("intent:B_REPAY:1", lambda: world.b.add_fault(Op.GET_LOAN, Fault(FaultKind.EXCHANGE_DOWN, times=6)))
    result = await world.engine(checkpoint=outage).start(world.plan())
    assert result.state is TradeState.SETTLING and (result.held or "").startswith("unresolved")
    held_again = await world.engine().advance("john", "t1")  # still down (the remaining faults)
    assert held_again.state is TradeState.SETTLING and not held_again.finished
    done = await world.engine().advance("john", "t1")  # answers: the repay never happened, so repay again
    assert done.finished and done.outcome in (Outcome.SUCCESS, Outcome.RECOVERED)
    repays = [x for x in await world.ledger.legs("john", "t1") if x.leg == "B_REPAY"]
    assert [x.status for x in repays] == [LegStatus.REJECTED, LegStatus.FILLED]  # not wrongly 'FILLED' first
    interest = [e for e in await world.ledger.entries("john", "t1") if e.kind is EntryKind.INTEREST]
    assert len(interest) == 1  # interest booked once, for the repay that really happened
    await assert_loan_closed(world)


# ---- hard time limit on the whole execution phase ----------------------------------------


async def test_execution_time_limit_stops_normal_flow_and_goes_to_recovery(world: World) -> None:
    world.cfg = world.cfg.model_copy(update={"execution_timeout_s": 1})
    world.b.add_fault(Op.BORROW, Fault(FaultKind.HANG_APPLIED))  # the borrow lands but never answers
    result = await world.engine(op_timeout_override_s=30).start(world.plan())
    assert result.outcome is Outcome.RECOVERED
    legs = await legs_by_name(world)
    assert legs["B_BORROW"] == [LegStatus.FILLED] and "B_SELL" not in legs  # found by loan query; no sell was sent
    assert "A_UNWIND" in legs
    await assert_loan_closed(world)


# ---- guards and safety nets ----------------------------------------


async def test_executing_without_revalidated_limits_is_refused(world: World) -> None:
    row = replace(world.plan().new_trade_row(), state="EXECUTING")
    await world.ledger.create_trade_limited(row, world.plan().to_json(), 1)
    with pytest.raises(RuntimeError, match="revalidated"):
        await world.engine().advance("john", "t1")


async def test_an_illegal_state_move_is_a_bug_not_a_silent_jump(world: World) -> None:
    engine = world.engine()

    async def bad(_: TradeCtx) -> Move:
        return Move(TradeState.DONE, "skip everything")

    engine._handlers[TradeState.ALLOCATED] = bad
    await world.ledger.create_trade_limited(world.plan().new_trade_row(), world.plan().to_json(), 1)
    with pytest.raises(RuntimeError, match="illegal transition"):
        await engine.advance("john", "t1")


async def test_losing_a_state_race_reloads_and_decides_again(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    real, seen = world.ledger.transition, []

    async def lose_once(*args: Any, **kwargs: Any) -> None:
        seen.append(1)
        if len(seen) == 1:
            from oddtrue.ledger.repository import StateConflict

            raise StateConflict("another worker moved it")
        await real(*args, **kwargs)

    monkeypatch.setattr(world.ledger, "transition", lose_once)
    result = await world.engine().start(world.plan())
    assert result.outcome is Outcome.SUCCESS and len(seen) == 9  # 8 moves + the one lost race


@pytest.mark.parametrize(
    ("error", "held"),
    [(ExchangeDown("down"), "exchange_unavailable:ExchangeDown"), (Rejected("weird"), "exchange_error:Rejected")],
)
async def test_unexpected_exchange_errors_park_the_trade_instead_of_crashing(
    world: World, error: Exception, held: str
) -> None:
    engine = world.engine()

    async def boom(_: TradeCtx) -> Move:
        raise error

    engine._handlers[TradeState.ALLOCATED] = boom
    await world.ledger.create_trade_limited(world.plan().new_trade_row(), world.plan().to_json(), 1)
    result = await engine.advance("john", "t1")
    assert result == RunResult(TradeState.ALLOCATED, None, held) and world.alerts.alerts


async def test_a_status_query_that_is_refused_for_another_reason_stays_unresolved(world: World) -> None:
    world.a.add_fault(Op.PLACE_ORDER, Fault(FaultKind.TIMEOUT_APPLIED))
    world.a.add_fault(Op.GET_ORDER, Fault(FaultKind.REJECT, times=6, code="maintenance"))
    result = await world.engine().start(world.plan())
    assert result.state is TradeState.EXECUTING and (result.held or "").startswith("unresolved")
    done = await world.engine().advance("john", "t1")
    assert done.outcome is Outcome.SUCCESS and len(world.acc_a._orders) == 1


async def test_prechecks_and_revalidation_cancel_when_reads_stay_unavailable(world: World) -> None:
    world.b.add_fault(Op.GET_BORROWABLE, Fault(FaultKind.EXCHANGE_DOWN, times=3))
    first = await world.engine().start(world.plan("t1"))
    assert first.state is TradeState.CANCELLED
    world.b.add_fault(Op.GET_MARGIN_ACCOUNT, Fault(FaultKind.EXCHANGE_DOWN, times=3))
    second = await world.engine().start(world.plan("t2"))
    assert second.state is TradeState.CANCELLED
    assert "revalidation_read_failed" in ((await world.ledger.state_history("john", "t2"))[-1][2] or "")


async def test_a_duplicate_response_is_recognised_as_the_same_order(world: World) -> None:
    """The exchange already holds an order with our id (e.g. a replayed request): adopt it, never double-buy."""
    coid = client_order_id("t1", "A_BUY", 1)
    req = OrderRequest(coid, "BTCUSDT", Side.BUY, QTY, D("100200"), TimeInForce.IOC, SPOT)
    await world.acc_a.place_order(req)
    result = await world.engine().start(world.plan())
    assert result.outcome is Outcome.SUCCESS
    assert len(world.acc_a._orders) == 1 and world.acc_a.duplicate_rejections == 1
    assert world.acc_a.balance(SPOT, "BTC") == D("0.003996")


# ---- recovery limits ----------------------------------------


async def test_recovery_holds_when_there_is_no_ask_to_price_the_shortfall(world: World) -> None:
    world.b.add_fault(Op.PLACE_ORDER, Fault(FaultKind.REJECT))
    drain = drain_inventory(world)

    def hook(name: str) -> None:
        drain(name)
        if name == "state:RECOVERY":
            world.b.set_book("BTCUSDT", [(D(100900), D(10))], [])

    result = await world.engine(checkpoint=hook).start(world.plan())
    assert result.state is TradeState.RECOVERY and result.held == "no_ask_to_price_shortfall"
    world.set_books()
    assert (await world.engine().advance("john", "t1")).outcome is Outcome.RECOVERED


async def test_recovery_holds_when_a_has_no_bid_to_sell_into(world: World) -> None:
    world.b.add_fault(Op.PLACE_ORDER, Fault(FaultKind.REJECT))
    hook = on("state:RECOVERY", lambda: world.a.set_book("BTCUSDT", [], [(D(100000), D(10))]))
    result = await world.engine(checkpoint=hook).start(world.plan())
    assert result.state is TradeState.RECOVERY and result.held == "surplus_not_sold"
    world.set_books()
    assert (await world.engine().advance("john", "t1")).outcome is Outcome.RECOVERED
    await assert_loan_closed(world)


async def test_surplus_on_a_that_cannot_be_sold_at_any_protected_price_holds(world: World) -> None:
    world.b.add_fault(Op.PLACE_ORDER, Fault(FaultKind.REJECT))

    def falling_bid(name: str) -> None:
        if name.startswith("intent:A_UNWIND:"):
            best = world.a._snapshot("BTCUSDT", 1).bids[0].price
            world.a.set_book("BTCUSDT", [(best * D("0.95"), D(10))], [(D(100000), D(10))])

    result = await world.engine(checkpoint=falling_bid).start(world.plan())
    assert result.state is TradeState.RECOVERY and result.held == "surplus_not_sold"
    unwinds = [x for x in await world.ledger.legs("john", "t1") if x.leg == "A_UNWIND"]
    assert len(unwinds) == 4 and all(x.status is LegStatus.EXPIRED for x in unwinds)
    assert AlertKind.COVER_ESCALATION_EXHAUSTED in world.alerts.kinds()


async def test_interest_purchase_that_cannot_fill_holds_in_recovery(world: World) -> None:
    world.b.add_fault(Op.PLACE_ORDER, Fault(FaultKind.REJECT))
    drain = drain_inventory(world)

    def hook(name: str) -> None:
        drain(name)
        if name.startswith("intent:B_REPAY_BUY:"):
            best = world.b._snapshot("BTCUSDT", 1).asks[0].price
            world.b.set_book("BTCUSDT", [(D(100900), D(10))], [(best * D("1.05"), D(10))])

    result = await world.engine(checkpoint=hook).start(world.plan())
    assert result.state is TradeState.RECOVERY and result.held == "shortfall_not_funded"
    assert (await world.acc_b.get_loan("BTC")).outstanding > 0


async def test_repay_stage_with_no_loan_row_is_a_noop(world: World) -> None:
    await world.ledger.create_trade_limited(world.plan().new_trade_row(), world.plan().to_json(), 1)
    ctx = TradeCtx(world.deps(), world.plan(), Gateway(world.deps()))
    assert await repay_stage(ctx) is None


async def test_settling_with_an_unmatched_position_goes_to_recovery_instead(world: World) -> None:
    """Defensive: if SETTLING ever finds exposure (A bought more than B sold), it recovers rather than repays."""
    plan = world.plan()
    row = replace(plan.new_trade_row(), state="SETTLING")
    await world.ledger.create_trade_limited(row, plan.to_json(), 1)
    led = world.ledger
    await led.open_loan("john", "t1", "bitget", "BTC", D(0))
    for name, qty in (("A_BUY", D("0.004")), ("B_SELL", D("0.003"))):
        leg = await led.record_leg_intent("john", "t1", name, 1, qty)
        await led.update_leg(
            "john", leg.client_order_id, LegStatus.FILLED,
            filled_qty=qty, avg_price=D(100000), fee=D(0), fee_asset="BTC",
        )  # fmt: skip
    result = await world.engine().advance("john", "t1")
    assert result.held is not None or result.finished
    history = [to for _, to, _ in await led.state_history("john", "t1")]
    assert "RECOVERY" in history


# ---- many clients at once ----------------------------------------


async def test_several_clients_trade_into_the_same_books_without_mixing(ledger: Any) -> None:
    world = build_world(ledger)
    world.set_books(depth="10")
    clients = ["c1", "c2", "c3"]
    engines, plans = [], []
    for c in clients:
        a, b = world.a.account(c), world.b.account(c)
        a.fund(SPOT, "USDT", D(1000))
        b.fund(MARGIN, "USDT", D(500))
        b.fund(MARGIN, "BTC", D("0.01"))
        engines.append(world.engine(venues={"binance": a, "bitget": b}))
        plans.append(replace(world.plan(f"{c}-t"), client_id=c))
    results = await asyncio.gather(*(e.start(p) for e, p in zip(engines, plans, strict=True)))
    assert all(r == RunResult(TradeState.DONE, Outcome.SUCCESS, None) for r in results)
    for c in clients:
        assert (await world.b.account(c).get_loan("BTC")).outstanding == 0
        assert (await ledger.trade_result(c, f"{c}-t")).net == (await ledger.client_net_total(c))
        assert [e.client_id for e in await ledger.entries(c)] == [c] * len(await ledger.entries(c))
    assert (await world.a.account("c1").get_orderbook("BTCUSDT", 1)).asks[0].qty == D(10) - 3 * QTY
    assert all(a.duplicate_rejections == 0 for a in world.a._accounts.values())


# ---- small direct checks of helpers that the scenarios reach only indirectly ----------------------------------------


async def test_run_repay_is_idempotent_per_attempt(world: World) -> None:
    from oddtrue.execution.legs import run_repay

    await world.engine().start(world.plan())  # leaves a settled trade with a FILLED B_REPAY attempt 1
    ctx = TradeCtx(world.deps(), TradePlan.from_json(await world.ledger.get_plan("john", "t1")), Gateway(world.deps()))
    done = next(x for x in await world.ledger.legs("john", "t1") if x.leg == "B_REPAY")
    assert (await run_repay(ctx, 1, D("1"), D("1"), D(0))).id == done.id  # terminal: returned as is, nothing sent
    assert world.acc_b.duplicate_rejections == 0
    await world.ledger.update_leg("john", done.client_order_id, LegStatus.SENT)  # pretend we died before saving
    again = await run_repay(ctx, 1, D("1"), D("1"), D(0))
    assert again.status is LegStatus.FILLED  # resolved from the persisted outstanding, by asking the exchange
    assert world.acc_b.duplicate_rejections == 0


async def test_fund_shortfall_does_nothing_when_nothing_is_short(world: World) -> None:
    from oddtrue.recovery.recovery import _fund_shortfall

    await world.ledger.create_trade_limited(world.plan().new_trade_row(), world.plan().to_json(), 1)
    ctx = TradeCtx(world.deps(), world.plan(), Gateway(world.deps()))
    assert await _fund_shortfall(ctx) is None


async def test_ledger_refuses_unknown_or_foreign_plans_and_loans(world: World) -> None:
    from oddtrue.ledger.repository import TradeNotFound

    await world.ledger.create_trade_limited(world.plan().new_trade_row(), world.plan().to_json(), 1)
    for call in (
        world.ledger.get_plan("mary", "t1"),
        world.ledger.set_plan("mary", "t1", {}),
        world.ledger.open_loan("mary", "t1", "bitget", "BTC", D(0)),
    ):
        with pytest.raises(TradeNotFound):
            await call


async def test_system_clock_reports_real_time_and_sleeps() -> None:
    from oddtrue.execution.deps import SystemClock

    clock = SystemClock()
    before = clock.now
    await clock.sleep(0)
    assert clock.now >= before > 1_600_000_000
