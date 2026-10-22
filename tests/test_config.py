from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from oddtrue.config import (
    ConfigError,
    Mode,
    RiskConfig,
    SettlementMethod,
    load_pairs,
    load_risk,
    load_scale,
    resolve_mode,
)

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"


def _risk(**over: object) -> RiskConfig:
    base = load_risk(CONFIG_DIR / "risk.yaml").model_dump()
    base.update(over)
    return RiskConfig.model_validate(base)


def test_shipped_configs_load_with_decimals() -> None:
    risk = load_risk(CONFIG_DIR / "risk.yaml")
    assert risk.client_trade_pct == Decimal("0.95")
    assert risk.client_reserve_pct == Decimal("0.05")
    assert risk.market_liquidity_pct == Decimal("0.90")
    assert risk.mode is Mode.PAPER
    assert risk.settlement_method is SettlementMethod.PREHELD_INVENTORY
    assert load_scale(CONFIG_DIR / "scale.yaml").max_active_clients == 5
    assert not load_pairs(CONFIG_DIR / "pairs.yaml").is_tradable("BTCUSDT")


def test_float_literal_rejected(tmp_path: Path) -> None:
    text = (CONFIG_DIR / "risk.yaml").read_text(encoding="utf-8").replace('"0.95"', "0.95")
    p = tmp_path / "risk.yaml"
    p.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError):
        load_risk(p)


def test_split_must_sum_to_one() -> None:
    with pytest.raises(ValidationError):
        _risk(client_reserve_pct=Decimal("0.10"))


def test_unknown_key_rejected() -> None:
    with pytest.raises(ValidationError):
        _risk(surprise=1)


def test_default_mode_is_paper() -> None:
    assert resolve_mode(_risk(), {}) is Mode.PAPER


def test_live_needs_all_three_gates() -> None:
    allow = ("client-1",)
    live_env = {"ODDTRUE_MODE": "live", "ODDTRUE_ALLOW_LIVE": "1"}
    with pytest.raises(ConfigError):  # no flag, no allowlist
        resolve_mode(_risk(), live_env)
    with pytest.raises(ConfigError):  # flag + allowlist, no env var
        resolve_mode(_risk(live_enabled=True, live_allowlist=allow), {"ODDTRUE_MODE": "live"})
    with pytest.raises(ConfigError):  # env + allowlist, no config flag
        resolve_mode(_risk(live_allowlist=allow), live_env)
    with pytest.raises(ConfigError):  # env + flag, empty allowlist
        resolve_mode(_risk(live_enabled=True), live_env)
    assert resolve_mode(_risk(live_enabled=True, live_allowlist=allow), live_env) is Mode.LIVE


def test_live_min_needs_allowlist() -> None:
    with pytest.raises(ConfigError):
        resolve_mode(_risk(), {"ODDTRUE_MODE": "live-min"})
    assert resolve_mode(_risk(live_allowlist=("c1",)), {"ODDTRUE_MODE": "live-min"}) is Mode.LIVE_MIN


def test_unknown_mode_is_error_not_live() -> None:
    with pytest.raises(ConfigError):
        resolve_mode(_risk(), {"ODDTRUE_MODE": "prod"})


@pytest.mark.parametrize(
    "ladder",
    [
        (),
        (Decimal("1"),),
        (Decimal("-0.1"),),
        (Decimal("0.005"), Decimal("0.002")),
        (Decimal("0.002"), Decimal("0.002")),
    ],
)
def test_recovery_ladder_must_be_escalating_and_bounded(ladder: tuple[Decimal, ...]) -> None:
    with pytest.raises(ValidationError):
        _risk(recovery_slippage_ladder=ladder)


def test_recovery_ladder_and_execution_limits_ship_with_safe_defaults() -> None:
    risk = load_risk(CONFIG_DIR / "risk.yaml")
    assert risk.recovery_slippage_ladder == (Decimal("0.002"), Decimal("0.005"), Decimal("0.01"))
    assert risk.execution_timeout_s > 0 and risk.unknown_resolve_attempts >= 1 and risk.repay_attempts >= 1


def test_non_mapping_config_is_rejected(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("- just\n- a list\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_risk(bad)
