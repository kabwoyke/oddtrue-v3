"""Demo: one margin borrow -> protected sell -> buy back -> repay cycle on the in-memory FakeExchange.

No network, no database, no real funds. Run: python scripts/demo_fake_cycle.py
"""

from __future__ import annotations

import asyncio
from decimal import Decimal as D

from oddtrue.exchanges.fake import FakeVenue
from oddtrue.exchanges.models import AccountType, OrderRequest, TimeInForce
from oddtrue.pricing.types import Level, Side, SymbolRules

SYMBOL = "BTCUSDT"


async def main() -> None:
    venue = FakeVenue("bitget")
    venue.set_rules(SYMBOL, SymbolRules(D("0.00001"), D(100), D("0.00001"), D("0.01"), D(5)))
    venue.set_book(SYMBOL, [(D(99990), D(1))], [(D(100000), D(1))])
    venue.set_borrow("BTC", D("0.0001"), D(10))  # 0.01% per hour
    acc = venue.account("john")
    acc.fund(AccountType.MARGIN, "USDT", D(500))

    await acc.borrow("BTC", D("0.005"), "demo-borrow")
    sell = await acc.place_order(
        OrderRequest("demo-sell", SYMBOL, Side.SELL, D("0.005"), D(99900), TimeInForce.IOC, AccountType.MARGIN)
    )
    print(f"sold {sell.filled_qty} BTC at {sell.avg_price}, fee {sell.fee} {sell.fee_asset}")

    venue.clock.advance(3600)  # one hour passes (fake clock, no real waiting)
    loan = await acc.get_loan("BTC")
    print(f"loan after 1h: principal {loan.principal} + interest {loan.interest}")

    await acc.place_order(
        OrderRequest("demo-buy", SYMBOL, Side.BUY, D("0.00501"), D(100010), TimeInForce.IOC, AccountType.MARGIN)
    )
    result = await acc.repay("BTC", (await acc.get_loan("BTC")).outstanding, "demo-repay")
    print(f"repaid; outstanding loan now {result.remaining.outstanding} (verified from the exchange)")
    book = await acc.get_orderbook(SYMBOL, 1)
    best_ask: Level = book.asks[0]
    print(f"best ask now {best_ask.price} x {best_ask.qty}")


if __name__ == "__main__":
    asyncio.run(main())
