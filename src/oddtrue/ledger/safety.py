"""Persistence for the safety layer: audit log, kill switches, freezes, reconciliation records and alerts.

Shares the ledger's connection and per-task lock, so a freeze or halt is atomic with the audit entry that records it
(CLAUDE.md rule 10: every halt/resume is audit-logged) and with trade creation (see ``Ledger.create_trade_limited``).
"""

from __future__ import annotations

import functools
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Concatenate

from psycopg.rows import DictRow
from psycopg.types.json import Jsonb

from oddtrue.ledger.repository import Ledger


class HaltScope(StrEnum):
    GLOBAL = "GLOBAL"
    EXCHANGE = "EXCHANGE"
    CLIENT = "CLIENT"


class ReconStatus(StrEnum):
    BASELINE = "BASELINE"  # first snapshot of a client: nothing to compare against yet
    OK = "OK"  # exchange matched the ledger's expectation
    MISMATCH = "MISMATCH"
    ACCEPTED = "ACCEPTED"  # an operator accepted the exchange's state as the new truth


KNOWN_GOOD = (ReconStatus.BASELINE, ReconStatus.OK, ReconStatus.ACCEPTED)


@dataclass(frozen=True, slots=True)
class HaltRow:
    id: int
    scope: HaltScope
    target: str | None
    reason: str
    set_by: str
    set_at: datetime


@dataclass(frozen=True, slots=True)
class FreezeRow:
    id: int
    client_id: str
    reason: str
    reconciliation_id: int | None
    trade_id: str | None
    frozen_at: datetime


@dataclass(frozen=True, slots=True)
class ReconRow:
    id: int
    client_id: str
    trigger: str
    status: ReconStatus
    trade_id: str | None
    baseline_leg_id: int
    expected: dict[str, Any]
    actual: dict[str, Any]
    diff: list[dict[str, Any]]
    at: datetime


@dataclass(frozen=True, slots=True)
class LegFact:
    """One settled leg joined to its trade: everything needed to say what it did to the client's balances."""

    leg_id: int
    leg: str
    status: str
    symbol: str
    buy_exchange: str
    borrow_exchange: str
    filled_qty: Decimal
    avg_price: Decimal
    fee: Decimal
    fee_asset: str | None
    meta: dict[str, Any]


@dataclass(frozen=True, slots=True)
class AlertRow:
    id: int
    severity: str
    kind: str
    client_id: str | None
    trade_id: str | None
    detail: str
    at: datetime
    acknowledged_by: str | None


def _now() -> datetime:
    return datetime.now(UTC)


def _locked[**P, R](
    fn: Callable[Concatenate[SafetyLedger, P], Awaitable[R]],
) -> Callable[Concatenate[SafetyLedger, P], Awaitable[R]]:
    @functools.wraps(fn)
    async def wrapper(self: SafetyLedger, /, *args: P.args, **kwargs: P.kwargs) -> R:
        async with self._ledger.lock:
            return await fn(self, *args, **kwargs)

    return wrapper


class SafetyLedger:
    def __init__(self, ledger: Ledger) -> None:
        self._ledger = ledger

    @property
    def _c(self) -> Any:
        return self._ledger.connection

    # ---- audit ----------------------------------------
    async def _audit(
        self, actor: str, action: str, detail: dict[str, Any], client_id: str | None, at: datetime
    ) -> None:
        await self._c.execute(
            "INSERT INTO audit_log (actor, client_id, action, detail, at) VALUES (%s, %s, %s, %s, %s)",
            (actor, client_id, action, Jsonb(detail), at),
        )

    @_locked
    async def audit(
        self,
        actor: str,
        action: str,
        detail: dict[str, Any] | None = None,
        *,
        client_id: str | None = None,
        at: datetime | None = None,
    ) -> None:
        await self._audit(actor, action, detail or {}, client_id, at or _now())

    @_locked
    async def audit_entries(self, action: str | None = None, client_id: str | None = None) -> list[dict[str, Any]]:
        cur = await self._c.execute(
            "SELECT actor, client_id, action, detail, at FROM audit_log "
            "WHERE (%s::text IS NULL OR action = %s) AND (%s::text IS NULL OR client_id = %s) ORDER BY id",
            (action, action, client_id, client_id),
        )
        rows: list[dict[str, Any]] = await cur.fetchall()
        return rows

    # ---- kill switches ----------------------------------------
    @_locked
    async def set_halt(
        self, scope: HaltScope, target: str | None, reason: str, actor: str, *, at: datetime | None = None
    ) -> HaltRow | None:
        """Start a halt. Returns None (and records nothing new) if the same halt is already active."""
        at = at or _now()
        async with self._c.transaction():
            cur = await self._c.execute(
                "INSERT INTO halts (scope, target, reason, set_by, set_at) VALUES (%s, %s, %s, %s, %s) "
                "ON CONFLICT (scope, COALESCE(target, '')) WHERE cleared_at IS NULL DO NOTHING RETURNING *",
                (scope.value, target, reason, actor, at),
            )
            row = await cur.fetchone()
            if row is None:
                return None
            await self._audit(
                actor,
                "halt.set",
                {"scope": scope.value, "target": target, "reason": reason},
                _client(scope, target),
                at,
            )
        return _halt(row)

    @_locked
    async def clear_halt(
        self, scope: HaltScope, target: str | None, actor: str, reason: str, *, at: datetime | None = None
    ) -> bool:
        at = at or _now()
        async with self._c.transaction():
            cur = await self._c.execute(
                "UPDATE halts SET cleared_by = %s, cleared_at = %s, clear_reason = %s "
                "WHERE scope = %s AND COALESCE(target, '') = COALESCE(%s, '') AND cleared_at IS NULL RETURNING id",
                (actor, at, reason, scope.value, target),
            )
            if await cur.fetchone() is None:
                return False
            await self._audit(
                actor,
                "halt.cleared",
                {"scope": scope.value, "target": target, "reason": reason},
                _client(scope, target),
                at,
            )
        return True

    @_locked
    async def active_halts(self) -> list[HaltRow]:
        cur = await self._c.execute("SELECT * FROM halts WHERE cleared_at IS NULL ORDER BY id")
        return [_halt(r) for r in await cur.fetchall()]

    # ---- freezes ----------------------------------------
    @_locked
    async def freeze(
        self,
        client_id: str,
        reason: str,
        *,
        reconciliation_id: int | None = None,
        trade_id: str | None = None,
        actor: str = "system:reconciler",
        at: datetime | None = None,
    ) -> FreezeRow | None:
        """Freeze a client (status RECONCILING). Returns None if it is already frozen."""
        at = at or _now()
        async with self._c.transaction():
            cur = await self._c.execute(
                "INSERT INTO client_freezes (client_id, reason, reconciliation_id, trade_id, frozen_at) "
                "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (client_id) WHERE resolved_at IS NULL DO NOTHING RETURNING *",
                (client_id, reason, reconciliation_id, trade_id, at),
            )
            row = await cur.fetchone()
            if row is None:
                return None
            await self._audit(actor, "client.frozen", {"reason": reason, "trade_id": trade_id}, client_id, at)
        return _freeze(row)

    @_locked
    async def active_freeze(self, client_id: str) -> FreezeRow | None:
        cur = await self._c.execute(
            "SELECT * FROM client_freezes WHERE client_id = %s AND resolved_at IS NULL", (client_id,)
        )
        row = await cur.fetchone()
        return None if row is None else _freeze(row)

    @_locked
    async def frozen_clients(self) -> list[str]:
        cur = await self._c.execute("SELECT client_id FROM client_freezes WHERE resolved_at IS NULL ORDER BY client_id")
        return [r["client_id"] for r in await cur.fetchall()]

    @_locked
    async def unfreeze(self, client_id: str, actor: str, resolution: str, *, at: datetime | None = None) -> bool:
        at = at or _now()
        async with self._c.transaction():
            cur = await self._c.execute(
                "UPDATE client_freezes SET resolved_at = %s, resolved_by = %s, resolution = %s "
                "WHERE client_id = %s AND resolved_at IS NULL RETURNING reconciliation_id",
                (at, actor, resolution, client_id),
            )
            row = await cur.fetchone()
            if row is None:
                return False
            if row["reconciliation_id"] is not None:
                await self._c.execute(
                    "UPDATE reconciliations SET resolved_at = %s, resolved_by = %s, note = %s WHERE id = %s",
                    (at, actor, resolution, row["reconciliation_id"]),
                )
            await self._audit(actor, "client.unfrozen", {"resolution": resolution}, client_id, at)
        return True

    # ---- reconciliation records ----------------------------------------
    @_locked
    async def record_reconciliation(
        self,
        client_id: str,
        trigger: str,
        status: ReconStatus,
        *,
        baseline_leg_id: int,
        expected: dict[str, Any],
        actual: dict[str, Any],
        diff: list[dict[str, Any]],
        trade_id: str | None = None,
        at: datetime | None = None,
    ) -> ReconRow:
        cur = await self._c.execute(
            "INSERT INTO reconciliations "
            "(client_id, trigger, status, trade_id, baseline_leg_id, expected, actual, diff, at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING *",
            (
                client_id, trigger, status.value, trade_id, baseline_leg_id,
                Jsonb(expected), Jsonb(actual), Jsonb(diff), at or _now(),
            ),
        )  # fmt: skip
        row = await cur.fetchone()
        if row is None:  # pragma: no cover - INSERT .. RETURNING always returns the row
            raise RuntimeError("insert returned no row")
        return _recon(row)

    @_locked
    async def latest_known_good(self, client_id: str) -> ReconRow | None:
        cur = await self._c.execute(
            "SELECT * FROM reconciliations WHERE client_id = %s AND status = ANY(%s) ORDER BY id DESC LIMIT 1",
            (client_id, [s.value for s in KNOWN_GOOD]),
        )
        row = await cur.fetchone()
        return None if row is None else _recon(row)

    @_locked
    async def reconciliations(self, client_id: str) -> list[ReconRow]:
        cur = await self._c.execute("SELECT * FROM reconciliations WHERE client_id = %s ORDER BY id", (client_id,))
        return [_recon(r) for r in await cur.fetchall()]

    @_locked
    async def max_leg_id(self, client_id: str) -> int:
        cur = await self._c.execute(
            "SELECT COALESCE(MAX(id), 0) AS m FROM trade_legs WHERE client_id = %s", (client_id,)
        )
        row = await cur.fetchone()
        return 0 if row is None else int(row["m"])

    @_locked
    async def legs_after(self, client_id: str, leg_id: int, up_to: int) -> list[LegFact]:
        """Settled legs with ``leg_id < id <= up_to``: what happened to the client's balances since the baseline."""
        cur = await self._c.execute(
            "SELECT l.id, l.leg, l.status, t.symbol, t.buy_exchange, t.borrow_exchange, l.filled_qty, l.avg_price, "
            "l.fee, l.fee_asset, l.meta FROM trade_legs l JOIN trades t ON t.id = l.trade_id "
            "WHERE l.client_id = %s AND l.id > %s AND l.id <= %s ORDER BY l.id",
            (client_id, leg_id, up_to),
        )
        return [
            LegFact(
                r["id"], r["leg"], r["status"], r["symbol"], r["buy_exchange"], r["borrow_exchange"],
                r["filled_qty"] or Decimal(0), r["avg_price"] or Decimal(0), r["fee"] or Decimal(0),
                r["fee_asset"], r["meta"],
            )
            for r in await cur.fetchall()
        ]  # fmt: skip

    @_locked
    async def unsettled_legs(self, client_id: str) -> int:
        """Legs whose outcome is still unknown (cannot reconcile around them)."""
        cur = await self._c.execute(
            "SELECT count(*) AS n FROM trade_legs WHERE client_id = %s AND status IN ('INTENT', 'SENT', 'UNKNOWN')",
            (client_id,),
        )
        row = await cur.fetchone()
        return 0 if row is None else int(row["n"])

    @_locked
    async def unfinished_trade_ids(self, client_id: str) -> list[str]:
        cur = await self._c.execute(
            "SELECT id FROM trades WHERE client_id = %s AND outcome IS NULL ORDER BY created_at, id", (client_id,)
        )
        return [r["id"] for r in await cur.fetchall()]

    @_locked
    async def open_loans(self, client_ids: Iterable[str] | None = None) -> list[tuple[str, str, str, datetime]]:
        """(client_id, trade_id, asset, opened_at) for every loan not yet verified as zero."""
        cur = await self._c.execute(
            "SELECT client_id, trade_id, asset, created_at FROM loans WHERE verified_zero_at IS NULL "
            "AND (%s::text[] IS NULL OR client_id = ANY(%s)) ORDER BY created_at",
            (None if client_ids is None else list(client_ids),) * 2,
        )
        return [(r["client_id"], r["trade_id"], r["asset"], r["created_at"]) for r in await cur.fetchall()]

    # ---- alerts ----------------------------------------
    @_locked
    async def save_alerts(self, rows: Iterable[tuple[str, str, str | None, str | None, str, datetime]]) -> int:
        """Persist (severity, kind, client_id, trade_id, detail, at) rows in one transaction."""
        count = 0
        async with self._c.transaction():
            for severity, kind, client_id, trade_id, detail, at in rows:
                await self._c.execute(
                    "INSERT INTO alerts (severity, kind, client_id, trade_id, detail, at) "
                    "VALUES (%s, %s, %s, %s, %s, %s)",
                    (severity, kind, client_id, trade_id, detail, at),
                )
                count += 1
        return count

    @_locked
    async def alerts(self, kind: str | None = None, client_id: str | None = None) -> list[AlertRow]:
        cur = await self._c.execute(
            "SELECT * FROM alerts WHERE (%s::text IS NULL OR kind = %s) "
            "AND (%s::text IS NULL OR client_id = %s) ORDER BY id",
            (kind, kind, client_id, client_id),
        )
        return [
            AlertRow(
                r["id"], r["severity"], r["kind"], r["client_id"], r["trade_id"], r["detail"], r["at"],
                r["acknowledged_by"],
            )
            for r in await cur.fetchall()
        ]  # fmt: skip

    @_locked
    async def acknowledge_alert(self, alert_id: int, actor: str, *, at: datetime | None = None) -> bool:
        cur = await self._c.execute(
            "UPDATE alerts SET acknowledged_by = %s, acknowledged_at = %s WHERE id = %s AND acknowledged_by IS NULL "
            "RETURNING id",
            (actor, at or _now(), alert_id),
        )
        return await cur.fetchone() is not None


def _client(scope: HaltScope, target: str | None) -> str | None:
    return target if scope is HaltScope.CLIENT else None


def _halt(r: DictRow) -> HaltRow:
    return HaltRow(r["id"], HaltScope(r["scope"]), r["target"], r["reason"], r["set_by"], r["set_at"])


def _freeze(r: DictRow) -> FreezeRow:
    return FreezeRow(r["id"], r["client_id"], r["reason"], r["reconciliation_id"], r["trade_id"], r["frozen_at"])


def _recon(r: DictRow) -> ReconRow:
    return ReconRow(
        r["id"], r["client_id"], r["trigger"], ReconStatus(r["status"]), r["trade_id"], r["baseline_leg_id"],
        r["expected"], r["actual"], r["diff"], r["at"],
    )  # fmt: skip
