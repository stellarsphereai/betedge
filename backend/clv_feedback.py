"""CLV feedback loop (self-cal Piece 5).

State machine:
  - Default digest_send_hour = 8 (8am)
  - Nightly at 23:50 we compute the rolling 10-bet CLV average across
    all settled bets ordered by id desc.
  - Rolling CLV < -0.05  →  flip digest_send_hour to 6 (early-bet timing)
                            timing_changed_at = now
                            consecutive_negative_at_6am stays 0 until next eval
  - Rolling CLV > +0.10  →  flip back to 8 (current timing is working)
  - When already at hour=6 and rolling CLV is still <0:
      consecutive_negative_at_6am += 1
      If counter reaches 14 (2 weeks): send "edges may not exist" alert email
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from database import db

log = logging.getLogger("arb.clv_feedback")

ROLLING_WINDOW = 10
LOWER_BOUND = -0.05
UPPER_BOUND = 0.10
EDGE_PROBLEM_DAYS = 14


def _state() -> dict:
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM clv_feedback_state WHERE id = 1"
        ).fetchone()
    return dict(row) if row else {"id": 1, "digest_send_hour": 8, "consecutive_negative_at_6am": 0}


def rolling_clv_avg() -> tuple[float | None, int]:
    with db() as conn:
        rows = conn.execute(
            """
            SELECT clv FROM bets_placed
            WHERE status IN ('won','lost') AND clv IS NOT NULL
            ORDER BY id DESC LIMIT ?
            """,
            (ROLLING_WINDOW,),
        ).fetchall()
    n = len(rows)
    if n == 0:
        return None, 0
    avg = sum(r["clv"] for r in rows) / n
    return round(avg, 4), n


def evaluate_and_update() -> dict:
    """Nightly entry point — pulls rolling CLV, updates the state row,
    and (when warranted) escalates to the edge-problem alert."""
    avg, n = rolling_clv_avg()
    state = _state()
    cur_hour = int(state.get("digest_send_hour") or 8)
    consecutive = int(state.get("consecutive_negative_at_6am") or 0)

    new_hour = cur_hour
    flip = False
    edge_alert = False

    if avg is not None:
        if cur_hour == 8 and avg < LOWER_BOUND:
            new_hour = 6
            flip = True
            consecutive = 0
        elif cur_hour == 6 and avg > UPPER_BOUND:
            new_hour = 8
            flip = True
            consecutive = 0
        elif cur_hour == 6 and avg < 0:
            consecutive += 1
            if consecutive >= EDGE_PROBLEM_DAYS:
                edge_alert = True

    with db() as conn:
        conn.execute(
            """
            UPDATE clv_feedback_state
               SET rolling_clv_avg = ?,
                   sample_size = ?,
                   digest_send_hour = ?,
                   timing_changed_at = COALESCE(CASE WHEN ? THEN datetime('now') END, timing_changed_at),
                   consecutive_negative_at_6am = ?,
                   last_updated = datetime('now')
             WHERE id = 1
            """,
            (avg, n, new_hour, flip, consecutive),
        )

    if edge_alert:
        try:
            import digest
            digest.send(
                "BetEdge — CLV feedback: edges may not exist",
                f"Rolling 10-bet CLV has been negative for {consecutive} consecutive evaluations\n"
                f"despite digest already firing at 06:00. Earlier betting isn't recovering edge\n"
                f"capture — this looks like a model-edge problem, not a timing problem."
            )
        except Exception:
            log.exception("clv_feedback edge-problem email failed")

    return {
        "rolling_clv": avg, "sample_size": n,
        "digest_send_hour": new_hour, "flipped": flip,
        "consecutive_negative_at_6am": consecutive,
        "edge_alert_sent": edge_alert,
    }


def current_send_hour() -> int:
    return int(_state().get("digest_send_hour") or 8)
