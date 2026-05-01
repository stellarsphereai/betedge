"""Model calibration metrics: Brier score + win-rate + 'ready for real money'.

The spec gates real-money mode on win-rate (≥0.65 over 20+ matches), but win
rate at n=20 has ±18% confidence — too noisy to be the primary signal.
We expose both: spec-style win_rate gate AND a CLV-based gate (positive avg
CLV over 50+ bets), and report both in the readiness payload.
"""
from __future__ import annotations

from database import db


def _brier_for_match(predicted: dict[str, float], actual_outcome: str) -> float:
    """Three-class Brier score = sum_i (p_i - actual_i)^2."""
    targets = {"home": 0.0, "draw": 0.0, "away": 0.0}
    if actual_outcome in targets:
        targets[actual_outcome] = 1.0
    return sum((predicted.get(k, 0.0) - targets[k]) ** 2 for k in targets)


def model_accuracy_report() -> dict:
    """Join settled fixtures to their most recent prediction and score them."""
    with db() as conn:
        rows = conn.execute(
            """
            SELECT f.match_id, f.result,
                   p.home_win_pct, p.draw_pct, p.away_win_pct
            FROM fixtures f
            JOIN model_predictions p ON p.match_id = f.match_id
            WHERE f.result IS NOT NULL
            ORDER BY f.kickoff_time DESC
            """
        ).fetchall()

    if not rows:
        return {"n": 0, "ready": False, "reason": "no settled predictions yet"}

    briers: list[float] = []
    correct = 0
    for r in rows:
        probs = {
            "home": r["home_win_pct"] or 0,
            "draw": r["draw_pct"] or 0,
            "away": r["away_win_pct"] or 0,
        }
        briers.append(_brier_for_match(probs, r["result"]))
        winner = max(probs, key=probs.get)
        if winner == r["result"]:
            correct += 1

    n = len(rows)
    win_rate = correct / n
    avg_brier = sum(briers) / n

    # CLV-based gate
    with db() as conn:
        clv_row = conn.execute(
            """
            SELECT AVG(clv) AS avg_clv, COUNT(*) AS n
            FROM bets_placed WHERE clv IS NOT NULL
            """
        ).fetchone()
    avg_clv = clv_row["avg_clv"]
    n_clv = clv_row["n"] or 0

    win_rate_gate = n >= 20 and win_rate >= 0.65
    clv_gate = n_clv >= 50 and (avg_clv or 0) > 0
    ready = win_rate_gate and clv_gate

    if n < 20:
        readiness = "EARLY · need ≥20 settled predictions"
    elif win_rate < 0.55 and n >= 30:
        readiness = "ADJUST WEIGHTS · win-rate < 55% over 30+ predictions"
    elif not clv_gate and n_clv < 50:
        readiness = f"NEED CLV SAMPLE · {n_clv}/50 bets with closing line"
    elif not clv_gate:
        readiness = "NEGATIVE CLV · do not switch to real money"
    elif ready:
        readiness = "READY FOR REAL MONEY"
    else:
        readiness = "PAPER ONLY"

    return {
        "n_predictions": n,
        "win_rate": round(win_rate, 4),
        "avg_brier": round(avg_brier, 4),
        "n_clv_samples": n_clv,
        "avg_clv": round(avg_clv, 2) if avg_clv is not None else None,
        "win_rate_gate": win_rate_gate,
        "clv_gate": clv_gate,
        "ready": ready,
        "readiness": readiness,
    }
