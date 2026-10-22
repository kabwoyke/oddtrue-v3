"""What a state handler returns: move to the next state, or hold here and say why."""

from __future__ import annotations

from dataclasses import dataclass

from oddtrue.execution.states import TradeState


@dataclass(frozen=True, slots=True)
class Move:
    to: TradeState
    reason: str


@dataclass(frozen=True, slots=True)
class Hold:
    """Stay in the current state (persisted). The trade is resumed later; nothing is retried blindly."""

    reason: str


Step = Move | Hold
