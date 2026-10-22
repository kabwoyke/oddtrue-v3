"""Test world for the trade engine: two fake exchanges, one funded client, shared fake clock."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal as D
from pathlib import Path
from typing import Any

from oddtrue.config.loader import load_risk
from oddtrue.config.models import RiskConfig
from oddtrue.exchanges.fake import FakeAccount, FakeClock, FakeVenue
from oddtrue.exchanges.models import AccountType
from oddtrue.execution.deps import EngineDeps, ListAlertSink, no_checkpoint
from oddtrue.execution.engine import TradeEngine
from oddtrue.execution.plan import TradePlan
from oddtrue.ledger.repository import Ledger
from oddtrue.pricing.types import SymbolRules

RISK = load_risk(Path(__file__).resolve().parents[1] / "config" / "risk.yaml")
SYMBOL = "BTCUSDT"
RULES = SymbolRules(D("0.00001"), D(100), D("0.00001"), D("0.01"), D(5))
SPOT, MARGIN = AccountType.SPOT, AccountType.MARGIN
QTY = D("0.004")
INVENTORY = D("0.01")  # BTC pre-held on the borrow side (D-027)


class SimulatedCrash(BaseException):
    """Raised from a checkpoint to kill the 'process'. BaseException so no except-Exception handler swallows it."""


def risk(**changes: Any) -> RiskConfig:
    return RISK.model_copy(update=changes)


@dataclass(slots=True)
class World:
    ledger: Ledger
    clock: FakeClock
    a: FakeVenue  # buy side
    b: FakeVenue  # borrow side
    acc_a: FakeAccount
    acc_b: FakeAccount
    alerts: ListAlertSink = field(default_factory=ListAlertSink)
    cfg: RiskConfig = RISK
    client: str = "john"

    def deps(self, **over: Any) -> EngineDeps:
        base: dict[str, Any] = {
            "ledger": self.ledger,
            "venues": {"binance": self.acc_a, "bitget": self.acc_b},
            "clock": self.clock,
            "risk": self.cfg,
            "alerts": self.alerts,
            "checkpoint": no_checkpoint,
        }
        base.update(over)
        return EngineDeps(**base)

    def engine(self, **over: Any) -> TradeEngine:
        return TradeEngine(self.deps(**over))

    def plan(self, trade_id: str = "t1", qty: D = QTY, **over: Any) -> TradePlan:
        fields: dict[str, Any] = {
            "trade_id": trade_id, "client_id": self.client, "symbol": SYMBOL,
            "buy_exchange": "binance", "borrow_exchange": "bitget", "qty": qty,
            "starting_usdt": D(500), "allocated_95": D(475), "reserve_5": D(25),
            "capital_used": qty * D(100000), "usable_buy": D(500), "usable_borrow": D(500),
        }  # fmt: skip
        fields.update(over)
        return TradePlan(**fields)

    def set_books(self, ask_a: str = "100000", bid_b: str = "100900", ask_b: str = "100910", depth: str = "10") -> None:
        self.a.set_book(SYMBOL, [(D(ask_a) - 10, D(depth))], [(D(ask_a), D(depth)), (D(ask_a) + 100, D(depth))])
        self.b.set_book(
            SYMBOL,
            [(D(bid_b), D(depth)), (D(bid_b) - 100, D(depth))],
            [(D(ask_b), D(depth)), (D(ask_b) + 100, D(depth))],
        )

    def coin_total(self) -> D:
        return self.acc_a.balance(SPOT, "BTC") + self.acc_b.balance(MARGIN, "BTC")

    def usdt_total(self) -> D:
        return self.acc_a.balance(SPOT, "USDT") + self.acc_b.balance(MARGIN, "USDT")


def build_world(ledger: Ledger, **cfg: Any) -> World:
    clock = FakeClock()
    venues = []
    for name in ("binance", "bitget"):
        v = FakeVenue(name, clock)
        v.set_rules(SYMBOL, RULES)
        v.set_borrow("BTC", D("0.0001"), D(100))
        venues.append(v)
    a, b = venues
    acc_a, acc_b = a.account("john"), b.account("john")
    acc_a.fund(SPOT, "USDT", D(1000))
    acc_b.fund(MARGIN, "USDT", D(500))
    acc_b.fund(MARGIN, "BTC", INVENTORY)
    w = World(ledger, clock, a, b, acc_a, acc_b, cfg=risk(**cfg) if cfg else RISK)
    w.set_books()
    return w


CheckpointFn = Callable[[str], None]
