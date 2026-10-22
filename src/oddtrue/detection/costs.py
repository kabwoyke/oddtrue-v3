"""Where detection gets symbol rules, fees and borrow rates. Reads only; cached with a TTL.

Rules, fees and rates change slowly, so they are fetched once per ``ttl_s`` per exchange and symbol, not per scan.
Some are signed reads on some exchanges (Binance fees and borrow rate), so without keys the caller can supply
explicit overrides. An override is always reported through ``assumed`` so the log shows the numbers are not the
exchange's.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from decimal import Decimal
from typing import Any, Protocol, cast

from oddtrue.exchanges.base import ExchangeAdapter
from oddtrue.exchanges.models import BorrowRate, Fees, base_asset
from oddtrue.pricing.types import SymbolRules


class CostProvider(Protocol):
    async def rules(self, exchange: str, symbol: str) -> SymbolRules: ...

    async def fees(self, exchange: str, symbol: str) -> Fees: ...

    async def borrow_rate(self, exchange: str, symbol: str) -> Decimal:
        """Per hour, on the exchange that would be the borrow/sell side."""
        ...

    @property
    def assumed(self) -> tuple[str, ...]: ...


class AdapterCosts:
    def __init__(
        self,
        adapters: Mapping[str, ExchangeAdapter],
        clock: Callable[[], float],
        *,
        ttl_s: float = 300.0,
        fee_override: Fees | None = None,
        borrow_rate_override: Decimal | None = None,
    ) -> None:
        self._adapters = adapters
        self._clock = clock
        self._ttl = ttl_s
        self._fee_override = fee_override
        self._rate_override = borrow_rate_override
        self._cache: dict[tuple[str, str, str], tuple[float, Any]] = {}

    @property
    def assumed(self) -> tuple[str, ...]:
        out = []
        if self._fee_override is not None:
            out.append(f"fees=maker {self._fee_override.maker} / taker {self._fee_override.taker}")
        if self._rate_override is not None:
            out.append(f"borrow_rate_per_hour={self._rate_override}")
        return tuple(out)

    async def _cached[T](self, kind: str, exchange: str, name: str, load: Callable[[], Awaitable[T]]) -> T:
        key = (kind, exchange, name)
        hit = self._cache.get(key)
        now = self._clock()
        if hit is not None and now - hit[0] < self._ttl:
            return cast(T, hit[1])
        value = await load()
        self._cache[key] = (now, value)
        return value

    async def rules(self, exchange: str, symbol: str) -> SymbolRules:
        adapter = self._adapters[exchange]
        return await self._cached("rules", exchange, symbol, lambda: adapter.get_symbol_rules(symbol))

    async def fees(self, exchange: str, symbol: str) -> Fees:
        if self._fee_override is not None:
            return self._fee_override
        adapter = self._adapters[exchange]
        return await self._cached("fees", exchange, symbol, lambda: adapter.get_fees(symbol))

    async def borrow_rate(self, exchange: str, symbol: str) -> Decimal:
        if self._rate_override is not None:
            return self._rate_override
        adapter, coin = self._adapters[exchange], base_asset(symbol)
        rate: BorrowRate = await self._cached("rate", exchange, coin, lambda: adapter.get_borrow_rate(coin))
        return rate.rate_per_hour
