"""Recovery paths (CLAUDE.md section 6; spec sections 21-27, 36; D-026, D-027). Every one ends with loan == 0 from the
exchange API, and the real cost of the failure stays in the ledger."""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal as D

from oddtrue.exchanges.fake import Fault, FaultKind, Op
from oddtrue.execution.deps import AlertKind
from oddtrue.execution.engine import RunResult
from oddtrue.execution.states import TradeState
from oddtrue.ledger.models import EntryKind, LegStatus, Outcome
from tests.world import MARGIN, SPOT, World

NAKED_COVER_ASK = D("100910")


async def assert_loan_closed(world: World, trade_id: str = "t1") -> None:
    """The exchange, not our ledger, must say the loan is zero, and the trade must have recorded that."""
    assert (await world.acc_b.get_loan("BTC")).outstanding == 0
    assert await world.acc_b.get_liabilities() == []
    rows = await world.ledger.loans("john", trade_id)
    assert rows and all(r.verified_zero_at is not None for r in rows)


async def legs_by_name(world: World, trade_id: str = "t1") -> dict[str, list[LegStatus]]:
    out: dict[str, list[LegStatus]] = {}
    for leg in await world.ledger.legs("john", trade_id):
        out.setdefault(leg.leg, []).append(leg.status)
    return out


async def path(world: World, trade_id: str = "t1") -> list[str]:
    return [to for _, to, _ in await world.ledger.state_history("john", trade_id)]


# ---- B sell fails after borrowing (journey step 10; spec 21-27) ----------------------------------------


async def test_sell_rejected_after_borrow_unwinds_a_and_repays_from_the_borrowed_coin(world: World) -> None:
    world.b.add_fault(Op.PLACE_ORDER, Fault(FaultKind.REJECT))  # B's first order is the protected sell
    result = await world.engine().start(world.plan())
    assert result == RunResult(TradeState.DONE, Outcome.RECOVERED, None)
    assert "RECOVERY" in await path(world) and "SETTLING" not in await path(world)
    await assert_loan_closed(world)

    legs = await legs_by_name(world)
    assert legs["B_SELL"] == [LegStatus.REJECTED]
    assert legs["A_UNWIND"] == [LegStatus.FILLED] and legs["B_REPAY"] == [LegStatus.FILLED]
    assert "B_COVER" not in legs and "B_REPAY_BUY" not in legs  # no short, and nothing needed buying
    assert AlertKind.NAKED_POSITION not in world.alerts.kinds()

    # unwind: A bought 0.004 @ 100000, sold the 0.003996 it kept (net of the coin fee), floored to 0.00399 @ 99990
    r = await world.ledger.trade_result("john", "t1")
    assert r.buy_cost == D(400) and r.sell_proceeds == D("0.00399") * D(99990)
    assert r.gross == D("0.00399") * D(99990) - 400 < 0  # the real unwind loss is in the books
    assert r.fees == D("0.4") + D("0.00399") * D(99990) * D("0.001")
    assert r.interest == D("0.0000004") * D("100900")  # valued at the revalidated sell price (no sell fill exists)
    assert r.net < 0 and r.outcome is Outcome.RECOVERED
    assert world.acc_a.balance(SPOT, "BTC") == D("0.000006")  # sub-minimum dust stays on A
    assert AlertKind.DUST_LEFT in world.alerts.kinds()
    assert await world.ledger.client_net_total("john") == r.net


async def test_protected_sell_that_cannot_fill_at_the_limit_fills_nothing_and_triggers_recovery(world: World) -> None:
    def market_drops(name: str) -> None:
        if name == "state:EXECUTING":
            world.b.set_book("BTCUSDT", [(D(100000), D(10))], [(D(100010), D(10))])  # far below the 100698.2 floor

    result = await world.engine(checkpoint=market_drops).start(world.plan())
    assert result.outcome is Outcome.RECOVERED
    sell = next(x for x in await world.ledger.legs("john", "t1") if x.leg == "B_SELL")
    assert sell.status is LegStatus.EXPIRED and sell.filled_qty == 0  # never sold below the protected price
    assert D(sell.meta["limit_price"]) == D("100698.20") and sell.meta["side"] == "sell"
    await assert_loan_closed(world)
    assert world.b.account("john") is world.acc_b and world.acc_b.balance(MARGIN, "USDT") == D(500)  # B untouched


async def test_borrow_rejected_leaves_only_the_a_position_to_unwind(world: World) -> None:
    world.b.add_fault(Op.BORROW, Fault(FaultKind.REJECT))
    result = await world.engine().start(world.plan())
    assert result.outcome is Outcome.RECOVERED
    legs = await legs_by_name(world)
    assert legs["B_BORROW"] == [LegStatus.REJECTED] and "B_SELL" not in legs  # no borrow, so no sell was attempted
    assert legs["A_UNWIND"] == [LegStatus.FILLED] and "B_REPAY" not in legs
    await assert_loan_closed(world)


async def test_everything_fails_is_a_failed_trade_with_no_cost(world: World) -> None:
    world.a.add_fault(Op.PLACE_ORDER, Fault(FaultKind.REJECT))
    world.b.add_fault(Op.BORROW, Fault(FaultKind.REJECT))
    result = await world.engine().start(world.plan())
    assert result == RunResult(TradeState.DONE, Outcome.FAILED, None)
    assert await world.ledger.entries("john") == []
    assert world.usdt_total() == D(1500) and world.acc_b.balance(MARGIN, "BTC") == D("0.01")
    await assert_loan_closed(world)


async def test_borrowed_but_neither_order_filled_repays_the_unsold_coin(world: World) -> None:
    world.a.add_fault(Op.PLACE_ORDER, Fault(FaultKind.REJECT))
    world.b.add_fault(Op.PLACE_ORDER, Fault(FaultKind.REJECT))
    result = await world.engine().start(world.plan())
    assert result.outcome is Outcome.RECOVERED
    await assert_loan_closed(world)
    r = await world.ledger.trade_result("john", "t1")
    assert r.gross == 0 and r.interest == D("0.04036") and r.net == -D("0.04036")  # only the interest was lost


# ---- A buy fails after B already sold: naked short (D-026) ----------------------------------------


async def test_a_buy_fails_after_b_sold_covers_the_short_immediately(world: World) -> None:
    world.a.add_fault(Op.PLACE_ORDER, Fault(FaultKind.REJECT))
    result = await world.engine().start(world.plan())
    assert result == RunResult(TradeState.DONE, Outcome.RECOVERED, None)
    assert AlertKind.NAKED_POSITION in world.alerts.kinds()
    legs = await legs_by_name(world)
    assert legs["A_BUY"] == [LegStatus.REJECTED] and legs["B_SELL"] == [LegStatus.FILLED]
    assert legs["B_COVER"] == [LegStatus.FILLED] and "A_UNWIND" not in legs
    await assert_loan_closed(world)

    # sold 0.004 @ 100900; covered by buying 0.00401 (grossed up for the 0.1% coin fee) @ 100910
    cover = next(x for x in await world.ledger.legs("john", "t1") if x.leg == "B_COVER")
    assert cover.filled_qty == D("0.00401") and cover.avg_price == D(100910)
    assert D(cover.meta["limit_price"]) == D("101111.82")  # best ask x 1.002, rounded down onto the tick
    r = await world.ledger.trade_result("john", "t1")
    assert r.sell_proceeds == D("403.6") and r.buy_cost == D("0.00401") * D(100910)
    assert r.gross == D("403.6") - D("0.00401") * D(100910) < 0
    assert r.net < r.gross  # fees and interest on top of the cover loss
    assert world.acc_b.balance(MARGIN, "BTC") >= D(0)  # no remaining short


async def test_a_buy_under_fills_so_the_uncovered_part_of_the_short_is_bought_back(world: World) -> None:
    world.a.add_fault(Op.PLACE_ORDER, Fault(FaultKind.PARTIAL_FILL, fraction=D("0.5")))
    result = await world.engine().start(world.plan())
    assert result.outcome is Outcome.RECOVERED
    legs = await legs_by_name(world)
    assert legs["A_BUY"] == [LegStatus.PARTIALLY_FILLED] and legs["B_COVER"] == [LegStatus.FILLED]
    cover = next(x for x in await world.ledger.legs("john", "t1") if x.leg == "B_COVER")
    assert cover.filled_qty == D("0.00201")  # the 0.002 short (not 0.004), grossed up for the fee
    await assert_loan_closed(world)
    # the matched 0.002 is a normal inventory shift: A holds it, B inventory paid the repayment
    assert world.acc_a.balance(SPOT, "BTC") == D("0.002") - D("0.000002")


async def test_b_sell_under_fills_so_the_surplus_on_a_is_sold_back(world: World) -> None:
    world.b.add_fault(Op.PLACE_ORDER, Fault(FaultKind.PARTIAL_FILL, fraction=D("0.5")))
    result = await world.engine().start(world.plan())
    assert result.outcome is Outcome.RECOVERED
    legs = await legs_by_name(world)
    assert legs["B_SELL"] == [LegStatus.PARTIALLY_FILLED] and legs["A_UNWIND"] == [LegStatus.FILLED]
    unwind = next(x for x in await world.ledger.legs("john", "t1") if x.leg == "A_UNWIND")
    assert unwind.filled_qty == D("0.002")  # exactly the part B did not sell
    await assert_loan_closed(world)


# ---- escalating cover (D-026) ----------------------------------------


def rising_ask(world: World, bumps: dict[int, str]) -> Callable[[str], None]:
    """Checkpoint hook: just after cover attempt n's intent is saved (price already chosen), the market runs away."""

    def hook(name: str) -> None:
        if name.startswith("intent:B_COVER:"):
            n = int(name.rsplit(":", 1)[1])
            if n in bumps:
                best = world.b._snapshot("BTCUSDT", 1).asks[0].price  # run away from wherever the market is now
                world.b.set_book(
                    "BTCUSDT", [(D(100900), D(10))], [(best * (1 + D(bumps[n])), D(10)), (best * 2, D(10))]
                )

    return hook


async def test_cover_escalates_through_the_ladder_until_it_fills(world: World) -> None:
    world.a.add_fault(Op.PLACE_ORDER, Fault(FaultKind.REJECT))
    hook = rising_ask(world, {1: "0.003", 2: "0.007"})  # beyond rung 0 (0.2%), then beyond rung 1 (0.5%)
    result = await world.engine(checkpoint=hook).start(world.plan())
    assert result.outcome is Outcome.RECOVERED
    covers = [x for x in await world.ledger.legs("john", "t1") if x.leg == "B_COVER"]
    assert [x.status for x in covers] == [LegStatus.EXPIRED, LegStatus.EXPIRED, LegStatus.FILLED]
    assert [x.attempt for x in covers] == [1, 2, 3] and len({x.client_order_id for x in covers}) == 3
    limits = [D(x.meta["limit_price"]) for x in covers]
    assert limits == sorted(limits) and len(set(limits)) == 3  # each rung is a wider protected limit, never a market
    await assert_loan_closed(world)


async def test_cover_that_never_fills_holds_in_recovery_alerts_and_blocks_the_client(world: World) -> None:
    world.a.add_fault(Op.PLACE_ORDER, Fault(FaultKind.REJECT))
    hook = rising_ask(world, dict.fromkeys(range(1, 10), "0.05"))  # the ask runs away 5% on every attempt
    engine = world.engine(checkpoint=hook)
    result = await engine.start(world.plan())
    assert result.state is TradeState.RECOVERY and result.held == "naked_short_not_covered" and not result.finished
    kinds = world.alerts.kinds()
    assert AlertKind.NAKED_POSITION in kinds and AlertKind.COVER_ESCALATION_EXHAUSTED in kinds
    covers = [x for x in await world.ledger.legs("john", "t1") if x.leg == "B_COVER"]
    assert len(covers) == 4 and all(x.status is LegStatus.EXPIRED for x in covers)  # bounded: ladder + 1, no hammering
    assert (await world.acc_b.get_loan("BTC")).outstanding > 0  # honest: the loan is still open
    # controlled recovery state: this client cannot start another trade
    from oddtrue.execution.engine import StartRefused

    try:
        await world.engine().start(world.plan("t2"))
    except StartRefused:
        pass
    else:
        raise AssertionError("a client with a trade stuck in RECOVERY must not start another")

    # past the recovery time limit the alert escalates to the loan-not-zero page
    world.clock.advance(world.cfg.recovery_timeout_s + 1)
    again = await world.engine(checkpoint=hook).advance("john", "t1")
    assert again.state is TradeState.RECOVERY
    assert AlertKind.LOAN_NOT_ZERO_AFTER_RECOVERY_TIMEOUT in world.alerts.kinds()

    # the market comes back; a later pass finishes the job
    world.set_books()
    done = await world.engine().advance("john", "t1")
    assert done == RunResult(TradeState.DONE, Outcome.RECOVERED, None)
    await assert_loan_closed(world)


# ---- interest purchase from the 5% reserve (spec 21-23) ----------------------------------------


def drain_inventory(world: World) -> Callable[[str], None]:
    def hook(name: str) -> None:
        if name == "state:EXECUTING":
            world.acc_b.drift(MARGIN, "BTC", -world.acc_b.balance(MARGIN, "BTC"))

    return hook


async def test_failed_sell_buys_only_what_is_missing_for_interest_within_the_reserve(world: World) -> None:
    world.b.add_fault(Op.PLACE_ORDER, Fault(FaultKind.REJECT))
    result = await world.engine(checkpoint=drain_inventory(world)).start(world.plan())
    assert result.outcome is Outcome.RECOVERED
    await assert_loan_closed(world)
    buys = [x for x in await world.ledger.legs("john", "t1") if x.leg == "B_REPAY_BUY"]
    assert len(buys) == 1
    # the unsold borrowed 0.004 repays the principal; only the 4e-7 interest is short. The smallest legal buy is
    # 0.00005 BTC (5 USDT minimum notional at ~100910), booked as a recovery cost, not as trading gross.
    assert buys[0].filled_qty == D("0.00005")
    r = await world.ledger.trade_result("john", "t1")
    assert r.recovery == D("0.00005") * D(100910) < world.plan().reserve_5
    assert r.interest == 0  # the interest was paid with purchased coin, so it is not booked twice
    assert r.net == r.gross - r.fees - r.recovery < 0


async def test_reserve_too_small_holds_in_recovery_instead_of_overspending(world: World) -> None:
    world.b.add_fault(Op.PLACE_ORDER, Fault(FaultKind.REJECT))
    plan = world.plan(reserve_5=D(3))  # below the ~5.05 USDT the smallest legal purchase costs
    result = await world.engine(checkpoint=drain_inventory(world)).start(plan)
    assert result.state is TradeState.RECOVERY and result.held == "reserve_insufficient"
    assert AlertKind.RESERVE_INSUFFICIENT in world.alerts.kinds()
    assert "B_REPAY_BUY" not in await legs_by_name(world)
    assert (await world.acc_b.get_loan("BTC")).outstanding > 0
    # the owner tops up the reserve; the next pass completes recovery
    stored = await world.ledger.get_plan("john", "t1")
    stored["reserve_5"] = "25"
    await world.ledger.set_plan("john", "t1", stored)
    done = await world.engine().advance("john", "t1")
    assert done.outcome is Outcome.RECOVERED
    await assert_loan_closed(world)


async def test_kinds_of_ledger_entries_for_a_recovered_trade_are_all_booked_once(world: World) -> None:
    world.b.add_fault(Op.PLACE_ORDER, Fault(FaultKind.REJECT))
    await world.engine().start(world.plan())
    entries = await world.ledger.entries("john", "t1")
    kinds = [e.kind for e in entries]
    assert kinds.count(EntryKind.BUY_COST) == 1 and kinds.count(EntryKind.SELL_PROCEEDS) == 1
    assert kinds.count(EntryKind.INTEREST) == 1 and kinds.count(EntryKind.FEE) == 2
    # re-running the booking never doubles anything
    from oddtrue.execution.context import TradeCtx
    from oddtrue.execution.gateway import Gateway
    from oddtrue.execution.legs import book_all_legs
    from oddtrue.execution.plan import TradePlan

    plan = TradePlan.from_json(await world.ledger.get_plan("john", "t1"))
    await book_all_legs(TradeCtx(world.deps(), plan, Gateway(world.deps())))
    assert len(await world.ledger.entries("john", "t1")) == len(entries)
