"""The supervisor sweep, and the Phase 4 exit proof that every page-worthy condition in CLAUDE.md section 12 fires."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from decimal import Decimal as D

import pytest

from oddtrue.exchanges.fake import Fault, FaultKind, Op
from oddtrue.execution.states import TradeState
from oddtrue.ledger.models import Outcome
from oddtrue.observability import metrics as m
from oddtrue.reconciliation.reconciler import Verdict
from tests.world import MARGIN, SPOT, Directory, Safe, SimulatedCrash, add_client


def directory(safe: Safe, *extra: str) -> Directory:
    clients = {"john": safe.world.deps().venues}
    for name in extra:
        clients[name] = add_client(safe.world, name)
    return Directory(safe, clients)


# ---- what a sweep does ----------------------------------------


async def test_a_sweep_finishes_a_trade_whose_process_died(safe: Safe) -> None:
    world, d = safe.world, directory(safe)

    def die(name: str) -> None:
        if name == "loan_opened":
            raise SimulatedCrash(name)

    with pytest.raises(SimulatedCrash):
        await safe.engine(checkpoint=die).start(world.plan("t1"))
    report = await d.supervisor().sweep()
    result = report.resumed["t1"]
    # a restarted trade never sends a fresh entry order from a stale revalidation: nothing happened, loan verified 0
    assert result.finished and result.outcome is Outcome.FAILED
    assert report.reconciled == {"john": Verdict.OK} and report.loans_open == 0  # baselined before the crash
    assert (await world.acc_b.get_loan("BTC")).outstanding == 0


async def test_idle_clients_are_baselined_then_checked_every_sweep(safe: Safe) -> None:
    d = directory(safe, "mary")
    sup = d.supervisor()
    assert (await sup.sweep()).reconciled == {"john": Verdict.BASELINE, "mary": Verdict.BASELINE}
    assert (await sup.sweep()).reconciled == {"john": Verdict.OK, "mary": Verdict.OK}
    assert safe.metrics.counter(m.RECONCILIATIONS, status="OK") == 2


async def test_only_the_drifted_clients_among_many_are_frozen(safe: Safe) -> None:
    names = [f"c{i:02d}" for i in range(20)]
    d = directory(safe, *names)
    sup = d.supervisor()
    await sup.sweep()  # baseline everyone
    safe.world.a.account("c07").drift(SPOT, "USDT", D(25))
    safe.world.b.account("c13").drift(MARGIN, "BTC", D("-0.002"))
    report = await sup.sweep()
    assert {c for c, v in report.reconciled.items() if v is Verdict.MISMATCH} == {"c07", "c13"}
    assert await safe.safety.frozen_clients() == ["c07", "c13"]
    again = await sup.sweep()
    assert again.skipped_frozen == ["c07", "c13"]  # left for a human; the sweep does not "fix" a freeze
    assert len(again.reconciled) == 19  # john + 18 clean clients, the two frozen ones left out
    assert all(v is Verdict.OK for v in again.reconciled.values())
    assert safe.metrics.gauge(m.CLIENTS_FROZEN) == 2


async def test_a_parked_trade_is_resumed_each_sweep_until_it_finishes(safe: Safe) -> None:
    world, d = safe.world, directory(safe)
    world.b.add_fault(Op.REPAY, Fault(FaultKind.REJECT, times=100))
    parked = await safe.engine().start(world.plan("t1"))
    assert parked.state is TradeState.RECOVERY
    sup = d.supervisor()
    assert not (await sup.sweep()).resumed["t1"].finished
    world.b.clear_faults()
    final = (await sup.sweep()).resumed["t1"]
    assert final.finished and final.outcome is Outcome.RECOVERED
    assert (await sup.sweep()).resumed == {}


async def test_the_run_loop_survives_a_failing_sweep_and_stops_on_request(
    safe: Safe, monkeypatch: pytest.MonkeyPatch
) -> None:
    sup = directory(safe).supervisor()
    stop, calls = asyncio.Event(), []
    real = sup.sweep

    async def flaky() -> object:
        calls.append(1)
        if len(calls) == 1:
            raise ConnectionError("db hiccup")
        if len(calls) == 3:
            stop.set()
        return await real()

    monkeypatch.setattr(sup, "sweep", flaky)
    await asyncio.wait_for(sup.run(0.01, stop), timeout=10)
    assert len(calls) == 3  # kept going after the failure, and stopped when asked


# ---- Phase 4 exit: every page-worthy condition in CLAUDE.md section 12 fires ----------------------------------------

Scenario = Callable[[Safe], Awaitable[None]]


async def _loan_not_zero_after_recovery_timeout(safe: Safe) -> None:
    safe.world.b.add_fault(Op.REPAY, Fault(FaultKind.REJECT, times=100))
    await safe.engine().start(safe.world.plan("t1"))
    safe.world.clock.advance(safe.world.cfg.recovery_timeout_s + 1)
    await safe.engine().advance("john", "t1")


async def _reconciliation_mismatch(safe: Safe) -> None:
    await safe.engine().start(safe.world.plan("t1"))
    safe.world.acc_a.drift(SPOT, "USDT", D(5))
    await safe.engine().start(safe.world.plan("t2"))


async def _unknown_outcome_unresolved(safe: Safe) -> None:
    safe.world.a.add_fault(Op.PLACE_ORDER, Fault(FaultKind.TIMEOUT_APPLIED))
    safe.world.a.add_fault(Op.GET_ORDER, Fault(FaultKind.EXCHANGE_DOWN, times=6))
    await safe.engine().start(safe.world.plan("t1"))


async def _naked_position(safe: Safe) -> None:
    safe.world.a.add_fault(Op.PLACE_ORDER, Fault(FaultKind.REJECT))
    await safe.engine().start(safe.world.plan("t1"))


async def _exchange_down_with_trades_in_flight(safe: Safe) -> None:
    safe.world.a.add_fault(Op.PLACE_ORDER, Fault(FaultKind.TIMEOUT_APPLIED))
    safe.world.a.add_fault(Op.GET_ORDER, Fault(FaultKind.EXCHANGE_DOWN, times=100))  # a real outage, not a blip
    await safe.engine().start(safe.world.plan("t1"))
    await directory(safe).supervisor().sweep()


async def _reserve_below_minimum(safe: Safe) -> None:
    safe.world.acc_b.drift(MARGIN, "USDT", D(-495))  # 5 USDT left; the minimum is 10
    await directory(safe).supervisor().sweep()


async def _loan_open_too_long(safe: Safe) -> None:
    safe.world.b.add_fault(Op.REPAY, Fault(FaultKind.REJECT, times=100))
    await safe.engine().start(safe.world.plan("t1"))
    safe.world.clock.advance(safe.world.cfg.loan_open_alert_s + 1)
    await directory(safe).supervisor().sweep()


PAGE_WORTHY: dict[str, Scenario] = {
    "loan_not_zero_after_recovery_timeout": _loan_not_zero_after_recovery_timeout,
    "reconciliation_mismatch": _reconciliation_mismatch,
    "unknown_outcome_unresolved": _unknown_outcome_unresolved,
    "naked_position": _naked_position,
    "exchange_down_in_flight": _exchange_down_with_trades_in_flight,
    "reserve_below_minimum": _reserve_below_minimum,
    "loan_open_too_long": _loan_open_too_long,
}


@pytest.mark.parametrize("kind", list(PAGE_WORTHY))
async def test_each_page_worthy_condition_pages_and_is_recorded(safe: Safe, kind: str) -> None:
    await PAGE_WORTHY[kind](safe)
    assert kind in safe.kinds(), f"{kind} never raised; got {safe.kinds()}"
    assert any(a.kind.value == kind for a in safe.channel.paged), f"{kind} raised but nobody was paged"
    persisted = await safe.safety.alerts(kind=kind)
    assert persisted and all(r.severity == "PAGE" for r in persisted)
    assert safe.metrics.counter(m.ALERTS, kind=kind, severity="PAGE") >= 1


async def test_an_exchange_down_alert_names_the_trade_and_both_exchanges(safe: Safe) -> None:
    await _exchange_down_with_trades_in_flight(safe)
    alert = next(a for a in safe.channel.paged if a.kind.value == "exchange_down_in_flight")
    assert alert.trade_id == "t1" and alert.client_id == "john"
    assert "binance" in alert.detail and "bitget" in alert.detail


async def test_loan_age_gauges_follow_the_oldest_open_loan(safe: Safe) -> None:
    await _loan_open_too_long(safe)
    assert safe.metrics.gauge(m.LOANS_OPEN) == 1
    assert safe.metrics.gauge(m.LOAN_OLDEST_AGE) > safe.world.cfg.loan_open_alert_s
    safe.world.b.clear_faults()
    await safe.engine().advance("john", "t1")
    await directory(safe).supervisor().sweep()
    assert safe.metrics.gauge(m.LOANS_OPEN) == 0 and safe.metrics.gauge(m.LOAN_OLDEST_AGE) == 0


async def test_a_healthy_world_raises_no_page(safe: Safe) -> None:
    assert (await safe.engine().start(safe.world.plan("t1"))).outcome is Outcome.SUCCESS
    await directory(safe).supervisor().sweep()
    assert [a for a in safe.hub.alerts if a.page] == [] and safe.channel.paged == []
    assert await safe.safety.alerts() == []
