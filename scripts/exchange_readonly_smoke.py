"""Live read-only smoke test for the real exchange adapters (Phase 5). Owner-run; never part of CI.

Public reads need nothing. Signed reads (balances, margin, permissions) run only if the key variables are set, and
the key should have READ permission only. Nothing here can place an order, borrow, repay or withdraw: the adapters'
HTTP layers only send allowlisted GETs.

    .venv/bin/python scripts/exchange_readonly_smoke.py binance [SYMBOL]   (default BTCUSDT)
    .venv/bin/python scripts/exchange_readonly_smoke.py bitget  [SYMBOL]

Binance: BINANCE_API_KEY, BINANCE_API_SECRET.  Bitget: BITGET_API_KEY, BITGET_API_SECRET, BITGET_API_PASSPHRASE.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time

from oddtrue.exchanges.base import ExchangeAdapter
from oddtrue.exchanges.binance import BinanceHttp, BinanceReadOnlyAdapter
from oddtrue.exchanges.binance.http import UrllibTransport
from oddtrue.exchanges.bitget import BitgetHttp, BitgetReadOnlyAdapter
from oddtrue.exchanges.errors import ExchangeError
from oddtrue.exchanges.models import AccountType
from oddtrue.ratelimit import WeightBudget


def build(exchange: str) -> tuple[ExchangeAdapter, bool]:
    """The adapter and whether credentials were supplied. Limits are deliberately below the real caps."""
    env = os.environ.get
    if exchange == "binance":
        key, secret = env("BINANCE_API_KEY", ""), env("BINANCE_API_SECRET", "")
        budget = WeightBudget(limit=1200, window_s=60, clock=time.time, exchange="binance")
        http = BinanceHttp(
            api_key=key or "public",
            secret=secret or "public",
            transport=UrllibTransport(),
            clock=time.time,
            budget=budget,
        )
        return BinanceReadOnlyAdapter(http, clock=time.time), bool(key and secret)
    if exchange == "bitget":
        key, secret, phrase = env("BITGET_API_KEY", ""), env("BITGET_API_SECRET", ""), env("BITGET_API_PASSPHRASE", "")
        bg = BitgetHttp(
            api_key=key or "public",
            secret=secret or "public",
            passphrase=phrase or "public",
            transport=UrllibTransport(),
            clock=time.time,
            ip_budget=WeightBudget(limit=10, window_s=1, clock=time.time, exchange="bitget"),
            uid_budget=WeightBudget(limit=5, window_s=1, clock=time.time, exchange="bitget"),
        )
        return BitgetReadOnlyAdapter(bg, clock=time.time), bool(key and secret and phrase)
    raise SystemExit("usage: exchange_readonly_smoke.py binance|bitget [SYMBOL]")


async def main(exchange: str, symbol: str) -> None:
    a, have_keys = build(exchange)
    coin = symbol.removesuffix("USDT")
    book = await a.get_orderbook(symbol, 5)
    print(f"{exchange} book: bid {book.bids[0].price} / ask {book.asks[0].price}")
    print("rules:", await a.get_symbol_rules(symbol))
    if not have_keys:
        print("no API key in the environment: skipping signed reads")
        return
    steps = {
        "permissions": a.check_permissions(),
        "fees": a.get_fees(symbol),
        "spot balances": a.get_balances(AccountType.SPOT),
        "margin balances": a.get_balances(AccountType.MARGIN),
        "margin account": a.get_margin_account(),
        "liabilities": a.get_liabilities(),
        "borrowable": a.get_borrowable(coin),
        "borrow rate": a.get_borrow_rate(coin),
    }
    for name, call in steps.items():
        try:
            print(f"{name}:", await call)
        except ExchangeError as exc:
            print(f"{name}: {type(exc).__name__} {exc.code}")
        await asyncio.sleep(1.1)  # Bitget allows one account-info call per second; keep every exchange polite


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "binance", sys.argv[2] if len(sys.argv) > 2 else "BTCUSDT"))
