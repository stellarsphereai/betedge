"""Per-market calibration loop (self-cal Pieces 1 + 2 + 3, plus Fix 3).

Nightly at 00:30:
  - Pull all settled paper bets, group by (market, outcome[, line])
  - For each bucket: model_avg_pct = mean(model_prob), actual_rate =
    wins / settled
  - calibration_factor = actual_rate / model_avg_pct
  - applied=1 when sample_size >= MIN_SAMPLE_GOAL_MARKET (10) AND factor
    is inside [0.70, 1.30]; outside that band flags a data problem, not
    a calibration need.

At /ev-bets time:
  - Load active factors once per call
  - Multiply per-bet model_prob by the factor for that (market, outcome,
    line) tuple before computing edge
  - Log every application to calibration_applications

Goal-market trigger lowered from the original 15-sample spec to 10
samples (Fix 3) to start applying calibration on goal markets sooner.
H2H still uses 20 since wider sample of true-positive home-favorite
calls is needed before nudging.
"""
from __future__ import annotations

import logging
from typing import Optional

from database import db

log = logging.getLogger("arb.market_calibration")

MIN_SAMPLE_GOAL_MARKET = 10  # Fix 3 — was 15 in spec
MIN_SAMPLE_H2H = 20
FACTOR_MIN = 0.70
FACTOR_MAX = 1.30


def _cal_key(market: str, outcome: str, market_line: Optional[float]) -> str:
    """Stable composite key — synced with the ev-bets reader."""
    if market_line is None:
        return f"{market}:{outcome}"
    return f"{market}:{market_line}:{outcome}"


def _min_sample(market: str) -> int:
    return MIN_SAMPLE_GOAL_MARKET if market in ("btts", "totals") else MIN_SAMPLE_H2H


def refresh_factors() -> dict:
    """Recompute every (market, outcome[, line]) bucket from settled
    paper bets. Idempotent. Returns a roll-up summary for logging."""
    summary = {"buckets": 0, "applied": 0, "deferred_sample": 0, "deferred_bounds": 0}
    with db() as conn:
        rows = conn.execute(
            """
            SELECT
              b.market                                 AS market,
              b.bet_type                               AS outcome,
              b.market_line                            AS market_line,
              p.home_win_pct                           AS home_p,
              p.draw_pct                               AS draw_p,
              p.away_win_pct                           AS away_p,
              p.btts_yes_pct                           AS btts_yes,
              p.score_matrix_json                      AS matrix_json,
              b.status                                 AS status
            FROM bets_placed b
            LEFT JOIN model_predictions p ON p.match_id = b.match_id
            WHERE b.is_paper = 1
              AND b.status IN ('won','lost')
              AND b.market IS NOT NULL
            """
        ).fetchall()

        # Group by (market, outcome, line) and compute model_avg + actual_rate
        from collections import defaultdict
        buckets: dict[tuple, list[tuple[float, bool]]] = defaultdict(list)
        for r in rows:
            mp = _model_prob_for_bet(r)
            if mp is None:
                continue
            won = r["status"] == "won"
            buckets[(r["market"], r["outcome"], r["market_line"])].append((mp, won))

        for (market, outcome, line), data in buckets.items():
            n = len(data)
            if n == 0:
                continue
            summary["buckets"] += 1
            model_avg = sum(d[0] for d in data) / n
            actual_rate = sum(1 for d in data if d[1]) / n
            factor = actual_rate / model_avg if model_avg > 0 else None
            if factor is None:
                continue
            min_n = _min_sample(market)
            sample_ok = n >= min_n
            bounds_ok = FACTOR_MIN <= factor <= FACTOR_MAX
            applied = sample_ok and bounds_ok
            if not sample_ok:
                summary["deferred_sample"] += 1
            elif not bounds_ok:
                summary["deferred_bounds"] += 1
            else:
                summary["applied"] += 1

            conn.execute(
                """
                INSERT INTO market_calibration_factors
                  (cal_key, market, outcome, market_line,
                   model_avg_pct, actual_rate, calibration_factor,
                   sample_size, applied, last_updated)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                ON CONFLICT(cal_key) DO UPDATE SET
                  model_avg_pct      = excluded.model_avg_pct,
                  actual_rate        = excluded.actual_rate,
                  calibration_factor = excluded.calibration_factor,
                  sample_size        = excluded.sample_size,
                  applied            = excluded.applied,
                  last_updated       = datetime('now')
                """,
                (
                    _cal_key(market, outcome, line),
                    market, outcome, line,
                    round(model_avg, 4), round(actual_rate, 4),
                    round(factor, 4), n, int(applied),
                ),
            )

            # Log a bias_log entry so morning digest's bias section can
            # surface the calibration drift.
            if market in ("btts", "totals"):
                _log_bias_entry(conn, market, outcome, line, model_avg, actual_rate, factor, applied, n)

    log.info("calibration: refresh — %s", summary)
    return summary


def _model_prob_for_bet(row) -> Optional[float]:
    """Pull the model's per-bet probability from the joined prediction."""
    market = row["market"] or "h2h"
    outcome = (row["outcome"] or "").lower()
    if market == "h2h":
        return {
            "home": row["home_p"], "draw": row["draw_p"], "away": row["away_p"],
        }.get(outcome)
    if market == "btts":
        yes = row["btts_yes"]
        if yes is None:
            return None
        return yes if outcome == "yes" else 1.0 - yes
    if market == "totals":
        if not row["matrix_json"] or row["market_line"] is None:
            return None
        try:
            import json as _json
            matrix = _json.loads(row["matrix_json"])
        except Exception:
            return None
        line = float(row["market_line"])
        over = sum(
            matrix[h][a]
            for h in range(len(matrix))
            for a in range(len(matrix[0]))
            if (h + a) > line
        )
        return over if outcome == "over" else 1.0 - over
    return None


def _log_bias_entry(conn, market, outcome, line, model_avg, actual_rate, factor, applied, n):
    """Spec Fix 3 — write a GOAL_MARKET_CALIBRATION bias_log row when the
    model is consistently off on goal markets. Only fires when |gap| > 10pp."""
    gap = actual_rate - model_avg
    if abs(gap) <= 0.10:
        return
    label = f"{market}:{outcome}" + (f":{line}" if line is not None else "")
    desc = (
        f"GOAL_MARKET_CALIBRATION {label} — model avg {model_avg*100:.1f}% vs "
        f"actual {actual_rate*100:.1f}% (gap {gap*100:+.1f}pp, n={n}); "
        f"factor {factor:.3f} {'applied' if applied else 'pending'}"
    )
    try:
        conn.execute(
            """
            INSERT INTO bias_log
              (check_name, league_key, sample_size, expected_rate, actual_rate,
               deviation, flagged, severity, suggested_adjustment, description)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "GOAL_MARKET_CALIBRATION", "all", n,
                round(model_avg, 4), round(actual_rate, 4),
                round(gap, 4), 1,
                "warn" if abs(gap) <= 0.20 else "critical",
                f"factor {factor:.3f} {'applied' if applied else 'pending'}",
                desc[:500],
            ),
        )
    except Exception:
        log.exception("bias_log insert failed for %s", label)


def get_active_factors() -> dict[str, dict]:
    """Read by the EV-bets pipeline. Returns only rows where applied=1."""
    with db() as conn:
        rows = conn.execute(
            """
            SELECT cal_key, market, outcome, market_line,
                   model_avg_pct, actual_rate, calibration_factor, sample_size
            FROM market_calibration_factors
            WHERE applied = 1
            """
        ).fetchall()
    return {r["cal_key"]: dict(r) for r in rows}


def all_factors() -> list[dict]:
    """For the morning digest + admin page — every bucket, applied or not."""
    with db() as conn:
        rows = conn.execute(
            """
            SELECT cal_key, market, outcome, market_line,
                   model_avg_pct, actual_rate, calibration_factor,
                   sample_size, applied, last_updated
            FROM market_calibration_factors
            ORDER BY market, outcome, COALESCE(market_line, 0)
            """
        ).fetchall()
    return [dict(r) for r in rows]


def log_application(*, bet_id, cal_key, raw_prob, factor,
                     calibrated_prob, edge_before, edge_after) -> None:
    """Called by the EV pipeline once per bet whose model_prob was nudged."""
    with db() as conn:
        conn.execute(
            """
            INSERT INTO calibration_applications
              (bet_id, cal_key, raw_model_prob, calibration_factor,
               calibrated_prob, edge_before, edge_after)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (bet_id, cal_key,
             round(raw_prob, 5), round(factor, 4),
             round(calibrated_prob, 5),
             round(edge_before, 5), round(edge_after, 5)),
        )
