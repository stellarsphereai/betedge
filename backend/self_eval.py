"""Nightly self-evaluation pipeline.

Two phases:
  1. Result logging — for every match that finished today, look up its
     prediction in model_predictions and emit a prediction_results row
     (predicted vs actual outcome, Brier, xG error, penalties, gamma, blend).
  2. Bias detection — five checks fired per league once n>=10 results exist:
       1. Home bias            — model's avg home-win prob vs actual home-win rate
       2. Favorite overconfidence — wins-among-predictions-≥70%
       3. Form recency bias   — accuracy split by 3+ win streak vs 3+ loss streak
       4. Edge materialization — paper-trade actual ROI vs expected ROI
       5. xG accuracy         — predicted total goals vs actual total goals

Wired into the existing 23:55 NY closing_and_pnl scheduler job. Runs after
closing-line capture so any settled paper bets are already in bets_placed.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Iterable

import httpx

import api_football
from database import db

log = logging.getLogger("arb.self_eval")

LEAGUE_TO_API_FOOTBALL = {
    "epl": api_football.EPL_LEAGUE_ID,
    "ucl": 2,
    "uel": 3,
    "world_cup": api_football.WORLD_CUP_LEAGUE_ID,
    "la_liga": 140,
}

BIAS_MIN_SAMPLE = 10            # min results-per-league before any bias check fires
HOME_BIAS_THRESHOLD = 0.08      # |expected − actual| > 8pp
FAV_OVERCONF_THRESHOLD = 0.10   # actual < predicted − 10pp at the 70% bucket
FORM_BIAS_THRESHOLD = 0.15      # |acc_winners − acc_losers| > 15pp
EDGE_MAT_THRESHOLD = 0.10       # actual_roi < expected_roi − 10pp
XG_HOT_RATIO = 1.30
XG_COLD_RATIO = 0.70
OU_BIAS_THRESHOLD = 0.15        # |mean_model_over − actual_over_rate| > 15pp

# Backtest reference for the dashboard's status check (set by the EPL 2023-24
# rounds 37-38 backtest after the model fixes landed). Refreshed when the
# league-average xG normalisation fix landed: winner accuracy held at 75%,
# Brier ticked from 0.4024 → 0.4152, and the 70%+ confidence bucket moved
# from 0.799/0.667 (over-confident) to 0.788/0.800 (well-calibrated).
BACKTEST_BASELINE = {
    "winner_accuracy": 0.7500,
    "avg_brier": 0.4152,
}


# ---- Phase 1: result logging --------------------------------------------


def _outcome_from_score(home_goals: int | None, away_goals: int | None) -> str | None:
    if home_goals is None or away_goals is None:
        return None
    if home_goals > away_goals:
        return "home"
    if away_goals > home_goals:
        return "away"
    return "draw"


def _brier(probs: dict[str, float], actual: str) -> float:
    targets = {"home": 0.0, "draw": 0.0, "away": 0.0}
    if actual in targets:
        targets[actual] = 1.0
    return sum((probs[k] - targets[k]) ** 2 for k in targets)


def _prior3_for_team(team: str, before_kickoff: str) -> str:
    """W/L/D string for the team's last three settled matches before this
    kickoff (oldest → newest, e.g. 'WLW'). Reads prediction_results which is
    populated by earlier days' runs of this job."""
    with db() as conn:
        rows = conn.execute(
            """
            SELECT actual_outcome, home_team, away_team
            FROM prediction_results
            WHERE (home_team = ? OR away_team = ?)
              AND kickoff_time < ?
              AND actual_outcome IS NOT NULL
            ORDER BY kickoff_time DESC
            LIMIT 3
            """,
            (team, team, before_kickoff),
        ).fetchall()
    out: list[str] = []
    for r in reversed(rows):
        if r["actual_outcome"] == "draw":
            out.append("D")
        elif (r["actual_outcome"] == "home" and r["home_team"] == team) or (
            r["actual_outcome"] == "away" and r["away_team"] == team
        ):
            out.append("W")
        else:
            out.append("L")
    return "".join(out)


async def _fetch_finished_today(client: httpx.AsyncClient, league: str, today: str) -> list[dict]:
    """Pull today's fixtures with status FT (full time) for the league. Used
    rather than reading from `fixtures` because the 00:00 sync may be stale by
    23:55 — we want fresh result data."""
    league_id = LEAGUE_TO_API_FOOTBALL.get(league)
    if league_id is None:
        return []
    season = datetime.utcnow().year if league == "world_cup" else (
        datetime.utcnow().year if datetime.utcnow().month >= 8 else datetime.utcnow().year - 1
    )
    try:
        fixtures = await api_football.fixtures_by_date_range(
            client, league_id, season, today, today, force=True
        )
    except api_football.PlanError as e:
        log.warning("self_eval: plan error fetching %s: %s", league, e)
        return []
    return [
        fx for fx in fixtures
        if (fx.get("fixture", {}).get("status", {}).get("short") or "") in ("FT", "AET", "PEN")
    ]


async def log_results_for_league(league: str, today: str | None = None) -> dict:
    """Walk today's finished fixtures for one league. For each, find the matching
    prediction in model_predictions and write a prediction_results row. Idempotent
    (UNIQUE on match_id; ON CONFLICT DO UPDATE)."""
    today = today or datetime.now(timezone.utc).date().isoformat()
    summary: dict = {"league": league, "date": today, "logged": 0, "missing_prediction": 0, "errors": []}

    async with httpx.AsyncClient() as client:
        finished = await _fetch_finished_today(client, league, today)

    if not finished:
        return summary

    league_id = LEAGUE_TO_API_FOOTBALL.get(league)

    for fx in finished:
        fid = fx["fixture"]["id"]
        match_id = f"af-{fid}"
        # Use fulltime (90 min) score for 3-way outcome evaluation.
        # fx["goals"] includes extra time; fx["score"]["fulltime"] is 90 min only.
        ft = (fx.get("score") or {}).get("fulltime") or {}
        g = {"home": ft.get("home"), "away": ft.get("away")}
        # Fallback to goals if fulltime score not available (older API responses)
        if g["home"] is None or g["away"] is None:
            g = fx.get("goals") or {}
        actual = _outcome_from_score(g.get("home"), g.get("away"))
        if actual is None:
            continue

        with db() as conn:
            pred = conn.execute(
                """
                SELECT match_id, home_team, away_team, kickoff_time,
                       home_win_pct, draw_pct, away_win_pct,
                       home_xg, away_xg,
                       gamma_used, season_blend_used, penalties_json, anomaly_flagged
                FROM model_predictions WHERE match_id = ?
                """,
                (match_id,),
            ).fetchone()
        if not pred:
            summary["missing_prediction"] += 1
            continue

        probs = {
            "home": pred["home_win_pct"] or 0,
            "draw": pred["draw_pct"] or 0,
            "away": pred["away_win_pct"] or 0,
        }
        predicted = max(probs, key=probs.get)
        brier = _brier(probs, actual)
        h_goals = g.get("home") or 0
        a_goals = g.get("away") or 0
        h_err = h_goals - (pred["home_xg"] or 0)
        a_err = a_goals - (pred["away_xg"] or 0)

        h_prior3 = _prior3_for_team(pred["home_team"], pred["kickoff_time"] or "")
        a_prior3 = _prior3_for_team(pred["away_team"], pred["kickoff_time"] or "")

        try:
            with db() as conn:
                conn.execute(
                    """
                    INSERT INTO prediction_results
                      (match_id, league_id, league_key, home_team, away_team, kickoff_time,
                       predicted_outcome, actual_outcome, correct,
                       predicted_home_prob, predicted_draw_prob, predicted_away_prob,
                       actual_home_goals, actual_away_goals,
                       brier_score,
                       home_xg_predicted, away_xg_predicted,
                       home_xg_error, away_xg_error,
                       league_gamma_used, penalties_applied, season_blend_used,
                       anomaly_flagged, home_team_prior3, away_team_prior3)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(match_id) DO UPDATE SET
                      actual_outcome    = excluded.actual_outcome,
                      correct           = excluded.correct,
                      actual_home_goals = excluded.actual_home_goals,
                      actual_away_goals = excluded.actual_away_goals,
                      brier_score       = excluded.brier_score,
                      home_xg_error     = excluded.home_xg_error,
                      away_xg_error     = excluded.away_xg_error,
                      home_team_prior3  = excluded.home_team_prior3,
                      away_team_prior3  = excluded.away_team_prior3
                    """,
                    (match_id, league_id, league,
                     pred["home_team"], pred["away_team"], pred["kickoff_time"],
                     predicted, actual, 1 if predicted == actual else 0,
                     probs["home"], probs["draw"], probs["away"],
                     h_goals, a_goals,
                     round(brier, 4),
                     pred["home_xg"], pred["away_xg"],
                     round(h_err, 3), round(a_err, 3),
                     pred["gamma_used"], pred["penalties_json"], pred["season_blend_used"],
                     pred["anomaly_flagged"], h_prior3, a_prior3),
                )
            summary["logged"] += 1
        except Exception as e:
            summary["errors"].append(f"{match_id}: {e}")
    return summary


# ---- Phase 2: bias checks -------------------------------------------------


def _insert_bias(row: dict) -> None:
    with db() as conn:
        conn.execute(
            """
            INSERT INTO bias_log
              (check_name, league_id, league_key, sample_size,
               expected_rate, actual_rate, deviation,
               flagged, severity, suggested_adjustment, description)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (row["check_name"], row.get("league_id"), row.get("league_key"),
             row.get("sample_size"),
             row.get("expected_rate"), row.get("actual_rate"), row.get("deviation"),
             1 if row.get("flagged") else 0,
             row.get("severity") or "info",
             row.get("suggested_adjustment"),
             row.get("description")),
        )


def _check_home_bias(league: str) -> dict | None:
    with db() as conn:
        rows = conn.execute(
            """
            SELECT predicted_home_prob, actual_outcome
            FROM prediction_results WHERE league_key = ?
            """,
            (league,),
        ).fetchall()
    if len(rows) < BIAS_MIN_SAMPLE:
        return None
    expected = sum(r["predicted_home_prob"] or 0 for r in rows) / len(rows)
    actual = sum(1 for r in rows if r["actual_outcome"] == "home") / len(rows)
    deviation = expected - actual
    flagged = abs(deviation) > HOME_BIAS_THRESHOLD
    suggestion = None
    if flagged:
        with db() as conn:
            cur = conn.execute(
                "SELECT gamma FROM league_config WHERE league_key = ?", (league,)
            ).fetchone()
        cur_gamma = float(cur["gamma"]) if cur else 1.30
        # If the model overestimates home wins, lower γ; if underestimates, raise.
        suggested_gamma = round(cur_gamma - deviation * 0.5, 3)
        suggestion = f"Consider adjusting gamma from {cur_gamma:.2f} to {suggested_gamma:.2f}"
    return {
        "check_name": "home_bias",
        "league_key": league,
        "league_id": LEAGUE_TO_API_FOOTBALL.get(league),
        "sample_size": len(rows),
        "expected_rate": round(expected, 4),
        "actual_rate": round(actual, 4),
        "deviation": round(deviation, 4),
        "flagged": flagged,
        "severity": "warn" if flagged else "info",
        "suggested_adjustment": suggestion,
        "description": (
            f"Home bias: model expects home wins {expected*100:.1f}% vs "
            f"actual {actual*100:.1f}% ({deviation*100:+.1f}pp gap)"
        ),
    }


def _check_favorite_overconfidence(league: str) -> dict | None:
    with db() as conn:
        rows = conn.execute(
            """
            SELECT predicted_outcome, predicted_home_prob, predicted_draw_prob,
                   predicted_away_prob, actual_outcome
            FROM prediction_results WHERE league_key = ?
            """,
            (league,),
        ).fetchall()
    fav_rows = []
    for r in rows:
        probs = {"home": r["predicted_home_prob"], "draw": r["predicted_draw_prob"],
                 "away": r["predicted_away_prob"]}
        max_p = max((probs[r["predicted_outcome"]] or 0), 0.0)
        if max_p > 0.70:
            fav_rows.append((max_p, r["predicted_outcome"] == r["actual_outcome"]))
    if len(fav_rows) < BIAS_MIN_SAMPLE:
        return None
    avg_predicted = sum(r[0] for r in fav_rows) / len(fav_rows)
    actual_hit = sum(1 for r in fav_rows if r[1]) / len(fav_rows)
    deviation = avg_predicted - actual_hit
    flagged = actual_hit < (avg_predicted - FAV_OVERCONF_THRESHOLD)
    return {
        "check_name": "favorite_overconfidence",
        "league_key": league,
        "league_id": LEAGUE_TO_API_FOOTBALL.get(league),
        "sample_size": len(fav_rows),
        "expected_rate": round(avg_predicted, 4),
        "actual_rate": round(actual_hit, 4),
        "deviation": round(deviation, 4),
        "flagged": flagged,
        "severity": "warn" if flagged else "info",
        "suggested_adjustment": (
            "Dampen probability output for >70% favorites — possibly tighten season_blend "
            "(give season mean more weight) or trim attack/defense ratios"
            if flagged else None
        ),
        "description": (
            f"On {len(fav_rows)} >70%-favorites: model expected {avg_predicted*100:.1f}% "
            f"vs actual hit rate {actual_hit*100:.1f}%"
        ),
    }


def _check_form_recency_bias(league: str) -> dict | None:
    with db() as conn:
        rows = conn.execute(
            """
            SELECT correct, home_team_prior3, away_team_prior3
            FROM prediction_results WHERE league_key = ?
              AND home_team_prior3 IS NOT NULL AND away_team_prior3 IS NOT NULL
            """,
            (league,),
        ).fetchall()
    if len(rows) < BIAS_MIN_SAMPLE:
        return None
    winners = [r for r in rows if r["home_team_prior3"] == "WWW" or r["away_team_prior3"] == "WWW"]
    losers = [r for r in rows if r["home_team_prior3"] == "LLL" or r["away_team_prior3"] == "LLL"]
    if len(winners) < 3 or len(losers) < 3:
        return None
    acc_winners = sum(r["correct"] or 0 for r in winners) / len(winners)
    acc_losers = sum(r["correct"] or 0 for r in losers) / len(losers)
    deviation = acc_winners - acc_losers
    flagged = abs(deviation) > FORM_BIAS_THRESHOLD
    return {
        "check_name": "form_recency_bias",
        "league_key": league,
        "league_id": LEAGUE_TO_API_FOOTBALL.get(league),
        "sample_size": len(winners) + len(losers),
        "expected_rate": round(acc_winners, 4),
        "actual_rate": round(acc_losers, 4),
        "deviation": round(deviation, 4),
        "flagged": flagged,
        "severity": "warn" if flagged else "info",
        "suggested_adjustment": (
            "If accuracy on winning streaks is high but losing streaks low, model is "
            "trusting recent form too much — increase season_blend weight on season mean"
            if flagged else None
        ),
        "description": (
            f"Form recency: {acc_winners*100:.0f}% accurate on 3+ win-streak teams "
            f"({len(winners)} matches) vs {acc_losers*100:.0f}% on 3+ loss-streak "
            f"({len(losers)} matches)"
        ),
    }


def _check_edge_materialization(league: str) -> dict | None:
    """Compare paper-trade expected ROI (avg edge) vs realized ROI."""
    with db() as conn:
        rows = conn.execute(
            """
            SELECT b.edge_at_placement, b.profit, b.stake
            FROM bets_placed b
            JOIN model_predictions p ON p.match_id = b.match_id
            WHERE p.league = ?
              AND b.status IN ('won', 'lost')
              AND b.is_paper = 1
              AND b.stake > 0
            """,
            (league,),
        ).fetchall()
    if len(rows) < BIAS_MIN_SAMPLE:
        return None
    expected_roi = sum((r["edge_at_placement"] or 0) for r in rows) / len(rows)
    total_stake = sum((r["stake"] or 0) for r in rows)
    actual_roi = (sum((r["profit"] or 0) for r in rows) / total_stake) if total_stake else 0.0
    deviation = expected_roi - actual_roi
    flagged = actual_roi < (expected_roi - EDGE_MAT_THRESHOLD)
    return {
        "check_name": "edge_materialization",
        "league_key": league,
        "league_id": LEAGUE_TO_API_FOOTBALL.get(league),
        "sample_size": len(rows),
        "expected_rate": round(expected_roi, 4),
        "actual_rate": round(actual_roi, 4),
        "deviation": round(deviation, 4),
        "flagged": flagged,
        "severity": "warn" if flagged else "info",
        "suggested_adjustment": (
            "Edges aren't materializing. Likely model over-confidence — tighten the "
            "anomaly_edge_threshold for this league or raise the recommended min_edge"
            if flagged else None
        ),
        "description": (
            f"Edge materialization on {len(rows)} settled paper bets: "
            f"expected ROI {expected_roi*100:.1f}% vs actual {actual_roi*100:.1f}%"
        ),
    }


def _check_xg_accuracy(league: str) -> dict | None:
    with db() as conn:
        rows = conn.execute(
            """
            SELECT home_xg_predicted, away_xg_predicted,
                   actual_home_goals, actual_away_goals
            FROM prediction_results WHERE league_key = ?
            """,
            (league,),
        ).fetchall()
    if len(rows) < BIAS_MIN_SAMPLE:
        return None
    pred_avg = sum(((r["home_xg_predicted"] or 0) + (r["away_xg_predicted"] or 0)) for r in rows) / len(rows)
    actual_avg = sum(((r["actual_home_goals"] or 0) + (r["actual_away_goals"] or 0)) for r in rows) / len(rows)
    if actual_avg <= 0:
        return None
    ratio = pred_avg / actual_avg
    flagged = ratio > XG_HOT_RATIO or ratio < XG_COLD_RATIO
    severity = "warn" if flagged else "info"
    direction = "hot" if ratio > XG_HOT_RATIO else "cold" if ratio < XG_COLD_RATIO else "ok"
    suggestion = None
    if direction == "hot":
        suggestion = "Predicted goals running hot — consider scaling attack weights down"
    elif direction == "cold":
        suggestion = "Predicted goals running cold — consider scaling attack weights up"
    return {
        "check_name": "xg_accuracy",
        "league_key": league,
        "league_id": LEAGUE_TO_API_FOOTBALL.get(league),
        "sample_size": len(rows),
        "expected_rate": round(pred_avg, 3),
        "actual_rate": round(actual_avg, 3),
        "deviation": round(ratio, 3),
        "flagged": flagged,
        "severity": severity,
        "suggested_adjustment": suggestion,
        "description": (
            f"xG accuracy on {len(rows)} matches: predicted total goals avg "
            f"{pred_avg:.2f} vs actual {actual_avg:.2f} (ratio {ratio:.2f}, {direction})"
        ),
    }


def _check_over_under_calibration(league: str) -> dict | None:
    """SYSTEMATIC_BIAS guard for the totals market.

    For the last N settled Over/Under bets in this league, pull the model's
    score-matrix snapshot from model_predictions and re-evaluate the model's
    Over% at the bet's market_line. Compare the mean against the actual rate
    at which total goals exceeded that line. Flag when the gap exceeds
    OU_BIAS_THRESHOLD (15pp).

    The score_matrix is the durable representation — same matrix the bet was
    priced from — so this catches drift even if model parameters change later.
    """
    import model
    with db() as conn:
        rows = conn.execute(
            """
            SELECT b.match_id, b.market_line,
                   p.score_matrix_json,
                   r.actual_home_goals, r.actual_away_goals
            FROM bets_placed b
            JOIN model_predictions p ON p.match_id = b.match_id
            JOIN prediction_results r ON r.match_id = b.match_id
            WHERE p.league = ?
              AND b.market = 'totals'
              AND b.bet_type IN ('over', 'under')
              AND b.status IN ('won', 'lost')
              AND b.market_line IS NOT NULL
              AND p.score_matrix_json IS NOT NULL
              AND r.actual_home_goals IS NOT NULL
              AND r.actual_away_goals IS NOT NULL
            ORDER BY b.timestamp DESC
            LIMIT ?
            """,
            (league, BIAS_MIN_SAMPLE),
        ).fetchall()
    if len(rows) < BIAS_MIN_SAMPLE:
        return None

    model_overs: list[float] = []
    actual_overs: list[int] = []
    for r in rows:
        try:
            matrix = json.loads(r["score_matrix_json"])
        except (TypeError, ValueError):
            continue
        line = float(r["market_line"])
        model_overs.append(model._over_from_matrix(matrix, line))
        actual_total = (r["actual_home_goals"] or 0) + (r["actual_away_goals"] or 0)
        actual_overs.append(1 if actual_total > line else 0)
    if len(model_overs) < BIAS_MIN_SAMPLE:
        return None

    expected = sum(model_overs) / len(model_overs)
    actual = sum(actual_overs) / len(actual_overs)
    deviation = expected - actual
    flagged = abs(deviation) > OU_BIAS_THRESHOLD
    suggestion = None
    if flagged:
        # Reduce the league's total-goals contribution by ~10pp of the deviation.
        # Actual operator action is documented; the loop is not auto-applied.
        direction = "down" if deviation > 0 else "up"
        suggestion = (
            f"Model Over% runs {deviation*100:+.1f}pp vs actual. "
            f"Scale total-goals contribution {direction} ~10% (e.g. multiplier "
            f"{0.90 if deviation > 0 else 1.10:.2f}x) and re-check after another "
            f"{BIAS_MIN_SAMPLE} settled O/U bets."
        )
    return {
        "check_name": "SYSTEMATIC_BIAS" if flagged else "ou_calibration",
        "league_key": league,
        "league_id": LEAGUE_TO_API_FOOTBALL.get(league),
        "sample_size": len(model_overs),
        "expected_rate": round(expected, 4),
        "actual_rate": round(actual, 4),
        "deviation": round(deviation, 4),
        "flagged": flagged,
        "severity": "critical" if flagged else "info",
        "suggested_adjustment": suggestion,
        "description": (
            f"Over/Under calibration on {len(model_overs)} settled bets: "
            f"model Over% avg {expected*100:.1f}% vs actual Over rate "
            f"{actual*100:.1f}% ({deviation*100:+.1f}pp gap)"
        ),
    }


def run_bias_checks(league: str) -> list[dict]:
    """Run all bias checks for one league. Returns the rows that fired
    (flagged or all-clear); persists each to bias_log."""
    out: list[dict] = []
    for fn in (
        _check_home_bias,
        _check_favorite_overconfidence,
        _check_form_recency_bias,
        _check_edge_materialization,
        _check_xg_accuracy,
        _check_over_under_calibration,
    ):
        try:
            row = fn(league)
        except Exception as e:
            log.exception("bias check %s crashed for %s: %s", fn.__name__, league, e)
            continue
        if row is None:
            continue
        _insert_bias(row)
        out.append(row)
    return out


# ---- Aggregate summary for the dashboard --------------------------------


def health_summary(league: str | None = None) -> dict:
    """Read prediction_results + bias_log and compose the payload the
    /model-health endpoint serves to the Model Health panel."""
    where = "WHERE league_key = ?" if league else ""
    args = (league,) if league else ()

    def _rolling(n: int) -> dict | None:
        with db() as conn:
            rows = conn.execute(
                f"""
                SELECT correct, brier_score FROM prediction_results
                {where}
                ORDER BY kickoff_time DESC LIMIT ?
                """,
                args + (n,),
            ).fetchall()
        if not rows:
            return None
        correct = sum(r["correct"] or 0 for r in rows)
        avg_brier = sum(r["brier_score"] or 0 for r in rows) / len(rows)
        return {
            "n": len(rows),
            "correct": correct,
            "winner_accuracy": round(correct / len(rows), 4),
            "avg_brier": round(avg_brier, 4),
        }

    last_10 = _rolling(10)
    last_20 = _rolling(20)
    last_50 = _rolling(50)

    # Status vs backtest baseline — based on the Brier delta of last 50 (or
    # whatever's available) compared to BACKTEST_BASELINE['avg_brier'].
    status = "no_data"
    color = "neutral"
    delta = None
    if last_50:
        delta = round(last_50["avg_brier"] - BACKTEST_BASELINE["avg_brier"], 4)
        # Brier is "lower is better" — positive delta = regression.
        rel = abs(delta) / BACKTEST_BASELINE["avg_brier"]
        if delta < 0 or rel < 0.05:
            status, color = "on_track", "green"
        elif rel < 0.15:
            status, color = "monitor", "amber"
        else:
            status, color = "review", "red"

    # Active bias alerts — most recent flagged bias row per (check_name, league).
    with db() as conn:
        bias_rows = conn.execute(
            f"""
            SELECT * FROM bias_log
            WHERE flagged = 1
              {('AND league_key = ?' if league else '')}
              AND id IN (
                SELECT MAX(id) FROM bias_log
                WHERE flagged = 1
                  {('AND league_key = ?' if league else '')}
                GROUP BY check_name, league_key
              )
            ORDER BY created_at DESC
            """,
            (league, league) if league else (),
        ).fetchall()
    alerts = [dict(r) for r in bias_rows]

    last_eval_iso = None
    next_eval_iso = None
    with db() as conn:
        last = conn.execute(
            "SELECT MAX(created_at) AS at FROM prediction_results"
        ).fetchone()
        if last and last["at"]:
            last_eval_iso = last["at"]
    # Next 23:55 NY in UTC. Crude — assumes EDT (UTC-4) which is correct
    # year-round in NY for late spring; not worth chasing the DST math here
    # since this is just a hint for the dashboard.
    now = datetime.now(timezone.utc)
    today = now.date().isoformat()
    candidate = datetime.fromisoformat(f"{today}T03:55:00+00:00")  # ~23:55 NY EDT
    if candidate <= now:
        from datetime import timedelta
        candidate = candidate + timedelta(days=1)
    next_eval_iso = candidate.isoformat()

    return {
        "league": league,
        "rolling": {"last_10": last_10, "last_20": last_20, "last_50": last_50},
        "baseline": BACKTEST_BASELINE,
        "delta_brier": delta,
        "status": status,
        "color": color,
        "alerts": alerts,
        "last_eval": last_eval_iso,
        "next_eval": next_eval_iso,
    }


# ---- Top-level entry called by the scheduler -----------------------------


async def run_nightly() -> dict:
    """Result-log every league + run their bias checks. Wrapped per-league so
    one league's failure doesn't kill the others."""
    today = datetime.now(timezone.utc).date().isoformat()
    out: dict[str, dict] = {"date": today, "leagues": {}}
    for league in LEAGUE_TO_API_FOOTBALL:
        league_out: dict = {}
        try:
            league_out["log"] = await log_results_for_league(league, today=today)
        except Exception as e:
            log.exception("self_eval log_results failed for %s", league)
            league_out["log_error"] = str(e)
        try:
            league_out["bias"] = run_bias_checks(league)
        except Exception as e:
            log.exception("self_eval bias checks failed for %s", league)
            league_out["bias_error"] = str(e)
        out["leagues"][league] = league_out
    return out
