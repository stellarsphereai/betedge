"""Spec 1.7 — Real-trade audit.

For each cash bet (is_paper=0):
  1. Find a matching paper counterpart on the same (match_id, market,
     market_line, outcome). Order of preference: same-day → most recent.
  2. Compute odds_diff (real - paper) and stake_diff.
  3. Flag any of:
        - no paper counterpart  → no_paper_flag
        - |odds_diff|  > 0.10   → odds_flag
        - |stake_diff| > $5     → stake_flag
  4. Roll up to a 'green' / 'amber' / 'red' quality:
        green = no flags
        amber = exactly one flag
        red   = two or more flags

Spec wording: "What was execution quality score?" — left as a categorical
green/amber/red rather than a numeric score, since the inputs are
discrete flags. The admin page renders this as a colored chip + reason
list per row.
"""
from __future__ import annotations

import logging

from database import db

log = logging.getLogger("arb.real_trade_audit")

ODDS_TOL = 0.10
STAKE_TOL = 5.0


def _quality(no_paper: bool, odds: bool, stake: bool) -> str:
    flags = sum([no_paper, odds, stake])
    if flags == 0:
        return "green"
    if flags == 1:
        return "amber"
    return "red"


def _notes(no_paper: bool, odds_diff: float | None, stake_diff: float | None) -> str:
    parts: list[str] = []
    if no_paper:
        parts.append("no paper counterpart found")
    if odds_diff is not None and abs(odds_diff) > ODDS_TOL:
        sign = "+" if odds_diff > 0 else ""
        parts.append(f"odds {sign}{odds_diff:.2f} vs paper")
    if stake_diff is not None and abs(stake_diff) > STAKE_TOL:
        sign = "+" if stake_diff > 0 else ""
        parts.append(f"stake {sign}${stake_diff:.0f} vs paper")
    return "; ".join(parts) or "matches paper exactly"


def refresh_all() -> dict:
    """Recompute the audit for every cash bet. Idempotent — safe to run
    nightly. Returns a summary dict."""
    summary = {"audited": 0, "green": 0, "amber": 0, "red": 0}
    with db() as conn:
        cash_bets = conn.execute(
            """
            SELECT id, match_id, market, market_line, bet_type AS outcome,
                   odds_at_placement AS odds, stake
            FROM bets_placed WHERE is_paper = 0
            """
        ).fetchall()
        for cb in cash_bets:
            # Find best paper counterpart on the same identity.
            paper = conn.execute(
                """
                SELECT id, odds_at_placement AS odds, stake
                FROM bets_placed
                WHERE is_paper = 1
                  AND match_id      IS  ?
                  AND market        IS  ?
                  AND market_line   IS  ?
                  AND bet_type      IS  ?
                ORDER BY abs(julianday('now') - julianday(timestamp)) ASC
                LIMIT 1
                """,
                (cb["match_id"], cb["market"], cb["market_line"], cb["outcome"]),
            ).fetchone()

            no_paper = paper is None
            odds_diff: float | None = None
            stake_diff: float | None = None
            if paper:
                odds_diff = float(cb["odds"] or 0) - float(paper["odds"] or 0)
                stake_diff = float(cb["stake"] or 0) - float(paper["stake"] or 0)

            odds_flag = bool(odds_diff is not None and abs(odds_diff) > ODDS_TOL)
            stake_flag = bool(stake_diff is not None and abs(stake_diff) > STAKE_TOL)
            quality = _quality(no_paper, odds_flag, stake_flag)
            notes = _notes(no_paper, odds_diff, stake_diff)

            conn.execute(
                """
                INSERT INTO real_trade_audit
                  (bet_id, paper_bet_id, odds_diff, stake_diff,
                   odds_flag, stake_flag, no_paper_flag, quality, notes,
                   last_audited)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                ON CONFLICT(bet_id) DO UPDATE SET
                  paper_bet_id   = excluded.paper_bet_id,
                  odds_diff      = excluded.odds_diff,
                  stake_diff     = excluded.stake_diff,
                  odds_flag      = excluded.odds_flag,
                  stake_flag     = excluded.stake_flag,
                  no_paper_flag  = excluded.no_paper_flag,
                  quality        = excluded.quality,
                  notes          = excluded.notes,
                  last_audited   = datetime('now')
                """,
                (
                    cb["id"], paper["id"] if paper else None,
                    odds_diff, stake_diff,
                    int(odds_flag), int(stake_flag), int(no_paper),
                    quality, notes,
                ),
            )
            summary["audited"] += 1
            summary[quality] += 1

    log.info("real_trade_audit: %s", summary)
    return summary


def report() -> list[dict]:
    """All audited cash bets joined with bet metadata for the admin page."""
    with db() as conn:
        rows = conn.execute(
            """
            SELECT a.bet_id, a.paper_bet_id, a.odds_diff, a.stake_diff,
                   a.odds_flag, a.stake_flag, a.no_paper_flag,
                   a.quality, a.notes, a.last_audited,
                   b.match_id, b.home_team, b.away_team,
                   b.market, b.market_line, b.bet_type AS outcome,
                   b.book, b.odds_at_placement, b.stake, b.status, b.profit
            FROM real_trade_audit a
            JOIN bets_placed b ON b.id = a.bet_id
            ORDER BY b.timestamp DESC
            """
        ).fetchall()
    return [dict(r) for r in rows]
