"""Live, log-only opportunity detection on real public order books (Phase 5). Owner-run; never part of CI.

Streams books from Binance (websocket) and Bitget (REST polling, interim) for a few symbols, evaluates both
directions on every scan, and reports what WOULD have been an opportunity. It cannot trade: it holds only the
read-only adapters, and the detection and market-data packages cannot import execution code (tested).

Fees and the borrow rate are signed reads on at least one exchange. With keys in .env they are read from the
exchanges; without keys you must pass them, and every opportunity is then logged with them marked ASSUMED.

    .venv/bin/python scripts/detect_live.py --seconds 60 --symbols BTCUSDT,ETHUSDT \
        --assume-taker-fee 0.001 --assume-borrow-rate-hourly 0.00001
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import time
from decimal import Decimal
from pathlib import Path

from oddtrue.config import load_risk
from oddtrue.detection.costs import AdapterCosts
from oddtrue.detection.direct import DetectionParams
from oddtrue.detection.scanner import Scanner
from oddtrue.exchanges.factory import EXCHANGES, load_env_file, make_readonly
from oddtrue.exchanges.models import Fees
from oddtrue.marketdata.cache import BookCache
from oddtrue.marketdata.feed import BookFeed
from oddtrue.observability import metrics as m
from oddtrue.observability.logging import configure_logging
from oddtrue.observability.metrics import Metrics

DEPTH = 50


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default="BTCUSDT,ETHUSDT")
    ap.add_argument("--seconds", type=float, default=60.0)
    ap.add_argument("--interval", type=float, default=0.5, help="seconds between scans")
    ap.add_argument("--assume-taker-fee", type=Decimal, default=None)
    ap.add_argument("--assume-borrow-rate-hourly", type=Decimal, default=None)
    ap.add_argument("--quiet", action="store_true", help="hide the JSON event log; print the summary only")
    args = ap.parse_args()

    load_env_file()
    risk = load_risk(Path("config/risk.yaml"))
    configure_logging(logging.ERROR if args.quiet else logging.INFO)
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    adapters, have_keys = {}, {}
    for ex in EXCHANGES:
        adapters[ex], have_keys[ex] = make_readonly(ex)

    fee = Fees(args.assume_taker_fee, args.assume_taker_fee) if args.assume_taker_fee is not None else None
    rate = args.assume_borrow_rate_hourly
    if (fee is None or rate is None) and not all(have_keys.values()):
        raise SystemExit(
            "Fees and the borrow rate need API keys (put them in .env) or explicit assumptions: pass "
            "--assume-taker-fee and --assume-borrow-rate-hourly. Nothing is guessed silently."
        )
    cache, metrics = BookCache(), Metrics()
    costs = AdapterCosts(adapters, time.time, fee_override=fee, borrow_rate_override=rate)
    params = DetectionParams(risk.max_slippage, risk.min_net_roi, risk.market_liquidity_pct, risk.expected_loan_seconds)
    scanner = Scanner(
        EXCHANGES, symbols, cache=cache, costs=costs, params=params,
        max_book_age_s=risk.max_book_age_s, clock=time.time, metrics=metrics,
    )  # fmt: skip
    feeds = [BookFeed(adapters[ex], symbols, depth=DEPTH, cache=cache, metrics=metrics) for ex in EXCHANGES]

    best: dict[str, Decimal] = {}
    scans = stale = 0
    started = time.time()
    tasks = [asyncio.create_task(f.run()) for f in feeds]
    try:
        while time.time() - started < args.seconds:
            await asyncio.sleep(args.interval)
            result = await scanner.scan_once()
            scans += 1
            stale += bool(result.stale)
            for v in result.verdicts:
                roi = v.net_roi_pct
                if roi is not None:
                    key = f"{v.symbol} buy {v.buy_exchange} / sell {v.sell_exchange}"
                    best[key] = max(best.get(key, roi), roi)
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        assumed = costs.assumed or "none (read from exchanges)"
        print(f"\nscans {scans} (with stale books: {stale}); assumptions: {assumed}")
        for ex in EXCHANGES:
            print(f"{ex}: {metrics.counter(m.BOOK_UPDATES, exchange=ex)} book updates")
        a, b = EXCHANGES
        opened = sum(metrics.counter(m.OPPORTUNITIES_FOUND, direction=d) for d in (f"{a}>{b}", f"{b}>{a}"))
        print(f"opportunities opened: {opened}")
        print(f"min net ROI required: {risk.min_net_roi}%. Best net ROI seen per direction (negative = no edge):")
        for key, roi in sorted(best.items()):
            print(f"  {key}: {roi:.4f}%")


if __name__ == "__main__":
    asyncio.run(main())
