"""World Cup calibration — completely separate from the regular-league cron.

The WC has a fundamentally different calibration problem:
  - Once every 4 years; one cycle = ~64 matches
  - National teams play sparsely (8-12 competitive games/year)
  - Knockout dynamics + neutral venues + ET/penalties
  - The standard n>=100 monthly threshold would NEVER trigger

Strategy (combines options 2 + 3 from the design discussion):
  1. PRE-TOURNAMENT: seed `model_params_wc.json` from a one-time grid search
     against UCL-knockout data — that's the closest proxy for WC knockout
     dynamics we have. Powered by calibrate_engine.grid_search_ucl_knockouts.
  2. DURING TOURNAMENT: live accuracy diagnostics only. Snapshot after group
     stage (~48 settled fixtures), and snapshot after final (~64).
  3. POST-TOURNAMENT: review numbers manually. The decision to update the
     persisted WC params for the NEXT cycle (4 years out) is a human one.

There is intentionally NO auto cron schedule for WC. Tournament dates aren't
predictable enough for a fixed monthly slot — the user fires admin endpoints
at the right moments. This module exposes the building blocks; main.py
exposes the endpoints.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import calibrate_engine
import model
from database import db

log = logging.getLogger("arb.wc_calibrate")

WC_LEAGUE = "world_cup"
# WC-specific: a full group stage is ~48 matches. Allow grid search after
# 30 settled predictions (still small but the most data the format ever
# produces in a single cycle).
N_MIN_FOR_GRID_SEARCH = 30


def tournament_phase() -> str:
    """Detect current WC phase from the fixtures table. Used to decide which
    calibration step (if any) is appropriate."""
    with db() as conn:
        total = conn.execute(
            "SELECT COUNT(*) FROM fixtures WHERE league = ?", (WC_LEAGUE,)
        ).fetchone()[0]
        settled = conn.execute(
            "SELECT COUNT(*) FROM fixtures WHERE league = ? AND result IS NOT NULL",
            (WC_LEAGUE,),
        ).fetchone()[0]

    if total == 0:
        return "no_data"
    if settled == 0:
        return "pre_tournament"
    # Group stage of WC 2026 is ~96 fixtures (Swiss-style 36-team format).
    # Below ~70 settled → still in group/swiss. Knockouts begin around 72.
    if settled < 72:
        return "group_stage"
    if settled < total:
        return "knockouts"
    return "concluded"


def settled_count_for_wc() -> int:
    with db() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS n
            FROM model_predictions p
            JOIN fixtures f ON f.match_id = p.match_id
            WHERE p.league = ? AND f.result IS NOT NULL
            """,
            (WC_LEAGUE,),
        ).fetchone()
    return row["n"] or 0


def accuracy_snapshot_for_wc() -> dict:
    """Compute current WC accuracy. Mirrors calibrate.accuracy_snapshot_for_league
    but lives here so WC reporting is fully independent."""
    with db() as conn:
        pred_rows = conn.execute(
            """
            SELECT f.result, p.home_win_pct, p.draw_pct, p.away_win_pct
            FROM model_predictions p
            JOIN fixtures f ON f.match_id = p.match_id
            WHERE p.league = ? AND f.result IS NOT NULL
            """,
            (WC_LEAGUE,),
        ).fetchall()
        clv_row = conn.execute(
            """
            SELECT AVG(b.clv) AS avg_clv, COUNT(b.clv) AS n_clv
            FROM bets_placed b
            JOIN model_predictions p ON p.match_id = b.match_id
            WHERE p.league = ? AND b.clv IS NOT NULL
            """,
            (WC_LEAGUE,),
        ).fetchone()

    n = len(pred_rows)
    if n == 0:
        return {
            "league": WC_LEAGUE, "n_settled": 0,
            "phase": tournament_phase(),
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
        "league": WC_LEAGUE,
        "n_settled": n,
        "phase": tournament_phase(),
        "avg_brier": round(brier_sum / n, 4),
        "win_rate": round(correct / n, 4),
        "n_clv_samples": clv_row["n_clv"] or 0,
        "avg_clv": round(clv_row["avg_clv"], 2) if clv_row["avg_clv"] is not None else None,
    }


def write_wc_snapshot() -> dict:
    snap = accuracy_snapshot_for_wc()
    today = datetime.now(timezone.utc).date().isoformat()
    with db() as conn:
        conn.execute(
            """
            INSERT INTO accuracy_snapshots
              (snapshot_date, league, n_settled, avg_brier, win_rate,
               n_clv_samples, avg_clv, phase)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(snapshot_date, league) DO UPDATE SET
              n_settled = excluded.n_settled,
              avg_brier = excluded.avg_brier,
              win_rate = excluded.win_rate,
              n_clv_samples = excluded.n_clv_samples,
              avg_clv = excluded.avg_clv,
              phase = excluded.phase
            """,
            (today, WC_LEAGUE, snap["n_settled"], snap["avg_brier"], snap["win_rate"],
             snap["n_clv_samples"], snap["avg_clv"], snap["phase"]),
        )
    return {"date": today, "snapshot": snap}


def _previous_phase() -> str | None:
    """The phase recorded by yesterday's (or any earlier) snapshot. Used by
    the nightly cron to detect transitions."""
    with db() as conn:
        row = conn.execute(
            """
            SELECT phase FROM accuracy_snapshots
            WHERE league = ? AND phase IS NOT NULL
            ORDER BY snapshot_date DESC LIMIT 1 OFFSET 1
            """,
            (WC_LEAGUE,),
        ).fetchone()
    return row["phase"] if row else None


def nightly_wc_check() -> dict:
    """Run nightly. Always writes a snapshot. If the phase changed since the
    last recorded snapshot, returns transition metadata so the caller can
    decide whether to email a phase-end report."""
    prev_phase = _previous_phase()
    payload = write_wc_snapshot()
    current_phase = payload["snapshot"]["phase"]
    transitioned = (prev_phase is not None) and (prev_phase != current_phase)
    return {
        "previous_phase": prev_phase,
        "current_phase": current_phase,
        "transitioned": transitioned,
        "snapshot": payload["snapshot"],
        "date": payload["date"],
    }


async def run_proxy_calibration_from_ucl_knockouts(apply: bool = False) -> dict:
    """Pre-tournament: grid search RHO × KO_DAMPING against cached UCL 2023-24
    knockouts and (optionally) persist the best-Brier params to
    model_params_wc.json for sync-time pickup.

    apply=False → grid result returned for review only.
    apply=True  → save best params + provenance to model_params_wc.json.
    """
    grid = await calibrate_engine.grid_search_ucl_knockouts()
    if not grid.get("ok"):
        return {"implemented": True, "stage": "pre_tournament", **grid}

    out = {
        "implemented": True,
        "stage": "pre_tournament",
        "applied": False,
        **grid,
    }
    if apply:
        best = grid["best"]
        params = model.ModelParams(
            rho=best["params"]["rho"],
            ko_draw_damping=best["params"]["ko_draw_damping"],
        )
        source = {
            "corpus": "UCL 2023-24 knockouts (cached)",
            "n_test_fixtures": grid["n_test_fixtures"],
            "n_combinations": grid["n_combinations"],
            "best_brier": best["avg_brier"],
            "baseline_brier": grid["baseline"]["avg_brier"] if grid.get("baseline") else None,
            "improvement_brier": grid["improvement_brier"],
            "calibrated_at": datetime.now(timezone.utc).isoformat(),
        }
        save = calibrate_engine.save_wc_params(params, source)
        out["applied"] = True
        out["saved"] = save
    return out


def run_post_phase_check(phase: str) -> dict:
    """After group stage / quarters / final: snapshot + report whether
    eligibility is met for a within-tournament refit. The post-tournament
    refit can use this same function with phase='concluded'."""
    snap = accuracy_snapshot_for_wc()
    n = snap["n_settled"]
    eligible = n >= N_MIN_FOR_GRID_SEARCH
    grid: dict | None = None
    if eligible:
        # Same stub as run_proxy_calibration_from_ucl_knockouts — eventually
        # this will run grid search against in-tournament settled fixtures.
        grid = {
            "implemented": False,
            "stage": phase,
            "note": "needs ModelParams refactor + WC settled-prediction inputs in model_predictions",
        }
    return {
        "phase": phase,
        "n_settled": n,
        "n_min_for_grid_search": N_MIN_FOR_GRID_SEARCH,
        "eligible": eligible,
        "snapshot": snap,
        "grid_search": grid,
        "ran_at": datetime.now(timezone.utc).isoformat(),
    }


def status() -> dict:
    """Aggregate WC calibration state for the admin panel."""
    return {
        "phase": tournament_phase(),
        "n_settled": settled_count_for_wc(),
        "n_min_for_grid_search": N_MIN_FOR_GRID_SEARCH,
        "wc_params_present": calibrate_engine.has_wc_params(),
        "wc_params_path": str(calibrate_engine.WC_PARAMS_FILE),
        "playbook": {
            "pre_tournament": "POST /admin/wc/calibrate-from-ucl-proxy?apply=false (review) then ?apply=true (persist)",
            "group_stage":    "POST /admin/wc/snapshot — track accuracy as fixtures settle",
            "knockouts":      "POST /admin/wc/snapshot — same",
            "concluded":      "POST /admin/wc/post-phase-check?phase=concluded — final review",
        },
    }
