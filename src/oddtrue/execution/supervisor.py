"""The periodic sweep that keeps the whole system honest (CLAUDE.md sections 4A, 12).

One sweep: (1) resume every parked or interrupted trade, (2) reconcile every idle, unfrozen client against its
exchanges, (3) watch open loans and reserves, (4) refresh gauges and flush alerts. It never trades; it only drives
existing trades to a safe end and raises the page-worthy conditions. A frozen client is left for a human.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Protocol

from oddtrue.config.models import RiskConfig
from oddtrue.exchanges.base import ExchangeAdapter
from oddtrue.execution.deps import Clock
from oddtrue.execution.engine import RunResult, TradeEngine
from oddtrue.execution.plan import TradePlan
from oddtrue.ledger.repository import Ledger
from oddtrue.ledger.safety import SafetyLedger
from oddtrue.observability import metrics as m
from oddtrue.observability.alerts import Alert, AlertKind, AlertSink
from oddtrue.observability.logging import get_logger
from oddtrue.observability.metrics import Metrics
from oddtrue.pricing.types import ZERO
from oddtrue.reconciliation import snapshot as snap
from oddtrue.reconciliation.reconciler import Reconciler, Verdict

log = get_logger("supervisor")


class ClientDirectory(Protocol):
    """Who the clients are and how to reach their exchanges. Backed by the clients table in Phase 8a."""

    async def client_ids(self) -> Sequence[str]: ...

    def venues_for(self, client_id: str) -> Mapping[str, ExchangeAdapter]: ...

    def engine_for(self, client_id: str) -> TradeEngine: ...


@dataclass(slots=True)
class SweepReport:
    resumed: dict[str, RunResult] = field(default_factory=dict)  # trade_id -> where it ended up
    reconciled: dict[str, Verdict] = field(default_factory=dict)  # client_id -> verdict
    skipped_frozen: list[str] = field(default_factory=list)
    reserve_low: list[str] = field(default_factory=list)
    loans_open: int = 0
    oldest_loan_age_s: float = 0.0


def _dt(epoch_s: float) -> datetime:
    return datetime.fromtimestamp(epoch_s, UTC)


class Supervisor:
    def __init__(
        self,
        directory: ClientDirectory,
        ledger: Ledger,
        reconciler: Reconciler,
        risk: RiskConfig,
        clock: Clock,
        alerts: AlertSink,
        metrics: Metrics,
    ) -> None:
        self._dir, self._led, self._rec = directory, ledger, reconciler
        self._safety = SafetyLedger(ledger)
        self._risk, self._clock, self._alerts, self._metrics = risk, clock, alerts, metrics

    async def sweep(self) -> SweepReport:
        report = SweepReport()
        await self._resume_trades(report)
        await self._reconcile_idle_clients(report)
        await self._watch_loans(report)
        self._metrics.set(m.CLIENTS_FROZEN, len(await self._safety.frozen_clients()))
        await self._alerts.flush()
        return report

    # ---- 1. interrupted and parked trades ----------------------------------------
    async def _resume_trades(self, report: SweepReport) -> None:
        for trade in await self._led.unfinished_trades():
            result = await self._dir.engine_for(trade.client_id).advance(trade.client_id, trade.id)
            report.resumed[trade.id] = result
            held = result.held or ""
            if held.startswith(("exchange_unavailable", "unresolved")):
                plan = TradePlan.from_json(await self._led.get_plan(trade.client_id, trade.id))
                self._alerts.raise_alert(
                    Alert(
                        AlertKind.EXCHANGE_DOWN_IN_FLIGHT, trade.client_id, trade.id,
                        f"in-flight trade parked in {result.state.value}: {held} "
                        f"(exchanges {plan.buy_exchange}, {plan.borrow_exchange})",
                    )
                )  # fmt: skip

    # ---- 2. idle clients ----------------------------------------
    async def _reconcile_idle_clients(self, report: SweepReport) -> None:
        frozen = set(await self._safety.frozen_clients())
        for client_id in await self._dir.client_ids():
            if client_id in frozen:
                report.skipped_frozen.append(client_id)
                continue
            if await self._safety.unfinished_trade_ids(client_id):
                continue  # parked mid-trade: its own post-trade reconciliation covers it
            result = await self._rec.reconcile(client_id, self._dir.venues_for(client_id), trigger="periodic")
            report.reconciled[client_id] = result.verdict
            if result.ok:
                await self._check_reserve(client_id, report)

    async def _check_reserve(self, client_id: str, report: SweepReport) -> None:
        """Rule 12: the 5% reserve must stay usable. Read from the snapshot just taken, so it costs no API calls."""
        known_good = await self._safety.latest_known_good(client_id)
        if known_good is None:  # pragma: no cover - a successful reconcile always leaves one
            return
        actual = snap.from_json(known_good.actual)
        # Only exchanges where the client keeps a margin account can be a borrow side, so only they need a reserve.
        low = [
            ex
            for ex in actual
            if actual[ex].get(snap.MARGIN) and snap.get(actual, ex, snap.MARGIN, "USDT") < self._risk.min_reserve_usdt
        ]
        if low:
            report.reserve_low.append(client_id)
            self._alerts.raise_alert(
                Alert(
                    AlertKind.RESERVE_BELOW_MINIMUM, client_id, "",
                    f"margin USDT below {self._risk.min_reserve_usdt} on {', '.join(sorted(low))}",
                )
            )  # fmt: skip

    # ---- 3. loans ----------------------------------------
    async def _watch_loans(self, report: SweepReport) -> None:
        now = _dt(self._clock.now)
        loans = await self._safety.open_loans()
        report.loans_open = len(loans)
        ages = [(c, t, a, (now - opened).total_seconds()) for c, t, a, opened in loans]
        report.oldest_loan_age_s = max((age for *_, age in ages), default=0.0)
        self._metrics.set(m.LOANS_OPEN, len(loans))
        self._metrics.set(m.LOAN_OLDEST_AGE, Decimal(str(report.oldest_loan_age_s)) if loans else ZERO)
        for client_id, trade_id, asset, age in ages:
            if age > self._risk.loan_open_alert_s:
                self._alerts.raise_alert(
                    Alert(
                        AlertKind.LOAN_OPEN_TOO_LONG, client_id, trade_id,
                        f"{asset} loan open {age:.0f}s (limit {self._risk.loan_open_alert_s}s)",
                    )
                )  # fmt: skip

    # ---- running ----------------------------------------
    async def run(self, interval_s: float, stop: asyncio.Event) -> None:
        """Sweep until ``stop`` is set. A failing sweep is logged and the next one still happens."""
        while not stop.is_set():
            try:
                await self.sweep()
            except Exception:
                log.exception("sweep failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=interval_s)
