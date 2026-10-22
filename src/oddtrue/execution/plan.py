"""The persisted description of one trade: what allocation granted plus what revalidation computed.

Stored as JSON on the trade row (decimals as strings) so any worker can resume the trade after a crash.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any

from oddtrue.exchanges.models import base_asset
from oddtrue.ledger.repository import NewTrade


@dataclass(frozen=True, slots=True)
class TradePlan:
    trade_id: str
    client_id: str
    symbol: str  # USDT-quoted, e.g. SOLUSDT
    buy_exchange: str  # exchange A: spend USDT, buy the coin
    borrow_exchange: str  # exchange B: borrow the coin, sell it
    qty: Decimal  # allocated quantity, already rounded to a step valid on both exchanges
    starting_usdt: Decimal
    allocated_95: Decimal
    reserve_5: Decimal  # the borrow side's reserve; pays for recovery purchases (D-022)
    capital_used: Decimal
    usable_buy: Decimal  # per-exchange usable capital (D-022)
    usable_borrow: Decimal
    # filled in by REVALIDATED:
    buy_vwap: Decimal | None = None
    sell_vwap: Decimal | None = None
    buy_limit: Decimal | None = None
    sell_limit: Decimal | None = None
    expected_net_roi_pct: Decimal | None = None
    # filled in when the trade heads to its end:
    outcome: str | None = None

    @property
    def coin(self) -> str:
        return base_asset(self.symbol)

    def with_(self, **changes: Any) -> TradePlan:
        return replace(self, **changes)

    def new_trade_row(self) -> NewTrade:
        return NewTrade(
            self.trade_id,
            self.client_id,
            self.symbol,
            self.buy_exchange,
            self.borrow_exchange,
            self.starting_usdt,
            self.allocated_95,
            self.reserve_5,
            self.capital_used,
            "ALLOCATED",
        )

    def to_json(self) -> dict[str, Any]:
        return {k: (str(v) if isinstance(v, Decimal) else v) for k, v in self._items()}

    def _items(self) -> list[tuple[str, Any]]:
        return [(f, getattr(self, f)) for f in self.__dataclass_fields__]

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> TradePlan:
        decimals = {f for f, t in cls.__annotations__.items() if "Decimal" in str(t)}
        kwargs: dict[str, Any] = {k: (Decimal(v) if k in decimals and v is not None else v) for k, v in data.items()}
        return cls(**kwargs)
