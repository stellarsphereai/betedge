"""Console diagnostic: full edge breakdown per book × market × outcome.

Shows every NY-legal book's offered odds and edge for every outcome, including
negative edges that don't appear on the dashboard. Useful for sanity-checking
the EV pipeline and for spotting line-shopping opportunities.

Usage:
    python3 print_all_edges.py                    # default: epl
    python3 print_all_edges.py --league ucl
    python3 print_all_edges.py --league uel
"""
from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

import httpx

import odds_client
import team_aliases
from database import db

LEAGUE_TO_SPORT_KEY = {
    "epl": "soccer_epl",
    "ucl": "soccer_uefa_champs_league",
    "uel": "soccer_uefa_europa_league",
    "world_cup": "soccer_fifa_world_cup",
}


def _print_market(label: str, per_book: dict[str, dict[str, float]], model_probs: dict[str, float],
                  outcomes: tuple[str, ...], display: dict[str, str]) -> None:
    if not per_book:
        return
    print(f"\n  {label}")
    print(f"    {'book':<14} {'outcome':<22} {'odds':>6} {'market%':>8} {'model%':>8} {'edge':>8}")
    print(f"    {'-' * 14} {'-' * 22} {'-' * 6} {'-' * 8} {'-' * 8} {'-' * 8}")

    rows: list[tuple] = []
    for book in sorted(per_book.keys()):
        odds_dict = per_book[book]
        # De-vig within this book's outcomes for this market
        implied = {k: 1.0 / v for k, v in odds_dict.items() if k in outcomes and v and v > 1.0}
        total = sum(implied.values())
        if total <= 0:
            continue
        for outcome in outcomes:
            if outcome not in odds_dict:
                continue
            o = odds_dict[outcome]
            mkt = implied[outcome] / total
            model = model_probs.get(outcome, 0.0)
            edge = model - mkt
            rows.append((book, display[outcome], o, mkt, model, edge))

    # Sort by outcome then by edge desc within outcome (best book first)
    outcome_order = {display[o]: i for i, o in enumerate(outcomes)}
    rows.sort(key=lambda r: (outcome_order.get(r[1], 99), -r[5]))

    for book, label_str, o, mkt, model, edge in rows:
        edge_pct = f"{edge * 100:+.2f}%"
        marker = " ★" if edge >= 0.03 else ""
        print(f"    {book:<14} {label_str:<22} {o:>6.2f} {mkt * 100:>7.1f}% {model * 100:>7.1f}% {edge_pct:>8}{marker}")


async def main(league: str) -> None:
    sport_key = LEAGUE_TO_SPORT_KEY[league]
    with db() as conn:
        preds = conn.execute(
            """
            SELECT match_id, home_team, away_team, kickoff_time,
                   home_win_pct, draw_pct, away_win_pct, btts_yes_pct, score_matrix_json
            FROM model_predictions
            WHERE league = ?
            ORDER BY kickoff_time
            """,
            (league,),
        ).fetchall()

    if not preds:
        print(f"No predictions for league={league}. Run /sync-data/run?league={league} first.")
        return

    async with httpx.AsyncClient() as client:
        raw = await odds_client.fetch_odds(client, sport_key)
        # Merge btts per fixture
        pred_keys = {(team_aliases.normalize_key(p["home_team"]), team_aliases.normalize_key(p["away_team"])) for p in preds}
        for m in raw:
            k = (team_aliases.normalize_key(m.get("home_team")), team_aliases.normalize_key(m.get("away_team")))
            if k not in pred_keys:
                continue
            try:
                btts_books = await odds_client.fetch_event_btts(client, sport_key, m["id"])
                odds_client.merge_btts_into_match(m, btts_books)
            except Exception:
                pass

    by_pair = {(team_aliases.normalize_key(m["home_team"]), team_aliases.normalize_key(m["away_team"])): m for m in raw}
    now = datetime.now(timezone.utc)

    shown = 0
    for p in preds:
        home, away = p["home_team"], p["away_team"]
        match = by_pair.get((team_aliases.normalize_key(home), team_aliases.normalize_key(away)))
        if not match:
            continue

        commence = match.get("commence_time")
        if commence:
            try:
                if datetime.fromisoformat(commence.replace("Z", "+00:00")) <= now:
                    continue
            except ValueError:
                pass

        markets = odds_client.parse_all_markets(match)
        matrix = json.loads(p["score_matrix_json"]) if p["score_matrix_json"] else None

        shown += 1
        print(f"\n{'=' * 88}")
        print(f"{home} vs {away}    {commence}")
        print(f"{'=' * 88}")

        if markets["h2h"]:
            model_probs = {"home": p["home_win_pct"], "draw": p["draw_pct"], "away": p["away_win_pct"]}
            _print_market(
                "1X2 (h2h)", markets["h2h"], model_probs,
                ("home", "draw", "away"),
                {"home": home[:22], "draw": "Draw", "away": away[:22]},
            )

        if markets["btts"] and p["btts_yes_pct"] is not None:
            yes = p["btts_yes_pct"]
            model_probs = {"yes": yes, "no": 1.0 - yes}
            _print_market(
                "BTTS", markets["btts"], model_probs,
                ("yes", "no"), {"yes": "Yes", "no": "No"},
            )

        if markets["totals"] and matrix:
            for line in sorted(markets["totals"].keys()):
                by_book = markets["totals"][line]
                over = sum(
                    matrix[h][a]
                    for h in range(len(matrix))
                    for a in range(len(matrix[0]))
                    if (h + a) > line
                )
                model_probs = {"over": over, "under": 1.0 - over}
                _print_market(
                    f"Totals {line}", by_book, model_probs,
                    ("over", "under"), {"over": f"Over {line}", "under": f"Under {line}"},
                )

    if shown == 0:
        print("No upcoming fixtures with both predictions and odds in the snapshot.")
    else:
        print(f"\n★ = +EV ≥ 3% (would appear on the dashboard)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--league", default="epl", choices=list(LEAGUE_TO_SPORT_KEY))
    args = parser.parse_args()
    asyncio.run(main(args.league))
