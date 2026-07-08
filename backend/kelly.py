"""Kelly stake sizing with cap.

Spec formula:   stake = (edge × bankroll) / decimal_odds
Capped at MAX_STAKE_PCT of bankroll, floored at $5, rounded to whole dollars.
Edge is capped at EDGE_CAP before sizing — edges above this are model noise.
"""
from __future__ import annotations

MIN_STAKE = 5.0
EDGE_CAP = 0.15  # cap edge at 15% for Kelly sizing


def kelly_stake(
    edge: float,
    decimal_odds: float,
    bankroll: float,
    max_stake_pct: float = 0.02,
    min_stake: float = MIN_STAKE,
) -> float:
    if edge <= 0 or decimal_odds <= 1.0 or bankroll <= 0:
        return 0.0
    capped_edge = min(edge, EDGE_CAP)
    raw = (capped_edge * bankroll) / decimal_odds
    capped = min(raw, bankroll * max_stake_pct)
    if capped < min_stake:
        return 0.0
    return float(round(capped))
