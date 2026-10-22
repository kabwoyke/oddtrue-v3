"""Phase 5a: Binance read-only adapter against a scripted transport (no network, no keys)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

from oddtrue.exchanges.binance import BinanceHttp, BinanceReadOnlyAdapter, ReadOnlyViolation
from oddtrue.exchanges.binance.depth import DepthBook
from oddtrue.exchanges.binance.http import READ_ONLY_PATHS, RawResponse, map_error
from oddtrue.exchanges.errors import (
    AuthError,
    ExchangeDown,
    InvalidQuantity,
    RateLimited,
    Rejected,
    StreamDropped,
)
from oddtrue.exchanges.models import AccountType, OrderRequest, TimeInForce
from oddtrue.pricing.types import Side
from oddtrue.ratelimit import Priority, WeightBudget

D = Decimal
KEY, SECRET = "k" * 20 + "KEYVALUE", "s" * 20 + "SECRETVALUE"


class Clock:
    def __init__(self) -> None:
        self.t = 1_700_000_000.0

    def __call__(self) -> float:
        return self.t


class Scripted:
    """Maps path -> (status, headers, body). Records every URL requested."""

    def __init__(self, routes: dict[str, Any]) -> None:
        self.routes = routes
        self.urls: list[str] = []
        self.headers: list[Mapping[str, str]] = []

    async def get(self, url: str, headers: Mapping[str, str], timeout_s: float) -> RawResponse:
        self.urls.append(url)
        self.headers.append(headers)
        r = self.routes[urlparse(url).path]
        if isinstance(r, Exception):
            raise r
        status, hdrs, body = r if isinstance(r, tuple) else (200, {}, r)
        return RawResponse(status, hdrs, json.dumps(body).encode() if not isinstance(body, bytes) else body)


def make(
    routes: dict[str, Any], *, limit: int = 6000, ws: Any = None
) -> tuple[BinanceReadOnlyAdapter, Scripted, Clock]:
    clock = Clock()
    transport = Scripted(routes)
    budget = WeightBudget(limit=limit, window_s=60, clock=clock, exchange="binance")
    http = BinanceHttp(api_key=KEY, secret=SECRET, transport=transport, clock=clock, budget=budget)
    return BinanceReadOnlyAdapter(http, clock=clock, ws_connect=ws), transport, clock


DEPTH = {"lastUpdateId": 100, "bids": [["99.5", "2"], ["99.0", "5"]], "asks": [["100.5", "1"], ["101.0", "4"]]}


async def test_orderbook_parses_and_orders_levels() -> None:
    a, t, _ = make({"/api/v3/depth": DEPTH})
    book = await a.get_orderbook("BTCUSDT", 5)
    assert [lv.price for lv in book.bids] == [D("99.5"), D("99.0")]
    assert [lv.price for lv in book.asks] == [D("100.5"), D("101.0")]
    assert parse_qs(urlparse(t.urls[0]).query) == {"symbol": ["BTCUSDT"], "limit": ["5"]}
    assert "signature" not in t.urls[0]


@pytest.mark.parametrize(
    ("depth", "limit", "weight"), [(5, 5, 5), (30, 50, 5), (150, 500, 25), (900, 1000, 50), (3000, 5000, 250)]
)
async def test_depth_limit_and_weight_tiers(depth: int, limit: int, weight: int) -> None:
    a, t, _ = make({"/api/v3/depth": DEPTH}, limit=6000)
    await a.get_orderbook("BTCUSDT", depth)
    assert parse_qs(urlparse(t.urls[0]).query)["limit"] == [str(limit)]
    # weight charged to the local budget
    assert a._http._budget.available == D(6000) - weight


EXCHANGE_INFO = {
    "symbols": [
        {
            "symbol": "BTCUSDT",
            "status": "TRADING",
            "filters": [
                {"filterType": "PRICE_FILTER", "minPrice": "0.01", "maxPrice": "1000000", "tickSize": "0.01"},
                {"filterType": "LOT_SIZE", "minQty": "0.00001", "maxQty": "9000", "stepSize": "0.00001"},
                {"filterType": "NOTIONAL", "minNotional": "5.0", "applyMinToMarket": True},
            ],
        }
    ]
}


async def test_symbol_rules() -> None:
    a, _, _ = make({"/api/v3/exchangeInfo": EXCHANGE_INFO})
    r = await a.get_symbol_rules("BTCUSDT")
    assert (r.min_qty, r.max_qty, r.step, r.tick, r.min_notional) == (
        D("0.00001"),
        D("9000"),
        D("0.00001"),
        D("0.01"),
        D("5.0"),
    )


async def test_symbol_rules_legacy_min_notional_and_failures() -> None:
    legacy = json.loads(json.dumps(EXCHANGE_INFO))
    legacy["symbols"][0]["filters"][2] = {"filterType": "MIN_NOTIONAL", "minNotional": "10"}
    a, _, _ = make({"/api/v3/exchangeInfo": legacy})
    assert (await a.get_symbol_rules("BTCUSDT")).min_notional == D("10")

    halted = json.loads(json.dumps(EXCHANGE_INFO))
    halted["symbols"][0]["status"] = "BREAK"
    a, _, _ = make({"/api/v3/exchangeInfo": halted})
    with pytest.raises(Rejected, match="BREAK"):
        await a.get_symbol_rules("BTCUSDT")

    a, _, _ = make({"/api/v3/exchangeInfo": {"symbols": []}})
    with pytest.raises(Rejected):
        await a.get_symbol_rules("BTCUSDT")

    nofilter = json.loads(json.dumps(EXCHANGE_INFO))
    nofilter["symbols"][0]["filters"] = []
    a, _, _ = make({"/api/v3/exchangeInfo": nofilter})
    with pytest.raises(Rejected, match="lacks filter"):
        await a.get_symbol_rules("BTCUSDT")


async def test_fees_add_tax_and_ignore_discount() -> None:
    body = {
        "standardCommission": {"maker": "0.00100000", "taker": "0.00100000"},
        "taxCommission": {"maker": "0.00000000", "taker": "0.00010000"},
        "discount": {"enabledForAccount": True, "discount": "0.75000000"},
    }
    a, t, _ = make({"/api/v3/account/commission": body})
    f = await a.get_fees("BTCUSDT")
    assert (f.maker, f.taker) == (D("0.001"), D("0.0011"))
    assert "signature=" in t.urls[0]


ACCOUNT = {
    "balances": [
        {"asset": "USDT", "free": "500.5", "locked": "0"},
        {"asset": "BTC", "free": "0", "locked": "0.2"},
        {"asset": "ETH", "free": "0", "locked": "0"},
    ]
}
MARGIN = {
    "marginLevel": "5",
    "totalNetAssetOfBtc": "0.5",
    "totalLiabilityOfBtc": "0.1",
    "userAssets": [
        {"asset": "BTC", "borrowed": "0.1", "free": "0.3", "interest": "0.00001", "locked": "0", "netAsset": "0.2"},
        {"asset": "USDT", "borrowed": "0", "free": "100", "interest": "0", "locked": "0", "netAsset": "100"},
    ],
}


async def test_balances_spot_and_margin_drop_zero_rows() -> None:
    a, _, _ = make({"/api/v3/account": ACCOUNT, "/sapi/v1/margin/account": MARGIN})
    spot = await a.get_balances(AccountType.SPOT)
    assert {(b.asset, b.free, b.locked) for b in spot} == {("USDT", D("500.5"), D(0)), ("BTC", D(0), D("0.2"))}
    margin = await a.get_balances(AccountType.MARGIN)
    assert {b.asset for b in margin} == {"BTC", "USDT"}


async def test_margin_account_values_in_usdt() -> None:
    a, _, _ = make({"/sapi/v1/margin/account": MARGIN, "/api/v3/ticker/price": {"symbol": "BTCUSDT", "price": "50000"}})
    m = await a.get_margin_account()
    assert (m.equity_usdt, m.liabilities_usdt, m.margin_level) == (D(25000), D(5000), D(5))


async def test_margin_level_none_without_liabilities() -> None:
    flat = {**MARGIN, "marginLevel": "999", "totalLiabilityOfBtc": "0"}
    a, _, _ = make({"/sapi/v1/margin/account": flat, "/api/v3/ticker/price": {"price": "50000"}})
    assert (await a.get_margin_account()).margin_level is None


async def test_loans_and_liabilities_from_exchange_report() -> None:
    a, _, _ = make({"/sapi/v1/margin/account": MARGIN})
    loans = await a.get_liabilities()
    assert [(x.asset, x.principal, x.interest, x.outstanding) for x in loans] == [
        ("BTC", D("0.1"), D("0.00001"), D("0.10001"))
    ]
    assert (await a.get_loan("BTC")).outstanding == D("0.10001")
    zero = await a.get_loan("DOGE")
    assert zero.outstanding == 0


async def test_borrowable_takes_the_smaller_limit() -> None:
    a, t, _ = make({"/sapi/v1/margin/maxBorrowable": {"amount": "1.69", "borrowLimit": "60"}})
    assert (await a.get_borrowable("BTC")).qty == D("1.69")
    a, _, _ = make({"/sapi/v1/margin/maxBorrowable": {"amount": "10", "borrowLimit": "3"}})
    assert (await a.get_borrowable("BTC")).qty == D(3)
    assert parse_qs(urlparse(t.urls[0]).query)["asset"] == ["BTC"]


async def test_borrow_rate_is_hourly() -> None:
    body = [{"asset": "BTC", "nextHourlyInterestRate": "0.00000571"}]
    a, t, _ = make({"/sapi/v1/margin/next-hourly-interest-rate": body})
    assert (await a.get_borrow_rate("BTC")).rate_per_hour == D("0.00000571")
    assert parse_qs(urlparse(t.urls[0]).query)["isIsolated"] == ["FALSE"]
    a, _, _ = make({"/sapi/v1/margin/next-hourly-interest-rate": []})
    with pytest.raises(Rejected):
        await a.get_borrow_rate("BTC")


async def test_permissions_mapping_and_unreadable_key() -> None:
    body = {"enableReading": True, "enableWithdrawals": True, "enableMargin": True, "enableSpotAndMarginTrading": False}
    a, _, _ = make({"/sapi/v1/account/apiRestrictions": body})
    p = await a.check_permissions()
    assert (p.read, p.trade, p.margin, p.borrow, p.withdraw) == (True, False, True, True, True)
    a, _, _ = make({"/sapi/v1/account/apiRestrictions": {**body, "enableReading": False}})
    with pytest.raises(AuthError):
        await a.check_permissions()


# ---- read-only guarantee ----
async def test_every_money_moving_method_is_refused_without_a_request() -> None:
    a, t, _ = make({})
    req = OrderRequest("c1", "BTCUSDT", Side.BUY, D(1), D(1), TimeInForce.IOC, AccountType.SPOT)
    calls = [
        a.place_order(req),
        a.get_order("c1", "BTCUSDT", AccountType.SPOT),
        a.cancel_order("c1", "BTCUSDT", AccountType.SPOT),
        a.borrow("BTC", D(1), "c1"),
        a.repay("BTC", D(1), "c1"),
        a.get_deposit_address("BTC", "BTC"),
        a.withdraw("BTC", D(1), "BTC", "addr", "c1"),
        a.get_transfer_status("c1"),
        a.get_withdraw_fee_and_min("BTC", "BTC"),
    ]
    for c in calls:
        with pytest.raises(ReadOnlyViolation) as e:
            await c
        assert e.value.code == "read_only"
    assert t.urls == []


async def test_http_layer_refuses_non_allowlisted_paths() -> None:
    a, t, _ = make({})
    for path in (
        "/api/v3/order",
        "/sapi/v1/margin/borrow-repay",
        "/sapi/v1/capital/withdraw/apply",
        "/sapi/v1/margin/order",
    ):
        with pytest.raises(ReadOnlyViolation):
            await a._http.get(path, weight=1, priority=Priority.RECOVERY)
    assert t.urls == []


def test_allowlist_has_no_trading_paths() -> None:
    banned = ("order", "borrow", "repay", "withdraw", "transfer", "loan")
    # maxBorrowable only reads a limit; it is the one allowed path with "borrow" in its name
    assert not [
        p for p in READ_ONLY_PATHS if p != "/sapi/v1/margin/maxBorrowable" and any(w in p.lower() for w in banned)
    ]


# ---- signing, secrets ----
async def test_signed_request_carries_valid_hmac_and_key_header_only() -> None:
    import hashlib
    import hmac

    a, t, _ = make(
        {
            "/sapi/v1/account/apiRestrictions": {
                "enableReading": True,
                "enableSpotAndMarginTrading": True,
                "enableMargin": True,
                "enableWithdrawals": False,
            }
        }
    )
    await a.check_permissions()
    url = t.urls[0]
    query, sig = url.split("?", 1)[1].rsplit("&signature=", 1)
    assert sig == hmac.new(SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()
    assert "timestamp=1700000000000" in query and "recvWindow=5000" in query
    assert SECRET not in url and KEY not in url
    assert t.headers[0] == {"X-MBX-APIKEY": KEY}


def test_repr_hides_credentials() -> None:
    a, _, _ = make({})
    assert KEY not in repr(a._http) and SECRET not in repr(a._http)


# ---- error mapping ----
@pytest.mark.parametrize(
    ("status", "headers", "body", "kind", "banned"),
    [
        (429, {"retry-after": "7"}, b"", RateLimited, False),
        (418, {"retry-after": "120"}, b"", RateLimited, True),
        (403, {}, b"", RateLimited, True),
        (503, {}, b"", ExchangeDown, None),
        (401, {}, b"", AuthError, None),
        (400, {}, b'{"code": -2015, "msg": "Invalid API-key"}', AuthError, None),
        (400, {}, b'{"code": -1013, "msg": "Filter failure"}', InvalidQuantity, None),
        (400, {}, b'{"code": -1003, "msg": "too many"}', RateLimited, False),
        (400, {}, b'{"code": -1121, "msg": "Invalid symbol."}', Rejected, None),
        (400, {}, b"not json", Rejected, None),
    ],
)
def test_error_mapping(
    status: int, headers: dict[str, str], body: bytes, kind: type[Exception], banned: bool | None
) -> None:
    err = map_error(status, headers, body)
    assert type(err) is kind
    if banned is not None:
        assert isinstance(err, RateLimited) and err.banned is banned


async def test_429_bans_locally_so_nothing_more_is_sent() -> None:
    a, t, clock = make({"/api/v3/depth": (429, {"retry-after": "30"}, b"")})
    with pytest.raises(RateLimited):
        await a.get_orderbook("BTCUSDT", 5)
    with pytest.raises(RateLimited) as e:
        await a.get_orderbook("BTCUSDT", 5)
    assert e.value.code == "local_ban" and len(t.urls) == 1
    clock.t += 31
    t.routes["/api/v3/depth"] = DEPTH
    await a.get_orderbook("BTCUSDT", 5)
    assert len(t.urls) == 2


async def test_network_failure_and_bad_body_are_exchange_down() -> None:
    a, _, _ = make({"/api/v3/depth": TimeoutError()})
    with pytest.raises(ExchangeDown):
        await a.get_orderbook("BTCUSDT", 5)
    a, _, _ = make({"/api/v3/depth": (200, {}, b"<html>")})
    with pytest.raises(ExchangeDown):
        await a.get_orderbook("BTCUSDT", 5)


async def test_used_weight_header_tightens_local_budget() -> None:
    a, _, _ = make({"/api/v3/depth": (200, {"x-mbx-used-weight-1m": "5900"}, DEPTH)})
    await a.get_orderbook("BTCUSDT", 5)
    assert a._http._budget.available <= D(100)


# ---- budget ----
def test_budget_priority_reserve_and_refill() -> None:
    clock = Clock()
    b = WeightBudget(limit=100, window_s=10, clock=clock)
    b.acquire(60, Priority.BACKGROUND)  # 40 left, below the 35 floor only after the next spend
    with pytest.raises(RateLimited) as e:
        b.acquire(10, Priority.BACKGROUND)  # would leave 30 < 35
    assert e.value.retry_after_s is not None and e.value.retry_after_s > 0
    b.acquire(10, Priority.RECOVERY)  # recovery may dig into the reserve
    b.acquire(30, Priority.RECOVERY)
    assert b.available == 0
    clock.t += 5
    assert b.available == 50
    with pytest.raises(ValueError):
        WeightBudget(limit=0, window_s=1, clock=clock)


# ---- depth sync ----
def ev(
    first: int, final: int, bids: list[list[str]] | None = None, asks: list[list[str]] | None = None
) -> dict[str, Any]:
    return {"e": "depthUpdate", "U": first, "u": final, "b": bids or [], "a": asks or []}


def test_depth_sync_rules() -> None:
    book = DepthBook("BTCUSDT", DEPTH)
    assert book.apply(ev(90, 100)) is False  # entirely old
    assert book.apply(ev(99, 102, [["99.5", "0"], ["99.7", "1"]], [["100.5", "3"]])) is True  # straddles 101
    top = book.top(5, 1.0)
    assert [(lv.price, lv.qty) for lv in top.bids] == [(D("99.7"), D(1)), (D("99.0"), D(5))]
    assert top.asks[0].qty == D(3)
    with pytest.raises(StreamDropped):
        book.apply(ev(104, 105))  # gap after 102
    assert book.apply(ev(103, 103, asks=[["101.0", "0"]])) is True
    assert [lv.price for lv in book.top(5, 1.0).asks] == [D("100.5")]


def test_depth_sync_gap_before_first_event() -> None:
    with pytest.raises(StreamDropped):
        DepthBook("BTCUSDT", DEPTH).apply(ev(150, 160))


@asynccontextmanager
async def fake_ws(events: list[Any]) -> AsyncIterator[AsyncIterator[str]]:
    async def gen() -> AsyncIterator[str]:
        for e in events:
            yield json.dumps(e)

    yield gen()


async def test_stream_yields_snapshot_then_updates_and_drops_on_gap() -> None:
    events = [
        {"e": "other"},
        ev(90, 99),  # old, ignored
        ev(100, 101, bids=[["99.9", "1"]]),
        ev(102, 102, asks=[["100.4", "2"]]),
        ev(110, 111),  # gap
    ]
    urls: list[str] = []

    def connect(url: str) -> Any:
        urls.append(url)
        return fake_ws(events)

    a, _, _ = make({"/api/v3/depth": DEPTH}, ws=connect)
    seen = []
    with pytest.raises(StreamDropped):
        async for book in a.stream_orderbook("BTCUSDT", 5):
            seen.append(book)
    assert urls == ["wss://stream.binance.com:9443/ws/btcusdt@depth@100ms"]
    assert [b.bids[0].price for b in seen] == [D("99.5"), D("99.9"), D("99.9")]
    assert seen[2].asks[0].price == D("100.4")


async def test_stream_without_connector_is_exchange_down() -> None:
    a, _, _ = make({"/api/v3/depth": DEPTH})
    with pytest.raises(ExchangeDown):
        async for _ in a.stream_orderbook("BTCUSDT", 5):
            pass


async def test_budget_exhaustion_blocks_before_sending() -> None:
    a, t, _ = make({"/api/v3/depth": DEPTH}, limit=5)
    with pytest.raises(RateLimited):
        await a.get_orderbook("BTCUSDT", 5)  # weight 5 would leave less than the revalidation reserve
    assert t.urls == []
    assert asyncio.iscoroutinefunction(a.get_orderbook)
