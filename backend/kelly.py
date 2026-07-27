"""Kelly stake sizing with cap and edge shrinkage.

Spec formula:   stake = (edge × shrinkage × bankroll) / decimal_odds
Capped at MAX_STAKE_PCT of bankroll, floored at $5, rounded to whole dollars.
Edge is capped at EDGE_CAP before sizing — edges above this are model noise.
Edge is shrunk by EDGE_SHRINKAGE to account for systematic model over-confidence
(the model finds 13-18% avg edges but actual ROI is much lower).
"""
from __future__ import annotations

MIN_STAKE = 5.0
EDGE_CAP = 0.15       # cap edge at 15% for Kelly sizing
EDGE_SHRINKAGE = 0.60  # multiply edge by 0.60 — the model over-estimates true edge


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
    shrunk_edge = capped_edge * EDGE_SHRINKAGE
    raw = (shrunk_edge * bankroll) / decimal_odds
    capped = min(raw, bankroll * max_stake_pct)
    if capped < min_stake:
        return 0.0
    return float(round(capped))
