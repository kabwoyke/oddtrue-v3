"""Safe exchange calls: hard time limits and query-by-client-id resolution (CLAUDE.md rules 2, 3, 8).

* Every call has a hard timeout; a timeout is an ``UnknownOutcome``, never a failure.
* An ambiguous order outcome is resolved by querying the order by its client order id, several times, with backoff.
  An order is deemed "never placed" only if every query says not found. The order is NEVER re-sent here.
* Reads retry on transient errors; if they stay unavailable the caller gets ``Unresolved`` and must hold the trade.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

from oddtrue.exchanges.base import ORDER_NOT_FOUND, ExchangeAdapter
from oddtrue.exchanges.errors import ExchangeDown, RateLimited, Rejected, UnknownOutcome
from oddtrue.exchanges.models import AccountType, OrderResult
from oddtrue.execution.deps import EngineDeps

T = TypeVar("T")


class Unresolved(Exception):
    """The outcome of an action could not be established within the allowed attempts: hold and alert."""

    def __init__(self, what: str) -> None:
        super().__init__(what)
        self.what = what


class Gateway:
    def __init__(self, deps: EngineDeps) -> None:
        self._d = deps
        override = deps.op_timeout_override_s
        self._timeout_s = override if override is not None else float(deps.risk.op_timeout_s)

    async def guard(self, label: str, fn: Callable[[], Awaitable[T]], client_id: str | None = None) -> T:
        """Run one exchange call under the hard per-operation limit (spec section 37)."""
        try:
            async with asyncio.timeout(self._timeout_s):
                return await fn()
        except TimeoutError:
            raise UnknownOutcome(
                f"{label} timed out after {self._timeout_s}s", operation=label, client_id=client_id
            ) from None

    async def _pause(self, error: Exception | None = None) -> None:
        wait = self._d.risk.unknown_resolve_backoff_s
        if isinstance(error, RateLimited) and error.retry_after_s is not None:
            wait = max(wait, error.retry_after_s)
        await self._d.clock.sleep(float(wait))

    async def read(self, label: str, fn: Callable[[], Awaitable[T]]) -> T:
        """A read-only call retried on transient errors. AuthError and definitive rejections propagate."""
        attempts = self._d.risk.unknown_resolve_attempts
        for i in range(attempts):
            try:
                return await self.guard(label, fn)
            except (RateLimited, UnknownOutcome, ExchangeDown) as exc:
                if i == attempts - 1:
                    break
                await self._pause(exc)
        raise Unresolved(f"{label}: unavailable after {attempts} attempts")

    async def resolve_order(
        self, adapter: ExchangeAdapter, client_order_id: str, symbol: str, account: AccountType
    ) -> OrderResult | None:
        """Find out what became of an order. Returns the result, or None if every query says it never existed.

        Raises ``Unresolved`` if the queries themselves kept failing (we then know nothing and must not guess).
        """
        attempts = self._d.risk.unknown_resolve_attempts
        not_found = 0
        for i in range(attempts):
            try:
                return await self.guard(
                    "get_order", lambda: adapter.get_order(client_order_id, symbol, account), client_order_id
                )
            except Rejected as exc:
                if exc.code == ORDER_NOT_FOUND:
                    not_found += 1
                    err: Exception | None = None
                else:  # any other refusal of a status query tells us nothing about the order: do not guess
                    err = exc
            except (RateLimited, UnknownOutcome, ExchangeDown) as exc:
                err = exc
            if i < attempts - 1:
                await self._pause(err)
        if not_found == attempts:
            return None
        raise Unresolved(f"order {client_order_id}: could not be queried reliably")
