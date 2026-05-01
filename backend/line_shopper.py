"""Pick the best NY-legal book for a given outcome and score the timing.

Timing buckets (line-movement vs opening odds, in 'cents'):
    moved < 5  → GREEN
    5 ≤ moved ≤ 15 → AMBER
    moved > 15 → RED  (skip unless edge > 0.08)

Injury-driven bets are flagged GREEN regardless.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class BestLine:
    best_book: str
    best_odds: float
    second_book: str | None
    second_odds: float | None
    movement_cents: float | None
    timing: str  # GREEN/AMBER/RED


def _decimal_to_us_cents(d: float) -> float:
    """Decimal odds → US-style cents for movement comparison."""
    if d >= 2.0:
        return (d - 1.0) * 100.0
    return -100.0 / (d - 1.0)


def best_line(
    offers: dict[str, float],
    opening_odds: float | None = None,
    injury_driven: bool = False,
    edge: float = 0.0,
) -> BestLine | None:
    """offers: book -> decimal_odds for ONE outcome."""
    if not offers:
        return None

    ranked = sorted(offers.items(), key=lambda kv: kv[1], reverse=True)
    best_book, best_odds = ranked[0]
    second_book, second_odds = (ranked[1] if len(ranked) > 1 else (None, None))

    movement = None
    if opening_odds is not None:
        movement = _decimal_to_us_cents(best_odds) - _decimal_to_us_cents(opening_odds)

    if injury_driven:
        timing = "GREEN"
    elif movement is None:
        timing = "GREEN"  # no opening line known yet → don't penalise
    else:
        m = abs(movement)
        if m < 5:
            timing = "GREEN"
        elif m <= 15:
            timing = "AMBER"
        else:
            timing = "RED" if edge <= 0.08 else "GREEN"

    return BestLine(
        best_book=best_book,
        best_odds=best_odds,
        second_book=second_book,
        second_odds=second_odds,
        movement_cents=round(movement, 2) if movement is not None else None,
        timing=timing,
    )
