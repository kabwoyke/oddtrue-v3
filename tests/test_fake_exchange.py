"""FakeExchange: full margin cycle, fault injection, rate limits, streams, 300 clients (CLAUDE.md 4A, 11)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from decimal import Decimal as D

import pytest

from oddtrue.exchanges import fake as fk
from oddtrue.exchanges.base import DUPLICATE_CLIENT_ID, ORDER_NOT_FOUND, ExchangeAdapter
from oddtrue.exchanges.errors import (
    AuthError,
    ExchangeDown,
    InsufficientFunds,
    InvalidQuantity,
    RateLimited,
    Rejected,
    StreamDropped,
    UnknownOutcome,
)
from oddtrue.exchanges.fake import FakeAccount, FakeVenue, Fault, FaultKind, Op, RateLimitConfig
from oddtrue.exchanges.models import (
    AccountType,
    OrderRequest,
    OrderStatus,
    Permissions,
    TimeInForce,
    TransferState,
    base_asset,
)
from oddtrue.ids import client_order_id
from oddtrue.pricing.types import Side, SymbolRules

SYM = "BTCUSDT"
RULES = SymbolRules(D("0.00001"), D(100), D("0.00001"), D("0.01"), D(5))
SPOT, MARGIN = AccountType.SPOT, AccountType.MARGIN


def venue(name: str = "binance", ask: str = "100000", bid: str = "99990") -> FakeVenue:
    v = FakeVenue(name)
    v.set_rules(SYM, RULES)
    v.set_book(SYM, [(D(bid), D(1)), (D(bid) - 10, D(2))], [(D(ask), D(1)), (D(ask) + 10, D(2))])
    v.set_borrow("BTC", D("0.0001"), D(10))
    return v


def req(
    coid: str, side: Side, qty: str, price: str, account: AccountType = SPOT, tif: TimeInForce = TimeInForce.IOC
) -> OrderRequest:
    return OrderRequest(coid, SYM, side, D(qty), D(price), tif, account)


def funded(v: FakeVenue, cid: str = "c1", usdt: str = "1000") -> FakeAccount:
    acc = v.account(cid)
    acc.fund(SPOT, "USDT", D(usdt))
    acc.fund(MARGIN, "USDT", D(usdt))
    return acc


def test_is_an_adapter_and_helpers() -> None:
    acc = funded(venue())
    assert isinstance(acc, ExchangeAdapter)
    assert acc.name == "binance"
    assert base_asset(SYM) == "BTC"
    with pytest.raises(ValueError):
        base_asset("BTCEUR")
    with pytest.raises(TypeError):
        ExchangeAdapter()  # type: ignore[abstract]


async def test_full_margin_cycle_borrow_sell_buy_back_repay() -> None:
    v = venue()
    acc = funded(v)
    assert (await acc.get_borrowable("BTC")).qty > D("0.005")
    await acc.borrow("BTC", D("0.005"), "b1")
    sell = await acc.place_order(req("s1", Side.SELL, "0.005", "99900", MARGIN))
    assert sell.status is OrderStatus.FILLED and sell.avg_price == D(99990) and sell.fee_asset == "USDT"
    v.clock.advance(3600)
    loan = await acc.get_loan("BTC")
    assert loan.principal == D("0.005") and loan.interest == D("0.0000005")
    buy = await acc.place_order(req("r1", Side.BUY, "0.00501", "100010", MARGIN))
    assert buy.fee_asset == "BTC"
    loan = await acc.get_loan("BTC")
    rep = await acc.repay("BTC", loan.outstanding, "p1")
    assert rep.remaining.outstanding == 0
    assert (await acc.get_loan("BTC")).outstanding == 0
    assert await acc.get_liabilities() == []


async def test_repay_pays_interest_first_and_rejects_bad_amounts() -> None:
    v = venue()
    acc = funded(v)
    await acc.borrow("BTC", D("0.01"), "b1")
    v.clock.advance(3600)
    r = await acc.repay("BTC", D("0.0000010"), "p1")
    assert r.remaining.interest == 0 and r.remaining.principal == D("0.01")
    with pytest.raises(InvalidQuantity):
        await acc.repay("BTC", D("1"), "p2")
    with pytest.raises(InvalidQuantity):
        await acc.repay("BTC", D(0), "p3")
    acc.drift(MARGIN, "BTC", D("-0.01"))  # drift: coin vanished
    with pytest.raises(InsufficientFunds):
        await acc.repay("BTC", D("0.01"), "p4")
    with pytest.raises(Rejected) as e:
        await acc.repay("BTC", D("0.0000001"), "p1")
    assert e.value.code == DUPLICATE_CLIENT_ID


async def test_borrow_limits_permissions_and_idempotency() -> None:
    v = venue()
    acc = funded(v)
    with pytest.raises(Rejected) as e:
        await acc.borrow("BTC", D(5), "b1")
    assert e.value.code == "borrow_limit"
    with pytest.raises(InvalidQuantity):
        await acc.borrow("BTC", D(0), "b0")
    await acc.borrow("BTC", D("0.001"), "b2")
    with pytest.raises(Rejected) as e:
        await acc.borrow("BTC", D("0.001"), "b2")
    assert e.value.code == DUPLICATE_CLIENT_ID
    assert (await acc.get_loan("BTC")).principal == D("0.001")
    ro = v.account("ro", Permissions(read=True, trade=False, margin=False, borrow=False, withdraw=False))
    for call in (ro.borrow("BTC", D("0.001"), "x"), ro.repay("BTC", D("0.001"), "y")):
        with pytest.raises(AuthError):
            await call
    with pytest.raises(AuthError):
        await ro.place_order(req("o", Side.BUY, "0.001", "100000"))
    assert (await ro.check_permissions()).withdraw is False


async def test_order_validation_and_funds() -> None:
    acc = funded(venue(), usdt="100")
    bad = [
        ("a", "0.00001", "100000"),  # below min notional (1 USDT)
        ("b", "0.000011", "100000"),  # off step...
        ("c", "0.001", "100000.005"),  # off tick
        ("d", "0.000001", "100000"),  # below min_qty
        ("e", "101", "100000"),  # above max_qty
        ("f", "-1", "100000"),
    ]
    for coid, qty, price in bad:
        with pytest.raises(InvalidQuantity):
            await acc.place_order(req(coid, Side.BUY, qty, price))
    with pytest.raises(InsufficientFunds):
        await acc.place_order(req("g", Side.BUY, "0.01", "100000"))
    with pytest.raises(InsufficientFunds):
        await acc.place_order(req("h", Side.SELL, "0.01", "99000"))
    with pytest.raises(Rejected) as e:
        await acc.place_order(OrderRequest("z", "ETHUSDT", Side.BUY, D(1), D(1), TimeInForce.IOC, SPOT))
    assert e.value.code == "unknown_symbol"


async def test_spot_buy_fee_in_coin_and_book_consumed() -> None:
    v = venue()
    acc = funded(v, usdt="1000000")
    r = await acc.place_order(req("o1", Side.BUY, "1.5", "100010"))
    assert r.status is OrderStatus.FILLED and r.avg_price == (D(100000) * 1 + D(100010) * D("0.5")) / D("1.5")
    assert r.fee == D("0.0015") and acc.balance(SPOT, "BTC") == D("1.4985")
    assert (await acc.get_balances(SPOT))[0].asset == "BTC"
    book = await acc.get_orderbook(SYM, 5)
    assert book.asks[0].qty == D("1.5") and book.asks[0].price == D(100010)


async def test_protected_limit_never_fills_worse_and_fok_is_all_or_nothing() -> None:
    v = venue()
    acc = funded(v, usdt="1000000")
    r = await acc.place_order(req("o1", Side.BUY, "0.5", "99999"))  # limit below best ask
    assert r.status is OrderStatus.EXPIRED and r.filled_qty == 0 and acc.balance(SPOT, "USDT") == D(1000000)
    r = await acc.place_order(req("o2", Side.BUY, "2", "100000", tif=TimeInForce.FOK))  # only 1 at <=100000
    assert r.status is OrderStatus.EXPIRED and (await acc.get_orderbook(SYM, 1)).asks[0].qty == 1
    r = await acc.place_order(req("o3", Side.BUY, "3", "100010", tif=TimeInForce.IOC))
    assert r.status is OrderStatus.FILLED
    r = await acc.place_order(req("o4", Side.BUY, "1", "100010"))
    assert r.status is OrderStatus.EXPIRED  # book empty


async def test_timeout_that_fills_then_query_by_client_id() -> None:
    v = venue()
    acc = funded(v, usdt="1000000")
    v.add_fault(Op.PLACE_ORDER, Fault(FaultKind.TIMEOUT_APPLIED))
    with pytest.raises(UnknownOutcome) as e:
        await acc.place_order(req("o1", Side.BUY, "0.1", "100000"))
    assert e.value.client_id == "o1" and e.value.operation == "place_order"
    got = await acc.get_order("o1", SYM, SPOT)
    assert got.status is OrderStatus.FILLED and acc.balance(SPOT, "BTC") == D("0.0999")
    with pytest.raises(Rejected) as dup:  # a blind retry must not execute twice
        await acc.place_order(req("o1", Side.BUY, "0.1", "100000"))
    assert dup.value.code == DUPLICATE_CLIENT_ID and acc.balance(SPOT, "BTC") == D("0.0999")
    assert (await acc.cancel_order("o1", SYM, SPOT)).status is OrderStatus.FILLED


async def test_timeout_not_applied_leaves_no_order() -> None:
    v = venue()
    acc = funded(v)
    v.add_fault(Op.PLACE_ORDER, Fault(FaultKind.TIMEOUT_NOT_APPLIED))
    with pytest.raises(UnknownOutcome):
        await acc.place_order(req("o1", Side.BUY, "0.1", "100000"))
    for call in (acc.get_order("o1", SYM, SPOT), acc.cancel_order("o1", SYM, SPOT)):
        with pytest.raises(Rejected) as e:
            await call
        assert e.value.code == ORDER_NOT_FOUND
    assert acc.balance(SPOT, "USDT") == D(1000)
    with pytest.raises(Rejected):
        await acc.get_order("o1", SYM, MARGIN)


async def test_borrow_repay_withdraw_timeouts_applied() -> None:
    v = venue()
    acc = funded(v)
    acc.permissions = Permissions(True, True, True, True, True)
    v.add_fault(Op.BORROW, Fault(FaultKind.TIMEOUT_APPLIED))
    with pytest.raises(UnknownOutcome):
        await acc.borrow("BTC", D("0.002"), "b1")
    assert (await acc.get_loan("BTC")).principal == D("0.002")
    acc.fund(MARGIN, "BTC", D("0.000001"))  # the first hour's interest (2e-7) is charged at once
    v.add_fault(Op.REPAY, Fault(FaultKind.TIMEOUT_APPLIED))
    with pytest.raises(UnknownOutcome):
        await acc.repay("BTC", D("0.0020002"), "p1")
    assert (await acc.get_loan("BTC")).outstanding == 0
    acc.fund(SPOT, "BTC", D(1))
    v.add_fault(Op.WITHDRAW, Fault(FaultKind.TIMEOUT_APPLIED))
    with pytest.raises(UnknownOutcome):
        await acc.withdraw("BTC", D("0.5"), "BTC", "addr", "w1")
    assert (await acc.get_transfer_status("w1")).state is TransferState.COMPLETED


async def test_partial_fill_ioc_only() -> None:
    v = venue()
    acc = funded(v, usdt="1000000")
    v.add_fault(Op.PLACE_ORDER, Fault(FaultKind.PARTIAL_FILL, fraction=D("0.3")))
    r = await acc.place_order(req("o1", Side.BUY, "0.5", "100000"))
    assert r.status is OrderStatus.PARTIALLY_FILLED and r.filled_qty == D("0.15")
    v.add_fault(Op.PLACE_ORDER, Fault(FaultKind.PARTIAL_FILL))
    r = await acc.place_order(req("o2", Side.BUY, "0.0001", "100000", tif=TimeInForce.FOK))
    assert r.status is OrderStatus.FILLED and r.filled_qty == D("0.0001")  # FOK ignores the partial-fill fault


@pytest.mark.parametrize(
    ("kind", "exc"),
    [
        (FaultKind.REJECT, Rejected),
        (FaultKind.RATE_LIMIT, RateLimited),
        (FaultKind.EXCHANGE_DOWN, ExchangeDown),
        (FaultKind.AUTH_ERROR, AuthError),
    ],
)
async def test_error_faults_scoped_and_consumed(kind: FaultKind, exc: type[Exception]) -> None:
    v = venue()
    a, b = funded(v, "a"), funded(v, "b")
    v.add_fault(Op.GET_BALANCES, Fault(kind, times=2, client_id="a", code="boom"))
    with pytest.raises(exc):
        await a.get_balances(SPOT)
    assert await b.get_balances(SPOT)  # other client unaffected
    with pytest.raises(exc):
        await a.get_balances(SPOT)
    assert await a.get_balances(SPOT)  # consumed
    v.add_fault(Op.GET_FEES, Fault(FaultKind.REJECT))
    v.clear_faults()
    assert (await a.get_fees(SYM)).taker == D("0.001")


async def test_rejected_order_does_not_move_funds() -> None:
    v = venue()
    acc = funded(v)
    v.add_fault(Op.PLACE_ORDER, Fault(FaultKind.REJECT))
    with pytest.raises(Rejected):
        await acc.place_order(req("o1", Side.BUY, "0.1", "100000"))
    assert acc.balance(SPOT, "USDT") == D(1000)


async def test_stale_book_fault_and_unknown_symbol() -> None:
    v = venue()
    acc = funded(v)
    v.add_fault(Op.GET_ORDERBOOK, Fault(FaultKind.STALE_BOOK, age_s=45))
    stale = await acc.get_orderbook(SYM, 1)
    assert v.clock.now - stale.ts == 45
    assert v.clock.now - (await acc.get_orderbook(SYM, 1)).ts == 0
    with pytest.raises(Rejected):
        await acc.get_orderbook("XYZUSDT", 1)
    with pytest.raises(Rejected):
        await acc.get_symbol_rules("XYZUSDT")
    assert await acc.get_symbol_rules(SYM) == RULES
    assert (await acc.get_borrow_rate("BTC")).rate_per_hour == D("0.0001")


async def test_stream_snapshot_update_drop_and_resync() -> None:
    v = venue()
    acc = funded(v)
    stream = acc.stream_orderbook(SYM, 1)
    first = await anext(stream)
    assert first.asks[0].price == D(100000)
    v.set_book(SYM, [(D(99000), D(1))], [(D(99100), D(1))])
    assert (await anext(stream)).asks[0].price == D(99100)
    v.drop_streams()
    with pytest.raises(StreamDropped):
        await anext(stream)
    assert issubclass(StreamDropped, ExchangeDown)
    resynced = acc.stream_orderbook(SYM, 1)
    assert (await anext(resynced)).asks[0].price == D(99100)  # fresh snapshot after reconnect
    assert isinstance(resynced, AsyncGenerator)
    await resynced.aclose()
    assert v._subs[SYM] == []


async def test_latency_advances_fake_clock_only() -> None:
    v = venue()
    v.latency_s = 0.25
    acc = funded(v)
    t0 = v.clock.now
    await acc.get_balances(SPOT)
    await acc.get_balances(SPOT)
    assert v.clock.now == t0 + 0.5


async def test_rate_limits_weights_ip_sharing_and_ban() -> None:
    v = venue()
    v.rate_limit = RateLimitConfig(
        window_s=1, ip_budget=10, client_budget=4, ban_after_violations=3, ban_s=60, weights={Op.PLACE_ORDER: 2}
    )
    a, b = funded(v, "a"), funded(v, "b")
    await a.get_balances(SPOT)
    await a.get_balances(SPOT)
    await a.place_order(req("o1", Side.BUY, "0.001", "100000"))  # weight 2 -> client budget 4 used
    with pytest.raises(RateLimited) as e:
        await a.get_balances(SPOT)
    assert not e.value.banned and e.value.retry_after_s is not None
    for _ in range(4):
        await b.get_balances(SPOT)  # IP used: 8
    c = funded(v, "c")
    await c.get_balances(SPOT)
    await c.get_balances(SPOT)
    with pytest.raises(RateLimited):  # shared IP budget (10) exhausted although c is under its own
        await c.get_balances(SPOT)
    with pytest.raises(RateLimited) as banned:  # third violation -> ban
        await c.get_balances(SPOT)
    assert banned.value.banned
    v.clock.advance(1.5)
    with pytest.raises(RateLimited) as still:
        await a.get_balances(SPOT)
    assert still.value.banned
    v.clock.advance(61)
    await a.get_balances(SPOT)  # ban expired, new window


async def test_margin_account_and_borrowable_follow_collateral() -> None:
    v = venue()
    acc = funded(v, usdt="500")
    m = await acc.get_margin_account()
    assert m.equity_usdt == 500 and m.liabilities_usdt == 0 and m.margin_level is None
    assert (await acc.get_borrowable("BTC")).qty == D(1000) / D(99995)
    await acc.borrow("BTC", D("0.004"), "b1")
    m = await acc.get_margin_account()
    interest_usdt = D("0.0000004") * D("99995")  # first billed hour, charged when the loan opens
    assert m.liabilities_usdt == D("399.98") + interest_usdt
    assert m.equity_usdt == 500 - interest_usdt and m.margin_level == (500 + D("399.98")) / m.liabilities_usdt
    assert [loan.asset for loan in await acc.get_liabilities()] == ["BTC"]
    v.set_borrow("BTC", D("0.0001"), D("0.0001"))
    assert (await acc.get_borrowable("BTC")).qty <= D("0.0001")  # pool caps it
    empty = v.account("nobook")
    v2 = FakeVenue("x")
    with pytest.raises(Rejected):
        v2.mid_price("BTC")
    assert (await empty.get_margin_account()).equity_usdt == 0


async def test_transfers() -> None:
    v = venue()
    acc = funded(v)
    acc.fund(SPOT, "BTC", D(1))
    with pytest.raises(AuthError):  # default keys have no withdraw permission
        await acc.withdraw("BTC", D("0.5"), "BTC", "addr", "w1")
    w = v.account("w", Permissions(True, True, True, True, True))
    w.fund(SPOT, "BTC", D(1))
    t = await w.withdraw("BTC", D("0.5"), "BTC", "addr", "w1")
    assert t.state is TransferState.COMPLETED and w.balance(SPOT, "BTC") == D("0.5")
    with pytest.raises(Rejected):
        await w.withdraw("BTC", D("0.1"), "BTC", "addr", "w1")
    with pytest.raises(InsufficientFunds):
        await w.withdraw("BTC", D(5), "BTC", "addr", "w2")
    with pytest.raises(Rejected) as e:
        await w.get_transfer_status("nope")
    assert e.value.code == ORDER_NOT_FOUND
    assert "w" in (await w.get_deposit_address("BTC", "BTC")).address
    lim = await w.get_withdraw_fee_and_min("BTC", "BTC")
    assert lim.fee == D("0.0005") and lim.asset == "BTC"


async def test_300_clients_share_one_book_without_leaking() -> None:
    v = venue()
    v.set_book(SYM, [(D(99990), D(100))], [(D(100000), D(100))])
    accounts = [funded(v, f"c{i:03d}", usdt="1000") for i in range(300)]
    assert len({id(a) for a in accounts}) == 300 and v.account("c000") is accounts[0]
    for i, acc in enumerate(accounts):
        r = await acc.place_order(req(client_order_id(f"t{i}", "A_BUY", 1), Side.BUY, "0.01", "100000"))
        assert r.status is OrderStatus.FILLED
    assert (await accounts[0].get_orderbook(SYM, 1)).asks[0].qty == D(97)
    assert all(a.balance(SPOT, "BTC") == D("0.00999") and a.balance(SPOT, "USDT") == 0 for a in accounts)
    assert accounts[7].balance(MARGIN, "BTC") == 0
    # the same client order id on different accounts does not collide
    other = await accounts[1].get_order(client_order_id("t1", "A_BUY", 1), SYM, SPOT)
    with pytest.raises(Rejected):
        await accounts[0].get_order(client_order_id("t1", "A_BUY", 1), SYM, SPOT)
    assert other.filled_qty == D("0.01")


def test_fake_clock_and_q8() -> None:
    assert fk._q8(D("0.000000011")) == D("0.00000002")
    c = fk.FakeClock(5.0)
    c.advance(1)
    assert c.now == 6.0


async def test_hang_faults_never_return_and_apply_or_not() -> None:
    v = venue()
    acc = funded(v, usdt="1000000")
    v.add_fault(Op.PLACE_ORDER, Fault(FaultKind.HANG_NOT_APPLIED))
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.05):
            await acc.place_order(req("o1", Side.BUY, "0.1", "100000"))
    with pytest.raises(Rejected):
        await acc.get_order("o1", SYM, SPOT)  # never executed
    v.add_fault(Op.PLACE_ORDER, Fault(FaultKind.HANG_APPLIED))
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.05):
            await acc.place_order(req("o2", Side.BUY, "0.1", "100000"))
    assert (await acc.get_order("o2", SYM, SPOT)).status is OrderStatus.FILLED  # executed despite the hang


async def test_topping_up_an_open_loan_pays_the_current_hour_on_the_extra() -> None:
    v = venue()
    acc = funded(v, usdt="100000")
    await acc.borrow("BTC", D("0.01"), "b1")
    first = await acc.get_loan("BTC")
    await acc.borrow("BTC", D("0.01"), "b2")
    second = await acc.get_loan("BTC")
    assert second.principal == D("0.02") and second.interest == first.interest + D("0.000001")
