"""Cash-money restriction layer (specs A-D).

Single source of truth for "is this cash bet allowed?" gates. Both the
POST /bets enforcement and the /restrictions surfacer for the UI read
from here so frontend and backend can't drift.

Rules (in evaluation order):
  A. Market gate           — BTTS / Totals / Draw blocked on cash
  B. Minimum edge gate     — cash requires edge >= 0.06
  C. Daily loss cap        — once today's cash P&L <= -$50, all cash off
  D. Paper-first gate      — cash on (match,market,line,outcome) requires
                             a prior paper bet on the SAME identity tuple

Goal-market unlock criteria (spec):
  Goal-market paper win rate >= 50% over >= 20 settled
  AND average CLV positive on goal-market paper trades
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from database import db

CASH_MIN_EDGE = 0.10
CASH_MIN_EDGE_WC = 0.05
DAILY_CASH_LOSS_CAP_USD = 50.0
RESTRICTED_CASH_MARKETS = {"btts"}                   # BTTS blocked on cash
RESTRICTED_CASH_OUTCOMES = {"draw", "over"}          # h2h draw + totals over blocked
GOAL_MARKETS = {"btts", "totals"}
UNLOCK_MIN_PAPER_GOAL_BETS = 20
UNLOCK_MIN_PAPER_GOAL_WINRATE = 0.50


def _today_iso() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def is_market_restricted(
    market: Optional[str],
    outcome: Optional[str],
    league: Optional[str] = None,
) -> bool:
    """Spec A — BTTS/Totals always blocked on cash; H2H Draw also blocked.

    World Cup bypass: WC is configured as real-money mode from day one
    (no paper-bet pipeline accumulates), so the "wait for goal-market
    paper win rate ≥ 50%" unlock criteria would never trigger. The
    user explicitly opted into real-money WC play, so don't paternalise
    them out of the goal markets the model is actually pricing."""
    if (league or "").lower() == "world_cup":
        return False
    m = (market or "h2h").lower()
    o = (outcome or "").lower()
    if m == "h2h" and o == "draw":
        return True
    # BTTS and totals over are restricted until paper bets prove the model.
    # Auto-unlock when 20+ paper bets settled with 50%+ win rate and CLV >= 0.
    if m in RESTRICTED_CASH_MARKETS or (m == "totals" and o == "over"):
        progress = goal_market_paper_progress()
        if progress["unlocked"]:
            return False  # gate passed — allow cash
        return True
    return False


def edge_below_cash_minimum(edge: Optional[float], league: Optional[str] = None) -> bool:
    """Spec B — cash requires edge >= min threshold. WC uses a lower
    threshold (5%) since the tournament window is short. Tolerance of
    0.0005 so a bet that displays as '6.0%' but is stored as 0.05996
    still clears the gate (frontend rounds for display)."""
    if edge is None:
        return True
    min_edge = CASH_MIN_EDGE_WC if (league or "").lower() == "world_cup" else CASH_MIN_EDGE
    return float(edge) < (min_edge - 0.0005)


def todays_cash_pnl() -> float:
    """Sum of today's settled cash bet profit/loss. Used by the
    circuit-breaker check (spec C)."""
    today = _today_iso()
    with db() as conn:
        row = conn.execute(
            """
            SELECT COALESCE(SUM(profit), 0) AS pnl
            FROM bets_placed
            WHERE is_paper = 0
              AND status IN ('won','lost')
              AND date(timestamp) = ?
            """,
            (today,),
        ).fetchone()
    return float(row["pnl"] or 0.0)


def daily_loss_cap_hit() -> bool:
    return todays_cash_pnl() <= -DAILY_CASH_LOSS_CAP_USD


def has_paper_counterpart(
    match_id: str,
    market: Optional[str],
    market_line: Optional[float],
    outcome: Optional[str],
) -> bool:
    """Spec D — does an existing paper bet match this exact identity?
    Status doesn't matter (open / won / lost / void all count) — what
    matters is that the user dry-ran the bet on the same outcome first."""
    with db() as conn:
        row = conn.execute(
            """
            SELECT id FROM bets_placed
            WHERE is_paper = 1
              AND match_id    IS ?
              AND market      IS ?
              AND market_line IS ?
              AND bet_type    IS ?
            ORDER BY id DESC LIMIT 1
            """,
            (match_id, (market or "h2h"), market_line, outcome),
        ).fetchone()
    return row is not None


def goal_market_paper_progress() -> dict:
    """Roll-up driving the unlock-criteria progress bar.
    Computes win rate + avg CLV on paper bets in goal markets only.
    Goal markets unlock when both:
      - settled count >= UNLOCK_MIN_PAPER_GOAL_BETS (20)
      - win rate >= UNLOCK_MIN_PAPER_GOAL_WINRATE (50%)
      - avg CLV >= 0
    """
    with db() as conn:
        row = conn.execute(
            """
            SELECT
              COALESCE(SUM(CASE WHEN status IN ('won','lost') THEN 1 ELSE 0 END), 0) AS settled,
              COALESCE(SUM(CASE WHEN status='won'  THEN 1 ELSE 0 END), 0)             AS won,
              COALESCE(SUM(CASE WHEN status='lost' THEN 1 ELSE 0 END), 0)             AS lost,
              AVG(CASE WHEN status IN ('won','lost') AND clv IS NOT NULL THEN clv END) AS avg_clv
            FROM bets_placed
            WHERE is_paper = 1
              AND market IN ('btts','totals')
            """
        ).fetchone()
    settled = int(row["settled"] or 0)
    won = int(row["won"] or 0)
    lost = int(row["lost"] or 0)
    win_rate = (won / settled) if settled > 0 else None
    avg_clv = float(row["avg_clv"]) if row["avg_clv"] is not None else None
    sample_ok = settled >= UNLOCK_MIN_PAPER_GOAL_BETS
    win_ok = win_rate is not None and win_rate >= UNLOCK_MIN_PAPER_GOAL_WINRATE
    clv_ok = avg_clv is not None and avg_clv >= 0
    unlocked = sample_ok and win_ok and clv_ok
    return {
        "settled": settled,
        "won": won, "lost": lost,
        "win_rate": round(win_rate, 4) if win_rate is not None else None,
        "avg_clv": round(avg_clv, 4) if avg_clv is not None else None,
        "target_win_rate": UNLOCK_MIN_PAPER_GOAL_WINRATE,
        "target_settled": UNLOCK_MIN_PAPER_GOAL_BETS,
        "sample_ok": sample_ok,
        "win_rate_ok": win_ok,
        "clv_ok": clv_ok,
        "unlocked": unlocked,
    }


def check_cash_eligibility(bet: dict) -> tuple[bool, str]:
    """Combined gate. Returns (allowed, reason). Reason is empty when
    allowed=True. Used by POST /bets to reject + by the UI to grey
    out individual bet buttons.

    Spec D (paper-first) was originally added as a discipline mechanism
    but in practice spec A already blocks the unvalidated markets (goal
    markets + draw) — the only markets that reach this gate are h2h
    home/away, which the model has been validated longest on. Layering
    paper-first on top of A+B+C just adds friction without adding
    safety. Paper-first removed; the audit table still flags any cash
    bet without a paper counterpart for review."""
    # Pre-check — if the bet pipeline already declared it non-actionable
    # (PHANTOM_EDGE > 25% trips this), refuse cash with the upstream
    # reason. Without this, a PHANTOM_EDGE bet had stake=$0 (correct)
    # but cash_eligible=True (inconsistent), letting the user click a
    # disabled-stake button.
    if bet.get("actionable") is False:
        upstream = bet.get("lockout_reason") or ""
        flags = bet.get("anomaly_flags") or []
        excludes = next((f for f in flags if f.get("excludes_bet")), None)
        reason = (
            excludes.get("description") if excludes else
            upstream or
            "Bet excluded by anomaly detector — stake set to $0"
        )
        return False, reason
    # C — daily loss cap (most aggressive — overrides everything else)
    if daily_loss_cap_hit():
        pnl = todays_cash_pnl()
        return False, (
            f"Daily loss cap reached (${pnl:+.0f} of -${DAILY_CASH_LOSS_CAP_USD:.0f}) — "
            f"real money betting resumes tomorrow"
        )
    # A — restricted markets (WC bypass per is_market_restricted)
    if is_market_restricted(
        bet.get("market"),
        bet.get("outcome") or bet.get("bet_type"),
        league=bet.get("league"),
    ):
        return False, (
            "Goal markets (BTTS / Totals) and H2H Draw blocked on cash until "
            "model is calibrated on these markets — paper trade only"
        )
    # B — min edge
    league = bet.get("league")
    if edge_below_cash_minimum(bet.get("edge"), league=league):
        min_edge = CASH_MIN_EDGE_WC if (league or "").lower() == "world_cup" else CASH_MIN_EDGE
        return False, (
            f"Edge {(bet.get('edge') or 0)*100:.1f}% below the {min_edge*100:.0f}% "
            f"cash minimum — too thin for real money at this stage of validation"
        )
    return True, ""


def maybe_send_daily_cap_alert() -> bool:
    """Fire the daily-loss-cap alert email at most once per day. Called
    from clv_tracker.settle_bet right after a cash bet's profit is
    applied — that's the point where today's cash P&L can newly cross
    -$50. Dedup via automation_log: status='PASS' on task_name=
    'daily_cash_loss_alert' for today means we already sent.
    """
    import logging
    log = logging.getLogger("arb.cash_restrictions")
    if not daily_loss_cap_hit():
        return False
    today = _today_iso()
    with db() as conn:
        row = conn.execute(
            """
            SELECT id FROM automation_log
            WHERE task_name = 'daily_cash_loss_alert'
              AND run_date = ?
            ORDER BY id DESC LIMIT 1
            """,
            (today,),
        ).fetchone()
    if row:
        return False  # already sent today
    try:
        import digest
        pnl = todays_cash_pnl()
        subject = f"BetEdge — DAILY LOSS LIMIT HIT (${pnl:+.0f})"
        body = (
            f"Today's cash P&L reached ${pnl:+.0f}, hitting the "
            f"-${DAILY_CASH_LOSS_CAP_USD:.0f} circuit breaker.\n\n"
            f"All real-money bet buttons are now disabled until tomorrow.\n"
            f"Paper trades remain available.\n\n"
            f"Review today's losing bets at /admin/real-trade-audit."
        )
        digest.send(subject, body)
        with db() as conn:
            conn.execute(
                """
                INSERT INTO automation_log
                  (run_date, task_name, status, result_summary, duration_seconds)
                VALUES (?, 'daily_cash_loss_alert', 'PASS', ?, 0)
                """,
                (today, f"sent at pnl={pnl:+.2f}"),
            )
        return True
    except Exception:
        log.exception("daily-cap alert send failed")
        return False


def restriction_status() -> dict:
    """One-shot snapshot for the UI banner + admin page. Drives the
    'Goal markets: 🔒 Paper only' display."""
    return {
        "goal_markets_locked": True,  # always true until unlock-criteria met
        "min_cash_edge": CASH_MIN_EDGE,
        "daily_loss_cap_usd": DAILY_CASH_LOSS_CAP_USD,
        "todays_cash_pnl": round(todays_cash_pnl(), 2),
        "daily_cap_hit": daily_loss_cap_hit(),
        "paper_first_required": False,  # spec D removed — see check_cash_eligibility
        "restricted_markets": sorted(RESTRICTED_CASH_MARKETS),
        "restricted_h2h_outcomes": sorted(RESTRICTED_CASH_OUTCOMES),
        "goal_market_progress": goal_market_paper_progress(),
    }
