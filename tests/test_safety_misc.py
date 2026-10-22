"""Remaining edges of the safety layer: adapter delegation, config validation, no-op paths."""

from __future__ import annotations

from decimal import Decimal as D

import pytest
from pydantic import ValidationError

from oddtrue.exchanges.models import OrderRequest, Permissions, TimeInForce
from oddtrue.ids import client_order_id
from oddtrue.ledger.models import EntryKind
from oddtrue.observability.alerts import Alert, AlertHub, AlertKind
from oddtrue.observability.metrics import Metrics
from oddtrue.pricing.types import Side
from oddtrue.safety.instrumented import InstrumentedAdapter
from tests.test_config import _risk
from tests.world import MARGIN, SPOT, SYMBOL, Safe, World


async def test_the_instrumented_adapter_forwards_every_method_unchanged(world: World) -> None:
    """Every ExchangeAdapter method reaches the inner adapter with the same arguments and returns what it returned."""
    inner = world.b.account("full", Permissions(True, True, True, True, True))
    inner.fund(SPOT, "BTC", D(1))
    inner.fund(MARGIN, "USDT", D(1000))
    w = InstrumentedAdapter(inner, Metrics(), world.clock)
    assert w.name == "bitget"
    assert await w.get_orderbook(SYMBOL, 1) == await inner.get_orderbook(SYMBOL, 1)
    stream = w.stream_orderbook(SYMBOL, 1)
    assert (await anext(stream)) == await inner.get_orderbook(SYMBOL, 1)
    await stream.aclose()  # type: ignore[attr-defined]
    assert await w.get_fees(SYMBOL) == await inner.get_fees(SYMBOL)
    assert await w.get_symbol_rules(SYMBOL) == await inner.get_symbol_rules(SYMBOL)
    assert await w.get_balances(SPOT) == await inner.get_balances(SPOT)
    assert await w.get_margin_account() == await inner.get_margin_account()
    assert await w.get_borrowable("BTC") == await inner.get_borrowable("BTC")
    assert await w.get_liabilities() == []
    assert await w.get_borrow_rate("BTC") == await inner.get_borrow_rate("BTC")
    assert await w.check_permissions() == Permissions(True, True, True, True, True)
    # mutating calls change the inner state exactly once
    order = OrderRequest(
        client_order_id("t", "A", 1), SYMBOL, Side.BUY, D("0.001"), D("100010"), TimeInForce.IOC, MARGIN
    )
    placed = await w.place_order(order)
    assert (
        placed
        == await w.get_order(order.client_order_id, SYMBOL, MARGIN)
        == await inner.get_order(order.client_order_id, SYMBOL, MARGIN)
    )
    assert (await w.cancel_order(order.client_order_id, SYMBOL, MARGIN)) == placed
    assert (await w.borrow("BTC", D("0.002"), "b1")).qty == D("0.002") and (await inner.get_loan("BTC")).principal == D(
        "0.002"
    )
    assert (await w.get_loan("BTC")) == await inner.get_loan("BTC")
    inner.fund(MARGIN, "BTC", D("0.001"))
    assert (await w.repay("BTC", D("0.002"), "r1")).remaining.principal == D("0.0000002")  # interest is paid first
    assert (await w.get_deposit_address("BTC", "BTC")).address == (
        await inner.get_deposit_address("BTC", "BTC")
    ).address
    assert (await w.withdraw("BTC", D("0.5"), "BTC", "addr", "w1")).qty == D("0.5")
    assert (await w.get_transfer_status("w1")).qty == D("0.5")
    assert await w.get_withdraw_fee_and_min("BTC", "BTC") == await inner.get_withdraw_fee_and_min("BTC", "BTC")


def test_per_client_exposure_cannot_exceed_global_exposure() -> None:
    with pytest.raises(ValidationError, match="per-client exposure"):
        _risk(max_exposure_per_client_usdt=D(100000), max_exposure_global_usdt=D(50000))


async def test_unfreezing_a_client_that_is_not_frozen_changes_nothing(safe: Safe) -> None:
    assert await safe.safety.unfreeze("john", "ops:alice", "nothing to do") is False
    assert await safe.safety.audit_entries("client.unfrozen") == []


async def test_an_exchange_that_is_already_halted_is_not_halted_again_by_health(safe: Safe) -> None:
    from oddtrue.safety.health import ExchangeHealth

    health = ExchangeHealth(safe.halts, threshold=1)
    assert await health.record_failure("bitget", "ExchangeDown") is True
    assert (
        await health.record_failure("bitget", "ExchangeDown") is False
    )  # already halted: no second halt, no new audit
    assert len(await safe.safety.audit_entries("halt.set")) == 1


async def test_a_hub_without_a_store_still_records_and_pages() -> None:
    class Clock:
        now = 5.0

        async def sleep(self, seconds: float) -> None: ...

    paged: list[Alert] = []

    class Channel:
        async def notify(self, alert: Alert) -> None:
            paged.append(alert)

    hub = AlertHub(Clock(), Metrics(), channels=(Channel(),))
    hub.raise_alert(Alert(AlertKind.GLOBAL_HALT, "", "", "halt"))
    await hub.flush()
    assert [a.kind for a in paged] == [AlertKind.GLOBAL_HALT]


async def test_a_client_level_ledger_entry_needs_no_trade(world: World) -> None:
    entry = await world.ledger.append_entry("john", EntryKind.TRANSFER_COST, D("1.5"), note="withdrawal fee")
    assert entry.trade_id is None and await world.ledger.client_net_total("john") == D("-1.5")
