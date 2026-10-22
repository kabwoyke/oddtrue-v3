"""Shared latest-order-book cache (CLAUDE.md 4A: each book is fetched once per exchange and shared by all clients).

A book is served only while it is fresh. Staleness is judged against the book's own timestamp (the matching-engine
time where the exchange gives one), and a feed that errors invalidates its entry so a dead stream can never keep
answering with its last book. The clock is injected.
"""

from __future__ import annotations

from decimal import Decimal

from oddtrue.exchanges.models import OrderBook


class BookCache:
    def __init__(self) -> None:
        self._books: dict[tuple[str, str], OrderBook] = {}

    def put(self, exchange: str, book: OrderBook) -> None:
        current = self._books.get((exchange, book.symbol))
        if current is None or book.ts >= current.ts:  # never replace a book with an older one
            self._books[(exchange, book.symbol)] = book

    def invalidate(self, exchange: str, symbol: str | None = None) -> None:
        """Forget one symbol, or every symbol of an exchange (its connection dropped)."""
        for key in [k for k in self._books if k[0] == exchange and (symbol is None or k[1] == symbol)]:
            del self._books[key]

    def fresh(self, exchange: str, symbol: str, now: float, max_age_s: Decimal) -> OrderBook | None:
        book = self._books.get((exchange, symbol))
        if book is None or Decimal(str(now - book.ts)) > max_age_s:
            return None
        return book

    def age(self, exchange: str, symbol: str, now: float) -> float | None:
        book = self._books.get((exchange, symbol))
        return None if book is None else now - book.ts
