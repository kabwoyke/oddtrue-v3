"""Kill the process at every durable step, restart, and prove the trade ends safely (CLAUDE.md section 13 Phase 3).

For each scenario: run once cleanly to list every checkpoint the engine passes; then, for each checkpoint, rerun on a
fresh world, kill the "process" exactly there (its database connection is abandoned like a dead process's), start a
NEW engine on a NEW connection, and resume. After every resume the same safety invariants must hold.
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal as D

import pytest

from oddtrue.exchanges.fake import Fault, FaultKind, Op
from oddtrue.execution.engine import RunResult, TradeEngine
from oddtrue.execution.states import ALLOWED, TradeState
from oddtrue.ledger import db
from oddtrue.ledger.models import LegStatus, Outcome
from oddtrue.ledger.repository import Ledger
from tests.conftest import TEST_DSN
from tests.world import INVENTORY, MARGIN, QTY, RULES, SPOT, SimulatedCrash, World, build_world

Setup = Callable[[World], None]


def _b_sell_rejected(w: World) -> None:
    w.b.add_fault(Op.PLACE_ORDER, Fault(FaultKind.REJECT))


def _a_buy_rejected(w: World) -> None:
    w.a.add_fault(Op.PLACE_ORDER, Fault(FaultKind.REJECT))


def _a_buy_partial(w: World) -> None:
    w.a.add_fault(Op.PLACE_ORDER, Fault(FaultKind.PARTIAL_FILL, fraction=D("0.5")))


def _sell_times_out_filled(w: World) -> None:
    w.b.add_fault(Op.PLACE_ORDER, Fault(FaultKind.TIMEOUT_APPLIED))


def _repay_times_out_unapplied(w: World) -> None:
    w.b.add_fault(Op.REPAY, Fault(FaultKind.TIMEOUT_NOT_APPLIED))


SCENARIOS: dict[str, Setup] = {
    "success": lambda w: None,
    "sell_rejected": _b_sell_rejected,
    "naked_short": _a_buy_rejected,
    "partial_buy": _a_buy_partial,
    "sell_timeout_filled": _sell_times_out_filled,
    "repay_timeout": _repay_times_out_unapplied,
}


async def _checkpoints(ledger: Ledger, setup: Setup) -> list[str]:
    seen: list[str] = []
    world = build_world(ledger)
    setup(world)
    await world.engine(checkpoint=seen.append).start(world.plan("clean"))
    return seen


async def _assert_safe(world: World, ledger: Ledger, trade_id: str, result: RunResult) -> None:
    # 1. the trade finished, with an outcome, and nothing is left unfinished
    assert result.finished and result.outcome is not None, result
    assert (await ledger.get_trade("john", trade_id)).outcome is result.outcome
    assert [t for t in await ledger.unfinished_trades() if t.id == trade_id] == []
    # 2. the EXCHANGE says the loan is zero, and the trade recorded that
    assert (await world.acc_b.get_loan("BTC")).outstanding == 0 and await world.acc_b.get_liabilities() == []
    assert all(r.verified_zero_at is not None for r in await ledger.loans("john", trade_id))
    # 2b. we never re-sent anything: the exchange never had to reject a duplicate client id
    assert world.acc_a.duplicate_rejections == 0 and world.acc_b.duplicate_rejections == 0
    # 3. nothing was sent without a persisted idempotency key, and no leg is left unresolved
    legs = await ledger.legs("john", trade_id)
    known = {x.client_order_id for x in legs}
    sent = (
        set(world.acc_a._orders) | set(world.acc_b._orders) | set(world.acc_b._borrow_ids) | set(world.acc_b._repay_ids)
    )
    assert sent <= known, f"exchange saw ids we never persisted: {sent - known}"
    # 3b. the ledger and the exchanges agree leg by leg: filled/expired means the exchange has it, rejected means not
    for x in legs:
        if x.status is LegStatus.REJECTED:
            assert x.client_order_id not in sent, f"{x.leg} booked as rejected but the exchange executed it"
        else:
            assert x.client_order_id in sent, f"{x.leg} booked as {x.status} but the exchange never saw it"
    assert all(
        x.status in (LegStatus.FILLED, LegStatus.PARTIALLY_FILLED, LegStatus.EXPIRED, LegStatus.REJECTED) for x in legs
    ), [(x.leg, x.status) for x in legs]
    # 4. no naked exposure and no leaked coin: total coin is the starting inventory less fees and interest, plus at
    #    most sub-minimum dust (below the exchange's 0.00001 minimum order) that could not be sold back
    total = world.coin_total()
    assert INVENTORY - QTY * D("0.02") <= total <= INVENTORY + RULES.min_qty, total
    assert world.acc_b.balance(MARGIN, "BTC") >= 0
    # 5. USDT lost is bounded by costs (a few percent of the notional at the very worst), never unexplained
    assert world.usdt_total() >= D(1500) - QTY * D(100000) * D("0.03"), world.usdt_total()
    # 6. the ledger agrees with itself and every state move was legal
    r = await ledger.trade_result("john", trade_id)
    assert r.outcome is result.outcome
    history = await ledger.state_history("john", trade_id)
    for src, dst, _ in history[1:]:
        assert src is not None and TradeState(dst) in ALLOWED[TradeState(src)], (src, dst)
    # 7. a clean outcome means the exchange balances are exactly what a trade that never crashed produces
    if result.outcome is Outcome.SUCCESS:
        assert world.acc_a.balance(SPOT, "BTC") == D("0.003996") and world.acc_a.balance(SPOT, "USDT") == D(600)
        assert world.acc_b.balance(MARGIN, "BTC") == INVENTORY - QTY - D("0.0000004")


@pytest.mark.parametrize("scenario", list(SCENARIOS))
async def test_crash_at_every_checkpoint_then_resume(scenario: str, ledger: Ledger) -> None:
    setup = SCENARIOS[scenario]
    names = await _checkpoints(ledger, setup)
    assert len(names) >= 20 and names[0] == "created" and names[-1] == "finished"
    outcomes: set[Outcome] = set()

    for k in range(1, len(names) + 1):
        trade_id = f"{scenario}-{k}"
        doomed = await db.connect(TEST_DSN)
        world = build_world(Ledger(doomed))
        setup(world)
        counter = 0

        def kill_here(name: str, k: int = k) -> None:
            nonlocal counter
            counter += 1
            if counter == k:
                raise SimulatedCrash(name)

        try:
            await world.engine(checkpoint=kill_here).start(world.plan(trade_id))
        except (SimulatedCrash, BaseExceptionGroup):
            pass
        else:
            pytest.fail(f"checkpoint {k} ({names[k - 1]}) was not reached in the crashed run")
        await doomed.close()  # the old process is gone; its connection with it

        survivor = await db.connect(TEST_DSN)  # a restarted worker: new connection, new engine, same exchanges
        try:
            led = Ledger(survivor)
            world.ledger = led
            restarted = TradeEngine(world.deps())
            results = await restarted.resume_all()
            row = await led.get_trade("john", trade_id)
            if names[k - 1] == "finished":
                assert results == [] and row.outcome is not None  # already complete: nothing left to resume
                final = RunResult(TradeState(row.state), row.outcome, None)
            else:
                assert len(results) == 1, f"expected one unfinished trade to resume, got {results}"
                final = results[0]
            await _assert_safe(world, led, trade_id, final)
            assert final.outcome is not None
            outcomes.add(final.outcome)
        except AssertionError as exc:
            raise AssertionError(f"crash after checkpoint #{k} {names[k - 1]!r}: {exc}") from exc
        finally:
            await survivor.close()

    assert outcomes  # and the sweep really exercised more than one ending where the scenario allows
    if scenario == "success":
        assert Outcome.SUCCESS in outcomes
