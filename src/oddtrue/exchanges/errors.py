"""Normalised exchange errors (CLAUDE.md section 5). Nothing exchange-specific escapes ``exchanges/``.

``UnknownOutcome`` is deliberately NOT a subclass of any retry-friendly error: it must always trigger
query-and-reconcile by client order id, never a blind retry (CLAUDE.md rule 2).
"""

from __future__ import annotations

from decimal import Decimal


class ExchangeError(Exception):
    def __init__(self, message: str = "", *, exchange: str | None = None, code: str | None = None) -> None:
        super().__init__(message or code or type(self).__name__)
        self.exchange = exchange
        self.code = code


class RateLimited(ExchangeError):
    def __init__(
        self,
        message: str = "",
        *,
        retry_after_s: Decimal | None = None,
        banned: bool = False,
        exchange: str | None = None,
        code: str | None = None,
    ) -> None:
        super().__init__(message, exchange=exchange, code=code)
        self.retry_after_s = retry_after_s
        self.banned = banned


class InsufficientFunds(ExchangeError):
    pass


class InsufficientLiquidity(ExchangeError):
    """Used by pre-checks when the book cannot absorb the quantity within slippage (do not borrow)."""


class InvalidQuantity(ExchangeError):
    """Quantity/price violates min, max, step, tick or min notional."""


class UnknownOutcome(ExchangeError):
    """Timeout / 5xx / dropped response: the action may or may not have happened. Query, then reconcile."""

    def __init__(
        self, message: str = "", *, operation: str, client_id: str | None = None, exchange: str | None = None
    ) -> None:
        super().__init__(message or f"unknown outcome for {operation}", exchange=exchange, code="unknown_outcome")
        self.operation = operation
        self.client_id = client_id


class Rejected(ExchangeError):
    """The exchange definitively refused the request (nothing happened)."""


class AuthError(ExchangeError):
    """Bad, revoked or under-permissioned credentials."""


class ExchangeDown(ExchangeError):
    pass


class StreamDropped(ExchangeDown):
    """A websocket stream ended; resync with a snapshot before trusting the book again."""
