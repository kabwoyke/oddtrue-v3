"""Adapter factory and .env loader (no network)."""

from __future__ import annotations

from pathlib import Path

import pytest

from oddtrue.exchanges.binance import BinanceReadOnlyAdapter
from oddtrue.exchanges.bitget import BitgetReadOnlyAdapter
from oddtrue.exchanges.factory import load_env_file, make_readonly


def test_env_file_loader_parses_comments_quotes_and_keeps_existing_values(tmp_path: Path) -> None:
    f = tmp_path / ".env"
    f.write_text("# comment\n\nA_KEY=\"quoted\"\nB_KEY='single'\nC_KEY= spaced \nEMPTY=\nNOEQUALS\nKEPT=from_file\n")
    env = {"KEPT": "from_shell"}
    load_env_file(f, env)
    assert env == {"KEPT": "from_shell", "A_KEY": "quoted", "B_KEY": "single", "C_KEY": "spaced", "EMPTY": ""}
    load_env_file(tmp_path / "missing.env", env)  # a missing file is fine


def test_make_readonly_reports_whether_keys_are_complete() -> None:
    a, keys = make_readonly("binance", env={})
    assert isinstance(a, BinanceReadOnlyAdapter) and keys is False
    _, keys = make_readonly("binance", env={"BINANCE_API_KEY": "k" * 10, "BINANCE_API_SECRET": "s" * 10})
    assert keys is True
    b, keys = make_readonly("bitget", env={"BITGET_API_KEY": "k" * 10, "BITGET_API_SECRET": "s" * 10})
    assert isinstance(b, BitgetReadOnlyAdapter) and keys is False  # the passphrase is missing
    _, keys = make_readonly(
        "bitget", env={"BITGET_API_KEY": "k" * 10, "BITGET_API_SECRET": "s" * 10, "BITGET_API_PASSPHRASE": "p" * 10}
    )
    assert keys is True
    with pytest.raises(ValueError, match="unknown exchange"):
        make_readonly("kraken", env={})
