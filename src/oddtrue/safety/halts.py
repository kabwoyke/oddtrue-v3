"""Kill switches (CLAUDE.md rule 10): global halt, per-exchange halt, per-client pause.

A halt stops NEW trades only. In-flight trades keep running to completion or recovery, because abandoning an open loan
is the one thing a halt must never cause. Every halt and resume is written to the append-only audit log in the same
transaction as the change itself. Whether a trade may start is decided from the database, never from a cache.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime

from oddtrue.execution.deps import Clock
from oddtrue.ledger.repository import Ledger
from oddtrue.ledger.safety import HaltRow, HaltScope, SafetyLedger
from oddtrue.observability import metrics as m
from oddtrue.observability.alerts import Alert, AlertKind, AlertSink
from oddtrue.observability.logging import get_logger
from oddtrue.observability.metrics import Metrics

log = get_logger("halts")

# Fresh checks run before a client rejoins (spec section 56): API, balances, margin, borrow, inventory, transfers.
# Phase 8a supplies the real one; it returns the reasons the client is not ready (empty = ready).
ReadinessCheck = Callable[[str], Awaitable[Sequence[str]]]


class ResumeRefused(Exception):
    def __init__(self, reasons: Sequence[str]) -> None:
        super().__init__("; ".join(reasons))
        self.reasons = list(reasons)


def _dt(epoch_s: float) -> datetime:
    return datetime.fromtimestamp(epoch_s, UTC)


class HaltController:
    def __init__(
        self,
        ledger: Ledger,
        clock: Clock,
        alerts: AlertSink,
        metrics: Metrics,
        *,
        readiness: ReadinessCheck | None = None,
    ) -> None:
        self._safety = SafetyLedger(ledger)
        self._ledger = ledger
        self._clock, self._alerts, self._metrics, self._readiness = clock, alerts, metrics, readiness

    # ---- halting ----------------------------------------
    async def halt_global(self, reason: str, actor: str) -> bool:
        return await self._halt(HaltScope.GLOBAL, None, reason, actor)

    async def halt_exchange(self, exchange: str, reason: str, actor: str) -> bool:
        return await self._halt(HaltScope.EXCHANGE, exchange, reason, actor)

    async def halt_client(self, client_id: str, reason: str, actor: str) -> bool:
        """Pause one client (operator or the client's own Pause)."""
        return await self._halt(HaltScope.CLIENT, client_id, reason, actor)

    async def _halt(self, scope: HaltScope, target: str | None, reason: str, actor: str) -> bool:
        if not reason.strip() or not actor.strip():
            raise ValueError("a halt needs an actor and a reason (it is audit-logged)")
        row = await self._safety.set_halt(scope, target, reason, actor, at=_dt(self._clock.now))
        if row is None:
            return False  # already halted: nothing changed, nothing new to audit
        log.warning("halt set: %s %s (%s) by %s", scope.value, target or "-", reason, actor)
        if scope is HaltScope.GLOBAL:
            self._alerts.raise_alert(Alert(AlertKind.GLOBAL_HALT, "", "", f"global halt by {actor}: {reason}"))
        elif scope is HaltScope.EXCHANGE:
            self._alerts.raise_alert(Alert(AlertKind.EXCHANGE_HALTED, "", "", f"{target} halted by {actor}: {reason}"))
        await self._refresh_gauges()
        return True

    # ---- resuming ----------------------------------------
    async def resume_global(self, reason: str, actor: str) -> bool:
        return await self._resume(HaltScope.GLOBAL, None, reason, actor)

    async def resume_exchange(self, exchange: str, reason: str, actor: str) -> bool:
        return await self._resume(HaltScope.EXCHANGE, exchange, reason, actor)

    async def resume_client(self, client_id: str, reason: str, actor: str) -> bool:
        """Lift a client pause, after fresh readiness checks. Raises ``ResumeRefused`` if the client is not ready."""
        if self._readiness is not None:
            problems = await self._readiness(client_id)
            if problems:
                await self._safety.audit(
                    actor, "halt.resume_refused", {"client": client_id, "reasons": list(problems)},
                    client_id=client_id, at=_dt(self._clock.now),
                )  # fmt: skip
                raise ResumeRefused(problems)
        return await self._resume(HaltScope.CLIENT, client_id, reason, actor)

    async def _resume(self, scope: HaltScope, target: str | None, reason: str, actor: str) -> bool:
        if not reason.strip() or not actor.strip():
            raise ValueError("a resume needs an actor and a reason (it is audit-logged)")
        cleared = await self._safety.clear_halt(scope, target, actor, reason, at=_dt(self._clock.now))
        if cleared:
            log.warning("halt cleared: %s %s (%s) by %s", scope.value, target or "-", reason, actor)
            await self._refresh_gauges()
        return cleared

    # ---- reading ----------------------------------------
    async def active(self) -> list[HaltRow]:
        return await self._safety.active_halts()

    async def blocked_reason(self, client_id: str, exchanges: Sequence[str]) -> str | None:
        """Satisfies ``TradeGate``: why a new trade may not start (halt or freeze), straight from the database."""
        return await self._ledger.blocked_reason(client_id, exchanges)

    async def _refresh_gauges(self) -> None:
        active = await self._safety.active_halts()
        for scope in HaltScope:
            self._metrics.set(m.HALTS_ACTIVE, sum(1 for h in active if h.scope is scope), scope=scope.value)
