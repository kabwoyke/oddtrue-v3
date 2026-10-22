"""Pure reconciliation maths: snapshots, expected balances from legs, and tolerance-aware diffs."""

from __future__ import annotations

from decimal import Decimal as D

from oddtrue.ledger.safety import LegFact
from oddtrue.reconciliation import snapshot as snap
from oddtrue.reconciliation.diff import Difference, diff_snapshots, summarize
from oddtrue.reconciliation.expected import apply_legs, total_coin
from oddtrue.reconciliation.snapshot import LIABILITIES, MARGIN, SPOT, Snapshot

TOL_COIN, TOL_USDT = D("0.00000001"), D("0.01")


def leg(
    name: str, qty: str, price: str, fee: str = "0", fee_asset: str | None = None, status: str = "FILLED"
) -> LegFact:
    return LegFact(1, name, status, "BTCUSDT", "binance", "bitget", D(qty), D(price), D(fee), fee_asset, {})


def start() -> Snapshot:
    s = snap.empty(["binance", "bitget"])
    snap.add(s, "binance", SPOT, "USDT", D(1000))
    snap.add(s, "bitget", MARGIN, "USDT", D(500))
    snap.add(s, "bitget", MARGIN, "BTC", D("0.01"))
    return s


def test_add_get_copy_and_zero_balances_are_dropped() -> None:
    s = snap.empty(["a"])
    snap.add(s, "a", SPOT, "BTC", D(2))
    snap.add(s, "a", SPOT, "BTC", D(-2))
    assert snap.get(s, "a", SPOT, "BTC") == 0 and s["a"][SPOT] == {}  # absent and zero are the same thing
    snap.add(s, "new", MARGIN, "ETH", D(1))  # an exchange not listed up front is created on demand
    c = snap.copy(s)
    snap.add(c, "new", MARGIN, "ETH", D(1))
    assert snap.get(s, "new", MARGIN, "ETH") == 1 and snap.get(c, "new", MARGIN, "ETH") == 2
    assert snap.get(s, "nowhere", SPOT, "X") == 0


def test_json_round_trip_is_exact() -> None:
    s = start()
    snap.add(s, "bitget", LIABILITIES, "BTC", D("0.00000001"))
    assert snap.from_json(snap.to_json(s)) == s and snap.to_json(s)["bitget"][MARGIN]["BTC"] == "0.01"


def test_a_successful_trade_moves_exactly_the_booked_amounts() -> None:
    legs = [
        leg("A_BUY", "0.004", "100000", "0.000004", "BTC"),  # fee is taken from the coin received
        leg("B_BORROW", "0.004", "0"),
        leg("B_SELL", "0.004", "100900", "0.4036", "USDT"),
        leg("B_REPAY", "0.0040004", "0"),  # principal + one billed hour of interest
    ]
    out = apply_legs(start(), legs)
    assert snap.get(out, "binance", SPOT, "USDT") == D(600)
    assert snap.get(out, "binance", SPOT, "BTC") == D("0.003996")
    assert snap.get(out, "bitget", MARGIN, "USDT") == D(500) + D("403.6") - D("0.4036")
    assert snap.get(out, "bitget", MARGIN, "BTC") == D("0.01") - D("0.0000004") - D("0.004") + D("0.004") - D("0.004")
    assert snap.get(out, "bitget", LIABILITIES, "BTC") == 0  # never rebuilt from legs
    assert snap.get(start(), "binance", SPOT, "USDT") == 1000  # the baseline itself is untouched


def test_recovery_legs_are_applied_in_the_right_place() -> None:
    out = apply_legs(
        start(),
        [
            leg("A_UNWIND", "0.00399", "99990", "0.3989601", "USDT"),
            leg("B_COVER", "0.00401", "100910", "0.00000401", "BTC"),
            leg("B_REPAY_BUY", "0.00005", "100910", "0.00000005", "BTC"),
        ],
    )
    assert snap.get(out, "binance", SPOT, "BTC") == -D("0.00399")
    assert snap.get(out, "binance", SPOT, "USDT") == D(1000) + D("0.00399") * D(99990) - D("0.3989601")
    spent = D("0.00401") * D(100910) + D("0.00005") * D(100910)
    assert snap.get(out, "bitget", MARGIN, "USDT") == D(500) - spent
    assert snap.get(out, "bitget", MARGIN, "BTC") == D("0.01") + D("0.00401") - D("0.00000401") + D("0.00005") - D(
        "0.00000005"
    )


def test_legs_that_did_nothing_change_nothing() -> None:
    legs = [
        leg("A_BUY", "0", "0", status="REJECTED"),
        leg("B_SELL", "0", "0", status="EXPIRED"),
        leg("A_UNWIND", "0", "0", status="EXPIRED"),
        leg("B_COVER", "0", "0", status="EXPIRED"),
        leg("B_BORROW", "0.004", "0", status="REJECTED"),
        leg("B_REPAY", "0.004", "0", status="REJECTED"),
        leg("SOMETHING_ELSE", "1", "1"),
    ]
    assert apply_legs(start(), legs) == start()


def test_total_coin_adds_both_exchanges_and_both_accounts() -> None:
    s = start()
    snap.add(s, "binance", SPOT, "BTC", D("0.5"))
    snap.add(s, "bitget", SPOT, "BTC", D("0.25"))
    assert total_coin(s, "BTC") == D("0.76") and total_coin(s, "ETH") == 0


def test_diff_ignores_rounding_but_reports_real_differences() -> None:
    expected, actual = start(), start()
    snap.add(actual, "binance", SPOT, "USDT", D("0.005"))  # inside the USDT tolerance
    snap.add(actual, "bitget", MARGIN, "BTC", D("0.000000005"))  # inside the coin tolerance
    assert diff_snapshots(expected, actual, TOL_COIN, TOL_USDT) == []
    snap.add(actual, "binance", SPOT, "USDT", D("0.02"))  # now over
    snap.add(actual, "bitget", MARGIN, "BTC", D("0.00000002"))
    snap.add(actual, "bitget", SPOT, "DOGE", D(5))  # an asset the ledger knows nothing about
    found = diff_snapshots(expected, actual, TOL_COIN, TOL_USDT)
    assert [(d.exchange, d.section, d.asset) for d in found] == [
        ("binance", SPOT, "USDT"), ("bitget", SPOT, "DOGE"), ("bitget", MARGIN, "BTC"),
    ]  # fmt: skip
    assert found[1].delta == 5 and found[1].expected == 0  # delta > 0: the exchange holds MORE than the ledger says


def test_diff_covers_exchanges_present_on_only_one_side() -> None:
    actual = snap.empty(["binance"])
    expected = snap.empty(["binance", "bitget"])
    snap.add(expected, "bitget", LIABILITIES, "BTC", D(1))
    found = diff_snapshots(expected, actual, TOL_COIN, TOL_USDT)
    assert found == [Difference("bitget", LIABILITIES, "BTC", D(1), D(0))] and found[0].delta == -1


def test_difference_json_and_summary() -> None:
    d = Difference("binance", SPOT, "USDT", D(600), D("599.5"))
    assert d.to_json() == {
        "exchange": "binance", "section": SPOT, "asset": "USDT", "expected": "600", "actual": "599.5", "delta": "-0.5",
    }  # fmt: skip
    assert summarize([d]) == "binance/spot/USDT: expected 600 actual 599.5"
    many = [Difference("x", SPOT, f"A{i}", D(0), D(1)) for i in range(7)]
    assert summarize(many).endswith("(+2 more)")


def test_a_fill_with_no_fee_changes_only_the_traded_amounts() -> None:
    out = apply_legs(start(), [leg("A_BUY", "0.001", "100000")])
    assert snap.get(out, "binance", SPOT, "USDT") == D(900) and snap.get(out, "binance", SPOT, "BTC") == D("0.001")
