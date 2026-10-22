"""Trade lifecycle states and the only legal moves between them (CLAUDE.md section 6)."""

from __future__ import annotations

from enum import StrEnum


class TradeState(StrEnum):
    ALLOCATED = "ALLOCATED"
    PRECHECK_B_LIQUIDITY = "PRECHECK_B_LIQUIDITY"
    PRECHECK_BORROW = "PRECHECK_BORROW"
    REVALIDATED = "REVALIDATED"
    EXECUTING = "EXECUTING"
    SETTLING = "SETTLING"
    RECOVERY = "RECOVERY"
    RECONCILING = "RECONCILING"
    REBALANCE_EVAL = "REBALANCE_EVAL"
    DONE = "DONE"
    CANCELLED = "CANCELLED"  # stopped before any order or borrow was sent


TERMINAL = frozenset({TradeState.DONE, TradeState.CANCELLED})

_S = TradeState
ALLOWED: dict[TradeState, frozenset[TradeState]] = {
    _S.ALLOCATED: frozenset({_S.PRECHECK_B_LIQUIDITY, _S.CANCELLED}),
    _S.PRECHECK_B_LIQUIDITY: frozenset({_S.PRECHECK_BORROW, _S.CANCELLED}),
    _S.PRECHECK_BORROW: frozenset({_S.REVALIDATED, _S.CANCELLED}),
    _S.REVALIDATED: frozenset({_S.EXECUTING, _S.CANCELLED}),
    # EXECUTING -> RECONCILING: orders were sent but nothing filled and no loan exists (outcome FAILED)
    _S.EXECUTING: frozenset({_S.SETTLING, _S.RECOVERY, _S.RECONCILING}),
    _S.SETTLING: frozenset({_S.RECONCILING, _S.RECOVERY}),
    _S.RECOVERY: frozenset({_S.RECONCILING}),
    _S.RECONCILING: frozenset({_S.REBALANCE_EVAL, _S.RECOVERY}),
    _S.REBALANCE_EVAL: frozenset({_S.DONE}),
    _S.DONE: frozenset(),
    _S.CANCELLED: frozenset(),
}


def is_legal(src: TradeState, dst: TradeState) -> bool:
    return dst in ALLOWED[src]
