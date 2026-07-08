"""Compare model probabilities to vig-removed market implied probabilities.

A bet is +EV when the model's probability exceeds the (de-vigged) implied
probability at a specific bookmaker by at least min_edge.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class BookOffer:
    book: str
    outcome: str   # "home" | "draw" | "away"
    decimal_odds: float


@dataclass
class EVBet:
    match_id: str
    home_team: str
    away_team: str
    outcome: str
    book: str
    decimal_odds: float
    model_prob: float
    true_implied_prob: float  # de-vigged
    edge: float                # model_prob - true_implied
    ev_pct: float              # raw EV at the offered odds
    confidence: str
    market: str = "h2h"
    market_label: str | None = None
    market_line: float | None = None


def _shin_z(implied: list[float], tol: float = 1e-8, max_iter: int = 100) -> float:
    """Solve for Shin's insider-trading parameter z via bisection.

    Given raw implied probabilities (summing to >1 due to vig), find z in
    (0, 1) such that  sum( sqrt(z**2 + 4*(1-z)*p_i/s) - z ) / (2*(1-z)) == 1
    where s = sum(p_i).
    """
    n = len(implied)
    s = sum(implied)
    if n == 0 or s <= 0:
        return 0.0

    def _residual(z: float) -> float:
        total = 0.0
        denom = 2.0 * (1.0 - z)
        for p in implied:
            total += ((z ** 2 + 4.0 * (1.0 - z) * p / s) ** 0.5 - z) / denom
        return total - 1.0

    lo, hi = 1e-12, 1.0 - 1e-12
    for _ in range(max_iter):
        mid = (lo + hi) / 2.0
        if _residual(mid) > 0:
            lo = mid
        else:
            hi = mid
        if hi - lo < tol:
            break
    return (lo + hi) / 2.0


def remove_vig(odds: dict[str, float]) -> dict[str, float]:
    """Shin de-vig: allocates more vig to favorites than longshots.

    Falls back to proportional de-vig for 2-way markets (btts, totals)
    where Shin's model adds little benefit.
    """
    if not odds:
        return {}
    implied = {k: 1.0 / v for k, v in odds.items() if v and v > 1.0}
    s = sum(implied.values())
    if s <= 0:
        return {}
    # For 2-way markets, proportional is fine
    if len(implied) <= 2:
        return {k: v / s for k, v in implied.items()}
    # Shin de-vig for 3-way markets (h2h)
    imp_list = list(implied.values())
    z = _shin_z(imp_list)
    result: dict[str, float] = {}
    denom = 2.0 * (1.0 - z)
    for k, p in implied.items():
        result[k] = ((z ** 2 + 4.0 * (1.0 - z) * p / s) ** 0.5 - z) / denom
    return result


def find_ev_bets(
    match_id: str,
    home_team: str,
    away_team: str,
    model_probs: dict[str, float],   # {"home", "draw", "away"} -> prob
    confidence: str,
    offers_by_book: dict[str, dict[str, float]],  # book -> {"home","draw","away"} -> odds
    min_edge: float,
) -> list[EVBet]:
    """Legacy 3-way h2h scan. Use find_ev_bets_market() for arbitrary markets."""
    return find_ev_bets_market(
        market="h2h", market_label=None, market_line=None,
        match_id=match_id, home_team=home_team, away_team=away_team,
        model_probs=model_probs, confidence=confidence,
        offers_by_book=offers_by_book, min_edge=min_edge,
        outcomes=("home", "draw", "away"),
    )


def find_ev_bets_market(
    *,
    market: str,                            # h2h | btts | totals
    market_label: str | None,                # display label e.g. "Over/Under 2.5"
    market_line: float | None,
    match_id: str,
    home_team: str,
    away_team: str,
    model_probs: dict[str, float],
    confidence: str,
    offers_by_book: dict[str, dict[str, float]],
    min_edge: float,
    outcomes: tuple[str, ...],
) -> list[EVBet]:
    """Generic per-market EV scan: de-vig within each book, emit edges over min_edge."""
    out: list[EVBet] = []
    for book, odds in offers_by_book.items():
        true_implied = remove_vig({k: v for k, v in odds.items() if k in outcomes})
        if not true_implied:
            continue
        for outcome in outcomes:
            offered = odds.get(outcome)
            true_p = true_implied.get(outcome)
            model_p = model_probs.get(outcome)
            if not offered or true_p is None or model_p is None:
                continue
            edge = model_p - true_p
            if edge < min_edge:
                continue
            ev_pct = model_p * offered - 1.0
            out.append(
                EVBet(
                    match_id=match_id,
                    home_team=home_team,
                    away_team=away_team,
                    outcome=outcome,
                    book=book,
                    decimal_odds=offered,
                    model_prob=round(model_p, 4),
                    true_implied_prob=round(true_p, 4),
                    edge=round(edge, 4),
                    ev_pct=round(ev_pct, 4),
                    confidence=confidence,
                    market=market,
                    market_label=market_label,
                    market_line=market_line,
                )
            )
    out.sort(key=lambda b: b.edge, reverse=True)
    return out
