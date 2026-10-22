"""Typed config schema. Money, price and ratio values are Decimal; floats are rejected at load time."""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Mode(StrEnum):
    PAPER = "paper"
    TESTNET = "testnet"
    LIVE_MIN = "live-min"
    LIVE = "live"


class SettlementMethod(StrEnum):
    """How the loan is repaid after a successful trade (DECISIONS D-020)."""

    BUY_ON_BORROW_SIDE = "buy_on_borrow_side"
    PREHELD_INVENTORY = "preheld_inventory"
    TRANSFER_TO_BORROW_SIDE = "transfer_to_borrow_side"


class MarginMode(StrEnum):
    CROSS = "cross"
    ISOLATED = "isolated"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class RiskConfig(_Frozen):
    mode: Mode = Mode.PAPER
    live_enabled: bool = False
    live_allowlist: tuple[str, ...] = ()

    client_trade_pct: Decimal = Field(gt=0, le=1)
    client_reserve_pct: Decimal = Field(ge=0, lt=1)
    market_liquidity_pct: Decimal = Field(gt=0, le=1)

    max_slippage: Decimal = Field(ge=0, lt=1)
    min_net_roi: Decimal = Field(ge=0)
    live_min_max_notional_usdt: Decimal = Field(gt=0)

    op_timeout_s: int = Field(gt=0)
    recovery_timeout_s: int = Field(gt=0)

    max_exposure_per_client_usdt: Decimal = Field(gt=0)
    max_exposure_global_usdt: Decimal = Field(gt=0)
    settlement_method: SettlementMethod
    margin_mode: MarginMode

    # Execution and recovery (Phase 3). All PROVISIONAL until Phases 6-7 (decision #12).
    max_book_age_s: Decimal = Field(default=Decimal(2), gt=0)  # order book older than this fails pre-check
    execution_timeout_s: int = Field(default=30, gt=0)  # hard bound for the whole EXECUTING phase
    unknown_resolve_attempts: int = Field(default=3, ge=1)  # queries before an order is deemed never placed
    unknown_resolve_backoff_s: Decimal = Field(default=Decimal("0.5"), ge=0)
    expected_loan_seconds: int = Field(default=60, ge=0)  # loan duration assumed for the interest estimate
    repay_attempts: int = Field(default=3, ge=1)  # repay passes to absorb interest accrued while repaying
    recovery_slippage_ladder: tuple[Decimal, ...] = (Decimal("0.002"), Decimal("0.005"), Decimal("0.01"))

    @model_validator(mode="after")
    def _split_sums_to_one(self) -> Self:
        if self.client_trade_pct + self.client_reserve_pct != Decimal(1):
            raise ValueError("client_trade_pct + client_reserve_pct must equal 1")
        if self.max_exposure_per_client_usdt > self.max_exposure_global_usdt:
            raise ValueError("per-client exposure cannot exceed global exposure")
        ladder = self.recovery_slippage_ladder
        if not ladder or any(not Decimal(0) <= x < Decimal(1) for x in ladder):
            raise ValueError("recovery_slippage_ladder needs values in [0, 1)")
        if list(ladder) != sorted(set(ladder)):
            raise ValueError("recovery_slippage_ladder must be strictly increasing (escalating cover, D-026)")
        return self


class ScaleConfig(_Frozen):
    max_active_clients: int = Field(gt=0)
    max_concurrent_trades_global: int = Field(gt=0)
    max_concurrent_trades_per_client: int = Field(gt=0)
    batch_size: int = Field(gt=0)
    batch_stagger_ms: int = Field(ge=0)
    queue_max_depth: int = Field(gt=0)
    client_op_timeout_s: int = Field(gt=0)
    exchange_budgets: dict[str, dict[str, int]] = Field(default_factory=dict)


class PairsConfig(_Frozen):
    allow: tuple[str, ...] = ()
    deny: tuple[str, ...] = ()

    def is_tradable(self, symbol: str) -> bool:
        return symbol in self.allow and symbol not in self.deny
