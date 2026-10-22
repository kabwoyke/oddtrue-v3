"""Reconciliation and freezing (CLAUDE.md rules 6-7). Phase 4 exit: a mismatch freezes only the affected client."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal as D

import pytest

from oddtrue.exchanges.fake import Fault, FaultKind, Op
from oddtrue.exchanges.models import AccountType
from oddtrue.execution.engine import RunResult, StartRefused
from oddtrue.execution.states import TradeState
from oddtrue.ledger.models import LegStatus, Outcome
from oddtrue.ledger.safety import ReconStatus
from oddtrue.observability import metrics as m
from oddtrue.reconciliation.reconciler import Verdict
from tests.test_engine_crash import SCENARIOS
from tests.world import MARGIN, SPOT, Safe, add_client


async def statuses(safe: Safe, client: str = "john") -> list[tuple[str, str]]:
    return [(r.trigger, r.status.value) for r in await safe.safety.reconciliations(client)]


# ---- a clean world reconciles clean, after every kind of trade ----------------------------------------


async def test_first_trade_baselines_then_each_trade_is_checked_before_and_after(safe: Safe) -> None:
    first = await safe.engine().start(safe.world.plan("t1"))
    assert first == RunResult(TradeState.DONE, Outcome.SUCCESS, None)
    assert await statuses(safe) == [("pre_trade", "BASELINE"), ("post_trade", "OK")]
    second = await safe.engine().start(safe.world.plan("t2"))
    assert second.outcome is Outcome.SUCCESS
    assert (await statuses(safe))[2:] == [("pre_trade", "OK"), ("post_trade", "OK")]
    assert await safe.safety.frozen_clients() == []
    assert safe.metrics.counter(m.RECONCILIATIONS, status="OK") == 3


@pytest.mark.parametrize("scenario", list(SCENARIOS))
async def test_expected_balances_match_the_exchange_after_every_outcome(safe: Safe, scenario: str) -> None:
    """The ledger's expectation (baseline + settled legs) equals the fake exchange to the last satoshi, fees included,
    whether the trade succeeded or went through any recovery path."""
    SCENARIOS[scenario](safe.world)
    result = await safe.engine().start(safe.world.plan("t1"))
    assert result.finished and result.outcome is not None
    assert await statuses(safe) == [("pre_trade", "BASELINE"), ("post_trade", "OK")], scenario
    last = (await safe.safety.reconciliations("john"))[-1]
    assert last.diff == [] and last.expected == last.actual
    assert await safe.safety.frozen_clients() == []


# ---- an induced mismatch freezes only the affected client ----------------------------------------


async def test_drift_between_trades_freezes_that_client_and_no_other(safe: Safe) -> None:
    world = safe.world
    mary = add_client(world, "mary")
    assert (await safe.engine().start(world.plan("t1"))).outcome is Outcome.SUCCESS
    world.acc_a.drift(SPOT, "USDT", D(50))  # funds appear that the ledger knows nothing about

    cancelled = await safe.engine().start(world.plan("t2"))
    assert cancelled.state is TradeState.CANCELLED
    assert (await world.ledger.state_history("john", "t2"))[-1][2] == "client_frozen:balance_mismatch"
    assert await world.ledger.legs("john", "t2") == []  # no money was put at risk

    freeze = await safe.safety.active_freeze("john")
    assert freeze is not None and "binance/spot/USDT: expected 600 actual 650" in freeze.reason
    recon = (await safe.safety.reconciliations("john"))[-1]
    assert recon.status is ReconStatus.MISMATCH and recon.diff[0]["delta"] == "50"

    # mary is untouched and trades normally, concurrently with john being frozen
    ok = await safe.engine(venues=mary).start(replace(world.plan("m1"), client_id="mary"))
    assert ok.outcome is Outcome.SUCCESS
    assert await safe.safety.frozen_clients() == ["john"]
    assert await safe.safety.active_freeze("mary") is None

    # john cannot start anything while frozen - enforced by the database, not by a flag we might forget to check
    with pytest.raises(StartRefused, match="client_frozen"):
        await safe.engine().start(world.plan("t3"))

    # alerts, audit trail and metrics all recorded it
    assert "reconciliation_mismatch" in safe.kinds() and "client_frozen" in safe.kinds()
    assert any(a.kind.value == "reconciliation_mismatch" and a.client_id == "john" for a in safe.channel.paged)
    audit = await safe.safety.audit_entries("client.frozen", "john")
    assert len(audit) == 1 and audit[0]["actor"] == "system:reconciler"
    assert safe.metrics.counter(m.RECONCILIATION_MISMATCH) == 1 and safe.metrics.gauge(m.CLIENTS_FROZEN) == 1


async def test_a_manual_action_on_the_exchange_is_a_mismatch_too(safe: Safe) -> None:
    await safe.engine().start(safe.world.plan("t1"))
    safe.world.acc_a.fund(SPOT, "DOGE", D(5))  # an asset the ledger has never heard of
    result = await safe.reconciler.reconcile("john", safe.world.deps().venues, trigger="periodic")
    assert result.verdict is Verdict.MISMATCH
    assert [(d.exchange, d.section, d.asset) for d in result.differences] == [("binance", SPOT, "DOGE")]


async def test_a_loan_that_appears_by_itself_is_a_mismatch(safe: Safe) -> None:
    await safe.engine().start(safe.world.plan("t1"))
    await safe.world.acc_b.borrow("BTC", D("0.001"), "client-did-this")
    result = await safe.reconciler.reconcile("john", safe.world.deps().venues, trigger="periodic")
    assert result.verdict is Verdict.MISMATCH
    sections = {d.section for d in result.differences}
    assert "liabilities" in sections


async def test_drift_during_a_trade_parks_it_and_freezes_the_client(safe: Safe) -> None:
    world = safe.world

    def drift(name: str) -> None:
        if name == "state:RECONCILING":
            world.acc_b.drift(MARGIN, "BTC", D("0.001"))

    result = await safe.engine(checkpoint=drift).start(world.plan("t1"))
    assert result.state is TradeState.RECONCILING and result.held == "balance_mismatch" and not result.finished
    assert (await world.ledger.get_trade("john", "t1")).outcome is None  # not DONE until a human resolves it
    assert await safe.safety.frozen_clients() == ["john"]
    with pytest.raises(StartRefused):
        await safe.engine().start(world.plan("t2"))


async def test_a_mismatch_found_by_the_per_leg_check_also_freezes(safe: Safe) -> None:
    from dataclasses import replace as rep

    world = safe.world

    def tamper(name: str) -> None:
        if name == "state:RECONCILING":
            order = next(iter(world.acc_a._orders.values()))
            world.acc_a._orders[order.client_order_id] = rep(order, filled_qty=order.filled_qty - D("0.001"))

    result = await safe.engine(checkpoint=tamper).start(world.plan("t1"))
    assert result.held == "reconciliation_mismatch"
    freeze = await safe.safety.active_freeze("john")
    assert freeze is not None and freeze.reason.startswith("leg_mismatch")


# ---- resolving a freeze ----------------------------------------


async def _frozen_by_drift(safe: Safe) -> None:
    await safe.engine().start(safe.world.plan("t1"))
    safe.world.acc_a.drift(SPOT, "USDT", D(50))
    await safe.engine().start(safe.world.plan("t2"))
    assert await safe.safety.frozen_clients() == ["john"]


async def test_recheck_lifts_the_freeze_once_the_exchange_matches_again(safe: Safe) -> None:
    await _frozen_by_drift(safe)
    venues = safe.world.deps().venues
    still = await safe.reconciler.recheck("john", venues, "ops:alice")
    assert still.verdict is Verdict.MISMATCH and await safe.safety.frozen_clients() == ["john"]
    first_freeze = await safe.safety.active_freeze("john")
    safe.world.acc_a.drift(SPOT, "USDT", D(-50))  # the client moved the money back
    clean = await safe.reconciler.recheck("john", venues, "ops:alice")
    assert clean.verdict is Verdict.OK and await safe.safety.frozen_clients() == []
    assert (
        first_freeze is not None
        and (await safe.safety.audit_entries("client.unfrozen", "john"))[0]["actor"] == "ops:alice"
    )
    assert (await safe.engine().start(safe.world.plan("t3"))).outcome is Outcome.SUCCESS


async def test_an_operator_can_accept_the_exchange_state_with_a_note(safe: Safe) -> None:
    await _frozen_by_drift(safe)
    venues = safe.world.deps().venues
    await safe.reconciler.accept_exchange_state("john", venues, "ops:alice", "client deposited 50 USDT by hand")
    assert await safe.safety.frozen_clients() == []
    row = (await safe.safety.reconciliations("john"))[-1]
    assert row.status is ReconStatus.ACCEPTED and row.trigger == "accepted_by:ops:alice"
    accepted = await safe.safety.audit_entries("reconciliation.accepted", "john")
    assert (
        accepted[0]["detail"]["note"].startswith("client deposited")
        and "balance_mismatch" in accepted[0]["detail"]["froze_because"]
    )
    # the new baseline is the exchange's state, so the next trade reconciles cleanly against it
    assert (await safe.engine().start(safe.world.plan("t3"))).outcome is Outcome.SUCCESS
    assert (await statuses(safe))[-1] == ("post_trade", "OK")


async def test_accepting_requires_a_note_and_a_frozen_client(safe: Safe) -> None:
    venues = safe.world.deps().venues
    with pytest.raises(ValueError, match="note"):
        await safe.reconciler.accept_exchange_state("john", venues, "ops:alice", "   ")
    with pytest.raises(ValueError, match="not frozen"):
        await safe.reconciler.accept_exchange_state("john", venues, "ops:alice", "because")


async def test_a_parked_trade_finishes_after_the_operator_resolves_the_freeze(safe: Safe) -> None:
    world = safe.world

    def drift(name: str) -> None:
        if name == "state:RECONCILING":
            world.acc_b.drift(MARGIN, "BTC", D("0.001"))

    parked = await safe.engine(checkpoint=drift).start(world.plan("t1"))
    assert parked.held == "balance_mismatch"
    await safe.reconciler.accept_exchange_state("john", world.deps().venues, "ops:alice", "client topped up inventory")
    done = await safe.engine().advance("john", "t1")
    assert done == RunResult(TradeState.DONE, Outcome.SUCCESS, None)


async def test_freezing_twice_is_one_incident(safe: Safe) -> None:
    await _frozen_by_drift(safe)
    await safe.reconciler.freeze_client("john", "second report of the same thing")
    assert len(await safe.safety.audit_entries("client.frozen", "john")) == 1
    assert await safe.safety.frozen_clients() == ["john"]


# ---- not a moment to compare, or no way to read ----------------------------------------


async def test_unreadable_exchanges_cancel_the_trade_but_do_not_freeze_the_client(safe: Safe) -> None:
    safe.world.a.add_fault(Op.GET_BALANCES, Fault(FaultKind.EXCHANGE_DOWN, times=3))
    result = await safe.engine().start(safe.world.plan("t1"))
    assert result.state is TradeState.CANCELLED
    assert "reconcile_unavailable" in (await safe.world.ledger.state_history("john", "t1"))[-1][2]  # type: ignore[operator]
    assert await safe.safety.frozen_clients() == []  # we know nothing, so we conclude nothing
    assert "reconciliation_unavailable" in safe.kinds()


async def test_unreadable_exchanges_after_a_trade_park_it_until_they_answer(safe: Safe) -> None:
    world = safe.world

    def outage(name: str) -> None:
        if name == "state:RECONCILING":
            world.a.add_fault(Op.GET_BALANCES, Fault(FaultKind.EXCHANGE_DOWN, times=3))

    parked = await safe.engine(checkpoint=outage).start(world.plan("t1"))
    assert parked.held == "reconcile_unavailable" and await safe.safety.frozen_clients() == []
    done = await safe.engine().advance("john", "t1")
    assert done.outcome is Outcome.SUCCESS


async def test_reconciliation_waits_while_another_trade_or_an_unknown_leg_exists(safe: Safe) -> None:
    world, venues = safe.world, safe.world.deps().venues
    await world.ledger.create_trade_limited(world.plan("t0").new_trade_row(), world.plan("t0").to_json(), 2)
    deferred = await safe.reconciler.reconcile("john", venues, trigger="periodic")
    assert deferred.verdict is Verdict.DEFERRED and "t0" in deferred.detail
    await world.ledger.record_leg_intent("john", "t0", "A_BUY", 1, D("0.004"))  # sent, outcome not known yet
    assert (await safe.reconciler.reconcile("john", venues, trigger="periodic", trade_id="t0")).detail == (
        "legs with unknown outcome"
    )
    assert await safe.safety.frozen_clients() == []


async def test_activity_during_the_snapshot_defers_instead_of_guessing(
    safe: Safe, monkeypatch: pytest.MonkeyPatch
) -> None:
    world, venues = safe.world, safe.world.deps().venues
    await world.ledger.create_trade_limited(world.plan("t0").new_trade_row(), world.plan("t0").to_json(), 2)
    real = safe.reconciler.take_snapshot

    async def snapshot_then_a_leg_appears(v: object) -> object:
        snap = await real(v)  # type: ignore[arg-type]
        await world.ledger.record_leg_intent("john", "t0", "A_BUY", 1, D("0.004"))
        return snap

    monkeypatch.setattr(safe.reconciler, "take_snapshot", snapshot_then_a_leg_appears)
    result = await safe.reconciler.reconcile("john", venues, trigger="periodic", trade_id="t0")
    assert result.verdict is Verdict.DEFERRED and result.detail == "activity during snapshot"


async def test_every_balance_is_read_on_both_exchanges_and_both_accounts(safe: Safe) -> None:
    snapshot = await safe.reconciler.take_snapshot(safe.world.deps().venues)
    assert snapshot["binance"]["spot"] == {"USDT": D(1000)}
    assert snapshot["bitget"]["margin"] == {"USDT": D(500), "BTC": D("0.01")}
    assert snapshot["bitget"]["liabilities"] == {}
    world = safe.world
    await world.acc_b.borrow("BTC", D("0.001"), "x")
    again = await safe.reconciler.take_snapshot(world.deps().venues)
    assert again["bitget"]["liabilities"]["BTC"] == D("0.001") + D("0.0000001")  # principal + first billed hour
    assert LegStatus.FILLED and AccountType.SPOT
