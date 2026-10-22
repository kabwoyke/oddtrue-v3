"""Everything the engine is given rather than reaches for: clock, exchanges, alerts, hooks.

Injecting these keeps the engine deterministic under test (fake clock, crash points) and lets later phases plug in
the real eligibility gate (Phase 8d), kill switches (Phase 4) and rebalancer (Phase 9) without touching the machine.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol

from oddtrue.config.models import RiskConfig
from oddtrue.exchanges.base import ExchangeAdapter
from oddtrue.ledger.repository import Ledger


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


class AlertKind(StrEnum):
    """Page-worthy conditions from CLAUDE.md section 12 (plus the context needed to act on them)."""

    LOAN_NOT_ZERO_AFTER_RECOVERY_TIMEOUT = "loan_not_zero_after_recovery_timeout"
    UNKNOWN_OUTCOME_UNRESOLVED = "unknown_outcome_unresolved"
    NAKED_POSITION = "naked_position"
    COVER_ESCALATION_EXHAUSTED = "cover_escalation_exhausted"
    RESERVE_INSUFFICIENT = "reserve_insufficient"
    RECONCILIATION_MISMATCH = "reconciliation_mismatch"
    EXCHANGE_DOWN_IN_FLIGHT = "exchange_down_in_flight"
    AUTH_INCIDENT = "auth_incident"
    LOAN_PRESENT_BEFORE_TRADE = "loan_present_before_trade"
    DUST_LEFT = "dust_left"


@dataclass(frozen=True, slots=True)
class Alert:
    kind: AlertKind
    client_id: str
    trade_id: str
    detail: str
    page: bool = True


class AlertSink(Protocol):
    def raise_alert(self, alert: Alert) -> None: ...


@dataclass(slots=True)
class ListAlertSink:
    alerts: list[Alert] = field(default_factory=list)

    def raise_alert(self, alert: Alert) -> None:
        self.alerts.append(alert)

    def kinds(self) -> list[AlertKind]:
        return [a.kind for a in self.alerts]


class TradeGate(Protocol):
    """Final eligibility check at revalidation (entitlement, halts, pause). Return a reason to block, else None."""

    async def blocked_reason(self, client_id: str) -> str | None: ...


class OpenGate:
    async def blocked_reason(self, client_id: str) -> str | None:
        return None


class RebalanceEvaluator(Protocol):
    async def evaluate(self, client_id: str, trade_id: str) -> None: ...


class NoRebalance:
    async def evaluate(self, client_id: str, trade_id: str) -> None:
        return None


def no_checkpoint(name: str) -> None:
    return None


@dataclass(slots=True)
class EngineDeps:
    ledger: Ledger
    venues: Mapping[str, ExchangeAdapter]  # this client's adapters, keyed by exchange name
    clock: Clock
    risk: RiskConfig
    alerts: AlertSink
    gate: TradeGate = field(default_factory=OpenGate)
    rebalance: RebalanceEvaluator = field(default_factory=NoRebalance)
    # Called at every durable step. Tests raise from it to kill the "process" there (crash-and-resume tests).
    checkpoint: Callable[[str], None] = no_checkpoint
    op_timeout_override_s: float | None = None  # tests only: shorter real timeout than the integer-second config
    max_unfinished_per_client: int = 1
