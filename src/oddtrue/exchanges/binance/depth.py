"""Local order book kept in sync from a REST snapshot plus the diff-depth stream. Pure: no I/O, no clock.

Procedure (Binance spot websocket docs, "How to manage a local order book correctly"): buffer events, take a
snapshot, drop events with ``u <= lastUpdateId``, the first applied event must straddle the snapshot
(``U <= lastUpdateId + 1 <= u``), and every later event must continue exactly (``U == previous u + 1``).
Any gap raises ``StreamDropped`` so the caller resyncs from a fresh snapshot instead of trusting a hole.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from decimal import Decimal
from typing import Any

from oddtrue.exchanges.errors import StreamDropped
from oddtrue.exchanges.models import OrderBook
from oddtrue.pricing.types import ZERO, Level

NAME = "binance"


def parse_levels(raw: Iterable[Iterable[Any]]) -> list[tuple[Decimal, Decimal]]:
    return [(Decimal(str(p)), Decimal(str(q))) for p, q, *_ in raw]


class DepthBook:
    def __init__(self, symbol: str, snapshot: Mapping[str, Any]) -> None:
        self.symbol = symbol
        self._last_id = int(snapshot["lastUpdateId"])
        self._bids = {p: q for p, q in parse_levels(snapshot["bids"]) if q > ZERO}
        self._asks = {p: q for p, q in parse_levels(snapshot["asks"]) if q > ZERO}
        self._synced = False

    def apply(self, event: Mapping[str, Any]) -> bool:
        """Apply one diff event. Returns False for an old event that was ignored, True when applied."""
        first, final = int(event["U"]), int(event["u"])
        if not self._synced:
            if final <= self._last_id:
                return False
            if first > self._last_id + 1:
                raise StreamDropped("depth stream starts after the snapshot", exchange=NAME, code="depth_gap")
            self._synced = True
        elif first != self._last_id + 1:
            raise StreamDropped("depth stream sequence gap", exchange=NAME, code="depth_gap")
        for book, raw in ((self._bids, event["b"]), (self._asks, event["a"])):
            for price, qty in parse_levels(raw):
                if qty == ZERO:
                    book.pop(price, None)
                else:
                    book[price] = qty
        self._last_id = final
        return True

    def top(self, depth: int, ts: float) -> OrderBook:
        bids = sorted(self._bids.items(), reverse=True)[:depth]
        asks = sorted(self._asks.items())[:depth]
        return OrderBook(
            symbol=self.symbol,
            bids=tuple(Level(p, q) for p, q in bids),
            asks=tuple(Level(p, q) for p, q in asks),
            ts=ts,
        )
