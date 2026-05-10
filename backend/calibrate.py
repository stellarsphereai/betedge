"""Calibration — measurement and (eventually) parameter tuning.

Two responsibilities:
  1. Weekly accuracy snapshots per league → trend tracking
  2. Monthly calibration check → if data sufficient, run a grid search over
     model parameters and email the recommendation. Recommend-only — never
     auto-applies (parameter changes that affect betting decisions need a human
     in the loop).

The grid search itself is a stub today: we don't yet store the TeamForm inputs
needed to re-run the model with different parameters. The plumbing is in place
so the eligibility gate fires at the right time; the grid-search engine slots
in once we either (a) have stored inputs or (b) accept post-matrix-only tuning.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from database import db

log = logging.getLogger("arb.calibrate")

CALIBRATION_LEAGUES = ("epl", "ucl", "uel")  # WC handled separately
N_MIN_FOR_GRID_SEARCH = 20  # Self-cal Piece 2 — lowered from 100 so auto-cal
                            # starts on real data sooner. Per-league grid
                            # searches re-trigger every ~10 new settled
                            # results (monthly cron + on-demand admin).


def settled_count_per_league() -> dict[str, int]:
    """How many predictions per league have a corresponding settled fixture?"""
    with db() as conn:
        rows = conn.execute(
            """
            SELECT p.league, COUNT(*) AS n
            FROM model_predictions p
            JOIN fixtures f ON f.match_id = p.match_id
            WHERE f.result IS NOT NULL
              AND p.league IN (?, ?, ?)
            GROUP BY p.league
            """,
            CALIBRATION_LEAGUES,
        ).fetchall()
    out = {league: 0 for league in CALIBRATION_LEAGUES}
    for r in rows:
        out[r["league"]] = r["n"]
    return out


def accuracy_snapshot_for_league(league: str) -> dict:
    """Compute current Brier / win-rate / CLV for one league from settled
    predictions and bets. Returns the snapshot dict; caller decides whether
    to persist it."""
    with db() as conn:
        pred_rows = conn.execute(
            """
            SELECT f.result, p.home_win_pct, p.draw_pct, p.away_win_pct
            FROM model_predictions p
            JOIN fixtures f ON f.match_id = p.match_id
            WHERE p.league = ? AND f.result IS NOT NULL
            """,
            (league,),
        ).fetchall()

        clv_row = conn.execute(
            """
            SELECT AVG(b.clv) AS avg_clv, COUNT(b.clv) AS n_clv
            FROM bets_placed b
            JOIN model_predictions p ON p.match_id = b.match_id
            WHERE p.league = ? AND b.clv IS NOT NULL
            """,
            (league,),
        ).fetchone()

    n = len(pred_rows)
    if n == 0:
        return {
            "league": league, "n_settled": 0,
            "avg_brier": None, "win_rate": None,
            "n_clv_samples": clv_row["n_clv"] or 0,
            "avg_clv": round(clv_row["avg_clv"], 2) if clv_row["avg_clv"] is not None else None,
        }

    correct = 0
    brier_sum = 0.0
    for r in pred_rows:
        probs = {"home": r["home_win_pct"] or 0, "draw": r["draw_pct"] or 0, "away": r["away_win_pct"] or 0}
        actual = r["result"]
        target = {"home": 0, "draw": 0, "away": 0}
        target[actual] = 1
        brier_sum += sum((probs[k] - target[k]) ** 2 for k in target)
        winner = max(probs, key=probs.get)
        if winner == actual:
            correct += 1

    return {
        "league": league,
        "n_settled": n,
        "avg_brier": round(brier_sum / n, 4),
        "win_rate": round(correct / n, 4),
        "n_clv_samples": clv_row["n_clv"] or 0,
        "avg_clv": round(clv_row["avg_clv"], 2) if clv_row["avg_clv"] is not None else None,
    }


def write_weekly_snapshot() -> dict:
    """Compute and persist one accuracy_snapshots row per regular league.
    Idempotent for the same date — UNIQUE(snapshot_date, league) prevents dupes."""
    today = datetime.now(timezone.utc).date().isoformat()
    snapshots: list[dict] = []
    with db() as conn:
        for league in CALIBRATION_LEAGUES:
            snap = accuracy_snapshot_for_league(league)
            conn.execute(
                """
                INSERT INTO accuracy_snapshots
                  (snapshot_date, league, n_settled, avg_brier, win_rate,
                   n_clv_samples, avg_clv)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(snapshot_date, league) DO UPDATE SET
                  n_settled = excluded.n_settled,
                  avg_brier = excluded.avg_brier,
                  win_rate = excluded.win_rate,
                  n_clv_samples = excluded.n_clv_samples,
                  avg_clv = excluded.avg_clv
                """,
                (today, league, snap["n_settled"], snap["avg_brier"], snap["win_rate"],
                 snap["n_clv_samples"], snap["avg_clv"]),
            )
            snapshots.append(snap)
    return {"date": today, "snapshots": snapshots}


def calibration_status() -> dict:
    """Where each league stands on the eligibility gate. Used by the admin
    dashboard and by the monthly cron's report email."""
    counts = settled_count_per_league()
    return {
        "n_min_for_grid_search": N_MIN_FOR_GRID_SEARCH,
        "leagues": [
            {
                "league": league,
                "n_settled": counts[league],
                "eligible_for_grid_search": counts[league] >= N_MIN_FOR_GRID_SEARCH,
                "shortfall": max(0, N_MIN_FOR_GRID_SEARCH - counts[league]),
            }
            for league in CALIBRATION_LEAGUES
        ],
    }


def run_grid_search_for_league(league: str) -> dict:
    """Stub. Real grid search needs stored TeamForm inputs (next phase).

    For now this returns a structured 'not implemented' so the caller can
    wire the rest of the flow (eligibility gate → email) and we can drop
    the engine in later without changing the API."""
    return {
        "league": league,
        "implemented": False,
        "note": "grid search engine arrives in v2 — needs TeamForm input storage in model_predictions",
    }


def run_monthly_calibration_check() -> dict:
    """The cron's main entry point. Per league:
      - count settled predictions
      - if >= N_MIN: invoke grid_search (stubbed today)
      - else: report shortfall
    Returns a summary used to render the email."""
    status = calibration_status()
    results: list[dict] = []
    for entry in status["leagues"]:
        if entry["eligible_for_grid_search"]:
            grid = run_grid_search_for_league(entry["league"])
            results.append({**entry, "grid_search": grid})
        else:
            results.append({**entry, "grid_search": None})
    return {
        "n_min_for_grid_search": status["n_min_for_grid_search"],
        "results": results,
        "ran_at": datetime.now(timezone.utc).isoformat(),
    }
