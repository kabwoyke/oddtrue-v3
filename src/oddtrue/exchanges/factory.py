"""Builds the read-only adapters (Phase 5) from environment variables. No trading adapter exists yet (Phase 6).

Without credentials the adapters still serve every public endpoint (books, symbol rules); ``have_keys`` tells the
caller whether signed reads can work. Request budgets are deliberately below the exchanges' real caps.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, MutableMapping
from pathlib import Path

from oddtrue.exchanges.base import ExchangeAdapter
from oddtrue.exchanges.binance import BinanceHttp, BinanceReadOnlyAdapter
from oddtrue.exchanges.binance.http import UrllibTransport
from oddtrue.exchanges.binance.ws import connect as binance_ws
from oddtrue.exchanges.bitget import BitgetHttp, BitgetReadOnlyAdapter
from oddtrue.ratelimit import WeightBudget

EXCHANGES = ("binance", "bitget")


def load_env_file(path: Path = Path(".env"), env: MutableMapping[str, str] | None = None) -> None:
    """Minimal KEY=VALUE loader (no dependency). Variables already set win. Values are never printed."""
    target = os.environ if env is None else env
    if not path.is_file():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        target.setdefault(name.strip(), value.strip().strip("\"'"))


def make_readonly(
    exchange: str,
    *,
    env: MutableMapping[str, str] | None = None,
    clock: Callable[[], float] = time.time,
) -> tuple[ExchangeAdapter, bool]:
    """The read-only adapter for ``exchange`` and whether a full set of credentials was supplied."""
    get = (os.environ if env is None else env).get
    if exchange == "binance":
        key, secret = get("BINANCE_API_KEY", ""), get("BINANCE_API_SECRET", "")
        budget = WeightBudget(limit=1200, window_s=60, clock=clock, exchange="binance")
        http = BinanceHttp(
            api_key=key or "public", secret=secret or "public", transport=UrllibTransport(), clock=clock, budget=budget
        )
        return BinanceReadOnlyAdapter(http, clock=clock, ws_connect=binance_ws), bool(key and secret)
    if exchange == "bitget":
        key, secret, phrase = get("BITGET_API_KEY", ""), get("BITGET_API_SECRET", ""), get("BITGET_API_PASSPHRASE", "")
        bg = BitgetHttp(
            api_key=key or "public",
            secret=secret or "public",
            passphrase=phrase or "public",
            transport=UrllibTransport(),
            clock=clock,
            ip_budget=WeightBudget(limit=10, window_s=1, clock=clock, exchange="bitget"),
            uid_budget=WeightBudget(limit=5, window_s=1, clock=clock, exchange="bitget"),
        )
        return BitgetReadOnlyAdapter(bg, clock=clock), bool(key and secret and phrase)
    raise ValueError(f"unknown exchange {exchange!r}; expected one of {EXCHANGES}")
