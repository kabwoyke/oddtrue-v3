"""Phase 5b: Bitget read-only adapter against a scripted transport (no network, no keys)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from collections.abc import Mapping
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

from oddtrue.exchanges.bitget import BitgetHttp, BitgetReadOnlyAdapter, ReadOnlyViolation
from oddtrue.exchanges.bitget.http import READ_ONLY_PATHS, RawResponse, map_error
from oddtrue.exchanges.errors import AuthError, ExchangeDown, RateLimited, Rejected
from oddtrue.exchanges.models import AccountType, OrderRequest, TimeInForce
from oddtrue.exchanges.readonly import ReadOnlyViolation as SharedViolation
from oddtrue.pricing.types import Side
from oddtrue.ratelimit import Priority, WeightBudget

D = Decimal
KEY, SECRET, PASS = "bg_" + "k" * 20, "s" * 24 + "SECRETVALUE", "pass" + "p" * 10


class Clock:
    def __init__(self) -> None:
        self.t = 1_700_000_000.0

    def __call__(self) -> float:
        return self.t


def ok(data: Any) -> tuple[int, dict[str, str], bytes]:
    return 200, {}, json.dumps({"code": "00000", "msg": "success", "requestTime": 1, "data": data}).encode()


class Scripted:
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
        return RawResponse(*r)


def make(
    routes: dict[str, Any], *, ip_limit: int = 20, uid_limit: int = 10
) -> tuple[BitgetReadOnlyAdapter, Scripted, Clock]:
    clock = Clock()
    transport = Scripted(routes)
    ip = WeightBudget(limit=ip_limit, window_s=1, clock=clock, exchange="bitget")
    uid = WeightBudget(limit=uid_limit, window_s=1, clock=clock, exchange="bitget")
    http = BitgetHttp(
        api_key=KEY, secret=SECRET, passphrase=PASS, transport=transport, clock=clock, ip_budget=ip, uid_budget=uid
    )

    async def no_sleep(_: float) -> None:
        clock.t += 1

    return BitgetReadOnlyAdapter(http, clock=clock, sleep=no_sleep), transport, clock


BOOK = {
    "asks": [["100.5", "1"], ["101", "4"], ["102", "0"]],
    "bids": [["99.5", "2"], ["99", "5"]],
    "ts": "1700000000500",
}


async def test_orderbook_uses_matching_engine_time_and_drops_empty_levels() -> None:
    a, t, _ = make({"/api/v2/spot/market/orderbook": ok(BOOK)})
    b = await a.get_orderbook("BTCUSDT", 5)
    assert [lv.price for lv in b.asks] == [D("100.5"), D(101)]
    assert [lv.price for lv in b.bids] == [D("99.5"), D(99)]
    assert b.ts == 1_700_000_000.5
    assert parse_qs(urlparse(t.urls[0]).query) == {"symbol": ["BTCUSDT"], "type": ["step0"], "limit": ["5"]}
    assert "ACCESS-KEY" not in t.headers[0]  # public call carries no credentials


async def test_orderbook_depth_is_capped_at_150_and_truncated() -> None:
    a, t, _ = make({"/api/v2/spot/market/orderbook": ok(BOOK)})
    b = await a.get_orderbook("BTCUSDT", 1000)
    assert parse_qs(urlparse(t.urls[0]).query)["limit"] == ["150"]
    assert len((await a.get_orderbook("BTCUSDT", 1)).bids) == 1
    assert len(b.bids) == 2


SYMBOL = {
    "symbol": "BTCUSDT",
    "status": "online",
    "minTradeAmount": "0",
    "maxTradeAmount": "900000000000000000000",
    "takerFeeRate": "0.002",
    "makerFeeRate": "0.001",
    "pricePrecision": "2",
    "quantityPrecision": "6",
    "minTradeUSDT": "1",
}


async def test_symbol_rules_and_fees() -> None:
    a, _, _ = make({"/api/v2/spot/public/symbols": ok([SYMBOL])})
    r = await a.get_symbol_rules("BTCUSDT")
    assert (r.min_qty, r.step, r.tick, r.min_notional) == (D(0), D("0.000001"), D("0.01"), D(1))
    assert r.max_qty == D("900000000000000000000")
    f = await a.get_fees("BTCUSDT")
    assert (f.maker, f.taker) == (D("0.001"), D("0.002"))


async def test_symbol_rules_edge_cases() -> None:
    a, _, _ = make({"/api/v2/spot/public/symbols": ok([{**SYMBOL, "maxTradeAmount": "0", "minTradeAmount": "0.5"}])})
    r = await a.get_symbol_rules("BTCUSDT")
    assert r.max_qty == D("1e20") and r.min_qty == D("0.5")
    a, _, _ = make({"/api/v2/spot/public/symbols": ok([{**SYMBOL, "status": "halt"}])})
    with pytest.raises(Rejected, match="halt"):
        await a.get_symbol_rules("BTCUSDT")
    a, _, _ = make({"/api/v2/spot/public/symbols": ok([])})
    with pytest.raises(Rejected):
        await a.get_symbol_rules("BTCUSDT")


SPOT = [
    {"coin": "usdt", "available": "500.5", "frozen": "1", "locked": "0.5", "limitAvailable": "0", "uTime": "1"},
    {"coin": "btc", "available": "0", "frozen": "0", "locked": "0", "limitAvailable": "0", "uTime": "1"},
]
CROSS = [
    {
        "coin": "BTC",
        "totalAmount": "0.3",
        "available": "0.3",
        "frozen": "0",
        "borrow": "0.1",
        "interest": "0.00001",
        "net": "0.19999",
        "coupon": "0",
    },
    {
        "coin": "USDT",
        "totalAmount": "100",
        "available": "100",
        "frozen": "0",
        "borrow": "0",
        "interest": "0",
        "net": "100",
        "coupon": "0",
    },
    {
        "coin": "ETH",
        "totalAmount": "0",
        "available": "0",
        "frozen": "0",
        "borrow": "0",
        "interest": "0",
        "net": "0",
        "coupon": "0",
    },
]


async def test_balances_spot_and_margin() -> None:
    a, t, _ = make({"/api/v2/spot/account/assets": ok(SPOT), "/api/v2/margin/crossed/account/assets": ok(CROSS)})
    spot = await a.get_balances(AccountType.SPOT)
    assert [(b.asset, b.free, b.locked) for b in spot] == [("USDT", D("500.5"), D("1.5"))]
    margin = await a.get_balances(AccountType.MARGIN)
    assert {b.asset for b in margin} == {"BTC", "USDT"}
    assert "ACCESS-SIGN" in t.headers[0]


async def test_loans_from_exchange_report() -> None:
    a, _, _ = make({"/api/v2/margin/crossed/account/assets": ok(CROSS)})
    loans = await a.get_liabilities()
    assert [(x.asset, x.principal, x.interest, x.outstanding) for x in loans] == [
        ("BTC", D("0.1"), D("0.00001"), D("0.10001"))
    ]
    assert (await a.get_loan("BTC")).outstanding == D("0.10001")
    assert (await a.get_loan("DOGE")).outstanding == 0


async def test_margin_account_values_in_usdt_with_risk_rate() -> None:
    routes = {
        "/api/v2/margin/crossed/account/assets": ok(CROSS),
        "/api/v2/spot/market/tickers": ok([{"symbol": "BTCUSDT", "lastPr": "50000"}]),
        "/api/v2/margin/crossed/account/risk-rate": ok({"riskRateRatio": "3.5"}),
    }
    a, t, _ = make(routes)
    m = await a.get_margin_account()
    assert m.equity_usdt == D("0.19999") * 50000 + 100
    assert m.liabilities_usdt == D("0.10001") * 50000
    assert m.margin_level == D("3.5")
    assert parse_qs(urlparse(next(u for u in t.urls if "tickers" in u)).query) == {"symbol": ["BTCUSDT"]}


async def test_margin_account_without_debt_has_no_level_and_no_risk_call() -> None:
    flat = [CROSS[1]]
    a, t, _ = make({"/api/v2/margin/crossed/account/assets": ok(flat)})
    m = await a.get_margin_account()
    assert (m.equity_usdt, m.liabilities_usdt, m.margin_level) == (D(100), D(0), None)
    assert not any("risk-rate" in u for u in t.urls)


async def test_margin_account_refuses_to_guess_a_price() -> None:
    routes = {
        "/api/v2/margin/crossed/account/assets": ok(CROSS),
        "/api/v2/spot/market/tickers": ok([]),
    }
    a, _, _ = make(routes)
    with pytest.raises(Rejected, match="no USDT price"):
        await a.get_margin_account()
    routes["/api/v2/spot/market/tickers"] = ok([{"symbol": "BTCUSDT", "lastPr": "0"}])
    with pytest.raises(Rejected):
        await a.get_margin_account()


RATE = [
    {
        "coin": "BTC",
        "leverage": "3",
        "transferable": True,
        "borrowable": True,
        "dailyInterestRate": "0.00024",
        "annualInterestRate": "0.0876",
        "maxBorrowableAmount": "100",
        "vipList": [],
    }
]


async def test_borrowable_needs_flag_and_amount() -> None:
    routes = {
        "/api/v2/margin/crossed/interest-rate-and-limit": ok(RATE),
        "/api/v2/margin/crossed/account/max-borrowable-amount": ok({"coin": "BTC", "maxBorrowableAmount": "1.5"}),
    }
    a, t, _ = make(routes)
    assert (await a.get_borrowable("BTC")).qty == D("1.5")
    assert any("max-borrowable-amount" in u for u in t.urls)
    routes["/api/v2/margin/crossed/interest-rate-and-limit"] = ok([{**RATE[0], "borrowable": False}])
    a, t, _ = make(routes)
    assert (await a.get_borrowable("BTC")).qty == 0
    assert not any("max-borrowable-amount" in u for u in t.urls)  # not borrowable: no further calls


async def test_borrow_rate_is_normalised_to_hourly_and_unknown_coin_rejected() -> None:
    a, _, _ = make({"/api/v2/margin/crossed/interest-rate-and-limit": ok(RATE)})
    assert (await a.get_borrow_rate("BTC")).rate_per_hour == D("0.00001")
    a, _, _ = make({"/api/v2/margin/crossed/interest-rate-and-limit": ok([])})
    with pytest.raises(Rejected, match="not a cross margin coin"):
        await a.get_borrow_rate("BTC")


async def test_permissions_from_authorities() -> None:
    info = {"authorities": ["stor", "smow", "wwow"], "ips": "1.2.3.4"}
    a, _, _ = make({"/api/v2/spot/account/info": ok(info)})
    p = await a.check_permissions()
    assert (p.read, p.trade, p.margin, p.borrow, p.withdraw) == (True, False, True, True, True)
    a, _, _ = make({"/api/v2/spot/account/info": ok({"authorities": ["stow", "smow"]})})
    p = await a.check_permissions()
    assert (p.trade, p.margin, p.withdraw) == (True, True, False)


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
        assert e.value.code == "read_only" and e.value.exchange == "bitget" and isinstance(e.value, SharedViolation)
    assert t.urls == []


async def test_http_layer_refuses_non_allowlisted_paths() -> None:
    a, t, _ = make({})
    paths = (
        "/api/v2/spot/trade/place-order",
        "/api/v2/margin/crossed/account/borrow",
        "/api/v2/margin/crossed/account/repay",
        "/api/v2/spot/wallet/withdrawal",
        "/api/v2/margin/crossed/place-order",
    )
    for path in paths:
        with pytest.raises(ReadOnlyViolation):
            await a._http.get(path, ip_weight=1, priority=Priority.RECOVERY)
    assert t.urls == []


def test_allowlist_has_no_trading_paths() -> None:
    banned = ("place-order", "cancel", "repay", "withdraw", "transfer", "/borrow", "flash", "wallet")
    assert not [p for p in READ_ONLY_PATHS if any(w in p.lower() for w in banned)]


# ---- signing, secrets ----
async def test_signed_request_carries_valid_signature_and_headers() -> None:
    a, t, _ = make(
        {
            "/api/v2/margin/crossed/account/max-borrowable-amount": ok({"maxBorrowableAmount": "1"}),
            "/api/v2/margin/crossed/interest-rate-and-limit": ok(RATE),
        }
    )
    await a.get_borrowable("BTC")
    url, h = t.urls[-1], t.headers[-1]
    assert "?coin=BTC" in url
    prehash = f"{h['ACCESS-TIMESTAMP']}GET/api/v2/margin/crossed/account/max-borrowable-amount?coin=BTC"
    expected = base64.b64encode(hmac.new(SECRET.encode(), prehash.encode(), hashlib.sha256).digest()).decode()
    assert h["ACCESS-SIGN"] == expected
    assert (h["ACCESS-KEY"], h["ACCESS-PASSPHRASE"], h["ACCESS-TIMESTAMP"]) == (KEY, PASS, "1700000000000")
    for secret in (SECRET, PASS):
        assert secret not in url


async def test_signature_without_query_omits_the_question_mark() -> None:
    a, t, _ = make({"/api/v2/spot/account/info": ok({"authorities": []})})
    await a.check_permissions()
    h = t.headers[0]
    prehash = f"{h['ACCESS-TIMESTAMP']}GET/api/v2/spot/account/info"
    assert (
        h["ACCESS-SIGN"]
        == base64.b64encode(hmac.new(SECRET.encode(), prehash.encode(), hashlib.sha256).digest()).decode()
    )


def test_repr_hides_credentials() -> None:
    a, _, _ = make({})
    text = repr(a._http)
    assert KEY not in text and SECRET not in text and PASS not in text


# ---- errors ----
@pytest.mark.parametrize(
    ("status", "headers", "body", "kind"),
    [
        (429, {"retry-after": "3"}, b"", RateLimited),
        (503, {}, b"", ExchangeDown),
        (401, {}, b"", AuthError),
        (400, {}, b'{"code":"40037","msg":"Apikey does not exist"}', AuthError),
        (400, {}, b'{"code":"40034","msg":"Parameter NOPEUSDT does not exist"}', Rejected),
        (400, {}, b"not json", Rejected),
    ],
)
def test_error_mapping(status: int, headers: dict[str, str], body: bytes, kind: type[Exception]) -> None:
    assert type(map_error(status, headers, body)) is kind


async def test_error_envelope_on_http_200_is_still_an_error() -> None:
    body = json.dumps({"code": "40037", "msg": "Apikey does not exist", "data": None}).encode()
    a, _, _ = make({"/api/v2/spot/account/info": (200, {}, body)})
    with pytest.raises(AuthError):
        await a.check_permissions()


async def test_429_bans_locally() -> None:
    a, t, clock = make({"/api/v2/spot/market/orderbook": (429, {"retry-after": "5"}, b"")})
    with pytest.raises(RateLimited):
        await a.get_orderbook("BTCUSDT", 5)
    with pytest.raises(RateLimited) as e:
        await a.get_orderbook("BTCUSDT", 5)
    assert e.value.code == "local_ban" and len(t.urls) == 1
    clock.t += 6
    t.routes["/api/v2/spot/market/orderbook"] = ok(BOOK)
    await a.get_orderbook("BTCUSDT", 5)
    assert len(t.urls) == 2


@pytest.mark.parametrize(
    "response",
    [TimeoutError(), (200, {}, b"<html>"), (200, {}, b"[1]"), (200, {}, b'{"data": 1}')],
)
async def test_network_failure_and_bad_bodies_are_exchange_down(response: Any) -> None:
    a, _, _ = make({"/api/v2/spot/market/orderbook": response})
    with pytest.raises(ExchangeDown):
        await a.get_orderbook("BTCUSDT", 5)


async def test_account_info_is_limited_to_once_per_second_per_uid() -> None:
    a, t, clock = make({"/api/v2/spot/account/info": ok({"authorities": []})})
    await a.check_permissions()
    with pytest.raises(RateLimited) as e:  # documented limit: 1 call/s per user
        await a.check_permissions()
    assert e.value.code == "local_interval"
    assert len(t.urls) == 1
    clock.t += 1
    await a.check_permissions()


# ---- interim polling stream ----
async def test_stream_polls_and_skips_unchanged_books() -> None:
    a, t, _ = make({"/api/v2/spot/market/orderbook": ok(BOOK)})
    seen = []
    async for book in a.stream_orderbook("BTCUSDT", 5):
        seen.append(book)
        if len(seen) == 1:
            t.routes["/api/v2/spot/market/orderbook"] = ok({**BOOK, "ts": "1700000001500"})
        if len(seen) == 2:
            break
    assert [b.ts for b in seen] == [1_700_000_000.5, 1_700_000_001.5]


async def test_stream_ends_with_the_error_when_a_poll_fails() -> None:
    a, t, _ = make({"/api/v2/spot/market/orderbook": ok(BOOK)})
    got = 0
    with pytest.raises(ExchangeDown):
        async for _ in a.stream_orderbook("BTCUSDT", 5):
            got += 1
            t.routes["/api/v2/spot/market/orderbook"] = (503, {}, b"")
    assert got == 1
