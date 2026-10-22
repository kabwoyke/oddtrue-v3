"""Per-trade working context shared by the engine and recovery handlers. Holds no state of its own beyond a cache."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from oddtrue.exchanges.base import ExchangeAdapter
from oddtrue.exchanges.models import AccountType, Fees
from oddtrue.execution.deps import Alert, AlertKind, EngineDeps, to_datetime
from oddtrue.execution.gateway import Gateway
from oddtrue.execution.plan import TradePlan
from oddtrue.pricing.types import SymbolRules


@dataclass(slots=True)
class TradeCtx:
    deps: EngineDeps
    plan: TradePlan
    gw: Gateway
    _rules: dict[str, SymbolRules] = field(default_factory=dict)
    _fees: dict[str, Fees] = field(default_factory=dict)

    @property
    def a(self) -> ExchangeAdapter:
        return self.deps.venues[self.plan.buy_exchange]

    @property
    def b(self) -> ExchangeAdapter:
        return self.deps.venues[self.plan.borrow_exchange]

    @property
    def coin(self) -> str:
        return self.plan.coin

    def now(self) -> datetime:
        return to_datetime(self.deps.clock.now)

    def cp(self, name: str) -> None:
        self.deps.checkpoint(name)

    def alert(self, kind: AlertKind, detail: str, *, page: bool = True) -> None:
        self.deps.alerts.raise_alert(Alert(kind, self.plan.client_id, self.plan.trade_id, detail, page))

    def alert_dust(self, leg_name: str, qty: Decimal) -> None:
        self.alert(
            AlertKind.DUST_LEFT, f"{leg_name}: {qty} {self.coin} below exchange minimum, left as dust", page=False
        )

    async def rules(self, exchange: str) -> SymbolRules:
        if exchange not in self._rules:
            adapter = self.deps.venues[exchange]
            self._rules[exchange] = await self.gw.read(
                "get_symbol_rules", lambda: adapter.get_symbol_rules(self.plan.symbol)
            )
        return self._rules[exchange]

    async def fees(self, exchange: str) -> Fees:
        if exchange not in self._fees:
            adapter = self.deps.venues[exchange]
            self._fees[exchange] = await self.gw.read("get_fees", lambda: adapter.get_fees(self.plan.symbol))
        return self._fees[exchange]

    async def coin_free_on_b(self) -> Decimal:
        balances = await self.gw.read("get_balances", lambda: self.b.get_balances(AccountType.MARGIN))
        return next((x.free for x in balances if x.asset == self.coin), Decimal(0))
