"""Closing Line Value tracking. Beating the closing line is the most reliable
proxy for sharpness — more so than win rate at small samples.

Workflow:
    1. log_bet()       → write a row at placement
    2. set_closing()   → at kickoff, fetch closing line and update
    3. settle_bet()    → after the match, write result, profit, status

Settled bets also push their signed profit into the book's balance via
book_balance.apply_settled_bet — paper bets are skipped automatically.
"""
from __future__ import annotations

import book_balance
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
    """Insert (or update) a bet. Idempotent on the bet's identity tuple
    (match_id, market, market_line, bet_type) for OPEN bets — clicking
    'Log paper' then 'Log cash' on the same row updates the existing row's
    mode rather than creating two records, which would duplicate the bet
    across the trade log's Paper/Cash sub-tabs."""
    with db() as conn:
        existing = conn.execute(
            """
            SELECT id FROM bets_placed
            WHERE match_id = ?
              AND COALESCE(market, 'h2h') = COALESCE(?, 'h2h')
              AND ((market_line IS NULL AND ? IS NULL) OR market_line = ?)
              AND bet_type = ?
              AND status = 'open'
            ORDER BY id DESC LIMIT 1
            """,
            (match_id, market, market_line, market_line, bet_type),
        ).fetchone()
        if existing:
            conn.execute(
                """
                UPDATE bets_placed
                SET is_paper = ?, stake = ?, odds_at_placement = ?,
                    edge_at_placement = ?, book = ?
                WHERE id = ?
                """,
                (int(is_paper), stake, odds_at_placement,
                 edge_at_placement, book, existing["id"]),
            )
            return int(existing["id"])
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
    """Capture closing lines for every open bet — paper AND cash — whose
    match has kicked off and whose closing_odds is still null. Skips
    fixtures still in the future.

    Originally filtered to paper only (back when the system was paper-only).
    Cash bets need CLV more than paper does — real money on the line — so
    the filter was wrong and produced empty CLV for every cash bet.
    """
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
            WHERE b.closing_odds IS NULL
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


async def sweep_pre_kickoff_closing_lines(league_to_sport_key: dict[str, str]) -> dict:
    """Capture closing lines ~30 min before kickoff using the LIVE odds API.

    The historical API only carries primary totals (2.5) and often misses
    alternate lines (1.5, 3.5, 4.5+) and BTTS. The live API has all markets
    including alternate_totals and btts, but is only available pre-kickoff.

    This sweep targets bets whose kickoff is within the next 45 minutes and
    whose closing_odds is still null. It fetches live odds for the sport,
    then per-event alternate_totals and btts as needed.
    """
    from datetime import datetime, timedelta, timezone

    import httpx
    import odds_client
    from team_aliases import normalize_key

    now = datetime.now(timezone.utc)
    window_start = now
    window_end = now + timedelta(minutes=45)
    captured = 0
    skipped: list[dict] = []
    errored: list[dict] = []

    with db() as conn:
        targets = conn.execute(
            """
            SELECT b.id, b.home_team, b.away_team, b.market, b.bet_type,
                   b.market_line, b.book, b.odds_at_placement,
                   f.kickoff_time, f.league, f.match_id
            FROM bets_placed b
            JOIN fixtures f ON f.match_id = b.match_id
            WHERE b.closing_odds IS NULL
              AND f.kickoff_time IS NOT NULL
            """,
        ).fetchall()

    # Filter to bets kicking off within the window
    upcoming: list[dict] = []
    for t in targets:
        try:
            ko = datetime.fromisoformat(t["kickoff_time"].replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            continue
        if window_start <= ko <= window_end:
            upcoming.append(dict(t))

    if not upcoming:
        return {"captured": 0, "skipped": 0, "errored": 0, "checked": 0}

    # Group by sport_key to minimize API calls
    by_sport: dict[str, list[dict]] = {}
    for bet in upcoming:
        sk = league_to_sport_key.get((bet["league"] or "").lower())
        if not sk:
            skipped.append({"bet_id": bet["id"], "reason": f"no sport_key for {bet['league']}"})
            continue
        by_sport.setdefault(sk, []).append(bet)

    def _norm_book(s):
        return (s or "").lower().replace(" ", "").replace(".", "")

    async with httpx.AsyncClient() as client:
        for sport_key, bets in by_sport.items():
            try:
                # Fetch live h2h + totals (primary line)
                live_matches = await odds_client.fetch_odds(client, sport_key)
            except Exception as e:
                for b in bets:
                    errored.append({"bet_id": b["id"], "error": str(e)[:120]})
                continue

            # Index live matches by normalized team names
            match_index: dict[tuple[str, str], dict] = {}
            for m in live_matches:
                hk = normalize_key(m.get("home_team", ""))
                ak = normalize_key(m.get("away_team", ""))
                match_index[(hk, ak)] = m

            # Collect event_ids that need alternate_totals or btts
            needs_alt_totals: set[str] = set()
            needs_btts: set[str] = set()
            for b in bets:
                mk = (b["market"] or "h2h").lower()
                hk = normalize_key(b["home_team"])
                ak = normalize_key(b["away_team"])
                match = match_index.get((hk, ak))
                if not match:
                    continue
                eid = match.get("id")
                if not eid:
                    continue
                if mk == "totals" and b["market_line"] is not None and abs(float(b["market_line"]) - 2.5) > 0.01:
                    needs_alt_totals.add(eid)
                if mk == "btts":
                    needs_btts.add(eid)

            # Fetch alternate totals and btts for events that need them
            alt_totals_by_event: dict[str, list[dict]] = {}
            btts_by_event: dict[str, list[dict]] = {}
            for eid in needs_alt_totals:
                try:
                    alt = await odds_client.fetch_event_alternate_totals(client, sport_key, eid)
                    alt_totals_by_event[eid] = alt
                except Exception:
                    pass
            for eid in needs_btts:
                try:
                    bt = await odds_client.fetch_event_btts(client, sport_key, eid)
                    btts_by_event[eid] = bt
                except Exception:
                    pass

            # Now match each bet to its closing price
            for b in bets:
                hk = normalize_key(b["home_team"])
                ak = normalize_key(b["away_team"])
                match = match_index.get((hk, ak))
                if not match:
                    skipped.append({"bet_id": b["id"], "reason": "match not in live odds"})
                    continue

                mk = (b["market"] or "h2h").lower()
                desired_book = _norm_book(b["book"])
                eid = match.get("id")

                # Build the bookmaker list to search
                search_bookmakers = list(match.get("bookmakers", []) or [])
                if mk == "totals" and eid and eid in alt_totals_by_event:
                    # Merge alternate totals — relabel as "totals" for matching
                    for bm in alt_totals_by_event[eid]:
                        for mkt in bm.get("markets", []):
                            if mkt.get("key") == "alternate_totals":
                                mkt["key"] = "totals"
                        search_bookmakers.append(bm)
                if mk == "btts" and eid and eid in btts_by_event:
                    search_bookmakers.extend(btts_by_event[eid])

                closing_price = None
                for bm in search_bookmakers:
                    if desired_book not in (_norm_book(bm.get("title")), _norm_book(bm.get("key"))):
                        continue
                    for mkt in bm.get("markets", []) or []:
                        if mkt.get("key") != mk:
                            continue
                        for o in mkt.get("outcomes", []) or []:
                            price = o.get("price")
                            if not isinstance(price, (int, float)):
                                continue
                            if not _outcome_matches(b, o, match):
                                continue
                            closing_price = float(price)
                            break

                if closing_price is None:
                    skipped.append({
                        "bet_id": b["id"],
                        "reason": f"no live price for {b['book']} / {mk}:{b['bet_type']}"
                                  + (f" line={b['market_line']}" if b["market_line"] is not None else ""),
                    })
                    continue

                try:
                    set_closing(b["id"], closing_price)
                    captured += 1
                except Exception as e:
                    errored.append({"bet_id": b["id"], "error": str(e)[:120]})

    return {
        "captured": captured,
        "skipped": len(skipped),
        "errored": len(errored),
        "checked": len(upcoming),
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
            "SELECT odds_at_placement, stake, book, is_paper FROM bets_placed WHERE id = ?", (bet_id,)
        )
        row = cur.fetchone()
        if not row:
            return
        profit = (row["odds_at_placement"] - 1) * row["stake"] if won else -row["stake"]
        conn.execute(
            "UPDATE bets_placed SET status = ?, profit = ? WHERE id = ?",
            ("won" if won else "lost", profit, bet_id),
        )
    book_balance.apply_settled_bet(row["book"], profit, is_paper=bool(row["is_paper"]))


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
            SELECT id, bet_type, market, market_line, odds_at_placement, stake,
                   book, is_paper
            FROM bets_placed
            WHERE match_id = ? AND status = 'open'
            """,
            (match_id,),
        ).fetchall()

        settled: list[dict] = []
        # Stage balance updates outside the conn block so we don't nest writers.
        balance_updates: list[tuple[str | None, float, bool]] = []
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
            balance_updates.append((r["book"], profit, bool(r["is_paper"])))

    # Apply balance changes outside the settlement transaction. Paper bets +
    # untracked books are filtered inside apply_settled_bet itself.
    for book_name, profit, is_paper in balance_updates:
        book_balance.apply_settled_bet(book_name, profit, is_paper=is_paper)

    # Spec C — fire the daily-loss-cap alert email if a cash bet just
    # pushed today's P&L past -$50. Paper-only settlement runs harmlessly
    # through this path; the cap check itself only counts cash bets.
    try:
        import cash_restrictions
        cash_restrictions.maybe_send_daily_cap_alert()
    except Exception:
        pass  # never let the alert break settlement

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
