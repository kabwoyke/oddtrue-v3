"""Kill switches (CLAUDE.md rule 10). Phase 4 exit: halts stop NEW trades but let in-flight trades finish or recover."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from decimal import Decimal as D

import pytest

from oddtrue.exchanges.errors import AuthError, ExchangeDown, RateLimited
from oddtrue.exchanges.fake import FakeVenue, Fault, FaultKind, Op
from oddtrue.execution.engine import StartRefused
from oddtrue.execution.states import TradeState
from oddtrue.ledger.models import Outcome
from oddtrue.ledger.safety import HaltScope
from oddtrue.observability import metrics as m
from oddtrue.safety.halts import ResumeRefused
from oddtrue.safety.health import ExchangeHealth
from oddtrue.safety.instrumented import InstrumentedAdapter, instrument
from tests.world import MARGIN, RULES, SYMBOL, Safe, World, add_client


def third_exchange(world: World) -> dict[str, object]:
    """A third venue ('okx') so one exchange can be halted while trades on the other two keep flowing."""
    okx = FakeVenue("okx", world.clock)
    okx.set_rules(SYMBOL, RULES)
    okx.set_borrow("BTC", D("0.0001"), D(100))
    okx.set_book(SYMBOL, [(D(100900), D(10))], [(D(100910), D(10))])
    acc = okx.account("john")
    acc.fund(MARGIN, "USDT", D(500))
    acc.fund(MARGIN, "BTC", D("0.01"))
    return {"binance": world.acc_a, "okx": acc}


# ---- global halt ----------------------------------------


async def test_global_halt_refuses_new_trades_even_if_the_application_gate_is_bypassed(safe: Safe) -> None:
    assert await safe.halts.halt_global("exchange incident", "ops:alice") is True
    with pytest.raises(StartRefused, match="global_halt"):
        await safe.engine().start(safe.world.plan())
    # no gate at all: the database check inside trade creation still refuses (authoritative, atomic with the insert)
    with pytest.raises(StartRefused, match="global_halt"):
        await safe.world.engine().start(safe.world.plan())
    assert await safe.world.ledger.unfinished_trades() == []


async def test_a_halt_never_stops_a_trade_already_executing(safe: Safe) -> None:
    world = safe.world
    tasks: list[asyncio.Task[bool]] = []

    def halt_mid_flight(name: str) -> None:
        if name == "state:EXECUTING" and not tasks:
            tasks.append(asyncio.get_running_loop().create_task(safe.halts.halt_global("runaway", "ops:alice")))

    result = await safe.engine(checkpoint=halt_mid_flight).start(world.plan("t1"))
    await tasks[0]
    assert result.outcome is Outcome.SUCCESS  # the in-flight trade completed normally, loan repaid
    assert (await world.acc_b.get_loan("BTC")).outstanding == 0
    assert [h.scope for h in await safe.halts.active()] == [HaltScope.GLOBAL]
    with pytest.raises(StartRefused):  # while the next one is refused
        await safe.engine().start(world.plan("t2"))


async def test_a_halt_never_blocks_recovery_of_a_parked_trade(safe: Safe) -> None:
    world = safe.world
    world.b.add_fault(Op.REPAY, Fault(FaultKind.REJECT, times=100))
    parked = await safe.engine().start(world.plan("t1"))
    assert parked.state is TradeState.RECOVERY and not parked.finished
    await safe.halts.halt_global("stop everything new", "ops:alice")
    world.b.clear_faults()
    done = await safe.engine().advance("john", "t1")  # recovery proceeds under a global halt
    assert done.finished and (await world.acc_b.get_loan("BTC")).outstanding == 0


async def test_a_trade_still_in_its_prechecks_is_cancelled_when_a_halt_arrives(safe: Safe) -> None:
    tasks: list[asyncio.Task[bool]] = []

    def halt_early(name: str) -> None:
        if name == "state:PRECHECK_BORROW" and not tasks:
            tasks.append(asyncio.get_running_loop().create_task(safe.halts.halt_global("halt", "ops:alice")))

    result = await safe.engine(checkpoint=halt_early).start(safe.world.plan("t1"))
    await tasks[0]
    assert result.state is TradeState.CANCELLED  # nothing was sent yet, so stopping it risks nothing
    assert (await safe.world.ledger.state_history("john", "t1"))[-1][2] == "gate:global_halt"
    assert await safe.world.ledger.legs("john", "t1") == []


# ---- per-exchange halt ----------------------------------------


async def test_an_exchange_halt_stops_only_the_trades_that_touch_that_exchange(safe: Safe) -> None:
    world = safe.world
    okx_venues = third_exchange(world)
    await safe.halts.halt_exchange("bitget", "bitget API incident", "ops:alice")
    with pytest.raises(StartRefused, match="exchange_halt:bitget"):
        await safe.engine().start(world.plan("t1"))  # binance -> bitget
    with pytest.raises(StartRefused, match="exchange_halt:bitget"):  # the halted exchange on the other side too
        await safe.engine().start(replace(world.plan("t2"), buy_exchange="bitget", borrow_exchange="binance"))
    other = await safe.engine(venues=okx_venues).start(replace(world.plan("t3"), borrow_exchange="okx"))
    assert other.outcome is Outcome.SUCCESS  # binance -> okx is unaffected
    assert "exchange_halted" in safe.kinds()
    assert any(a.kind.value == "exchange_halted" for a in safe.channel.paged)


# ---- per-client pause ----------------------------------------


async def test_pausing_one_client_does_not_touch_another(safe: Safe) -> None:
    world = safe.world
    mary = add_client(world, "mary")
    await safe.halts.halt_client("john", "client pressed Pause", "client:john")
    with pytest.raises(StartRefused, match="client_halt"):
        await safe.engine().start(world.plan("t1"))
    assert (
        await safe.engine(venues=mary).start(replace(world.plan("m1"), client_id="mary"))
    ).outcome is Outcome.SUCCESS
    await safe.halts.resume_client("john", "client pressed Resume", "client:john")
    assert (await safe.engine().start(world.plan("t2"))).outcome is Outcome.SUCCESS


async def test_resuming_a_client_runs_fresh_readiness_checks_first(safe: Safe) -> None:
    problems = ["margin health below minimum"]

    async def readiness(client_id: str) -> list[str]:
        return list(problems)

    halts = type(safe.halts)(safe.world.ledger, safe.world.clock, safe.hub, safe.metrics, readiness=readiness)
    await halts.halt_client("john", "pause", "client:john")
    with pytest.raises(ResumeRefused) as err:
        await halts.resume_client("john", "resume", "client:john")
    assert err.value.reasons == problems
    assert [h.target for h in await halts.active()] == ["john"]  # still paused
    refused = await safe.safety.audit_entries("halt.resume_refused", "john")
    assert refused[0]["detail"]["reasons"] == problems
    problems.clear()
    assert await halts.resume_client("john", "resume", "client:john") is True


# ---- audit, idempotence, metrics ----------------------------------------


async def test_every_halt_and_resume_is_audit_logged_with_who_and_why(safe: Safe) -> None:
    await safe.halts.halt_global("exchange incident", "ops:alice")
    assert await safe.halts.resume_global("incident over", "ops:bob") is True
    audit = await safe.safety.audit_entries()
    assert [(a["action"], a["actor"]) for a in audit] == [("halt.set", "ops:alice"), ("halt.cleared", "ops:bob")]
    assert audit[0]["detail"] == {"scope": "GLOBAL", "target": None, "reason": "exchange incident"}
    assert audit[1]["detail"]["reason"] == "incident over"
    assert (await safe.world.ledger.blocked_reason("john", ["binance", "bitget"])) is None


async def test_the_audit_log_cannot_be_edited_or_deleted(safe: Safe) -> None:
    import psycopg

    await safe.halts.halt_global("x", "ops:alice")
    conn = safe.world.ledger.connection
    for sql in ("UPDATE audit_log SET actor = 'someone else'", "DELETE FROM audit_log", "TRUNCATE audit_log"):
        with pytest.raises(psycopg.errors.RestrictViolation):
            await conn.execute(sql.encode())


async def test_halting_twice_is_one_halt_and_resuming_nothing_changes_nothing(safe: Safe) -> None:
    assert await safe.halts.halt_exchange("bitget", "r", "ops:alice") is True
    assert await safe.halts.halt_exchange("bitget", "r again", "ops:bob") is False
    assert len(await safe.safety.audit_entries("halt.set")) == 1
    assert await safe.halts.resume_exchange("bitget", "ok", "ops:alice") is True
    assert await safe.halts.resume_exchange("bitget", "ok", "ops:alice") is False
    assert await safe.halts.resume_client("nobody", "ok", "ops:alice") is False


async def test_a_halt_needs_an_actor_and_a_reason(safe: Safe) -> None:
    for call in (
        safe.halts.halt_global("", "ops:alice"),
        safe.halts.halt_global("why", " "),
        safe.halts.resume_global("", "ops:alice"),
    ):
        with pytest.raises(ValueError, match="audit-logged"):
            await call


async def test_active_halts_are_exposed_as_gauges(safe: Safe) -> None:
    await safe.halts.halt_global("g", "ops:alice")
    await safe.halts.halt_exchange("bitget", "e", "ops:alice")
    assert (
        safe.metrics.gauge(m.HALTS_ACTIVE, scope="GLOBAL") == 1
        and safe.metrics.gauge(m.HALTS_ACTIVE, scope="EXCHANGE") == 1
    )
    await safe.halts.resume_global("done", "ops:alice")
    assert safe.metrics.gauge(m.HALTS_ACTIVE, scope="GLOBAL") == 0
    assert safe.metrics.gauge(m.HALTS_ACTIVE, scope="CLIENT") == 0


# ---- automatic per-exchange halt from health ----------------------------------------


async def test_repeated_infrastructure_failures_halt_that_exchange_only(safe: Safe) -> None:
    world = safe.world
    health = ExchangeHealth(safe.halts, threshold=3)
    flaky = instrument(world.deps().venues, safe.metrics, world.clock, health)
    world.b.add_fault(Op.GET_FEES, Fault(FaultKind.EXCHANGE_DOWN, times=3))
    for _ in range(3):
        with pytest.raises(ExchangeDown):
            await flaky["bitget"].get_fees(SYMBOL)
    assert [(h.scope, h.target, h.set_by) for h in await safe.halts.active()] == [
        (HaltScope.EXCHANGE, "bitget", "system:health")
    ]
    await flaky["binance"].get_fees(SYMBOL)  # the healthy exchange is unaffected
    assert await world.ledger.blocked_reason("john", ["binance"]) is None
    assert await world.ledger.blocked_reason("john", ["binance", "bitget"]) == "exchange_halt:bitget"
    # lifting it is a deliberate human action
    assert (await safe.safety.audit_entries("halt.set"))[0]["actor"] == "system:health"


async def test_only_failures_that_indict_the_exchange_count(safe: Safe) -> None:
    world = safe.world
    health = ExchangeHealth(safe.halts, threshold=2)
    wrapped = InstrumentedAdapter(world.acc_b, safe.metrics, world.clock, health)
    # business and credential errors are the client's problem, not the exchange's
    world.b.add_fault(Op.GET_FEES, Fault(FaultKind.REJECT, times=2))
    world.b.add_fault(Op.GET_FEES, Fault(FaultKind.AUTH_ERROR, times=2))
    for _ in range(4):
        with pytest.raises((Exception,)):
            await wrapped.get_fees(SYMBOL)
    assert health.consecutive_failures("bitget") == 0 and await safe.halts.active() == []
    # a success resets the streak
    world.b.add_fault(Op.GET_FEES, Fault(FaultKind.EXCHANGE_DOWN, times=1))
    with pytest.raises(ExchangeDown):
        await wrapped.get_fees(SYMBOL)
    assert health.consecutive_failures("bitget") == 1
    await wrapped.get_fees(SYMBOL)
    assert health.consecutive_failures("bitget") == 0
    # a ban counts, an ordinary 429 does not
    world.b.add_fault(Op.GET_FEES, Fault(FaultKind.RATE_LIMIT, times=1))
    with pytest.raises(RateLimited):
        await wrapped.get_fees(SYMBOL)
    assert health.consecutive_failures("bitget") == 0
    assert await health.record_failure("bitget", "Banned") is False and await health.record_failure("bitget", "Banned")


async def test_the_instrumented_adapter_measures_every_call_and_changes_nothing(safe: Safe) -> None:
    world = safe.world
    wrapped = instrument(world.deps().venues, safe.metrics, world.clock)["bitget"]
    world.b.latency_s = 0.5
    assert (await wrapped.get_fees(SYMBOL)).taker == D("0.001")  # same answer as the inner adapter
    with pytest.raises(AuthError):  # same error too: default keys have no withdraw permission
        await wrapped.withdraw("BTC", D(1), "BTC", "addr", "w1")
    assert wrapped.name == "bitget"
    assert safe.metrics.counter(m.EXCHANGE_CALLS, exchange="bitget", result="ok") == 1
    assert safe.metrics.counter(m.EXCHANGE_CALLS, exchange="bitget", result="error") == 1
    assert safe.metrics.counter(m.EXCHANGE_ERRORS, exchange="bitget", error="AuthError") == 1
    assert safe.metrics.histogram_count(m.EXCHANGE_LATENCY, exchange="bitget") == 2
