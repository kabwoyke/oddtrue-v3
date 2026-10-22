"""Per-exchange request-weight budget with priority classes (CLAUDE.md 4A).

A token bucket sized to the exchange's weight limit. Lower-priority classes may only spend tokens above a
reserved floor, so recovery/repay traffic (class 1) can never starve behind scanning or dashboard reads.
The exchange's own usage header is the truth: ``observe_used`` pulls our count down to it, never up past it.
The clock is injected; there is no I/O here.
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal
from enum import IntEnum

from oddtrue.exchanges.errors import RateLimited


class Priority(IntEnum):
    RECOVERY = 1  # recovery and repay
    EXECUTION = 2
    REVALIDATION = 3
    RECONCILIATION = 4
    BACKGROUND = 5  # scanning support, dashboard


# Share of the bucket that must stay free for every more important class. Class 1 may use everything.
DEFAULT_RESERVE: dict[Priority, Decimal] = {
    Priority.RECOVERY: Decimal("0"),
    Priority.EXECUTION: Decimal("0.05"),
    Priority.REVALIDATION: Decimal("0.10"),
    Priority.RECONCILIATION: Decimal("0.20"),
    Priority.BACKGROUND: Decimal("0.35"),
}


class WeightBudget:
    def __init__(
        self,
        *,
        limit: int,
        window_s: int,
        clock: Callable[[], float],
        reserve: dict[Priority, Decimal] | None = None,
        exchange: str | None = None,
    ) -> None:
        if limit <= 0 or window_s <= 0:
            raise ValueError("limit and window_s must be positive")
        self._limit = Decimal(limit)
        self._rate = Decimal(limit) / Decimal(window_s)  # tokens per second
        self._clock = clock
        self._reserve = reserve or DEFAULT_RESERVE
        self._exchange = exchange
        self._tokens = Decimal(limit)
        self._last = clock()
        self._banned_until = 0.0

    def _refill(self) -> None:
        now = self._clock()
        self._tokens = min(self._limit, self._tokens + self._rate * Decimal(str(max(0.0, now - self._last))))
        self._last = now

    @property
    def available(self) -> Decimal:
        self._refill()
        return self._tokens

    def acquire(self, weight: int, priority: Priority) -> None:
        """Spend ``weight`` or raise ``RateLimited`` locally, before any request is sent."""
        now = self._clock()
        if now < self._banned_until:
            raise RateLimited(
                "exchange ban in force",
                retry_after_s=Decimal(str(self._banned_until - now)),
                banned=True,
                exchange=self._exchange,
                code="local_ban",
            )
        self._refill()
        floor = self._limit * self._reserve[priority]
        if self._tokens - Decimal(weight) < floor:
            wait = (floor + Decimal(weight) - self._tokens) / self._rate
            raise RateLimited(
                f"local budget exhausted for priority {priority.name}",
                retry_after_s=wait,
                exchange=self._exchange,
                code="local_budget",
            )
        self._tokens -= Decimal(weight)

    def observe_used(self, used_weight: int) -> None:
        """Trust the exchange's reported usage for the current window if it is higher than our own count."""
        self._refill()
        self._tokens = min(self._tokens, max(Decimal(0), self._limit - Decimal(used_weight)))

    def ban(self, seconds: Decimal) -> None:
        """The exchange told us to stop (429/418 with Retry-After): nothing is sent until it passes."""
        self._banned_until = max(self._banned_until, self._clock() + float(seconds))
