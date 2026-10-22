"""Config loading and mode resolution. Live mode is gated three ways (CLAUDE.md section 10)."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from oddtrue.config.models import Mode, PairsConfig, RiskConfig, ScaleConfig

ENV_MODE = "ODDTRUE_MODE"
ENV_ALLOW_LIVE = "ODDTRUE_ALLOW_LIVE"


class ConfigError(Exception):
    pass


class _NoFloatLoader(yaml.SafeLoader):
    """Safe YAML loader that refuses float literals: decimals must be written as strings."""


def _reject_float(loader: yaml.SafeLoader, node: yaml.Node) -> Any:
    raise ConfigError(f"float literal at line {node.start_mark.line + 1}: write decimals as quoted strings")


_NoFloatLoader.add_constructor("tag:yaml.org,2002:float", _reject_float)


def _read(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as fh:
        data = yaml.load(fh, Loader=_NoFloatLoader)  # noqa: S506 - SafeLoader subclass
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain a mapping")
    return data


def load_risk(path: Path) -> RiskConfig:
    return RiskConfig.model_validate(_read(path))


def load_scale(path: Path) -> ScaleConfig:
    return ScaleConfig.model_validate(_read(path))


def load_pairs(path: Path) -> PairsConfig:
    return PairsConfig.model_validate(_read(path))


def resolve_mode(risk: RiskConfig, env: Mapping[str, str]) -> Mode:
    """Return the effective mode. Anything unset or unrecognised falls back to paper, never to live.

    live requires: ODDTRUE_ALLOW_LIVE=1, live_enabled in config, and a non-empty allowlist.
    live-min requires a non-empty allowlist (real funds, tiny cap).
    """
    raw = env.get(ENV_MODE) or risk.mode.value
    try:
        requested = Mode(raw)
    except ValueError:
        raise ConfigError(f"unknown mode {raw!r}") from None

    if requested is Mode.LIVE:
        if env.get(ENV_ALLOW_LIVE) != "1":
            raise ConfigError(f"live mode requires {ENV_ALLOW_LIVE}=1")
        if not risk.live_enabled:
            raise ConfigError("live mode requires live_enabled: true in risk.yaml")
    if requested in (Mode.LIVE, Mode.LIVE_MIN) and not risk.live_allowlist:
        raise ConfigError(f"{requested.value} mode requires a non-empty live_allowlist")
    return requested
