"""Unknown outcomes, hard timeouts, repay failures, exchange outages and reconciliation mismatches (rules 2, 3, 7, 8).

The invariant behind every test here: an action that MAY have happened is resolved by querying by client id. It is
never sent a second time, so the exchange never sees a duplicate and we never double-borrow or double-buy.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal as D

from oddtrue.exchanges.fake import Fault, FaultKind, Op
from oddtrue.execution.deps import AlertKind
from oddtrue.execution.engine import RunResult
from oddtrue.execution.states import TradeState
from oddtrue.ledger.models import LegStatus, Outcome
from tests.test_engine_recovery import assert_loan_closed, legs_by_name
from tests.world import MARGIN, SPOT, World


def order_count(world: World, side_a: bool = True) -> int:
    return len((world.acc_a if side_a else world.acc_b)._orders)


# ---- a timeout that actually filled is NOT a failure ----------------------------------------


async def test_a_buy_times_out_but_filled_is_found_by_query_and_not_resent(world: World) -> None:
    world.a.add_fault(Op.PLACE_ORDER, Fault(FaultKind.TIMEOUT_APPLIED))
    result = await world.engine().start(world.plan())
    assert result == RunResult(TradeState.DONE, Outcome.SUCCESS, None)
    assert order_count(world) == 1  # one A order exists on the exchange, ever
    assert world.acc_a.balance(SPOT, "BTC") == D("0.003996")  # bought once, not twice
    await assert_loan_closed(world)


async def test_b_sell_times_out_but_filled_is_found_by_query_and_not_resent(world: World) -> None:
    world.b.add_fault(Op.PLACE_ORDER, Fault(FaultKind.TIMEOUT_APPLIED))
    result = await world.engine().start(world.plan())
    assert result.outcome is Outcome.SUCCESS
    assert order_count(world, side_a=False) == 1
    assert world.acc_b.balance(MARGIN, "USDT") == D(500) + D("403.6") - D("0.4036")
    await assert_loan_closed(world)


async def test_borrow_times_out_but_applied_is_found_by_loan_not_repeated(world: World) -> None:
    world.b.add_fault(Op.BORROW, Fault(FaultKind.TIMEOUT_APPLIED))
    pool_before = world.b._borrow_pool["BTC"]
    result = await world.engine().start(world.plan())
    assert result.outcome is Outcome.SUCCESS
    assert world.b._borrow_pool["BTC"] == pool_before  # borrowed 0.004 once and repaid it, never 0.008
    row = (await world.ledger.loans("john", "t1"))[0]
    assert row.borrowed_qty == D("0.004") and row.verified_zero_at is not None
    await assert_loan_closed(world)


async def test_repay_times_out_but_applied_is_found_by_loan(world: World) -> None:
    world.b.add_fault(Op.REPAY, Fault(FaultKind.TIMEOUT_APPLIED))
    result = await world.engine().start(world.plan())
    assert result.outcome is Outcome.SUCCESS
    assert (await legs_by_name(world))["B_REPAY"] == [LegStatus.FILLED]  # one repay, not two
    await assert_loan_closed(world)


# ---- a timeout that did NOT happen: still never blindly retried ----------------------------------------


async def test_a_buy_timeout_not_applied_is_deemed_unplaced_after_queries_and_the_short_is_covered(
    world: World,
) -> None:
    world.a.add_fault(Op.PLACE_ORDER, Fault(FaultKind.TIMEOUT_NOT_APPLIED))
    t0 = world.clock.now
    result = await world.engine().start(world.plan())
    assert result.outcome is Outcome.RECOVERED
    legs = await legs_by_name(world)
    assert legs["A_BUY"] == [LegStatus.REJECTED] and legs["B_COVER"] == [LegStatus.FILLED]
    leg = next(x for x in await world.ledger.legs("john", "t1") if x.leg == "A_BUY")
    assert leg.meta["reason"] == "never_placed" and order_count(world) == 0  # not retried
    assert world.clock.now - t0 >= 2 * float(world.cfg.unknown_resolve_backoff_s)  # it waited between queries
    await assert_loan_closed(world)


async def test_borrow_timeout_not_applied_means_no_loan_and_a_is_unwound(world: World) -> None:
    world.b.add_fault(Op.BORROW, Fault(FaultKind.TIMEOUT_NOT_APPLIED))
    pool_before = world.b._borrow_pool["BTC"]
    result = await world.engine().start(world.plan())
    assert result.outcome is Outcome.RECOVERED
    legs = await legs_by_name(world)
    assert legs["B_BORROW"] == [LegStatus.REJECTED] and "B_SELL" not in legs and "A_UNWIND" in legs
    assert world.b._borrow_pool["BTC"] == pool_before
    await assert_loan_closed(world)


async def test_sell_timeout_not_applied_unwinds_a_and_repays(world: World) -> None:
    world.b.add_fault(Op.PLACE_ORDER, Fault(FaultKind.TIMEOUT_NOT_APPLIED))
    result = await world.engine().start(world.plan())
    assert result.outcome is Outcome.RECOVERED
    assert (await legs_by_name(world))["B_SELL"] == [LegStatus.REJECTED]
    await assert_loan_closed(world)


async def test_repay_timeout_not_applied_is_repaid_under_a_new_attempt_id(world: World) -> None:
    world.b.add_fault(Op.REPAY, Fault(FaultKind.TIMEOUT_NOT_APPLIED))
    result = await world.engine().start(world.plan())
    assert result.outcome is Outcome.SUCCESS
    repays = [x for x in await world.ledger.legs("john", "t1") if x.leg == "B_REPAY"]
    assert [x.status for x in repays] == [LegStatus.REJECTED, LegStatus.FILLED]
    assert repays[0].client_order_id != repays[1].client_order_id and [x.attempt for x in repays] == [1, 2]
    await assert_loan_closed(world)


async def test_repay_rejected_then_retried_succeeds(world: World) -> None:
    world.b.add_fault(Op.REPAY, Fault(FaultKind.REJECT))
    result = await world.engine().start(world.plan())
    assert result.outcome is Outcome.SUCCESS
    assert (await legs_by_name(world))["B_REPAY"] == [LegStatus.REJECTED, LegStatus.FILLED]
    await assert_loan_closed(world)


async def test_repay_that_keeps_failing_stays_in_recovery_with_the_loan_open_and_alerts(world: World) -> None:
    world.b.add_fault(Op.REPAY, Fault(FaultKind.REJECT, times=100))
    result = await world.engine().start(world.plan())
    assert result.state is TradeState.RECOVERY and (result.held or "").startswith("loan_not_zero")
    assert (await world.acc_b.get_loan("BTC")).outstanding > 0
    assert not any(r.verified_zero_at for r in await world.ledger.loans("john", "t1"))  # never claimed closed
    world.clock.advance(world.cfg.recovery_timeout_s + 1)
    await world.engine().advance("john", "t1")
    assert AlertKind.LOAN_NOT_ZERO_AFTER_RECOVERY_TIMEOUT in world.alerts.kinds()
    world.b.clear_faults()
    done = await world.engine().advance("john", "t1")
    assert done.outcome is Outcome.RECOVERED  # settlement slipped into recovery, so it is recorded as such
    await assert_loan_closed(world)


# ---- hard time limits (spec 37): a call that never returns ----------------------------------------


async def test_a_call_that_hangs_after_filling_hits_the_hard_limit_and_is_resolved(world: World) -> None:
    world.a.add_fault(Op.PLACE_ORDER, Fault(FaultKind.HANG_APPLIED))
    result = await world.engine(op_timeout_override_s=0.05).start(world.plan())
    assert result.outcome is Outcome.SUCCESS and order_count(world) == 1
    await assert_loan_closed(world)


async def test_a_borrow_that_hangs_without_applying_is_treated_as_no_loan(world: World) -> None:
    world.b.add_fault(Op.BORROW, Fault(FaultKind.HANG_NOT_APPLIED))
    result = await world.engine(op_timeout_override_s=0.05).start(world.plan())
    assert result.outcome is Outcome.RECOVERED
    assert (await legs_by_name(world))["B_BORROW"] == [LegStatus.REJECTED]
    await assert_loan_closed(world)


async def test_a_read_that_hangs_cancels_the_trade_before_anything_is_sent(world: World) -> None:
    world.b.add_fault(Op.GET_ORDERBOOK, Fault(FaultKind.HANG_NOT_APPLIED, times=3))
    result = await world.engine(op_timeout_override_s=0.05).start(world.plan())
    assert result.state is TradeState.CANCELLED
    assert await world.ledger.legs("john", "t1") == []


# ---- ambiguity that cannot be resolved: hold, alert, never guess ----------------------------------------


async def test_unresolvable_outcome_holds_the_trade_and_resumes_when_the_exchange_answers(world: World) -> None:
    world.a.add_fault(Op.PLACE_ORDER, Fault(FaultKind.TIMEOUT_APPLIED))
    world.a.add_fault(Op.GET_ORDER, Fault(FaultKind.EXCHANGE_DOWN, times=6))  # two full rounds of status queries fail
    result = await world.engine().start(world.plan())
    assert result.state is TradeState.EXECUTING and (result.held or "").startswith("unresolved")
    assert AlertKind.UNKNOWN_OUTCOME_UNRESOLVED in world.alerts.kinds()
    assert (await legs_by_name(world))["A_BUY"] == [LegStatus.UNKNOWN]
    assert order_count(world) == 1  # we did not guess and re-send
    done = await world.engine().advance("john", "t1")  # the exchange is back
    assert done == RunResult(TradeState.DONE, Outcome.SUCCESS, None)
    assert order_count(world) == 1
    await assert_loan_closed(world)


async def test_exchange_down_while_settling_holds_then_finishes(world: World) -> None:
    def outage(name: str) -> None:
        if name == "state:SETTLING":
            world.b.add_fault(Op.GET_LOAN, Fault(FaultKind.EXCHANGE_DOWN, times=3))

    result = await world.engine(checkpoint=outage).start(world.plan())
    assert result.state is TradeState.SETTLING and (result.held or "").startswith("unresolved")
    done = await world.engine().advance("john", "t1")
    assert done.outcome is Outcome.SUCCESS
    await assert_loan_closed(world)


async def test_rate_limit_during_the_burst_is_waited_out_not_hammered(world: World) -> None:
    world.b.add_fault(Op.GET_LOAN, Fault(FaultKind.RATE_LIMIT, times=2))
    t0 = world.clock.now
    result = await world.engine().start(world.plan())
    assert result.outcome is Outcome.SUCCESS
    assert world.clock.now > t0  # it slept (retry-after) between attempts
    await assert_loan_closed(world)


async def test_auth_failure_before_anything_is_sent_cancels_with_an_incident(world: World) -> None:
    world.b.add_fault(Op.GET_LOAN, Fault(FaultKind.AUTH_ERROR, times=1))
    result = await world.engine().start(world.plan())
    assert result.state is TradeState.CANCELLED and AlertKind.AUTH_INCIDENT in world.alerts.kinds()
    assert await world.ledger.legs("john", "t1") == []


async def test_auth_failure_mid_trade_raises_an_incident_and_holds(world: World) -> None:
    def revoked(name: str) -> None:
        if name == "state:SETTLING":
            world.b.add_fault(Op.GET_LOAN, Fault(FaultKind.AUTH_ERROR, times=1))

    result = await world.engine(checkpoint=revoked).start(world.plan())
    assert result.state is TradeState.SETTLING and result.held == "auth_error"
    assert AlertKind.AUTH_INCIDENT in world.alerts.kinds()
    done = await world.engine().advance("john", "t1")  # keys fixed
    assert done.outcome is Outcome.SUCCESS


async def test_an_order_that_gets_an_auth_error_is_recorded_and_the_trade_recovers(world: World) -> None:
    world.a.add_fault(Op.PLACE_ORDER, Fault(FaultKind.AUTH_ERROR))
    result = await world.engine().start(world.plan())
    assert result.outcome is Outcome.RECOVERED
    assert AlertKind.AUTH_INCIDENT in world.alerts.kinds()
    await assert_loan_closed(world)


# ---- reconciliation: exchange state is the truth, ledger is the expectation (rule 7) ----------------------


async def test_reconciliation_mismatch_holds_the_trade_and_alerts(world: World) -> None:
    def tamper(name: str) -> None:
        if name == "state:RECONCILING":  # the exchange's record of the A buy differs from what we booked
            order = next(iter(world.acc_a._orders.values()))
            world.acc_a._orders[order.client_order_id] = replace(order, filled_qty=order.filled_qty - D("0.001"))

    result = await world.engine(checkpoint=tamper).start(world.plan())
    assert result.state is TradeState.RECONCILING and result.held == "reconciliation_mismatch"
    assert AlertKind.RECONCILIATION_MISMATCH in world.alerts.kinds()
    assert not result.finished
    assert (await world.ledger.get_trade("john", "t1")).outcome is None  # not DONE until resolved


async def test_a_loan_that_appears_late_is_caught_before_the_trade_can_finish(world: World) -> None:
    """Borrow outcome was judged 'no loan', then the borrow lands anyway: reconcile finds it and recovers."""
    world.b.add_fault(Op.BORROW, Fault(FaultKind.TIMEOUT_NOT_APPLIED))
    landed: list[int] = []

    def late_borrow(name: str) -> None:
        if name == "state:RECONCILING" and not landed:
            landed.append(1)
            st = world.acc_b._loans.setdefault("BTC", __import__("oddtrue.exchanges.fake", fromlist=["x"])._LoanState())
            st.principal = D("0.004")
            st.opened_ts, st.hours_charged = world.clock.now, 1
            world.acc_b.fund(MARGIN, "BTC", D("0.004"))

    result = await world.engine(checkpoint=late_borrow).start(world.plan())
    assert result.state is TradeState.DONE
    assert "RECOVERY" in [to for _, to, _ in await world.ledger.state_history("john", "t1")][5:]
    await assert_loan_closed(world)
