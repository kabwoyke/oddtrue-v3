"""Phase 5: shared book cache, self-healing feed, log-only scanner, and the no-trading boundary."""

from __future__ import annotations

import ast
import asyncio
import json
import logging
from collections.abc import AsyncIterator, Iterator
from decimal import Decimal
from io import StringIO
from pathlib import Path

import pytest

from oddtrue.detection.costs import AdapterCosts
from oddtrue.detection.direct import DetectionParams, Opportunity
from oddtrue.detection.scanner import Scanner
from oddtrue.exchanges.errors import ExchangeDown, RateLimited, StreamDropped
from oddtrue.exchanges.fake import FakeClock, FakeVenue, Fault, FaultKind, Op
from oddtrue.exchanges.models import Fees, OrderBook
from oddtrue.marketdata.cache import BookCache
from oddtrue.marketdata.feed import BookFeed
from oddtrue.observability import metrics as m
from oddtrue.observability.logging import configure_logging
from oddtrue.observability.metrics import Metrics
from oddtrue.pricing.types import Level, SymbolRules

D = Decimal
SYM = "BTCUSDT"
RULES = SymbolRules(D("0.001"), D(1000), D("0.001"), D("0.01"), D(5))
PARAMS = DetectionParams(D("0.002"), D("0.30"), D("0.90"), 60)


def ob(ts: float, bid: str = "99", ask: str = "100", symbol: str = SYM) -> OrderBook:
    return OrderBook(symbol, (Level(D(bid), D(10)),), (Level(D(ask), D(10)),), ts)


# ---- cache ----
def test_cache_freshness_ordering_and_invalidation() -> None:
    c = BookCache()
    c.put("binance", ob(100.0))
    assert c.fresh("binance", SYM, 101.0, D(2)) is not None
    assert c.fresh("binance", SYM, 102.5, D(2)) is None  # too old
    assert c.fresh("bitget", SYM, 101.0, D(2)) is None  # never seen
    c.put("binance", ob(90.0, bid="1"))  # an older book never replaces a newer one
    assert c.fresh("binance", SYM, 101.0, D(2)).bids[0].price == D(99)  # type: ignore[union-attr]
    assert c.age("binance", SYM, 103.0) == 3.0 and c.age("bitget", SYM, 1.0) is None
    c.put("binance", ob(100.0, symbol="ETHUSDT"))
    c.invalidate("binance", SYM)
    assert c.fresh("binance", SYM, 101.0, D(2)) is None and c.fresh("binance", "ETHUSDT", 101.0, D(2)) is not None
    c.invalidate("binance")
    assert c.fresh("binance", "ETHUSDT", 101.0, D(2)) is None


# ---- feed ----
class Scripted:
    """An adapter whose stream plays a script: books, or exceptions to raise."""

    name = "binance"

    def __init__(self, script: list[object]) -> None:
        self.script = script
        self.calls = 0

    async def stream_orderbook(self, symbol: str, depth: int) -> AsyncIterator[OrderBook]:
        self.calls += 1
        step = self.script.pop(0)
        for item in step if isinstance(step, list) else [step]:
            if isinstance(item, Exception):
                raise item
            assert isinstance(item, OrderBook)
            yield item


async def test_feed_resyncs_with_backoff_and_invalidates_on_failure() -> None:
    cache, metrics, sleeps = BookCache(), Metrics(), []

    async def sleep(s: float) -> None:
        sleeps.append(s)

    adapter = Scripted(
        [
            [ob(1.0), StreamDropped("gap", exchange="binance")],
            ExchangeDown("down"),
            [ob(5.0)],
            [ob(6.0), ExchangeDown("x")],
        ]
    )
    feed = BookFeed(
        adapter, [SYM], depth=5, cache=cache, metrics=metrics, sleep=sleep, backoff_base_s=1.0, backoff_max_s=4.0
    )
    await feed.run_symbol(SYM, max_restarts=3)
    assert adapter.calls == 4
    # healthy update resets the delay to the base; a failure with no update in between doubles it
    assert sleeps == [1.0, 2.0, 1.0, 1.0]  # drop after a good book; down; (book 5.0 ended); drop after 6.0
    assert cache.fresh("binance", SYM, 6.0, D(10)) is None  # invalidated by the last failure
    assert metrics.counter(m.BOOK_UPDATES, exchange="binance") == 3
    assert metrics.counter(m.BOOK_RESYNCS, exchange="binance", reason="StreamDropped") == 1
    assert metrics.counter(m.BOOK_RESYNCS, exchange="binance", reason="ExchangeDown") == 2
    assert metrics.counter(m.BOOK_RESYNCS, exchange="binance", reason="stream_ended") == 1


async def test_feed_honours_rate_limit_retry_after_and_caps_backoff() -> None:
    sleeps: list[float] = []

    async def sleep(s: float) -> None:
        sleeps.append(s)

    adapter = Scripted([RateLimited(retry_after_s=D(30)), ExchangeDown("d"), ExchangeDown("d"), ExchangeDown("d")])
    feed = BookFeed(
        adapter,
        [SYM],
        depth=5,
        cache=BookCache(),
        metrics=Metrics(),
        sleep=sleep,
        backoff_base_s=1.0,
        backoff_max_s=3.0,
    )
    await feed.run_symbol(SYM, max_restarts=3)
    assert sleeps == [30.0, 2.0, 3.0, 3.0]


async def test_feed_run_starts_a_task_per_symbol_and_can_be_cancelled() -> None:
    venue = FakeVenue("binance", FakeClock())
    for s in (SYM, "ETHUSDT"):
        venue.set_rules(s, RULES)
        venue.set_book(s, [(D(99), D(5))], [(D(100), D(5))])
    cache = BookCache()
    feed = BookFeed(venue.account("c"), [SYM, "ETHUSDT"], depth=5, cache=cache, metrics=Metrics())
    task = asyncio.create_task(feed.run())
    for _ in range(5):
        await asyncio.sleep(0)
    assert cache.fresh("binance", SYM, venue.clock.now, D(1)) and cache.fresh(
        "binance", "ETHUSDT", venue.clock.now, D(1)
    )
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


# ---- scanner, end to end on two fake exchanges ----
class Rig:
    def __init__(self, fee_override: Fees | None = None, borrow_rate_override: D | None = None) -> None:
        self.clock = FakeClock()
        self.a, self.b = FakeVenue("binance", self.clock), FakeVenue("bitget", self.clock)
        for v in (self.a, self.b):
            v.set_rules(SYM, RULES)
            v.set_borrow("BTC", D("0.00001"), D(100))
        self.adapters = {"binance": self.a.account("c"), "bitget": self.b.account("c")}
        self.cache, self.metrics = BookCache(), Metrics()
        self.costs = AdapterCosts(
            self.adapters, lambda: self.clock.now, fee_override=fee_override, borrow_rate_override=borrow_rate_override
        )
        self.scanner = Scanner(
            ("binance", "bitget"),
            [SYM],
            cache=self.cache,
            costs=self.costs,
            params=PARAMS,
            max_book_age_s=D(2),
            clock=lambda: self.clock.now,
            metrics=self.metrics,
        )

    async def books(self, a_ask: str, b_bid: str, *, a_bid: str = "99", b_ask: str = "102") -> None:
        self.a.set_book(SYM, [(D(a_bid), D(10))], [(D(a_ask), D(10))])
        self.b.set_book(SYM, [(D(b_bid), D(10))], [(D(b_ask), D(10))])
        for ex, adapter in self.adapters.items():
            self.cache.put(ex, await adapter.get_orderbook(SYM, 5))


@pytest.fixture
def log_lines() -> Iterator[StringIO]:
    buf = StringIO()
    handler = configure_logging(logging.INFO, buf)
    yield buf
    logging.getLogger("oddtrue").removeHandler(handler)


def events(buf: StringIO) -> list[dict[str, object]]:
    return [e for e in map(json.loads, buf.getvalue().splitlines()) if "event" in e]


async def test_scan_opens_then_closes_an_opportunity_and_logs_it(log_lines: StringIO) -> None:
    r = Rig()
    await r.books(a_ask="100", b_bid="101")  # A is the cheap side: buy A, borrow+sell B
    res = await r.scanner.scan_once()
    opps = [v for v in res.verdicts if isinstance(v, Opportunity)]
    assert [(o.buy_exchange, o.sell_exchange) for o in opps] == [("binance", "bitget")]
    assert r.scanner.open_count == 1 and r.metrics.counter(m.OPPORTUNITIES_FOUND, direction="binance>bitget") == 1
    opened = events(log_lines)
    assert [e["event"] for e in opened] == ["opportunity_open"]
    assert opened[0]["net_roi_pct"] and opened[0]["assumed"] == []

    r.clock.advance(1)
    await r.books(a_ask="100", b_bid="101")  # still open: no second "open" line
    await r.scanner.scan_once()
    assert [e["event"] for e in events(log_lines)] == ["opportunity_open"]

    r.clock.advance(1)
    await r.books(a_ask="100", b_bid="100.1")  # edge gone
    await r.scanner.scan_once()
    closed = events(log_lines)[-1]
    assert closed["event"] == "opportunity_close" and closed["why"] == "below_min_roi"
    assert closed["lifetime_s"] == 2.0 and r.scanner.open_count == 0
    assert r.metrics.histogram_count("oddtrue_opportunity_lifetime_seconds") == 1
    assert r.metrics.counter(m.DETECTION_REJECTS, reason="below_min_roi") >= 1


async def test_both_directions_are_evaluated() -> None:
    r = Rig()
    await r.books(a_ask="102", b_bid="99", a_bid="101", b_ask="100")  # now B is the cheap side
    res = await r.scanner.scan_once()
    assert [(v.buy_exchange, v.sell_exchange) for v in res.verdicts if isinstance(v, Opportunity)] == [
        ("bitget", "binance")
    ]
    assert len(res.verdicts) == 2


async def test_stale_or_missing_books_end_open_opportunities(log_lines: StringIO) -> None:
    r = Rig()
    await r.books(a_ask="100", b_bid="101")
    await r.scanner.scan_once()
    r.clock.advance(5)  # beyond max_book_age_s: nothing refreshed the books
    res = await r.scanner.scan_once()
    assert res.stale == (SYM,) and not res.verdicts and r.scanner.open_count == 0
    assert events(log_lines)[-1]["why"] == "stale_book"
    r.cache.invalidate("bitget")
    assert (await r.scanner.scan_once()).stale == (SYM,)


async def test_cost_read_failure_skips_the_symbol_and_closes(log_lines: StringIO) -> None:
    r = Rig()
    await r.books(a_ask="100", b_bid="101")
    await r.scanner.scan_once()
    r.costs._cache.clear()
    r.b.add_fault(Op.GET_BORROW_RATE, Fault(FaultKind.EXCHANGE_DOWN))
    res = await r.scanner.scan_once()
    assert res.cost_errors == (f"{SYM}:ExchangeDown",) and r.scanner.open_count == 0
    assert events(log_lines)[-1]["why"] == "costs_unavailable"


async def test_overrides_are_reported_as_assumed(log_lines: StringIO) -> None:
    r = Rig(fee_override=Fees(D("0.002"), D("0.002")), borrow_rate_override=D("0.00002"))
    await r.books(a_ask="100", b_bid="101")
    await r.scanner.scan_once()
    assumed = events(log_lines)[0]["assumed"]
    assert assumed == ["fees=maker 0.002 / taker 0.002", "borrow_rate_per_hour=0.00002"]


async def test_costs_are_cached_between_scans() -> None:
    r = Rig()
    await r.books(a_ask="100", b_bid="101")
    await r.scanner.scan_once()
    r.b.add_fault(Op.GET_SYMBOL_RULES, Fault(FaultKind.EXCHANGE_DOWN))
    r.a.add_fault(Op.GET_FEES, Fault(FaultKind.EXCHANGE_DOWN))
    assert not (await r.scanner.scan_once()).cost_errors  # served from the cache
    r.clock.advance(301)
    await r.books(a_ask="100", b_bid="101")
    assert (await r.scanner.scan_once()).cost_errors  # TTL expired: re-read, and the read fails


async def test_run_loop_scans_until_stopped() -> None:
    r = Rig()
    await r.books(a_ask="100", b_bid="101")
    stop = asyncio.Event()
    sleeps = []

    async def sleep(s: float) -> None:
        sleeps.append(s)
        if len(sleeps) == 3:
            stop.set()

    r.scanner._sleep = sleep
    await r.scanner.run(0.5, stop)
    assert sleeps == [0.5] * 3 and r.metrics.counter(m.DETECTION_SCANS) == 3


async def test_live_feed_into_scanner_with_a_websocket_drop() -> None:
    r = Rig()
    r.a.set_book(SYM, [(D(99), D(10))], [(D(100), D(10))])
    r.b.set_book(SYM, [(D(101), D(10))], [(D(102), D(10))])
    feeds = [
        BookFeed(r.adapters[ex], [SYM], depth=5, cache=r.cache, metrics=r.metrics, sleep=r.clock.sleep)
        for ex in r.adapters
    ]
    tasks = [asyncio.create_task(f.run()) for f in feeds]
    for _ in range(6):
        await asyncio.sleep(0)
    assert len([v for v in (await r.scanner.scan_once()).verdicts if isinstance(v, Opportunity)]) == 1
    r.a.drop_streams()
    for _ in range(3):
        await asyncio.sleep(0)
    assert r.metrics.counter(m.BOOK_RESYNCS, exchange="binance", reason="StreamDropped") == 1
    for _ in range(6):  # the feed backs off, restarts and delivers a fresh snapshot
        await asyncio.sleep(0)
    assert r.cache.fresh("binance", SYM, r.clock.now, D(2)) is not None
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


# ---- Phase 5 boundary: detection and market data cannot reach trading ----
FORBIDDEN_MODULES = (
    "oddtrue.execution",
    "oddtrue.recovery",
    "oddtrue.ledger",
    "oddtrue.safety",
    "oddtrue.reconciliation",
)
FORBIDDEN_CALLS = {"place_order", "cancel_order", "borrow", "repay", "withdraw", "margin_sell", "get_order"}


@pytest.mark.parametrize("package", ["detection", "marketdata"])
def test_detection_and_marketdata_cannot_reach_trading(package: str) -> None:
    root = Path(__file__).parent.parent / "src" / "oddtrue" / package
    for path in root.glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.startswith(FORBIDDEN_MODULES), f"{path.name} imports {node.module}"
            if isinstance(node, ast.Import):
                assert not any(a.name.startswith(FORBIDDEN_MODULES) for a in node.names), path.name
            if isinstance(node, ast.Attribute):
                assert node.attr not in FORBIDDEN_CALLS, f"{path.name} touches .{node.attr}"


async def test_feed_survives_an_unexpected_exception_and_counts_it(log_lines: StringIO) -> None:
    sleeps: list[float] = []

    async def sleep(s: float) -> None:
        sleeps.append(s)

    metrics = Metrics()
    adapter = Scripted([OSError("boom"), [ob(1.0)]])
    feed = BookFeed(adapter, [SYM], depth=5, cache=BookCache(), metrics=metrics, sleep=sleep)
    await feed.run_symbol(SYM, max_restarts=1)
    assert metrics.counter(m.BOOK_RESYNCS, exchange="binance", reason="unexpected") == 1
    assert metrics.counter(m.BOOK_UPDATES, exchange="binance") == 1  # it kept going after the error
    assert "book stream failed unexpectedly" in log_lines.getvalue()


# ---- the Binance websocket connector turns transport errors into our error types ----
class _FakeWs:
    """Stands in for the ``websockets`` package so the connector's error handling runs without a socket."""

    class WebSocketException(Exception):
        pass

    def __init__(self, *, connect_error: BaseException | None = None) -> None:
        self.connect_error = connect_error
        self.exceptions = self

    def connect(self, url: str, **_: object) -> _FakeWs:
        return self

    async def __aenter__(self) -> AsyncIterator[str]:
        if self.connect_error is not None:
            raise self.connect_error
        return self._messages()

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def _messages(self) -> AsyncIterator[str]:
        yield "{}"


@pytest.mark.parametrize(
    ("connect_error", "expected"),
    [
        (OSError("Name or service not known"), ExchangeDown),
        (TimeoutError(), ExchangeDown),
        (_FakeWs.WebSocketException(), ExchangeDown),
    ],
)
async def test_ws_connect_failures_are_exchange_down(
    monkeypatch: pytest.MonkeyPatch, connect_error: BaseException, expected: type[Exception]
) -> None:
    import sys

    from oddtrue.exchanges.binance.ws import connect

    fake = _FakeWs(connect_error=connect_error)
    monkeypatch.setitem(sys.modules, "websockets", fake)
    monkeypatch.setitem(sys.modules, "websockets.exceptions", fake)
    with pytest.raises(expected):
        async with connect("wss://x"):
            pass


async def test_ws_breaks_mid_stream_are_stream_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    from oddtrue.exchanges.binance.ws import connect

    fake = _FakeWs()
    monkeypatch.setitem(sys.modules, "websockets", fake)
    monkeypatch.setitem(sys.modules, "websockets.exceptions", fake)
    with pytest.raises(StreamDropped):
        async with connect("wss://x") as messages:
            assert [m async for m in messages] == ["{}"]
            raise _FakeWs.WebSocketException  # the real library raises this from the iterator when the socket closes
