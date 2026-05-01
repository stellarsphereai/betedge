"""Run the model on three sample EPL fixtures with mocked xG.

Use this to sanity-check the math before Phase B (real Understat scraper)
and Phase C (frontend) come online.

    python3 demo.py
"""
from __future__ import annotations

import json

import ev_calculator
import kelly
import line_shopper
import model

# Mocked Understat-style data — realistic-ish xG values, most recent first.
# Three fixtures showing different shapes: solid favourite, evenly matched,
# and a tired/injured underdog.
SAMPLES = [
    {
        "label": "Manchester City vs Brentford (heavy fav)",
        "home": model.TeamForm(
            name="Manchester City",
            xg_for=[2.7, 2.1, 1.9, 2.5, 1.8],
            xg_against=[0.9, 1.1, 0.8, 0.7, 1.2],
            rest_days=4,
            top_scorer_out=False,
        ),
        "away": model.TeamForm(
            name="Brentford",
            xg_for=[1.1, 0.8, 1.4, 0.9, 1.0],
            xg_against=[1.7, 2.0, 1.5, 1.8, 1.3],
            rest_days=4,
            top_scorer_out=False,
        ),
        "knockout": False,
    },
    {
        "label": "Liverpool vs Chelsea (evenly matched)",
        "home": model.TeamForm(
            name="Liverpool",
            xg_for=[2.0, 1.8, 2.3, 1.7, 1.9],
            xg_against=[1.0, 1.3, 0.9, 1.2, 1.1],
            rest_days=5,
            top_scorer_out=False,
        ),
        "away": model.TeamForm(
            name="Chelsea",
            xg_for=[1.7, 1.9, 1.5, 2.0, 1.6],
            xg_against=[1.1, 1.0, 1.4, 1.0, 1.2],
            rest_days=4,
            top_scorer_out=False,
        ),
        "knockout": False,
    },
    {
        "label": "Leeds United vs Burnley (tired + injured underdog)",
        "home": model.TeamForm(
            name="Leeds United",
            xg_for=[1.4, 1.2, 1.5, 1.0, 1.3],
            xg_against=[1.2, 1.5, 1.4, 1.6, 1.3],
            rest_days=2,  # short rest
            top_scorer_out=True,  # top scorer out
        ),
        "away": model.TeamForm(
            name="Burnley",
            xg_for=[1.0, 0.9, 1.2, 0.8, 1.0],
            xg_against=[1.6, 1.7, 1.5, 1.8, 1.4],
            rest_days=6,
            top_scorer_out=False,
        ),
        "knockout": False,
    },
]


def fmt_pct(p: float) -> str:
    return f"{p * 100:.2f}%"


def main() -> None:
    print("=" * 72)
    print("Dixon-Coles model demo — 3 EPL fixtures, mocked xG")
    print("=" * 72)

    for s in SAMPLES:
        p = model.predict(s["home"], s["away"], knockout=s["knockout"], league_id=39)
        print()
        print(f"▸ {s['label']}")
        print(f"  Expected goals : {p.home_team} {p.home_xg}  ·  {p.away_team} {p.away_xg}")
        print(
            f"  Probabilities  : {fmt_pct(p.home_win_pct)} home / "
            f"{fmt_pct(p.draw_pct)} draw / {fmt_pct(p.away_win_pct)} away"
        )
        print(f"  Confidence     : {p.confidence}")
        s_check = p.home_win_pct + p.draw_pct + p.away_win_pct
        print(f"  Probability sum: {s_check:.4f} (should be ≈ 1.0)")

    print()
    print("=" * 72)
    print("EV calculator demo — synthetic NY-book odds vs the City model above")
    print("=" * 72)
    city = SAMPLES[0]
    pred = model.predict(city["home"], city["away"], league_id=39)
    # Synthetic odds: DraftKings is sharp, Bally Bet leaks edge on the underdog.
    offers = {
        "DraftKings": {"home": 1.45, "draw": 4.80, "away": 7.50},
        "FanDuel":    {"home": 1.46, "draw": 4.70, "away": 7.20},
        "Bally Bet":  {"home": 1.50, "draw": 5.00, "away": 9.00},
    }
    bets = ev_calculator.find_ev_bets(
        match_id="demo-1",
        home_team=pred.home_team,
        away_team=pred.away_team,
        model_probs={"home": pred.home_win_pct, "draw": pred.draw_pct, "away": pred.away_win_pct},
        confidence=pred.confidence,
        offers_by_book=offers,
        min_edge=0.0,
    )
    for b in bets:
        outcome_offers = {bk: o[b.outcome] for bk, o in offers.items() if b.outcome in o}
        shop = line_shopper.best_line(outcome_offers, opening_odds=None, edge=b.edge)
        stake = kelly.kelly_stake(b.edge, b.decimal_odds, bankroll=1000, max_stake_pct=0.02)
        print(
            f"  {b.outcome:<5} @ {b.book:<14} {b.decimal_odds:.2f}  "
            f"model={fmt_pct(b.model_prob):<7} fair={fmt_pct(b.true_implied_prob):<7} "
            f"edge={fmt_pct(b.edge):<7} EV={fmt_pct(b.ev_pct):<7} ¼-Kelly=${stake:.0f}  "
            f"best={shop.best_book if shop else '?'}@{shop.best_odds if shop else '?'} ({shop.timing if shop else '?'})"
        )
    print()


if __name__ == "__main__":
    main()
