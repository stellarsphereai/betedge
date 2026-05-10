"""Trend detection + form-breakpoint detection (Additions 1 + 2).

Two helpers consumed by data_sync.py:
  - compute_trends(xg_for, xg_against) — last-3 vs prior-4 deltas →
    attack_trend_adj, defense_trend_adj (capped at ±15%)
  - detect_breakpoint(xg_history) — last-5 vs prev-5 ratio test;
    fires when |ratio - 1.0| > 0.25, signaling the model should weight
    the recent window much more heavily (80/20 blend override).

Trend is signed:
  attack_trend > 0  → improving in attack  → bonus to xg_for
  defense_trend > 0 → conceding more       → penalty to xg_against
  defense_trend < 0 → tightening up        → bonus (lowers xg_against)
"""
from __future__ import annotations

from dataclasses import dataclass

# Threshold above which a trend triggers an adjustment.
TREND_THRESHOLD = 0.30
ATTACK_BONUS = 0.08
DEFENSE_BONUS = 0.10
TREND_CAP = 0.15

# Form-breakpoint trigger — last_5/prev_5 deviation from 1.0
BREAKPOINT_THRESHOLD = 0.25
BREAKPOINT_BLEND = 0.80


@dataclass
class TrendResult:
    attack_trend: float           # raw last-3 minus prior-4 mean (xg_for)
    defense_trend: float          # raw last-3 minus prior-4 mean (xg_against)
    attack_adjustment: float      # multiplicative adj to xg_for arrays
    defense_adjustment: float     # multiplicative adj to xg_against arrays
    applied: bool


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def compute_trends(xg_for: list[float], xg_against: list[float]) -> TrendResult:
    """Compare last-3 to next-4 (games 4-7). Need ≥7 games for a stable
    delta — otherwise return zero adjustments."""
    if len(xg_for) < 7 or len(xg_against) < 7:
        return TrendResult(0.0, 0.0, 0.0, 0.0, False)

    recent_for = _mean(xg_for[:3])
    prior_for  = _mean(xg_for[3:7])
    recent_ag  = _mean(xg_against[:3])
    prior_ag   = _mean(xg_against[3:7])

    attack_trend  = recent_for - prior_for
    defense_trend = recent_ag - prior_ag

    attack_adj = 0.0
    defense_adj = 0.0
    applied = False

    # Attack: positive trend → bonus, negative → penalty.
    if attack_trend > TREND_THRESHOLD:
        attack_adj = ATTACK_BONUS
        applied = True
    elif attack_trend < -TREND_THRESHOLD:
        attack_adj = -ATTACK_BONUS
        applied = True

    # Defense: positive trend (conceding MORE) is bad → penalty (raise xGA);
    # negative trend (conceding LESS) is good → bonus (lower xGA).
    if defense_trend > TREND_THRESHOLD:
        defense_adj = DEFENSE_BONUS
        applied = True
    elif defense_trend < -TREND_THRESHOLD:
        defense_adj = -DEFENSE_BONUS
        applied = True

    # Cap (defensive — the BONUS constants are already inside ±0.10/±0.08
    # so this only fires if someone widens them later).
    attack_adj  = max(-TREND_CAP, min(TREND_CAP, attack_adj))
    defense_adj = max(-TREND_CAP, min(TREND_CAP, defense_adj))

    return TrendResult(
        attack_trend=round(attack_trend, 3),
        defense_trend=round(defense_trend, 3),
        attack_adjustment=attack_adj,
        defense_adjustment=defense_adj,
        applied=applied,
    )


@dataclass
class BreakpointResult:
    detected: bool
    side: str | None           # 'attack' | 'defense'
    ratio: float | None        # last_5_avg / prev_5_avg (defense flipped so >1 still means "deteriorating")
    last_5_avg: float | None
    prev_5_avg: float | None


def detect_breakpoint(xg_history: list[float]) -> BreakpointResult:
    """Compare last-5 mean to prev-5 (games 6-10). Fire when the ratio
    deviates from 1.0 by more than 25%. ≥10 games required."""
    if len(xg_history) < 10:
        return BreakpointResult(False, None, None, None, None)
    last_5 = _mean(xg_history[:5])
    prev_5 = _mean(xg_history[5:10])
    if prev_5 <= 0:
        return BreakpointResult(False, None, None, last_5, prev_5)
    ratio = last_5 / prev_5
    if abs(ratio - 1.0) <= BREAKPOINT_THRESHOLD:
        return BreakpointResult(False, None, round(ratio, 3), round(last_5, 3), round(prev_5, 3))
    return BreakpointResult(
        detected=True, side=None,  # caller sets 'attack' or 'defense'
        ratio=round(ratio, 3),
        last_5_avg=round(last_5, 3),
        prev_5_avg=round(prev_5, 3),
    )


def apply_trend_to_arrays(
    xg_for: list[float],
    xg_against: list[float],
    trend: TrendResult,
) -> tuple[list[float], list[float]]:
    """Multiplicatively adjust the form arrays. The downstream weighted-
    average + Poisson math then naturally produces the trend-adjusted
    home_xg / away_xg."""
    if not trend.applied:
        return xg_for, xg_against
    fa = 1.0 + trend.attack_adjustment
    da = 1.0 + trend.defense_adjustment
    return (
        [v * fa for v in xg_for],
        [v * da for v in xg_against],
    )
