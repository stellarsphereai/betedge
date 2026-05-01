"""Kelly stake sizing with cap.

Spec formula:   stake = (edge × bankroll) / decimal_odds
Capped at MAX_STAKE_PCT of bankroll, floored at $5, rounded to whole dollars.
"""
from __future__ import annotations

MIN_STAKE = 5.0


def kelly_stake(
    edge: float,
    decimal_odds: float,
    bankroll: float,
    max_stake_pct: float = 0.02,
) -> float:
    if edge <= 0 or decimal_odds <= 1.0 or bankroll <= 0:
        return 0.0
    raw = (edge * bankroll) / decimal_odds
    capped = min(raw, bankroll * max_stake_pct)
    if capped < MIN_STAKE:
        return 0.0
    return float(round(capped))
