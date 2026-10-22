"""Ledger persistence (CLAUDE.md sections 2, 9; docs/DATA_MODEL.md).

Rules enforced here and in the schema:
* A leg's idempotency key is derived from trade_id + leg + attempt and stored BEFORE the exchange call.
* ``ledger_entries``, ``trade_state_log`` and ``rotation_log`` are append-only (database triggers).
* Every read of client data takes a ``client_id`` and filters on it.
* Trade results are derived from ledger entries only: actual figures, never estimates.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Concatenate

from psycopg import AsyncConnection
from psycopg.rows import DictRow
from psycopg.types.json import Jsonb

from oddtrue.allocation.rotation import RotationDecision
from oddtrue.ids import client_order_id
from oddtrue.ledger.models import Entry, EntryKind, LegStatus, Outcome, TradeResult, net_from_parts
from oddtrue.pricing.profit import net_roi_pct
from oddtrue.pricing.types import ZERO


class LedgerError(Exception):
    pass


class StateConflict(LedgerError):
    """The trade was not in the state the caller expected: another worker moved it. Reload and decide again."""


class TradeNotFound(LedgerError):
    pass


class ConcurrencyLimit(LedgerError):
    """The client already has the maximum number of unfinished trades."""


class TradeBlocked(LedgerError):
    """A halt (global, exchange or client) or a client freeze forbids starting a new trade."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class NewTrade:
    id: str
    client_id: str
    symbol: str
    buy_exchange: str
    borrow_exchange: str
    starting_usdt: Decimal
    allocated_95: Decimal
    reserve_5: Decimal
    capital_used: Decimal
    state: str


@dataclass(frozen=True, slots=True)
class Leg:
    id: int
    trade_id: str
    client_id: str
    leg: str
    attempt: int
    client_order_id: str
    exchange_order_id: str | None
    requested_qty: Decimal
    filled_qty: Decimal | None
    avg_price: Decimal | None
    fee: Decimal | None
    fee_asset: str | None
    status: LegStatus
    meta: dict[str, Any]


@dataclass(frozen=True, slots=True)
class LoanRow:
    id: int
    trade_id: str
    client_id: str
    exchange: str
    asset: str
    principal_before: Decimal
    borrowed_qty: Decimal | None
    verified_zero_at: datetime | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class TradeRow:
    id: str
    client_id: str
    state: str
    outcome: Outcome | None
    finished_at: datetime | None


class _TaskLock:
    """Re-entrant (per asyncio task) lock: one connection must never run two transactions interleaved.

    Concurrent legs of a trade are separate tasks sharing the ledger; each ledger method runs to completion while it
    holds this lock, and a method calling another ledger method (same task) re-enters without deadlock.
    A connection pool replaces this at scale (Phase 7).
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._owner: asyncio.Task[Any] | None = None
        self._depth = 0

    async def __aenter__(self) -> None:
        task = asyncio.current_task()
        if self._owner is not None and self._owner is task:
            self._depth += 1
            return
        await self._lock.acquire()
        self._owner, self._depth = task, 1

    async def __aexit__(self, *exc: object) -> None:
        self._depth -= 1
        if self._depth == 0:
            self._owner = None
            self._lock.release()


def _serialized[**P, R](
    fn: Callable[Concatenate[Ledger, P], Coroutine[Any, Any, R]],
) -> Callable[Concatenate[Ledger, P], Coroutine[Any, Any, R]]:
    async def wrapper(self: Ledger, /, *args: P.args, **kwargs: P.kwargs) -> R:
        async with self._lock:
            return await fn(self, *args, **kwargs)

    wrapper.__name__, wrapper.__doc__ = fn.__name__, fn.__doc__
    return wrapper


def _now() -> datetime:
    return datetime.now(UTC)


def _leg(row: DictRow) -> Leg:
    return Leg(
        id=row["id"],
        trade_id=row["trade_id"],
        client_id=row["client_id"],
        leg=row["leg"],
        attempt=row["attempt"],
        client_order_id=row["client_order_id"],
        exchange_order_id=row["exchange_order_id"],
        requested_qty=row["requested_qty"],
        filled_qty=row["filled_qty"],
        avg_price=row["avg_price"],
        fee=row["fee"],
        fee_asset=row["fee_asset"],
        status=LegStatus(row["status"]),
        meta=row["meta"],
    )


def _loan(row: DictRow) -> LoanRow:
    return LoanRow(
        id=row["id"],
        trade_id=row["trade_id"],
        client_id=row["client_id"],
        exchange=row["exchange"],
        asset=row["asset"],
        principal_before=row["principal_before"],
        borrowed_qty=row["borrowed_qty"],
        verified_zero_at=row["verified_zero_at"],
        created_at=row["created_at"],
    )


def _entry(row: DictRow) -> Entry:
    return Entry(
        id=row["id"],
        client_id=row["client_id"],
        trade_id=row["trade_id"],
        kind=EntryKind(row["kind"]),
        amount_usdt=row["amount_usdt"],
        asset=row["asset"],
        qty=row["qty"],
        note=row["note"],
    )


class Ledger:
    """Use with a connection in autocommit mode (``oddtrue.ledger.db.connect``); each method is one transaction."""

    def __init__(self, conn: AsyncConnection[DictRow]) -> None:
        self._c = conn
        self._lock = _TaskLock()

    @property
    def connection(self) -> AsyncConnection[DictRow]:
        """For the sibling stores in this package that share this connection and its lock."""
        return self._c

    @property
    def lock(self) -> _TaskLock:
        return self._lock

    async def _blocked_reason(self, client_id: str, exchanges: Iterable[str]) -> str | None:
        """Why a NEW trade for this client on these exchanges must not start, read from the database (never cached)."""
        cur = await self._c.execute(
            "SELECT scope, target FROM halts WHERE cleared_at IS NULL AND (scope = 'GLOBAL' "
            "OR (scope = 'CLIENT' AND target = %s) OR (scope = 'EXCHANGE' AND target = ANY(%s))) ORDER BY id LIMIT 1",
            (client_id, list(exchanges)),
        )
        halt = await cur.fetchone()
        if halt is not None:
            return {"GLOBAL": "global_halt", "EXCHANGE": f"exchange_halt:{halt['target']}", "CLIENT": "client_halt"}[
                halt["scope"]
            ]
        cur = await self._c.execute(
            "SELECT 1 AS frozen FROM client_freezes WHERE client_id = %s AND resolved_at IS NULL", (client_id,)
        )
        return "client_frozen" if await cur.fetchone() is not None else None

    @_serialized
    async def blocked_reason(self, client_id: str, exchanges: Iterable[str]) -> str | None:
        return await self._blocked_reason(client_id, exchanges)

    # ---- trades ----------------------------------------
    @_serialized
    async def create_trade(self, t: NewTrade, *, at: datetime | None = None) -> None:
        at = at or _now()
        async with self._c.transaction():
            await self._c.execute(
                "INSERT INTO trades (id, client_id, symbol, buy_exchange, borrow_exchange, starting_usdt, "
                "allocated_95, reserve_5, capital_used, state, started_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    t.id,
                    t.client_id,
                    t.symbol,
                    t.buy_exchange,
                    t.borrow_exchange,
                    t.starting_usdt,
                    t.allocated_95,
                    t.reserve_5,
                    t.capital_used,
                    t.state,
                    at,
                ),
            )
            await self._c.execute(
                "INSERT INTO trade_state_log (trade_id, from_state, to_state, reason, at) "
                "VALUES (%s, NULL, %s, %s, %s)",
                (t.id, t.state, "created", at),
            )

    @_serialized
    async def create_trade_limited(
        self, t: NewTrade, plan: dict[str, Any], max_unfinished: int, *, at: datetime | None = None
    ) -> None:
        """Create a trade only if the client has fewer than ``max_unfinished`` unfinished trades (CLAUDE.md 4A).

        Serialised per client with an advisory lock, so two workers cannot both slip a trade in.
        """
        at = at or _now()
        async with self._c.transaction():
            await self._c.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (t.client_id,))
            blocked = await self._blocked_reason(t.client_id, (t.buy_exchange, t.borrow_exchange))
            if blocked is not None:  # authoritative check, atomic with creating the trade
                raise TradeBlocked(blocked)
            cur = await self._c.execute(
                "SELECT count(*) AS n FROM trades WHERE client_id = %s AND outcome IS NULL", (t.client_id,)
            )
            row = await cur.fetchone()
            if row is not None and row["n"] >= max_unfinished:
                raise ConcurrencyLimit(f"client {t.client_id} already has {row['n']} unfinished trade(s)")
            await self.create_trade(t, at=at)
            await self._c.execute("UPDATE trades SET plan = %s WHERE id = %s", (Jsonb(plan), t.id))

    @_serialized
    async def get_plan(self, client_id: str, trade_id: str) -> dict[str, Any]:
        cur = await self._c.execute("SELECT plan FROM trades WHERE id = %s AND client_id = %s", (trade_id, client_id))
        row = await cur.fetchone()
        if row is None:
            raise TradeNotFound(trade_id)
        plan: dict[str, Any] = row["plan"]
        return plan

    @_serialized
    async def set_plan(self, client_id: str, trade_id: str, plan: dict[str, Any]) -> None:
        cur = await self._c.execute(
            "UPDATE trades SET plan = %s WHERE id = %s AND client_id = %s RETURNING id",
            (Jsonb(plan), trade_id, client_id),
        )
        if await cur.fetchone() is None:
            raise TradeNotFound(trade_id)

    @_serialized
    async def state_entered_at(self, client_id: str, trade_id: str, state: str) -> datetime | None:
        """When the trade FIRST entered ``state`` (recovery deadlines are measured from here)."""
        cur = await self._c.execute(
            "SELECT min(l.at) AS at FROM trade_state_log l JOIN trades t ON t.id = l.trade_id "
            "WHERE l.trade_id = %s AND t.client_id = %s AND l.to_state = %s",
            (trade_id, client_id, state),
        )
        row = await cur.fetchone()
        return None if row is None else row["at"]

    @_serialized
    async def get_trade(self, client_id: str, trade_id: str) -> TradeRow:
        cur = await self._c.execute(
            "SELECT id, client_id, state, outcome, finished_at FROM trades WHERE id = %s AND client_id = %s",
            (trade_id, client_id),
        )
        row = await cur.fetchone()
        if row is None:
            raise TradeNotFound(trade_id)
        return TradeRow(
            row["id"], row["client_id"], row["state"], Outcome(row["outcome"]) if row["outcome"] else None,
            row["finished_at"],
        )  # fmt: skip

    @_serialized
    async def transition(
        self, client_id: str, trade_id: str, from_state: str, to_state: str, reason: str, *, at: datetime | None = None
    ) -> None:
        """Compare-and-set the trade state and log it, atomically. Raises StateConflict if the state moved."""
        at = at or _now()
        async with self._c.transaction():
            cur = await self._c.execute(
                "UPDATE trades SET state = %s WHERE id = %s AND client_id = %s AND state = %s RETURNING id",
                (to_state, trade_id, client_id, from_state),
            )
            if await cur.fetchone() is None:
                raise StateConflict(f"trade {trade_id} is not in state {from_state}")
            await self._c.execute(
                "INSERT INTO trade_state_log (trade_id, from_state, to_state, reason, at) VALUES (%s, %s, %s, %s, %s)",
                (trade_id, from_state, to_state, reason, at),
            )

    @_serialized
    async def finish_trade(
        self, client_id: str, trade_id: str, outcome: Outcome, *, at: datetime | None = None
    ) -> None:
        at = at or _now()
        cur = await self._c.execute(
            "UPDATE trades SET outcome = %s, finished_at = %s "
            "WHERE id = %s AND client_id = %s AND outcome IS NULL RETURNING id",
            (outcome.value, at, trade_id, client_id),
        )
        if await cur.fetchone() is None:
            raise StateConflict(f"trade {trade_id} is unknown, not yours, or already finished")

    @_serialized
    async def state_history(self, client_id: str, trade_id: str) -> list[tuple[str | None, str, str | None]]:
        cur = await self._c.execute(
            "SELECT l.from_state, l.to_state, l.reason FROM trade_state_log l JOIN trades t ON t.id = l.trade_id "
            "WHERE l.trade_id = %s AND t.client_id = %s ORDER BY l.id",
            (trade_id, client_id),
        )
        return [(r["from_state"], r["to_state"], r["reason"]) for r in await cur.fetchall()]

    @_serialized
    async def unfinished_trades(self) -> list[TradeRow]:
        """Every trade without an outcome: what a restarted worker must reload and reconcile (Phase 3)."""
        cur = await self._c.execute(
            "SELECT id, client_id, state, outcome, finished_at FROM trades "
            "WHERE outcome IS NULL ORDER BY created_at, id"
        )
        return [TradeRow(r["id"], r["client_id"], r["state"], None, None) for r in await cur.fetchall()]

    # ---- legs ----------------------------------------
    @_serialized
    async def record_leg_intent(
        self,
        client_id: str,
        trade_id: str,
        leg: str,
        attempt: int,
        requested_qty: Decimal,
        meta: dict[str, Any] | None = None,
    ) -> Leg:
        """Persist the leg and its idempotency key BEFORE sending. Calling twice returns the same row (no new key)."""
        coid = client_order_id(trade_id, leg, attempt)
        async with self._c.transaction():
            await self._c.execute(
                "INSERT INTO trade_legs "
                "(trade_id, client_id, leg, attempt, client_order_id, requested_qty, status, meta) "
                "SELECT t.id, t.client_id, %s, %s, %s, %s, %s, %s FROM trades t WHERE t.id = %s AND t.client_id = %s "
                "ON CONFLICT (client_order_id) DO NOTHING",
                (leg, attempt, coid, requested_qty, LegStatus.INTENT.value, Jsonb(meta or {}), trade_id, client_id),
            )
            cur = await self._c.execute(
                "SELECT * FROM trade_legs WHERE client_order_id = %s AND client_id = %s", (coid, client_id)
            )
            row = await cur.fetchone()
        if row is None:
            raise TradeNotFound(trade_id)
        return _leg(row)

    @_serialized
    async def update_leg(
        self,
        client_id: str,
        coid: str,
        status: LegStatus,
        *,
        exchange_order_id: str | None = None,
        filled_qty: Decimal | None = None,
        avg_price: Decimal | None = None,
        fee: Decimal | None = None,
        fee_asset: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> Leg:
        cur = await self._c.execute(
            "UPDATE trade_legs SET status = %s, exchange_order_id = COALESCE(%s, exchange_order_id), "
            "filled_qty = COALESCE(%s, filled_qty), avg_price = COALESCE(%s, avg_price), fee = COALESCE(%s, fee), "
            "fee_asset = COALESCE(%s, fee_asset), meta = meta || %s, updated_at = now() "
            "WHERE client_order_id = %s AND client_id = %s RETURNING *",
            (
                status.value,
                exchange_order_id,
                filled_qty,
                avg_price,
                fee,
                fee_asset,
                Jsonb(meta or {}),
                coid,
                client_id,
            ),
        )
        row = await cur.fetchone()
        if row is None:
            raise LedgerError(f"unknown leg {coid}")
        return _leg(row)

    @_serialized
    async def legs(self, client_id: str, trade_id: str) -> list[Leg]:
        cur = await self._c.execute(
            "SELECT * FROM trade_legs WHERE trade_id = %s AND client_id = %s ORDER BY id", (trade_id, client_id)
        )
        return [_leg(r) for r in await cur.fetchall()]

    # ---- loans ----------------------------------------
    @_serialized
    async def open_loan(
        self,
        client_id: str,
        trade_id: str,
        exchange: str,
        asset: str,
        principal_before: Decimal,
        *,
        at: datetime | None = None,
    ) -> LoanRow:
        """Record that a borrow is about to be sent (persist first). Idempotent per trade/exchange/asset."""
        async with self._c.transaction():
            await self._c.execute(
                "INSERT INTO loans (trade_id, client_id, exchange, asset, principal_before, created_at) "
                "SELECT t.id, t.client_id, %s, %s, %s, %s FROM trades t WHERE t.id = %s AND t.client_id = %s "
                "ON CONFLICT (trade_id, exchange, asset) DO NOTHING",
                (exchange, asset, principal_before, at or _now(), trade_id, client_id),
            )
            cur = await self._c.execute(
                "SELECT * FROM loans WHERE trade_id = %s AND client_id = %s AND exchange = %s AND asset = %s",
                (trade_id, client_id, exchange, asset),
            )
            row = await cur.fetchone()
        if row is None:
            raise TradeNotFound(trade_id)
        return _loan(row)

    @_serialized
    async def loans(self, client_id: str, trade_id: str) -> list[LoanRow]:
        cur = await self._c.execute(
            "SELECT * FROM loans WHERE trade_id = %s AND client_id = %s ORDER BY id", (trade_id, client_id)
        )
        return [_loan(r) for r in await cur.fetchall()]

    @_serialized
    async def set_loan_borrowed(self, client_id: str, loan_id: int, borrowed_qty: Decimal) -> None:
        await self._c.execute(
            "UPDATE loans SET borrowed_qty = %s WHERE id = %s AND client_id = %s", (borrowed_qty, loan_id, client_id)
        )

    @_serialized
    async def verify_loan_zero(self, client_id: str, loan_id: int, *, at: datetime | None = None) -> None:
        """Call ONLY after the exchange API reported outstanding == 0 (CLAUDE.md rule 6)."""
        await self._c.execute(
            "UPDATE loans SET verified_zero_at = COALESCE(verified_zero_at, %s) WHERE id = %s AND client_id = %s",
            (at or _now(), loan_id, client_id),
        )

    # ---- ledger entries (append-only) ----------------------------------------
    @_serialized
    async def append_entry(
        self,
        client_id: str,
        kind: EntryKind,
        amount_usdt: Decimal,
        *,
        trade_id: str | None = None,
        asset: str | None = None,
        qty: Decimal | None = None,
        ref_leg_id: int | None = None,
        note: str | None = None,
        at: datetime | None = None,
    ) -> Entry:
        if trade_id is not None:
            await self.get_trade(client_id, trade_id)  # an entry can only attach to this client's own trade
        cur = await self._c.execute(
            "INSERT INTO ledger_entries (client_id, trade_id, kind, amount_usdt, asset, qty, ref_leg_id, note, at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING *",
            (client_id, trade_id, kind.value, amount_usdt, asset, qty, ref_leg_id, note, at or _now()),
        )
        row = await cur.fetchone()
        if row is None:  # pragma: no cover - INSERT .. RETURNING always returns the row
            raise LedgerError("insert returned no row")
        return _entry(row)

    @_serialized
    async def record_entry_once(
        self,
        client_id: str,
        trade_id: str,
        leg: Leg,
        kind: EntryKind,
        amount_usdt: Decimal,
        *,
        asset: str | None = None,
        qty: Decimal | None = None,
        note: str | None = None,
        at: datetime | None = None,
    ) -> bool:
        """Book an entry tied to ``leg``; a repeat for the same (leg, kind) is a no-op. Returns True if written."""
        await self.get_trade(client_id, trade_id)
        cur = await self._c.execute(
            "INSERT INTO ledger_entries (client_id, trade_id, kind, amount_usdt, asset, qty, ref_leg_id, note, at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (ref_leg_id, kind) WHERE ref_leg_id IS NOT NULL "
            "DO NOTHING RETURNING id",
            (client_id, trade_id, kind.value, amount_usdt, asset, qty, leg.id, note, at or _now()),
        )
        return await cur.fetchone() is not None

    @_serialized
    async def entries(self, client_id: str, trade_id: str | None = None) -> list[Entry]:
        if trade_id is None:
            cur = await self._c.execute("SELECT * FROM ledger_entries WHERE client_id = %s ORDER BY id", (client_id,))
        else:
            cur = await self._c.execute(
                "SELECT * FROM ledger_entries WHERE client_id = %s AND trade_id = %s ORDER BY id", (client_id, trade_id)
            )
        return [_entry(r) for r in await cur.fetchall()]

    # ---- derived results (spec sections 32-33) ----------------------------------------
    @_serialized
    async def trade_result(self, client_id: str, trade_id: str) -> TradeResult:
        cur = await self._c.execute("SELECT * FROM trades WHERE id = %s AND client_id = %s", (trade_id, client_id))
        trade = await cur.fetchone()
        if trade is None:
            raise TradeNotFound(trade_id)
        cur = await self._c.execute(
            "SELECT kind, SUM(amount_usdt) AS amount, SUM(qty) AS qty FROM ledger_entries "
            "WHERE client_id = %s AND trade_id = %s GROUP BY kind",
            (client_id, trade_id),
        )
        by_kind = {r["kind"]: (r["amount"] or ZERO, r["qty"]) for r in await cur.fetchall()}

        def amount(kind: EntryKind) -> Decimal:
            return by_kind.get(kind.value, (ZERO, None))[0]

        def qty(kind: EntryKind) -> Decimal:
            return by_kind.get(kind.value, (ZERO, None))[1] or ZERO

        buy_cost, proceeds = amount(EntryKind.BUY_COST), amount(EntryKind.SELL_PROCEEDS)
        qty_bought, qty_sold = qty(EntryKind.BUY_COST), qty(EntryKind.SELL_PROCEEDS)
        fees, interest, slippage = amount(EntryKind.FEE), amount(EntryKind.INTEREST), amount(EntryKind.SLIPPAGE)
        recovery, transfer = amount(EntryKind.RECOVERY_COST), amount(EntryKind.TRANSFER_COST)
        rebalance, adjustments = amount(EntryKind.REBALANCE_COST), amount(EntryKind.ADJUSTMENT)
        gross = proceeds - buy_cost
        net = net_from_parts(gross, fees, interest, slippage, recovery, transfer, rebalance, adjustments)
        capital = trade["capital_used"]
        return TradeResult(
            trade_id=trade_id,
            client_id=client_id,
            starting_usdt=trade["starting_usdt"],
            allocated_95=trade["allocated_95"],
            reserve_5=trade["reserve_5"],
            capital_used=capital,
            qty_bought=qty_bought,
            qty_sold=qty_sold,
            buy_vwap=buy_cost / qty_bought if qty_bought else None,
            sell_vwap=proceeds / qty_sold if qty_sold else None,
            buy_cost=buy_cost,
            sell_proceeds=proceeds,
            gross=gross,
            fees=fees,
            interest=interest,
            slippage=slippage,
            recovery=recovery,
            transfer=transfer,
            rebalance=rebalance,
            adjustments=adjustments,
            net=net,
            realized_roi_pct=net_roi_pct(net, capital) if capital > 0 else ZERO,
            ending_balance=trade["starting_usdt"] + net,
            outcome=Outcome(trade["outcome"]) if trade["outcome"] else None,
        )

    @_serialized
    async def client_net_total(self, client_id: str) -> Decimal:
        """Net across all of a client's ledger entries; must equal the sum of its trade results (DATA_MODEL inv. 1)."""
        cur = await self._c.execute(
            "SELECT kind, SUM(amount_usdt) AS amount FROM ledger_entries WHERE client_id = %s GROUP BY kind",
            (client_id,),
        )
        s: dict[str, Decimal] = {r["kind"]: r["amount"] for r in await cur.fetchall()}

        def get(kind: EntryKind) -> Decimal:
            return s.get(kind.value, ZERO)

        return net_from_parts(
            get(EntryKind.SELL_PROCEEDS) - get(EntryKind.BUY_COST),
            get(EntryKind.FEE),
            get(EntryKind.INTEREST),
            get(EntryKind.SLIPPAGE),
            get(EntryKind.RECOVERY_COST),
            get(EntryKind.TRANSFER_COST),
            get(EntryKind.REBALANCE_COST),
            get(EntryKind.ADJUSTMENT),
        )

    # ---- rotation audit (D-023) ----------------------------------------
    @_serialized
    async def log_rotation(
        self, round_id: str, opportunity_id: str, decisions: Iterable[RotationDecision], *, at: datetime | None = None
    ) -> None:
        at = at or _now()
        async with self._c.transaction():
            for d in decisions:
                await self._c.execute(
                    "INSERT INTO rotation_log (round_id, opportunity_id, client_id, position, qty, reason, at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    (round_id, opportunity_id, d.client_id, d.position, d.qty, d.reason.value, at),
                )

    @_serialized
    async def last_participation(self, client_ids: Iterable[str]) -> dict[str, datetime | None]:
        """Input for ``plan_rotation``: when each client last got an INCLUDED decision (None = never)."""
        ids = list(client_ids)
        cur = await self._c.execute(
            "SELECT client_id, MAX(at) AS last FROM rotation_log "
            "WHERE reason = 'included' AND client_id = ANY(%s) GROUP BY client_id",
            (ids,),
        )
        found = {r["client_id"]: r["last"] for r in await cur.fetchall()}
        return {c: found.get(c) for c in ids}

    @_serialized
    async def rotation_history(self, client_id: str) -> list[tuple[str, int, Decimal, str]]:
        cur = await self._c.execute(
            "SELECT round_id, position, qty, reason FROM rotation_log WHERE client_id = %s ORDER BY id", (client_id,)
        )
        return [(r["round_id"], r["position"], r["qty"], r["reason"]) for r in await cur.fetchall()]
