"""Everything the engine is given rather than reaches for: clock, exchanges, alerts, hooks.

Injecting these keeps the engine deterministic under test (fake clock, crash points) and lets later phases plug in
the real eligibility gate (Phase 8d), kill switches (Phase 4) and rebalancer (Phase 9) without touching the machine.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol

from oddtrue.config.models import RiskConfig
from oddtrue.exchanges.base import ExchangeAdapter
from oddtrue.ledger.repository import Ledger
from oddtrue.observability.alerts import Alert, AlertKind, AlertSink, ListAlertSink
from oddtrue.observability.metrics import Metrics

if TYPE_CHECKING:
    from oddtrue.reconciliation.reconciler import ReconResult


class Clock(Protocol):
    @property
    def now(self) -> float: ...

    async def sleep(self, seconds: float) -> None: ...


class SystemClock:
    @property
    def now(self) -> float:
        return time.time()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


def to_datetime(epoch_s: float) -> datetime:
    return datetime.fromtimestamp(epoch_s, UTC)


class TradeGate(Protocol):
    """Eligibility check for a NEW trade (entitlement, pause, halts). Return a reason to block, else None.

    The database check in ``Ledger.create_trade_limited`` is authoritative for halts and freezes; this hook is for
    everything else (e.g. paid entitlement in Phase 8d) and runs again at revalidation.
    """

    async def blocked_reason(self, client_id: str, exchanges: Sequence[str]) -> str | None: ...


class OpenGate:
    async def blocked_reason(self, client_id: str, exchanges: Sequence[str]) -> str | None:
        return None


class RebalanceEvaluator(Protocol):
    async def evaluate(self, client_id: str, trade_id: str) -> None: ...


class NoRebalance:
    async def evaluate(self, client_id: str, trade_id: str) -> None:
        return None


class BalanceReconciler(Protocol):
    """Compares the exchanges' balances with the ledger's expectation and freezes the client on a mismatch."""

    async def reconcile(
        self, client_id: str, venues: Mapping[str, ExchangeAdapter], *, trigger: str, trade_id: str | None = None
    ) -> ReconResult: ...

    async def freeze_client(self, client_id: str, reason: str, trade_id: str | None = None) -> None: ...


def no_checkpoint(name: str) -> None:
    return None


@dataclass(slots=True)
class EngineDeps:
    ledger: Ledger
    venues: Mapping[str, ExchangeAdapter]  # this client's adapters, keyed by exchange name
    clock: Clock
    risk: RiskConfig
    alerts: AlertSink
    metrics: Metrics = field(default_factory=Metrics)
    gate: TradeGate = field(default_factory=OpenGate)
    rebalance: RebalanceEvaluator = field(default_factory=NoRebalance)
    reconciler: BalanceReconciler | None = None  # None = leg-level checks only (unit tests of the machine)
    # Called at every durable step. Tests raise from it to kill the "process" there (crash-and-resume tests).
    checkpoint: Callable[[str], None] = no_checkpoint
    op_timeout_override_s: float | None = None  # tests only: shorter real timeout than the integer-second config
    max_unfinished_per_client: int = 1


__all__ = [
    "Alert",
    "AlertKind",
    "AlertSink",
    "BalanceReconciler",
    "Clock",
    "EngineDeps",
    "ListAlertSink",
    "NoRebalance",
    "OpenGate",
    "RebalanceEvaluator",
    "SystemClock",
    "TradeGate",
    "no_checkpoint",
    "to_datetime",
]
