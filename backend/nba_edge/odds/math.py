"""Odds conversions. American odds are integers; +100 and -100 both mean even money."""
from __future__ import annotations


def american_to_decimal(american: int | float) -> float:
    a = float(american)
    if a == 0 or -100 < a < 100:
        raise ValueError(f"invalid American odds: {american!r}")
    return 1 + a / 100 if a > 0 else 1 + 100 / abs(a)


def decimal_to_american(decimal: float) -> int:
    d = float(decimal)
    if d <= 1:
        raise ValueError(f"invalid decimal odds: {decimal!r}")
    return round((d - 1) * 100) if d >= 2 else round(-100 / (d - 1))


def implied_probability(american: int | float) -> float:
    """Raw implied probability of a single price (includes the book's margin)."""
    return 1 / american_to_decimal(american)
