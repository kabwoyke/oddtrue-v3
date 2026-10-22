"""Exchange-vs-ledger reconciliation and client freezing (CLAUDE.md rules 6-7, spec section 27).

Exchange state is the source of truth; the ledger is the expectation. The expectation for an IDLE client is its last
known-good snapshot plus the effect of every settled leg since (``apply_legs``). Any difference beyond tolerance
freezes that client - and only that client - until a human or a clean recheck resolves it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from oddtrue.config.models import RiskConfig
from oddtrue.exchanges.base import ExchangeAdapter
from oddtrue.exchanges.models import AccountType, Balance
from oddtrue.execution.deps import Clock
from oddtrue.execution.gateway import Gateway, Unresolved
from oddtrue.ledger.repository import Ledger
from oddtrue.ledger.safety import ReconStatus, SafetyLedger
from oddtrue.observability import metrics as m
from oddtrue.observability.alerts import Alert, AlertKind, AlertSink
from oddtrue.observability.logging import get_logger, log_context
from oddtrue.observability.metrics import Metrics
from oddtrue.reconciliation import snapshot as snap
from oddtrue.reconciliation.diff import Difference, diff_snapshots, summarize
from oddtrue.reconciliation.expected import apply_legs
from oddtrue.reconciliation.snapshot import LIABILITIES, MARGIN, SPOT, Snapshot

log = get_logger("reconciliation")


class Verdict(StrEnum):
    BASELINE = "BASELINE"  # first look at this client: recorded as the starting point
    OK = "OK"
    MISMATCH = "MISMATCH"  # the client is now frozen
    DEFERRED = "DEFERRED"  # trades in flight or legs unsettled: not a moment at which balances can be compared
    UNAVAILABLE = "UNAVAILABLE"  # the exchanges could not be read; nothing is concluded either way


@dataclass(frozen=True, slots=True)
class ReconResult:
    verdict: Verdict
    differences: tuple[Difference, ...] = ()
    reconciliation_id: int | None = None
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.verdict in (Verdict.OK, Verdict.BASELINE)


@dataclass(slots=True)
class _Env:
    risk: RiskConfig
    clock: Clock
    op_timeout_override_s: float | None = None


def _dt(epoch_s: float) -> datetime:
    return datetime.fromtimestamp(epoch_s, UTC)


class Reconciler:
    def __init__(
        self,
        ledger: Ledger,
        risk: RiskConfig,
        clock: Clock,
        alerts: AlertSink,
        metrics: Metrics,
        *,
        op_timeout_override_s: float | None = None,
    ) -> None:
        self._led = ledger
        self.safety = SafetyLedger(ledger)
        self._risk, self._clock, self._alerts, self._metrics = risk, clock, alerts, metrics
        self._gw = Gateway(_Env(risk, clock, op_timeout_override_s))

    # ---- reading the exchanges ----------------------------------------
    async def take_snapshot(self, venues: Mapping[str, ExchangeAdapter]) -> Snapshot:
        """Every balance the client has on every exchange. Raises ``Unresolved`` if any read stays unavailable."""
        out = snap.empty(sorted(venues))
        for name, adapter in venues.items():
            for section, account in ((SPOT, AccountType.SPOT), (MARGIN, AccountType.MARGIN)):
                for b in await self._balances(adapter, account):
                    snap.add(out, name, section, b.asset, b.free + b.locked)
            for loan in await self._gw.read("get_liabilities", adapter.get_liabilities):
                snap.add(out, name, LIABILITIES, loan.asset, loan.principal + loan.interest)
        return out

    async def _balances(self, adapter: ExchangeAdapter, account: AccountType) -> list[Balance]:
        return await self._gw.read("get_balances", lambda: adapter.get_balances(account))

    # ---- reconciling ----------------------------------------
    async def reconcile(
        self, client_id: str, venues: Mapping[str, ExchangeAdapter], *, trigger: str, trade_id: str | None = None
    ) -> ReconResult:
        with log_context(client_id=client_id, trade_id=trade_id):
            result = await self._reconcile(client_id, venues, trigger, trade_id)
            self._metrics.inc(m.RECONCILIATIONS, status=result.verdict.value)
            log.info("reconciliation %s", result.verdict.value, extra={"trigger": trigger, "detail": result.detail})
            return result

    async def _reconcile(
        self, client_id: str, venues: Mapping[str, ExchangeAdapter], trigger: str, trade_id: str | None
    ) -> ReconResult:
        sf = self.safety
        others = [t for t in await sf.unfinished_trade_ids(client_id) if t != trade_id]
        if others:
            return ReconResult(Verdict.DEFERRED, detail=f"other trades in flight: {others}")
        if await sf.unsettled_legs(client_id):
            return ReconResult(Verdict.DEFERRED, detail="legs with unknown outcome")
        up_to = await sf.max_leg_id(client_id)
        try:
            actual = await self.take_snapshot(venues)
        except Unresolved as exc:
            self._alerts.raise_alert(
                Alert(AlertKind.RECONCILIATION_UNAVAILABLE, client_id, trade_id or "", exc.what, page=False)
            )
            return ReconResult(Verdict.UNAVAILABLE, detail=exc.what)
        if await sf.max_leg_id(client_id) != up_to:  # something started while we were reading: cannot compare
            return ReconResult(Verdict.DEFERRED, detail="activity during snapshot")

        now = _dt(self._clock.now)
        known_good = await sf.latest_known_good(client_id)
        if known_good is None:
            row = await sf.record_reconciliation(
                client_id, trigger, ReconStatus.BASELINE, baseline_leg_id=up_to,
                expected=snap.to_json(actual), actual=snap.to_json(actual), diff=[], trade_id=trade_id, at=now,
            )  # fmt: skip
            return ReconResult(Verdict.BASELINE, reconciliation_id=row.id)

        legs = await sf.legs_after(client_id, known_good.baseline_leg_id, up_to)
        expected = apply_legs(snap.from_json(known_good.actual), legs)
        differences = diff_snapshots(
            expected, actual, self._risk.reconcile_tolerance_coin, self._risk.reconcile_tolerance_usdt
        )
        status = ReconStatus.MISMATCH if differences else ReconStatus.OK
        row = await sf.record_reconciliation(
            client_id, trigger, status, baseline_leg_id=up_to, expected=snap.to_json(expected),
            actual=snap.to_json(actual), diff=[d.to_json() for d in differences], trade_id=trade_id, at=now,
        )  # fmt: skip
        if not differences:
            return ReconResult(Verdict.OK, reconciliation_id=row.id)
        summary = summarize(differences)
        self._metrics.inc(m.RECONCILIATION_MISMATCH)
        await self._freeze(client_id, f"balance_mismatch: {summary}", row.id, trade_id)
        return ReconResult(Verdict.MISMATCH, tuple(differences), row.id, summary)

    # ---- freezing ----------------------------------------
    async def freeze_client(self, client_id: str, reason: str, trade_id: str | None = None) -> None:
        """Freeze for a mismatch found elsewhere (e.g. the engine's per-leg check). Idempotent."""
        await self._freeze(client_id, reason, None, trade_id)

    async def _freeze(self, client_id: str, reason: str, reconciliation_id: int | None, trade_id: str | None) -> None:
        row = await self.safety.freeze(
            client_id, reason, reconciliation_id=reconciliation_id, trade_id=trade_id, at=_dt(self._clock.now)
        )
        self._alerts.raise_alert(Alert(AlertKind.RECONCILIATION_MISMATCH, client_id, trade_id or "", reason))
        if row is not None:  # first freeze for this incident
            self._alerts.raise_alert(Alert(AlertKind.CLIENT_FROZEN, client_id, trade_id or "", reason, page=False))
            log.error("client frozen: %s", reason)
        self._metrics.set(m.CLIENTS_FROZEN, len(await self.safety.frozen_clients()))

    # ---- resolving ----------------------------------------
    async def recheck(self, client_id: str, venues: Mapping[str, ExchangeAdapter], actor: str) -> ReconResult:
        """Reconcile again; if the exchanges now match the ledger the freeze is lifted. Otherwise it stays."""
        result = await self.reconcile(client_id, venues, trigger="recheck")
        if result.verdict is Verdict.OK and await self.safety.unfreeze(
            client_id, actor, "recheck_clean", at=_dt(self._clock.now)
        ):
            self._metrics.set(m.CLIENTS_FROZEN, len(await self.safety.frozen_clients()))
        return result

    async def accept_exchange_state(
        self, client_id: str, venues: Mapping[str, ExchangeAdapter], actor: str, note: str
    ) -> ReconResult:
        """An operator decides the exchanges are right (e.g. the client moved funds by hand): re-baseline and unfreeze.

        Requires a note; the previous mismatch and this decision are both kept in the audit log.
        """
        if not note.strip():
            raise ValueError("accepting the exchange state requires a note explaining why")
        freeze = await self.safety.active_freeze(client_id)
        if freeze is None:
            raise ValueError(f"client {client_id} is not frozen")
        actual = await self.take_snapshot(venues)  # Unresolved propagates: do not accept what we could not read
        up_to = await self.safety.max_leg_id(client_id)
        now = _dt(self._clock.now)
        row = await self.safety.record_reconciliation(
            client_id, f"accepted_by:{actor}", ReconStatus.ACCEPTED, baseline_leg_id=up_to,
            expected=snap.to_json(actual), actual=snap.to_json(actual), diff=[], at=now,
        )  # fmt: skip
        detail: dict[str, Any] = {"note": note, "froze_because": freeze.reason, "reconciliation_id": row.id}
        await self.safety.audit(actor, "reconciliation.accepted", detail, client_id=client_id, at=now)
        await self.safety.unfreeze(client_id, actor, f"accepted_exchange_state: {note}", at=now)
        self._metrics.inc(m.RECONCILIATIONS, status=ReconStatus.ACCEPTED.value)
        self._metrics.set(m.CLIENTS_FROZEN, len(await self.safety.frozen_clients()))
        return ReconResult(Verdict.OK, reconciliation_id=row.id, detail="accepted")
