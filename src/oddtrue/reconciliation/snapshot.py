"""A client's balances across both exchanges at one moment. Pure data; exact Decimals; JSON-friendly."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from oddtrue.pricing.types import ZERO

SPOT = "spot"
MARGIN = "margin"
LIABILITIES = "liabilities"
SECTIONS = (SPOT, MARGIN, LIABILITIES)

# exchange -> section -> asset -> quantity. Zero balances are omitted so "absent" and "0" compare equal.
Snapshot = dict[str, dict[str, dict[str, Decimal]]]


def empty(exchanges: list[str]) -> Snapshot:
    return {ex: {section: {} for section in SECTIONS} for ex in exchanges}


def copy(snapshot: Snapshot) -> Snapshot:
    return {ex: {sec: dict(assets) for sec, assets in secs.items()} for ex, secs in snapshot.items()}


def add(snapshot: Snapshot, exchange: str, section: str, asset: str, delta: Decimal) -> None:
    """Add ``delta`` (may be negative) to one balance, creating it if needed and dropping it if it nets to zero."""
    assets = snapshot.setdefault(exchange, {s: {} for s in SECTIONS}).setdefault(section, {})
    total = assets.get(asset, ZERO) + delta
    if total == ZERO:
        assets.pop(asset, None)
    else:
        assets[asset] = total


def get(snapshot: Snapshot, exchange: str, section: str, asset: str) -> Decimal:
    return snapshot.get(exchange, {}).get(section, {}).get(asset, ZERO)


def to_json(snapshot: Snapshot) -> dict[str, Any]:
    return {
        ex: {sec: {a: str(q) for a, q in assets.items()} for sec, assets in secs.items()}
        for ex, secs in snapshot.items()
    }


def from_json(data: dict[str, Any]) -> Snapshot:
    return {
        ex: {sec: {a: Decimal(q) for a, q in assets.items()} for sec, assets in secs.items()}
        for ex, secs in data.items()
    }
