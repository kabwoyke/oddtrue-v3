"""Exchange health: consecutive infrastructure failures auto-halt THAT exchange only (CLAUDE.md section 4A).

Business errors (a rejected order, insufficient funds) and credential errors say nothing about the exchange's health
and are not counted. A halted exchange stops new trades that touch it; trades on other exchanges, and in-flight trades
everywhere, carry on. Lifting the halt is a deliberate operator action, never automatic.
"""

from __future__ import annotations

from oddtrue.observability.logging import get_logger
from oddtrue.safety.halts import HaltController

log = get_logger("health")
SYSTEM_ACTOR = "system:health"


class ExchangeHealth:
    def __init__(self, halts: HaltController, threshold: int) -> None:
        self._halts, self._threshold = halts, threshold
        self._consecutive: dict[str, int] = {}

    def consecutive_failures(self, exchange: str) -> int:
        return self._consecutive.get(exchange, 0)

    def record_success(self, exchange: str) -> None:
        self._consecutive[exchange] = 0

    async def record_failure(self, exchange: str, error: str) -> bool:
        """Count one infrastructure failure. Returns True if this call tripped the automatic halt."""
        n = self._consecutive.get(exchange, 0) + 1
        self._consecutive[exchange] = n
        if n < self._threshold:
            return False
        tripped = await self._halts.halt_exchange(
            exchange, f"auto: {n} consecutive failures (last: {error})", SYSTEM_ACTOR
        )
        if tripped:
            log.error("exchange %s auto-halted after %s consecutive failures", exchange, n)
        return tripped
