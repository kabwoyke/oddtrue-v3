"""Exchange-boundary rounding. Quantities round DOWN, repay amounts round UP (CLAUDE.md rule 1)."""

from __future__ import annotations

from decimal import Decimal
from fractions import Fraction
from math import gcd, lcm

from oddtrue.pricing.types import ZERO


def floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    """Largest multiple of ``step`` that is <= ``value`` (value >= 0)."""
    if step <= ZERO:
        raise ValueError("step must be positive")
    if value < ZERO:
        raise ValueError("value cannot be negative")
    return (value // step) * step


def ceil_to_step(value: Decimal, step: Decimal) -> Decimal:
    """Smallest multiple of ``step`` that is >= ``value`` (value >= 0)."""
    floored = floor_to_step(value, step)
    return floored if floored == value else floored + step


def is_multiple_of(value: Decimal, step: Decimal) -> bool:
    if step <= ZERO:
        raise ValueError("step must be positive")
    return value % step == ZERO


def common_step(*steps: Decimal) -> Decimal:
    """Smallest positive step that is a multiple of every given step (exact, via rational lcm)."""
    if not steps:
        raise ValueError("at least one step required")
    fractions = [Fraction(s) for s in steps]
    if any(f <= 0 for f in fractions):
        raise ValueError("steps must be positive")
    numerator = lcm(*(f.numerator for f in fractions))
    denominator = gcd(*(f.denominator for f in fractions))
    return Decimal(numerator) / Decimal(denominator)
