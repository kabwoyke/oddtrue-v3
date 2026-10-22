"""Log-only opportunity scanner over the shared book cache (Phase 5: detection on live data, no trading).

Each scan evaluates both directions for every symbol from fresh books and records transitions: an opportunity
OPENS when a direction starts to pay, and CLOSES when it stops (or its books go stale), with its lifetime and best
net ROI. Lifetimes are the data Phase 7 needs to size batches. The scanner can read books and costs and write
logs/metrics; it has no handle to anything that places, borrows or repays.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal

from oddtrue.detection.costs import CostProvider
from oddtrue.detection.direct import DetectionParams, Opportunity, Rejection, Verdict, evaluate
from oddtrue.exchanges.errors import ExchangeError
from oddtrue.marketdata.cache import BookCache
from oddtrue.observability import metrics as m
from oddtrue.observability.logging import get_logger
from oddtrue.observability.metrics import Metrics

log = get_logger("detection")


@dataclass(slots=True)
class _Open:
    since: float
    best_roi: Decimal
    last: Opportunity


@dataclass(frozen=True, slots=True)
class ScanResult:
    verdicts: tuple[Verdict, ...]
    stale: tuple[str, ...]  # symbols skipped because a book was missing or too old
    cost_errors: tuple[str, ...]


class Scanner:
    def __init__(
        self,
        exchanges: tuple[str, str],
        symbols: Sequence[str],
        *,
        cache: BookCache,
        costs: CostProvider,
        params: DetectionParams,
        max_book_age_s: Decimal,
        clock: Callable[[], float],
        metrics: Metrics,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._ex = exchanges
        self._symbols = tuple(symbols)
        self._cache = cache
        self._costs = costs
        self._params = params
        self._max_age = max_book_age_s
        self._clock = clock
        self._metrics = metrics
        self._sleep = sleep
        self._open: dict[tuple[str, str, str], _Open] = {}

    @property
    def open_count(self) -> int:
        return len(self._open)

    async def scan_once(self) -> ScanResult:
        now = self._clock()
        self._metrics.inc(m.DETECTION_SCANS)
        verdicts: list[Verdict] = []
        stale: list[str] = []
        cost_errors: list[str] = []
        for symbol in self._symbols:
            books = {
                ex: book for ex in self._ex if (book := self._cache.fresh(ex, symbol, now, self._max_age)) is not None
            }
            if len(books) < len(self._ex):
                stale.append(symbol)
                self._close_all(symbol, now, "stale_book")
                continue
            try:
                rules = {ex: await self._costs.rules(ex, symbol) for ex in self._ex}
                fees = {ex: await self._costs.fees(ex, symbol) for ex in self._ex}
                rates = {ex: await self._costs.borrow_rate(ex, symbol) for ex in self._ex}
            except ExchangeError as exc:
                cost_errors.append(f"{symbol}:{type(exc).__name__}")
                self._close_all(symbol, now, "costs_unavailable")
                continue
            for buy_ex, sell_ex in (self._ex, self._ex[::-1]):
                buy_book, sell_book = books[buy_ex], books[sell_ex]
                verdict = evaluate(
                    symbol, buy_ex, sell_ex, buy_book, sell_book, rules[buy_ex], rules[sell_ex],
                    fees[buy_ex], fees[sell_ex], rates[sell_ex], self._params,
                )  # fmt: skip
                verdicts.append(verdict)
                self._record(verdict, now)
        return ScanResult(tuple(verdicts), tuple(stale), tuple(cost_errors))

    def _record(self, verdict: Verdict, now: float) -> None:
        key = (verdict.symbol, verdict.buy_exchange, verdict.sell_exchange)
        if isinstance(verdict, Rejection):
            self._metrics.inc(m.DETECTION_REJECTS, reason=verdict.reason.value)
            self._close(key, now, verdict.reason.value)
            return
        current = self._open.get(key)
        if current is None:
            self._open[key] = _Open(now, verdict.net_roi_pct, verdict)
            self._metrics.inc(m.OPPORTUNITIES_FOUND, direction=f"{verdict.buy_exchange}>{verdict.sell_exchange}")
            log.info(
                "would-be opportunity (not traded)",
                extra={
                    "event": "opportunity_open",
                    "symbol": verdict.symbol,
                    "buy_exchange": verdict.buy_exchange,
                    "sell_exchange": verdict.sell_exchange,
                    "qty": str(verdict.qty),
                    "buy_vwap": str(verdict.buy_vwap),
                    "sell_vwap": str(verdict.sell_vwap),
                    "gross": str(verdict.gross),
                    "fees": str(verdict.fees),
                    "interest": str(verdict.interest),
                    "net": str(verdict.net),
                    "net_roi_pct": str(verdict.net_roi_pct),
                    "assumed": list(self._costs.assumed),
                },
            )
        else:
            current.best_roi = max(current.best_roi, verdict.net_roi_pct)
            current.last = verdict

    def _close(self, key: tuple[str, str, str], now: float, why: str) -> None:
        current = self._open.pop(key, None)
        if current is None:
            return
        self._metrics.observe("oddtrue_opportunity_lifetime_seconds", now - current.since)
        log.info(
            "opportunity closed",
            extra={
                "event": "opportunity_close",
                "symbol": key[0],
                "buy_exchange": key[1],
                "sell_exchange": key[2],
                "lifetime_s": round(now - current.since, 3),
                "best_net_roi_pct": str(current.best_roi),
                "why": why,
            },
        )

    def _close_all(self, symbol: str, now: float, why: str) -> None:
        for key in [k for k in self._open if k[0] == symbol]:
            self._close(key, now, why)

    async def run(self, interval_s: float, stop: asyncio.Event) -> None:
        while not stop.is_set():
            await self.scan_once()
            await self._sleep(interval_s)
