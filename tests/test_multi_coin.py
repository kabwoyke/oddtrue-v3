"""USDT is the one constant quote asset; the traded coin can be anything (CLAUDE.md section 1, spec section 3)."""

from __future__ import annotations

from decimal import Decimal as D

import pytest

from oddtrue.exchanges.fake import FakeVenue
from oddtrue.exchanges.models import AccountType, OrderRequest, OrderStatus, TimeInForce, base_asset
from oddtrue.pricing.types import Side, SymbolRules

MARGIN = AccountType.MARGIN

# symbol -> (best bid, best ask, rules). Very different prices, steps, ticks and notional minimums.
COINS = {
    "ETHUSDT": (D("3500.10"), D("3500.20"), SymbolRules(D("0.0001"), D(1000), D("0.0001"), D("0.01"), D(5))),
    "SOLUSDT": (D("150.55"), D("150.60"), SymbolRules(D("0.01"), D(10000), D("0.01"), D("0.01"), D(5))),
    "DOGEUSDT": (D("0.12340"), D("0.12350"), SymbolRules(D(1), D(10_000_000), D(1), D("0.00001"), D(5))),
    "1000SHIBUSDT": (D("0.02510"), D("0.02520"), SymbolRules(D(1), D(10_000_000), D(1), D("0.00001"), D(5))),
    "USDCUSDT": (D("0.9998"), D("0.9999"), SymbolRules(D(1), D(10_000_000), D(1), D("0.0001"), D(5))),
}


@pytest.mark.parametrize(
    ("symbol", "coin"),
    [("BTCUSDT", "BTC"), ("ETHUSDT", "ETH"), ("1000SHIBUSDT", "1000SHIB"), ("USDCUSDT", "USDC"), ("AUSDT", "A")],
)
def test_base_asset_is_whatever_precedes_usdt(symbol: str, coin: str) -> None:
    assert base_asset(symbol) == coin


@pytest.mark.parametrize("symbol", ["USDT", "BTCUSDC", "BTCEUR", "", "USDTBTC"])
def test_only_usdt_quoted_symbols_are_accepted(symbol: str) -> None:
    with pytest.raises(ValueError):
        base_asset(symbol)


def make_venue() -> FakeVenue:
    v = FakeVenue("bitget")
    for symbol, (bid, ask, rules) in COINS.items():
        v.set_rules(symbol, rules)
        v.set_book(symbol, [(bid, D(100000))], [(ask, D(100000))])
        v.set_borrow(base_asset(symbol), D("0.0001"), D(1_000_000))
    return v


@pytest.mark.parametrize("symbol", list(COINS))
async def test_borrow_sell_buy_back_repay_for_any_coin(symbol: str) -> None:
    coin = base_asset(symbol)
    bid, ask, rules = COINS[symbol]
    v = make_venue()
    acc = v.account("john")
    acc.fund(MARGIN, "USDT", D(1000))
    qty = max(rules.min_qty, D(100) / bid // rules.step * rules.step)  # about 100 USDT of coin, on the step
    assert (await acc.get_borrowable(coin)).qty >= qty

    await acc.borrow(coin, qty, f"b-{coin}")
    sold = await acc.place_order(OrderRequest(f"s-{coin}", symbol, Side.SELL, qty, bid, TimeInForce.IOC, MARGIN))
    assert sold.status is OrderStatus.FILLED and sold.fee_asset == "USDT"
    assert acc.balance(MARGIN, coin) == 0
    v.clock.advance(3600)
    loan = await acc.get_loan(coin)
    assert loan.principal == qty and loan.interest > 0

    buy_qty = ((loan.outstanding / D("0.999")) // rules.step + 1) * rules.step  # cover the coin-denominated fee
    bought = await acc.place_order(OrderRequest(f"r-{coin}", symbol, Side.BUY, buy_qty, ask, TimeInForce.IOC, MARGIN))
    assert bought.fee_asset == coin
    await acc.repay(coin, (await acc.get_loan(coin)).outstanding, f"p-{coin}")
    assert (await acc.get_loan(coin)).outstanding == 0


async def test_several_coins_at_once_keep_separate_loans_and_collateral_counts_all() -> None:
    v = make_venue()
    acc = v.account("john")
    acc.fund(MARGIN, "USDT", D(1000))
    await acc.borrow("ETH", D("0.1"), "b1")
    await acc.borrow("SOL", D("2"), "b2")
    await acc.borrow("DOGE", D(500), "b3")
    loans = {loan.asset: loan.principal for loan in await acc.get_liabilities()}
    assert loans == {"DOGE": D(500), "ETH": D("0.1"), "SOL": D(2)}
    margin = await acc.get_margin_account()
    expected = D("0.1") * D("3500.15") + 2 * D("150.575") + 500 * D("0.12345")
    assert margin.liabilities_usdt == expected * D("1.0001")  # plus the first billed hour at 0.01%
    # repaying one coin leaves the others untouched
    acc.fund(MARGIN, "SOL", D("0.001"))  # covers the billed interest
    await acc.repay("SOL", (await acc.get_loan("SOL")).outstanding, "p2")
    assert [loan.asset for loan in await acc.get_liabilities()] == ["DOGE", "ETH"]


async def test_coin_without_a_usdt_book_cannot_be_borrowed() -> None:
    v = make_venue()
    v.set_borrow("XRP", D("0.0001"), D(1000))  # borrowable on paper, but no XRPUSDT book to price it
    acc = v.account("john")
    acc.fund(MARGIN, "USDT", D(1000))
    with pytest.raises(Exception, match="no price"):
        await acc.get_borrowable("XRP")


async def test_each_coin_obeys_its_own_lot_rules() -> None:
    v = make_venue()
    acc = v.account("john")
    acc.fund(AccountType.SPOT, "USDT", D(10000))
    from oddtrue.exchanges.errors import InvalidQuantity  # local: only this test needs it

    with pytest.raises(InvalidQuantity):  # DOGE step is 1 whole coin
        await acc.place_order(
            OrderRequest("o1", "DOGEUSDT", Side.BUY, D("100.5"), D("0.1235"), TimeInForce.IOC, AccountType.SPOT)
        )
    with pytest.raises(InvalidQuantity):  # ETH tick is 0.01
        await acc.place_order(
            OrderRequest("o2", "ETHUSDT", Side.BUY, D("0.1"), D("3500.205"), TimeInForce.IOC, AccountType.SPOT)
        )
    ok = await acc.place_order(
        OrderRequest("o3", "SOLUSDT", Side.BUY, D("1.25"), D("150.60"), TimeInForce.IOC, AccountType.SPOT)
    )
    assert ok.status is OrderStatus.FILLED
