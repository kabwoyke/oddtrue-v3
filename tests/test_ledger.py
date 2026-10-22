"""Ledger on Postgres: append-only, idempotency keys, derived results (spec 32-33), isolation, invariants."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import psycopg
import pytest
from psycopg import AsyncConnection
from psycopg.rows import DictRow

from oddtrue.allocation.rotation import Candidate, RotationReason, plan_rotation
from oddtrue.ids import client_order_id
from oddtrue.ledger import db
from oddtrue.ledger.models import EntryKind, LegStatus, Outcome
from oddtrue.ledger.repository import Ledger, LedgerError, NewTrade, StateConflict, TradeNotFound

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def trade(tid: str = "t1", client: str = "john", capital: str = "475") -> NewTrade:
    return NewTrade(tid, client, "BTCUSDT", "binance", "bitget", D(500), D(475), D(25), D(capital), "ALLOCATED")


async def journey_trade(ledger: Ledger, tid: str = "t1", client: str = "john") -> None:
    """Spec-style successful trade: buy 0.005 @ 100,000, sell @ 100,900, fees, interest."""
    await ledger.create_trade(trade(tid, client, "500"))
    await ledger.append_entry(client, EntryKind.BUY_COST, D(500), trade_id=tid, asset="BTC", qty=D("0.005"))
    await ledger.append_entry(client, EntryKind.SELL_PROCEEDS, D("504.5"), trade_id=tid, asset="BTC", qty=D("0.005"))
    await ledger.append_entry(client, EntryKind.FEE, D("1.0045"), trade_id=tid)
    await ledger.append_entry(client, EntryKind.INTEREST, D("0.05"), trade_id=tid)
    await ledger.finish_trade(client, tid, Outcome.SUCCESS)


async def test_migrations_apply_once_and_detect_tampering(conn: AsyncConnection[DictRow]) -> None:
    assert await db.migrate(conn) == []  # already applied by the fixture
    await conn.execute("UPDATE schema_migrations SET checksum = 'x' WHERE version = 1")
    with pytest.raises(db.MigrationError):
        await db.migrate(conn)


async def test_ledger_entries_are_append_only(ledger: Ledger, conn: AsyncConnection[DictRow]) -> None:
    await ledger.create_trade(trade())
    e = await ledger.append_entry("john", EntryKind.FEE, D("1"), trade_id="t1")
    for sql in (
        "UPDATE ledger_entries SET amount_usdt = 0",
        "DELETE FROM ledger_entries",
        "TRUNCATE ledger_entries",
        "UPDATE trade_state_log SET reason = 'x'",
        "DELETE FROM trade_state_log",
        "TRUNCATE trade_state_log",
    ):
        with pytest.raises(psycopg.errors.RestrictViolation):
            await conn.execute(sql.encode())
    await ledger.log_rotation("r1", "o1", plan_rotation([Candidate("john", D(1), None)], D(10)))
    for sql in ("UPDATE rotation_log SET qty = 0", "DELETE FROM rotation_log", "TRUNCATE rotation_log"):
        with pytest.raises(psycopg.errors.RestrictViolation):
            await conn.execute(sql.encode())
    assert [x.id for x in await ledger.entries("john")] == [e.id]


async def test_correction_is_a_new_adjustment_entry(ledger: Ledger) -> None:
    await journey_trade(ledger)
    before = (await ledger.trade_result("john", "t1")).net
    await ledger.append_entry("john", EntryKind.ADJUSTMENT, D("-2"), trade_id="t1", note="fee correction")
    after = await ledger.trade_result("john", "t1")
    assert after.net == before - 2 and after.adjustments == D(-2)
    assert len(await ledger.entries("john", "t1")) == 5
    with pytest.raises(psycopg.errors.CheckViolation):  # only ADJUSTMENT may be negative
        await ledger.append_entry("john", EntryKind.FEE, D("-1"), trade_id="t1")


async def test_trade_result_reproduces_spec_32_33(ledger: Ledger) -> None:
    await journey_trade(ledger)
    r = await ledger.trade_result("john", "t1")
    assert (r.starting_usdt, r.allocated_95, r.reserve_5, r.capital_used) == (D(500), D(475), D(25), D(500))
    assert r.qty_bought == r.qty_sold == D("0.005")
    assert r.buy_vwap == D(100000) and r.sell_vwap == D(100900)
    assert r.gross == D("4.5")
    assert r.net == D("4.5") - D("1.0045") - D("0.05") == D("3.4455")
    assert r.realized_roi_pct == D("3.4455") / 500 * 100
    assert r.ending_balance == D(500) + r.net
    assert r.outcome is Outcome.SUCCESS


async def test_recovered_trade_keeps_its_losses_and_costs(ledger: Ledger) -> None:
    """Spec/journey step 10: unwind loss + recovery costs stay in the ledger and in net."""
    await ledger.create_trade(trade("t2"))
    await ledger.append_entry("john", EntryKind.BUY_COST, D("475"), trade_id="t2", asset="BTC", qty=D("0.00475"))
    await ledger.append_entry("john", EntryKind.SELL_PROCEEDS, D("470"), trade_id="t2", asset="BTC", qty=D("0.00475"))
    await ledger.append_entry("john", EntryKind.RECOVERY_COST, D(2), trade_id="t2", note="interest purchase")
    await ledger.append_entry("john", EntryKind.INTEREST, D("0.02"), trade_id="t2")
    await ledger.finish_trade("john", "t2", Outcome.RECOVERED)
    r = await ledger.trade_result("john", "t2")
    assert r.gross == D(-5) and r.recovery == D(2) and r.net == D("-7.02") and r.realized_roi_pct < 0
    assert r.outcome is Outcome.RECOVERED


async def test_cancelled_trade_without_entries(ledger: Ledger) -> None:
    await ledger.create_trade(trade("t3", capital="0"))
    await ledger.finish_trade("john", "t3", Outcome.CANCELLED)
    r = await ledger.trade_result("john", "t3")
    assert r.net == 0 and r.realized_roi_pct == 0 and r.buy_vwap is None and r.sell_vwap is None


async def test_client_total_equals_sum_of_trade_results(ledger: Ledger) -> None:
    """DATA_MODEL invariant 1 / dashboard rule: sum of trade rows == headline totals."""
    await journey_trade(ledger, "t1")
    await journey_trade(ledger, "t2")
    await ledger.create_trade(trade("t3"))
    await ledger.append_entry("john", EntryKind.TRANSFER_COST, D("1.5"), trade_id="t3")
    await ledger.append_entry("john", EntryKind.REBALANCE_COST, D("0.5"), trade_id="t3")
    total = sum([(await ledger.trade_result("john", t)).net for t in ("t1", "t2", "t3")], D(0))
    assert total == await ledger.client_net_total("john") == D("3.4455") * 2 - 2
    assert await ledger.client_net_total("nobody") == 0


async def test_clients_are_isolated(ledger: Ledger) -> None:
    await journey_trade(ledger, "t1", "john")
    await journey_trade(ledger, "t9", "mary")
    assert [e.trade_id for e in await ledger.entries("mary")] == ["t9"] * 4
    with pytest.raises(TradeNotFound):
        await ledger.trade_result("mary", "t1")
    with pytest.raises(TradeNotFound):
        await ledger.get_trade("mary", "t1")
    with pytest.raises(TradeNotFound):  # cannot attach an entry to someone else's trade
        await ledger.append_entry("mary", EntryKind.FEE, D(1), trade_id="t1")
    with pytest.raises(StateConflict):
        await ledger.finish_trade("mary", "t1", Outcome.FAILED)
    with pytest.raises(StateConflict):
        await ledger.transition("mary", "t1", "ALLOCATED", "EXECUTING", "x")
    assert await ledger.legs("mary", "t1") == []
    assert await ledger.state_history("mary", "t1") == []
    with pytest.raises(TradeNotFound):
        await ledger.record_leg_intent("mary", "t1", "A_BUY", 1, D(1))
    assert await ledger.client_net_total("john") == await ledger.client_net_total("mary")


async def test_leg_intent_is_persisted_first_and_idempotent(ledger: Ledger) -> None:
    await ledger.create_trade(trade())
    leg = await ledger.record_leg_intent("john", "t1", "A_BUY", 1, D("0.005"))
    assert leg.client_order_id == client_order_id("t1", "A_BUY", 1) and leg.status is LegStatus.INTENT
    again = await ledger.record_leg_intent("john", "t1", "A_BUY", 1, D("0.005"))  # e.g. after a crash/resume
    assert again.id == leg.id and len(await ledger.legs("john", "t1")) == 1
    retry = await ledger.record_leg_intent("john", "t1", "A_BUY", 2, D("0.005"))
    assert retry.client_order_id != leg.client_order_id

    sent = await ledger.update_leg("john", leg.client_order_id, LegStatus.SENT)
    assert sent.status is LegStatus.SENT and sent.filled_qty is None
    done = await ledger.update_leg(
        "john", leg.client_order_id, LegStatus.FILLED,
        exchange_order_id="x-1", filled_qty=D("0.005"), avg_price=D(100000), fee=D("0.000005"), fee_asset="BTC",
    )  # fmt: skip
    assert (done.exchange_order_id, done.filled_qty, done.avg_price, done.fee_asset) == (
        "x-1",
        D("0.005"),
        D(100000),
        "BTC",
    )
    unknown = await ledger.update_leg("john", leg.client_order_id, LegStatus.UNKNOWN)
    assert unknown.filled_qty == D("0.005")  # status change never erases recorded fill data
    with pytest.raises(LedgerError):
        await ledger.update_leg("mary", leg.client_order_id, LegStatus.FILLED)


async def test_state_transitions_are_compare_and_set_and_logged(ledger: Ledger) -> None:
    await ledger.create_trade(trade(), at=T0)
    await ledger.transition("john", "t1", "ALLOCATED", "PRECHECK_B_LIQUIDITY", "ok")
    with pytest.raises(StateConflict):  # a second worker with a stale view loses
        await ledger.transition("john", "t1", "ALLOCATED", "PRECHECK_B_LIQUIDITY", "ok")
    assert (await ledger.get_trade("john", "t1")).state == "PRECHECK_B_LIQUIDITY"
    assert await ledger.state_history("john", "t1") == [
        (None, "ALLOCATED", "created"),
        ("ALLOCATED", "PRECHECK_B_LIQUIDITY", "ok"),
    ]


async def test_unfinished_trades_for_crash_resume(ledger: Ledger) -> None:
    await ledger.create_trade(trade("t1"))
    await ledger.create_trade(trade("t2", "mary"))
    await journey_trade(ledger, "t3", "john")
    open_ids = {t.id for t in await ledger.unfinished_trades()}
    assert open_ids == {"t1", "t2"}
    await ledger.finish_trade("john", "t1", Outcome.FAILED)
    with pytest.raises(StateConflict):  # an outcome is final
        await ledger.finish_trade("john", "t1", Outcome.SUCCESS)
    assert {t.id for t in await ledger.unfinished_trades()} == {"t2"}


async def test_schema_constraints(ledger: Ledger, conn: AsyncConnection[DictRow]) -> None:
    await ledger.create_trade(trade())
    with pytest.raises(psycopg.errors.UniqueViolation):
        await ledger.create_trade(trade())
    bad = NewTrade("t5", "john", "BTCUSDT", "binance", "binance", D(1), D(1), D(0), D(1), "S")
    with pytest.raises(psycopg.errors.CheckViolation):
        await ledger.create_trade(bad)
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        await conn.execute("INSERT INTO ledger_entries (client_id, trade_id, kind, amount_usdt, at) "
                           "VALUES ('john', 'ghost', 'FEE', 1, now())")  # fmt: skip
    # decimals stay exact: no float coercion anywhere in the round trip
    await ledger.append_entry("john", EntryKind.FEE, D("0.12345678901234567890"), trade_id="t1")
    assert (await ledger.entries("john", "t1"))[0].amount_usdt == D("0.12345678901234567890")


async def test_rotation_audit_and_last_participation(ledger: Ledger) -> None:
    d1 = plan_rotation([Candidate("a", D(5), None), Candidate("b", D(5), None), Candidate("c", D(5), None)], D(10))
    await ledger.log_rotation("round1", "opp1", d1, at=T0)
    assert [d.reason for d in d1] == [
        RotationReason.INCLUDED,
        RotationReason.INCLUDED,
        RotationReason.SKIPPED_DOES_NOT_FIT,
    ]
    last = await ledger.last_participation(["a", "b", "c", "d"])
    assert last == {"a": T0, "b": T0, "c": None, "d": None}  # a skipped client is not a failed client
    hist = await ledger.rotation_history("c")
    assert hist == [("round1", 3, D(0), "skipped_does_not_fit")]
    # next round, built from persisted state: c (never participated) goes first
    nxt = plan_rotation([Candidate(c, D(5), t) for c, t in last.items()], D(10))
    assert [d.client_id for d in nxt][:2] == ["c", "d"]
    await ledger.log_rotation("round2", "opp2", nxt, at=T0 + timedelta(minutes=5))
    assert (await ledger.last_participation(["c"]))["c"] == T0 + timedelta(minutes=5)
    with pytest.raises(psycopg.errors.UniqueViolation):  # a round is logged once per client
        await ledger.log_rotation("round2", "opp2", nxt)
