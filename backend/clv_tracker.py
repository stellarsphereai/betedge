"""Closing Line Value tracking. Beating the closing line is the most reliable
proxy for sharpness — more so than win rate at small samples.

Workflow:
    1. log_bet()       → write a row at placement
    2. set_closing()   → at kickoff, fetch closing line and update
    3. settle_bet()    → after the match, write result, profit, status
"""
from __future__ import annotations

from database import db


def log_bet(
    match_id: str,
    home_team: str,
    away_team: str,
    bet_type: str,
    book: str,
    odds_at_placement: float,
    stake: float,
    edge_at_placement: float,
    is_paper: bool = True,
    market: str | None = None,
    market_line: float | None = None,
) -> int:
    with db() as conn:
        cur = conn.execute(
            """
            INSERT INTO bets_placed
                (match_id, home_team, away_team, bet_type, book,
                 odds_at_placement, stake, edge_at_placement, is_paper, status,
                 market, market_line)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)
            """,
            (
                match_id, home_team, away_team, bet_type, book,
                odds_at_placement, stake, edge_at_placement, int(is_paper),
                market, market_line,
            ),
        )
        return cur.lastrowid


def set_closing(bet_id: int, closing_odds: float) -> None:
    with db() as conn:
        cur = conn.execute(
            "SELECT odds_at_placement FROM bets_placed WHERE id = ?", (bet_id,)
        )
        row = cur.fetchone()
        if not row:
            return
        clv = round(row["odds_at_placement"] - closing_odds, 2)
        conn.execute(
            "UPDATE bets_placed SET closing_odds = ?, clv = ? WHERE id = ?",
            (round(closing_odds, 2), clv, bet_id),
        )


async def sweep_closing_lines(league_to_sport_key: dict[str, str]) -> dict:
    """Capture closing lines for every open paper bet whose match has kicked off
    and whose closing_odds is still null. Skips fixtures still in the future."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    captured = 0
    skipped: list[dict] = []
    errored: list[dict] = []

    with db() as conn:
        targets = conn.execute(
            """
            SELECT b.id, f.kickoff_time, f.league
            FROM bets_placed b
            LEFT JOIN fixtures f ON f.match_id = b.match_id
            WHERE b.is_paper = 1
              AND b.closing_odds IS NULL
              AND f.kickoff_time IS NOT NULL
            """
        ).fetchall()

    for t in targets:
        try:
            kickoff = datetime.fromisoformat(t["kickoff_time"].replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            continue
        if kickoff > now:
            skipped.append({"bet_id": t["id"], "reason": "kickoff in future"})
            continue
        sport_key = league_to_sport_key.get((t["league"] or "").lower())
        if not sport_key:
            skipped.append({"bet_id": t["id"], "reason": f"no sport_key for league {t['league']}"})
            continue
        try:
            res = await capture_closing_for_bet(t["id"], sport_key)
            if res.get("ok") and not res.get("preview"):
                captured += 1
            else:
                skipped.append({"bet_id": t["id"], "reason": res.get("reason") or "preview"})
        except Exception as e:
            errored.append({"bet_id": t["id"], "error": str(e)[:120]})

    return {
        "captured": captured,
        "skipped": len(skipped),
        "errored": len(errored),
        "details": {"skipped": skipped[:10], "errored": errored[:10]},
    }


def _outcome_matches(bet, outcome: dict, target_match: dict) -> bool:
    """Decide whether an Odds API outcome row matches the bet's selection.
    `bet` may be a sqlite3.Row OR a dict — we use bracket access throughout."""
    market = (bet["market"] or "h2h").lower()
    bet_type = (bet["bet_type"] or "").lower()
    name = (outcome.get("name") or "").strip()
    name_lower = name.lower()

    if market == "h2h":
        team = {
            "home": target_match.get("home_team"),
            "away": target_match.get("away_team"),
            "draw": "Draw",
        }.get(bet_type)
        return bool(team) and name_lower == team.lower()

    if market == "btts":
        return name_lower == bet_type  # "yes" / "no"

    if market == "totals":
        if name_lower not in ("over", "under") or name_lower != bet_type:
            return False
        line = bet["market_line"]  # sqlite3.Row supports bracket only
        if line is None:
            return True
        try:
            return abs(float(outcome.get("point")) - float(line)) < 1e-6
        except (TypeError, ValueError):
            return False

    return False


async def capture_closing_for_bet(bet_id: int, sport_key: str) -> dict:
    """Fetch the historical Odds API snapshot at the bet's kickoff time, locate
    the same (bookmaker, outcome) the bet was placed at, and persist it as
    closing_odds + CLV. Idempotent: if closing_odds is already set, returns it.

    Pre-kickoff invocations clamp to the most recent past snapshot ("preview")
    instead of a future timestamp the API rejects. Re-run after kickoff for the
    real closing line.
    """
    from datetime import datetime, timedelta, timezone

    import historical_lines  # local import to avoid cycles
    import httpx
    from team_aliases import normalize_key

    with db() as conn:
        bet = conn.execute(
            "SELECT * FROM bets_placed WHERE id = ?", (bet_id,)
        ).fetchone()
        if not bet:
            return {"ok": False, "reason": f"bet {bet_id} not found"}
        if bet["closing_odds"] is not None:
            return {"ok": True, "already_captured": True, "closing_odds": bet["closing_odds"], "clv": bet["clv"]}

        fixture = conn.execute(
            """
            SELECT * FROM fixtures
            WHERE league IS NOT NULL
              AND home_team = ? AND away_team = ?
            ORDER BY kickoff_time DESC LIMIT 1
            """,
            (bet["home_team"], bet["away_team"]),
        ).fetchone()
        if not fixture or not fixture["kickoff_time"]:
            return {"ok": False, "reason": "no fixture / kickoff_time for this bet"}

    # If kickoff is in the future, the historical API rejects it. Use a recent
    # snapshot instead and flag the response as a preview, not the closing line.
    kickoff_dt = datetime.fromisoformat(fixture["kickoff_time"].replace("Z", "+00:00"))
    now = datetime.now(timezone.utc)
    is_preview = kickoff_dt > now
    snapshot_ts = (now - timedelta(minutes=2)) if is_preview else kickoff_dt
    snapshot_iso = snapshot_ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    bet_market = (bet["market"] or "h2h").lower()

    def _norm_book(s: str | None) -> str:
        return (s or "").lower().replace(" ", "").replace(".", "")
    desired_book = _norm_book(bet["book"])

    async with httpx.AsyncClient() as client:
        # Bulk historical handles h2h + totals. BTTS needs a per-event call,
        # which itself needs the Odds API event_id — we look that up from the
        # bulk h2h snapshot first.
        bulk_markets = "h2h,totals" if bet_market in ("h2h", "totals") else "h2h"
        bulk = await historical_lines.fetch_historical_odds(
            client, sport_key, snapshot_iso, markets=bulk_markets
        )
        if not bulk.get("available"):
            return {"ok": False, "reason": bulk.get("reason"), "paid_only": bulk.get("paid_only")}

        snap_fixtures = (bulk.get("data") or {}).get("data", []) or []
        home_key = normalize_key(bet["home_team"])
        away_key = normalize_key(bet["away_team"])
        target = next(
            (m for m in snap_fixtures
             if normalize_key(m["home_team"]) == home_key and normalize_key(m["away_team"]) == away_key),
            None,
        )
        if not target:
            return {"ok": False, "reason": "match not found in snapshot", "snapshot_count": len(snap_fixtures)}

        # For BTTS, the bulk call only got us the event_id; now fetch btts.
        if bet_market == "btts":
            event_id = target.get("id")
            if not event_id:
                return {"ok": False, "reason": "no event_id in snapshot for btts lookup"}
            ev = await historical_lines.fetch_historical_event_odds(
                client, sport_key, event_id, snapshot_iso, markets="btts"
            )
            if not ev.get("available"):
                return {"ok": False, "reason": ev.get("reason"), "paid_only": ev.get("paid_only")}
            target = (ev.get("data") or {}).get("data") or {}

    closing_price = None
    matched_book = None
    for bm in target.get("bookmakers", []) or []:
        if desired_book not in (_norm_book(bm.get("title")), _norm_book(bm.get("key"))):
            continue
        for mk in bm.get("markets", []) or []:
            if mk.get("key") != bet_market:
                continue
            for o in mk.get("outcomes", []) or []:
                price = o.get("price")
                if not isinstance(price, (int, float)):
                    continue
                if not _outcome_matches(bet, o, target):
                    continue
                closing_price = float(price)
                matched_book = bm.get("title")
                break

    if closing_price is None:
        return {"ok": False,
                "reason": f"no closing price for {bet['book']} / {bet_market}:{bet['bet_type']}"
                          + (f" line={bet['market_line']}" if bet["market_line"] is not None else "")}

    snapshot_at = (bulk.get("data") or {}).get("timestamp")

    # Only persist real closing lines — preview lookups don't get saved so a
    # later post-kickoff re-run captures the actual closing odds.
    if is_preview:
        return {
            "ok": True,
            "preview": True,
            "bet_id": bet_id,
            "book": matched_book,
            "kickoff": fixture["kickoff_time"],
            "snapshot_at": snapshot_at,
            "odds_at_placement": bet["odds_at_placement"],
            "preview_closing_odds": closing_price,
            "preview_clv": round(bet["odds_at_placement"] - closing_price, 2),
            "note": "kickoff in the future — re-run after kickoff to persist real closing line",
        }

    set_closing(bet_id, closing_price)
    with db() as conn:
        row = conn.execute(
            "SELECT odds_at_placement, closing_odds, clv FROM bets_placed WHERE id = ?",
            (bet_id,),
        ).fetchone()
    return {
        "ok": True,
        "bet_id": bet_id,
        "book": matched_book,
        "kickoff": fixture["kickoff_time"],
        "snapshot_at": snapshot_at,
        "odds_at_placement": row["odds_at_placement"],
        "closing_odds": row["closing_odds"],
        "clv": row["clv"],
    }


def settle_bet(bet_id: int, won: bool) -> None:
    with db() as conn:
        cur = conn.execute(
            "SELECT odds_at_placement, stake FROM bets_placed WHERE id = ?", (bet_id,)
        )
        row = cur.fetchone()
        if not row:
            return
        profit = (row["odds_at_placement"] - 1) * row["stake"] if won else -row["stake"]
        conn.execute(
            "UPDATE bets_placed SET status = ?, profit = ? WHERE id = ?",
            ("won" if won else "lost", profit, bet_id),
        )


def mark_match_result(
    match_id: str,
    home_goals: int | None = None,
    away_goals: int | None = None,
    result: str | None = None,
) -> dict:
    """Manually settle a fixture's outcome.

    Pass either:
      - home_goals + away_goals  → settles h2h, btts AND totals bets
      - result ('home'/'draw'/'away') without goals → settles h2h only
        (kept for backwards-compat with the legacy 3-button picker).

    Idempotent. Already-settled bets are left alone.
    """
    have_goals = home_goals is not None and away_goals is not None

    if have_goals:
        if home_goals < 0 or away_goals < 0:
            raise ValueError("goals cannot be negative")
        if home_goals > away_goals:
            result = "home"
        elif away_goals > home_goals:
            result = "away"
        else:
            result = "draw"
    elif result not in ("home", "draw", "away"):
        raise ValueError("supply home_goals+away_goals OR result=home/draw/away")

    with db() as conn:
        if have_goals:
            conn.execute(
                """
                INSERT INTO fixtures (match_id, result, home_goals, away_goals)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(match_id) DO UPDATE SET
                    result = excluded.result,
                    home_goals = excluded.home_goals,
                    away_goals = excluded.away_goals
                """,
                (match_id, result, home_goals, away_goals),
            )
        else:
            conn.execute(
                """
                INSERT INTO fixtures (match_id, result) VALUES (?, ?)
                ON CONFLICT(match_id) DO UPDATE SET result = excluded.result
                """,
                (match_id, result),
            )

        rows = conn.execute(
            """
            SELECT id, bet_type, market, market_line, odds_at_placement, stake
            FROM bets_placed
            WHERE match_id = ? AND status = 'open'
            """,
            (match_id,),
        ).fetchall()

        settled: list[dict] = []
        for r in rows:
            market = (r["market"] or "h2h").lower()
            bet_type = (r["bet_type"] or "").lower()
            won: bool | None = None

            if market == "h2h":
                won = bet_type == result
            elif have_goals and market == "btts":
                btts_hit = (home_goals > 0 and away_goals > 0)
                won = (bet_type == "yes" and btts_hit) or (bet_type == "no" and not btts_hit)
            elif have_goals and market == "totals" and r["market_line"] is not None:
                total = home_goals + away_goals
                if total == r["market_line"]:
                    # Push: refund stake, profit = 0 → status 'won' with 0 profit is wrong
                    # Use a 'push' status if you want, but most NY books treat .5 lines so
                    # this branch only fires on integer lines. Refund as profit=0.
                    conn.execute(
                        "UPDATE bets_placed SET status = ?, profit = ? WHERE id = ?",
                        ("won", 0.0, r["id"]),
                    )
                    settled.append({"bet_id": r["id"], "market": market, "outcome": "push", "profit": 0.0})
                    continue
                over = total > r["market_line"]
                won = (bet_type == "over" and over) or (bet_type == "under" and not over)

            if won is None:
                continue  # market needs goals to settle but we only have result

            profit = (r["odds_at_placement"] - 1) * r["stake"] if won else -r["stake"]
            conn.execute(
                "UPDATE bets_placed SET status = ?, profit = ? WHERE id = ?",
                ("won" if won else "lost", profit, r["id"]),
            )
            settled.append({
                "bet_id": r["id"], "market": market, "bet_type": bet_type,
                "won": won, "profit": profit,
            })

    return {
        "match_id": match_id, "result": result,
        "home_goals": home_goals, "away_goals": away_goals,
        "settled": settled, "settled_count": len(settled),
    }


def weekly_report() -> dict:
    with db() as conn:
        rows = conn.execute(
            """
            SELECT clv, profit, stake, status FROM bets_placed
            WHERE timestamp >= datetime('now', '-7 days')
              AND status IN ('won', 'lost')
            """
        ).fetchall()

    if not rows:
        return {"total_bets": 0}

    total = len(rows)
    wins = sum(1 for r in rows if r["status"] == "won")
    profit = sum(r["profit"] or 0 for r in rows)
    staked = sum(r["stake"] or 0 for r in rows)
    clvs = [r["clv"] for r in rows if r["clv"] is not None]
    return {
        "total_bets": total,
        "win_rate": round(wins / total, 4),
        "total_profit": round(profit, 2),
        "roi": round(profit / staked, 4) if staked > 0 else 0.0,
        "avg_clv": round(sum(clvs) / len(clvs), 2) if clvs else None,
        "n_clv_samples": len(clvs),
    }
