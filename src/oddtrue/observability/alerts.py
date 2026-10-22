"""Alerts (CLAUDE.md section 12): record, count, log, persist and page. Alerting never breaks trading.

``raise_alert`` is synchronous and cheap (engine code calls it from anywhere); ``flush`` persists what is pending and
delivers page-worthy alerts to the notification channels. An identical alert inside the de-duplication window is
counted in metrics but not repeated, so a stuck trade cannot flood the on-call channel.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

from oddtrue.observability import metrics as m
from oddtrue.observability.logging import get_logger, redact_text
from oddtrue.observability.metrics import Metrics

if TYPE_CHECKING:
    from oddtrue.execution.deps import Clock
    from oddtrue.ledger.safety import SafetyLedger

log = get_logger("alerts")


class AlertKind(StrEnum):
    """The page-worthy conditions of CLAUDE.md section 12, plus the context needed to act on them."""

    LOAN_NOT_ZERO_AFTER_RECOVERY_TIMEOUT = "loan_not_zero_after_recovery_timeout"
    UNKNOWN_OUTCOME_UNRESOLVED = "unknown_outcome_unresolved"
    NAKED_POSITION = "naked_position"
    COVER_ESCALATION_EXHAUSTED = "cover_escalation_exhausted"
    RESERVE_INSUFFICIENT = "reserve_insufficient"
    RESERVE_BELOW_MINIMUM = "reserve_below_minimum"
    RECONCILIATION_MISMATCH = "reconciliation_mismatch"
    RECONCILIATION_UNAVAILABLE = "reconciliation_unavailable"
    EXCHANGE_DOWN_IN_FLIGHT = "exchange_down_in_flight"
    AUTH_INCIDENT = "auth_incident"
    LOAN_PRESENT_BEFORE_TRADE = "loan_present_before_trade"
    LOAN_OPEN_TOO_LONG = "loan_open_too_long"
    CLIENT_FROZEN = "client_frozen"
    EXCHANGE_HALTED = "exchange_halted"
    GLOBAL_HALT = "global_halt"
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

    async def flush(self) -> None: ...


@dataclass(slots=True)
class ListAlertSink:
    """In-memory sink for tests and local runs."""

    alerts: list[Alert] = field(default_factory=list)

    def raise_alert(self, alert: Alert) -> None:
        self.alerts.append(alert)

    async def flush(self) -> None:
        return None

    def kinds(self) -> list[AlertKind]:
        return [a.kind for a in self.alerts]


class Channel(Protocol):
    """Where page-worthy alerts go (email, SMS, Telegram, a pager). Implemented in Phase 8; tests use a recorder."""

    async def notify(self, alert: Alert) -> None: ...


def _dt(epoch_s: float) -> datetime:
    return datetime.fromtimestamp(epoch_s, UTC)


class AlertHub:
    def __init__(
        self,
        clock: Clock,
        metrics: Metrics,
        *,
        store: SafetyLedger | None = None,
        channels: tuple[Channel, ...] = (),
        dedupe_s: float = 60,
    ) -> None:
        self._clock, self._metrics, self._store = clock, metrics, store
        self._channels, self._dedupe_s = channels, dedupe_s
        self._last: dict[tuple[str, str, str, str], float] = {}
        self._pending: list[tuple[Alert, datetime]] = []
        self.alerts: list[Alert] = []

    def kinds(self) -> list[AlertKind]:
        return [a.kind for a in self.alerts]

    def raise_alert(self, alert: Alert) -> None:
        now = self._clock.now
        key = (alert.kind.value, alert.client_id, alert.trade_id, alert.detail)
        last = self._last.get(key)
        if last is not None and now - last < self._dedupe_s:
            self._metrics.inc(m.ALERTS_SUPPRESSED, kind=alert.kind.value)
            return
        self._last[key] = now
        severity = "PAGE" if alert.page else "WARN"
        self._metrics.inc(m.ALERTS, kind=alert.kind.value, severity=severity)
        self.alerts.append(alert)
        self._pending.append((alert, _dt(now)))
        (log.error if alert.page else log.warning)(
            redact_text(alert.detail),
            extra={
                "alert": alert.kind.value,
                "severity": severity,
                "client_id": alert.client_id or None,
                "trade_id": alert.trade_id or None,
            },
        )

    async def flush(self) -> None:
        """Persist pending alerts, then page. A failing store or channel is logged and counted, never raised."""
        pending, self._pending = self._pending, []
        if not pending:
            return
        if self._store is not None:
            try:
                await self._store.save_alerts(
                    (
                        "PAGE" if a.page else "WARN",
                        a.kind.value,
                        a.client_id or None,
                        a.trade_id or None,
                        redact_text(a.detail),
                        at,
                    )
                    for a, at in pending
                )
            except Exception:
                log.exception("could not persist alerts")
                self._metrics.inc(m.ALERTS_SUPPRESSED, kind="persist_failed")
        for alert, _ in pending:
            if not alert.page:
                continue
            for channel in self._channels:
                try:
                    await channel.notify(alert)
                except Exception:
                    log.exception("alert channel failed")
                    self._metrics.inc(m.ALERTS_SUPPRESSED, kind="channel_failed")
