import pytest

from oddtrue.ids import client_order_id


def test_deterministic_and_distinct_per_leg_and_attempt() -> None:
    a = client_order_id("t1", "A_BUY", 1)
    assert a == client_order_id("t1", "A_BUY", 1) and a.startswith("ot-") and len(a) == 27
    assert (
        len(
            {
                a,
                client_order_id("t1", "A_BUY", 2),
                client_order_id("t1", "B_SELL", 1),
                client_order_id("t2", "A_BUY", 1),
            }
        )
        == 4
    )


@pytest.mark.parametrize(
    ("trade", "leg", "attempt"), [("", "A", 1), ("t", "", 1), ("t", "A", 0), ("a|b", "A", 1), ("t", "A|B", 1)]
)
def test_rejects_bad_input(trade: str, leg: str, attempt: int) -> None:
    with pytest.raises(ValueError):
        client_order_id(trade, leg, attempt)
