"""Keeps the shared book cache filled from one exchange's stream, resyncing on any failure.

One task per symbol. A dropped or failed stream invalidates that symbol's cached book at once, waits (honouring a
rate-limit ``retry_after``, otherwise exponential backoff) and restarts the stream, which begins with a fresh
snapshot. Nothing here can place an order: it only calls ``stream_orderbook``.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from typing import Protocol

from oddtrue.exchanges.errors import ExchangeError, RateLimited
from oddtrue.exchanges.models import OrderBook
from oddtrue.marketdata.cache import BookCache
from oddtrue.observability import metrics as m
from oddtrue.observability.logging import get_logger
from oddtrue.observability.metrics import Metrics

log = get_logger("marketdata")


class BookSource(Protocol):
    """All the feed may use of an adapter: its name and its book stream (no order, borrow or account access)."""

    @property
    def name(self) -> str: ...

    def stream_orderbook(self, symbol: str, depth: int) -> AsyncIterator[OrderBook]: ...


class BookFeed:
    def __init__(
        self,
        adapter: BookSource,
        symbols: Sequence[str],
        *,
        depth: int,
        cache: BookCache,
        metrics: Metrics,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        backoff_base_s: float = 1.0,
        backoff_max_s: float = 30.0,
    ) -> None:
        self._adapter = adapter
        self._symbols = tuple(symbols)
        self._depth = depth
        self._cache = cache
        self._metrics = metrics
        self._sleep = sleep
        self._base = backoff_base_s
        self._max = backoff_max_s

    async def run_symbol(self, symbol: str, *, max_restarts: int | None = None) -> None:
        name, delay, restarts = self._adapter.name, self._base, 0
        while max_restarts is None or restarts <= max_restarts:
            try:
                async for book in self._adapter.stream_orderbook(symbol, self._depth):
                    self._cache.put(name, book)
                    self._metrics.inc(m.BOOK_UPDATES, exchange=name)
                    delay = self._base  # healthy again: reset the backoff
                reason, wait = "stream_ended", delay
            except RateLimited as exc:
                reason = "rate_limited"
                wait = max(delay, float(exc.retry_after_s or 0))
            except ExchangeError as exc:
                reason, wait = type(exc).__name__, delay
            except Exception:
                log.exception("book stream failed unexpectedly", extra={"exchange": name, "symbol": symbol})
                reason, wait = "unexpected", delay
            self._cache.invalidate(name, symbol)
            self._metrics.inc(m.BOOK_RESYNCS, exchange=name, reason=reason)
            log.warning(
                "book stream restarting", extra={"exchange": name, "symbol": symbol, "reason": reason, "wait_s": wait}
            )
            restarts += 1
            await self._sleep(wait)
            delay = min(delay * 2, self._max)

    async def run(self) -> None:
        """Run every symbol until cancelled."""
        async with asyncio.TaskGroup() as group:
            for symbol in self._symbols:
                group.create_task(self.run_symbol(symbol))
