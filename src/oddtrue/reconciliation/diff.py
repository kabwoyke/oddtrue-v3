"""Compare expected balances with what the exchanges report (pure)."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Any

from oddtrue.reconciliation import snapshot as snap
from oddtrue.reconciliation.snapshot import SECTIONS, Snapshot

USDT = "USDT"


def plain(x: Decimal) -> str:
    """600.000 -> '600', 0.0100 -> '0.01', never scientific notation: stable text for reasons and JSON."""
    return f"{x.normalize():f}" if x != 0 else "0"


@dataclass(frozen=True, slots=True)
class Difference:
    exchange: str
    section: str
    asset: str
    expected: Decimal
    actual: Decimal

    @property
    def delta(self) -> Decimal:
        """actual - expected: positive means the exchange holds MORE than the ledger accounts for."""
        return self.actual - self.expected

    def to_json(self) -> dict[str, Any]:
        d = asdict(self)
        return {k: (plain(v) if isinstance(v, Decimal) else v) for k, v in d.items()} | {"delta": plain(self.delta)}


def diff_snapshots(
    expected: Snapshot, actual: Snapshot, tolerance_coin: Decimal, tolerance_usdt: Decimal
) -> list[Difference]:
    """Every (exchange, section, asset) whose difference exceeds tolerance, in a stable order."""
    found: list[Difference] = []
    for ex in sorted(set(expected) | set(actual)):
        for section in SECTIONS:
            assets = set(expected.get(ex, {}).get(section, {})) | set(actual.get(ex, {}).get(section, {}))
            for asset in sorted(assets):
                e, a = snap.get(expected, ex, section, asset), snap.get(actual, ex, section, asset)
                if abs(a - e) > (tolerance_usdt if asset == USDT else tolerance_coin):
                    found.append(Difference(ex, section, asset, e, a))
    return found


def summarize(differences: list[Difference], limit: int = 5) -> str:
    head = "; ".join(
        f"{d.exchange}/{d.section}/{d.asset}: expected {plain(d.expected)} actual {plain(d.actual)}"
        for d in differences[:limit]
    )
    more = len(differences) - limit
    return head + (f" (+{more} more)" if more > 0 else "")
