"""APScheduler — fully automatic daily cadence.

    00:00  sync EPL data
    01:00  sync UCL data
    02:00  sync Europa League data
    03:00  sync World Cup data
    06:00  pre-warm /ev-bets across leagues (no upstream calls — cached data)
    08:00  send morning digest email
    23:55  capture closing lines + daily P&L snapshot

OFF by default — set SCHEDULER_ENABLED=true in .env (or POST /admin/scheduler/start)
to start the loop. Designed to run unattended for weeks at a time.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

import api_quota
import calibrate
import clv_tracker
import data_sync
import digest
import self_eval
import wc_calibrate
from database import db

log = logging.getLogger("arb.scheduler")

TIMEZONE = os.getenv("TIMEZONE", "America/New_York")
ENABLED = (os.getenv("SCHEDULER_ENABLED", "false").strip().lower() == "true")

LEAGUE_TO_SPORT_KEY = {
    "epl": "soccer_epl",
    "ucl": "soccer_uefa_champs_league",
    "uel": "soccer_uefa_europa_league",
    "world_cup": "soccer_fifa_world_cup",
    "la_liga": "soccer_spain_la_liga",
}

_scheduler: AsyncIOScheduler | None = None


# ---------- jobs ----------


def _league_sync_job(league_key: str):
    """Closure factory so each cron-registered job carries its own league."""
    async def _job():
        if api_quota.today_count() >= api_quota.DEFAULT_DAILY_LIMIT:
            log.warning("scheduler: quota exceeded — skipping %s sync", league_key)
            return
        log.info("scheduler: syncing %s", league_key)
        try:
            summary = await data_sync.sync_daily(league=league_key, force=False)
            log.info("scheduler: sync %s done: %s", league_key, summary)
        except Exception:
            log.exception("scheduler: sync %s crashed", league_key)
    return _job


async def job_morning_ev():
    """06:00 NY: pre-warm /ev-bets for each league. The actual EV math is
    on-demand in the FastAPI endpoint; this just ensures cached data is
    fresh before the digest renders at 08:00."""
    log.info("scheduler: 06:00 EV pre-warm across leagues")
    import httpx
    async with httpx.AsyncClient(base_url="http://127.0.0.1:8002", timeout=30.0) as c:
        for league in LEAGUE_TO_SPORT_KEY:
            try:
                await c.get(f"/ev-bets?league={league}")
            except Exception as e:
                log.warning("ev pre-warm %s failed: %s", league, e)


async def job_auto_paper_bets():
    """06:15 NY: auto-place paper bets for restricted markets (BTTS, totals over)
    so the unlock gate (20 settled, 50%+ win rate) can fill up without manual clicks.
    Only places paper bets — never cash. Idempotent via log_bet's dedup on
    (match_id, market, market_line, bet_type) for open bets."""
    log.info("scheduler: auto-paper sweep")
    import httpx
    AUTO_PAPER_MARKETS = {
        ("btts", "yes"), ("btts", "no"),
        ("totals", "over"),
        ("h2h", "draw"),
    }
    placed = 0
    skipped = 0
    errors = 0
    async with httpx.AsyncClient(base_url="http://127.0.0.1:8002", timeout=30.0) as c:
        for league in LEAGUE_TO_SPORT_KEY:
            try:
                resp = await c.get(f"/ev-bets?league={league}")
                data = resp.json()
            except Exception as e:
                log.warning("auto-paper: %s ev-bets fetch failed: %s", league, e)
                errors += 1
                continue
            for bet in data.get("bets", []):
                market = (bet.get("market") or "").lower()
                outcome = (bet.get("outcome") or "").lower()
                if (market, outcome) not in AUTO_PAPER_MARKETS:
                    continue
                try:
                    clv_tracker.log_bet(
                        match_id=bet["match_id"],
                        home_team=bet["home_team"],
                        away_team=bet["away_team"],
                        bet_type=outcome,
                        book=bet.get("best_book", ""),
                        odds_at_placement=bet.get("best_odds", 0),
                        stake=bet.get("stake", 0) or bet.get("kelly_stake_full", 0),
                        edge_at_placement=bet.get("edge", 0),
                        is_paper=True,
                        market=market,
                        market_line=bet.get("market_line"),
                    )
                    placed += 1
                except Exception as e:
                    log.warning("auto-paper: failed to log %s %s:%s: %s",
                                bet.get("match_id"), market, outcome, e)
                    errors += 1
    log.info("auto-paper: placed %d, skipped %d, errors %d", placed, skipped, errors)
    return {"placed": placed, "skipped": skipped, "errors": errors}


async def job_morning_digest():
    """08:00 NY: render and send the digest email."""
    log.info("scheduler: 08:00 morning digest")
    import httpx
    async with httpx.AsyncClient(base_url="http://127.0.0.1:8002", timeout=30.0) as c:
        ev = (await c.get("/ev-bets")).json()
        stats = (await c.get("/stats")).json()
    subject, body = digest.render(ev, stats)
    result = digest.send(subject, body)
    log.info("scheduler: digest send result: %s", result)


async def job_weekly_accuracy_snapshot():
    """Sun 04:00 NY: persist a per-league accuracy snapshot + email a digest.
    Always runs; reports zeros when no fixtures have settled yet."""
    log.info("scheduler: weekly accuracy snapshot")
    try:
        payload = calibrate.write_weekly_snapshot()
        subject, body = digest.render_weekly_accuracy(payload)
        result = digest.send(subject, body)
        log.info("scheduler: weekly snapshot email: %s", result)
    except Exception:
        log.exception("scheduler: weekly accuracy snapshot crashed")


async def job_nightly_wc_check():
    """04:30 NY (after the 03:00 sync_world_cup): write a WC snapshot and,
    if the tournament phase changed since the last recorded snapshot, email
    a phase-end report. Idempotent — silently no-ops outside the tournament
    window (no_data → no_data → no_data, no transition, no email)."""
    log.info("scheduler: nightly WC calibration check")
    try:
        result = wc_calibrate.nightly_wc_check()
    except Exception:
        log.exception("scheduler: WC nightly check crashed")
        return

    if not result["transitioned"]:
        log.info("scheduler: WC phase=%s (no transition)", result["current_phase"])
        return

    prev = result["previous_phase"]
    current = result["current_phase"]
    log.info("scheduler: WC phase transition %s → %s", prev, current)

    # Email a phase-end report for the phase we just left
    snap = result["snapshot"]
    payload = {
        "phase": prev,
        "n_settled": snap["n_settled"],
        "n_min_for_grid_search": wc_calibrate.N_MIN_FOR_GRID_SEARCH,
        "eligible": snap["n_settled"] >= wc_calibrate.N_MIN_FOR_GRID_SEARCH,
        "snapshot": snap,
        "grid_search": None,
    }
    subject, body = digest.render_wc_phase_report(payload)
    # Prepend the transition banner so the email clearly says "we just moved to X"
    body = f"PHASE TRANSITION: {prev} → {current}\n\n" + body
    digest.send(subject, body)


async def job_monthly_calibration_check():
    """1st of month 04:00 NY: per regular league, count settled predictions
    and email a status report. If a league crosses n>=100 we'd run the grid
    search; today the search is stubbed and the email surfaces that."""
    log.info("scheduler: monthly calibration check")
    try:
        payload = calibrate.run_monthly_calibration_check()
        subject, body = digest.render_monthly_calibration(payload)
        result = digest.send(subject, body)
        log.info("scheduler: monthly calibration email: %s", result)
    except Exception:
        log.exception("scheduler: monthly calibration check crashed")


async def job_auto_settle_open_bets():
    """02:00 NY-local — sweep every open bet (paper + real), look up its
    fixture status from API-Football, and call mark_match_result() for any
    match that finished. Per-bet settlement was already implemented (manual
    'auto-mark' button on each row); this job just runs the same flow on a
    timer so cash bets can't go un-settled overnight.

    Idempotent: mark_match_result skips already-settled bets, so running
    twice in a row is safe. Fixtures still in progress get skipped quietly.
    """
    import httpx
    import api_football
    log.info("scheduler: 02:00 auto-settle sweep")
    with db() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT match_id FROM bets_placed
            WHERE status = 'open'
              AND match_id LIKE 'af-%'
            """
        ).fetchall()
    open_match_ids = [r["match_id"] for r in rows]
    log.info("auto-settle: %d open match(es) to check", len(open_match_ids))
    settled_total = 0
    skipped_in_progress = 0
    errors = 0
    async with httpx.AsyncClient() as client:
        for mid in open_match_ids:
            try:
                fixture_id = int(mid.removeprefix("af-"))
                fixture = await api_football.fetch_fixture(client, fixture_id)
            except Exception as e:
                log.warning("auto-settle: %s fetch failed: %s", mid, e)
                errors += 1
                continue
            if not fixture:
                continue
            status_short = (
                fixture.get("fixture", {}).get("status", {}).get("short") or ""
            ).upper()
            _FINISHED = {"FT", "AET", "PEN", "FT_PEN"}
            if status_short not in _FINISHED:
                skipped_in_progress += 1
                continue
            # Use 90-min (fulltime) score for settlement — all markets
            # (h2h, btts, totals) settle on regular time, not extra time.
            ft = (fixture.get("score") or {}).get("fulltime") or {}
            home_goals = ft.get("home")
            away_goals = ft.get("away")
            # Only fallback to goals for FT matches. For AET/PEN, goals
            # includes extra time and MUST NOT be used — skip and retry later.
            if home_goals is None or away_goals is None:
                if status_short in ("AET", "PEN", "FT_PEN"):
                    log.warning("auto-settle: %s is %s but fulltime score missing — skipping (would use ET score)", mid, status_short)
                    skipped_in_progress += 1
                    continue
                home_goals = fixture.get("goals", {}).get("home")
                away_goals = fixture.get("goals", {}).get("away")
            if home_goals is None or away_goals is None:
                continue
            try:
                result = clv_tracker.mark_match_result(
                    mid, home_goals=int(home_goals), away_goals=int(away_goals)
                )
                settled_total += int(result.get("settled_count") or 0)
            except Exception:
                log.exception("auto-settle: %s mark_match_result crashed", mid)
                errors += 1
    log.info(
        "auto-settle: settled %d bet(s), skipped %d in-progress, %d errors",
        settled_total, skipped_in_progress, errors,
    )
    return {
        "settled": settled_total,
        "skipped_in_progress": skipped_in_progress,
        "errors": errors,
        "matches_checked": len(open_match_ids),
    }


async def job_settle_fixtures():
    """Settle ALL past fixtures (not just ones with open bets). Without this,
    fixtures without bets stay result=NULL and downstream consumers (rest_days,
    _wc_matches_played, xG history from settled WC matches) use stale data.

    Runs twice daily: after the nightly sync and at midday."""
    import httpx
    import api_football
    log.info("scheduler: fixture settlement sweep")
    with db() as conn:
        rows = conn.execute(
            """
            SELECT match_id, home_team, away_team FROM fixtures
            WHERE result IS NULL AND kickoff_time < datetime('now')
              AND match_id LIKE 'af-%'
            """
        ).fetchall()
    if not rows:
        log.info("fixture-settle: no unsettled past fixtures")
        return {"settled": 0, "checked": 0}

    log.info("fixture-settle: %d unsettled past fixture(s) to check", len(rows))
    settled = 0
    async with httpx.AsyncClient() as client:
        for r in rows:
            mid = r["match_id"]
            try:
                fid = int(mid.removeprefix("af-"))
                fx = await api_football.fetch_fixture(client, fid)
            except Exception as e:
                log.warning("fixture-settle: %s fetch failed: %s", mid, e)
                continue
            if not fx:
                continue
            status_short = (
                fx.get("fixture", {}).get("status", {}).get("short") or ""
            ).upper()
            _FINISHED = {"FT", "AET", "PEN", "FT_PEN"}
            # Use 90-min fulltime score, not final score with extra time
            ft = (fx.get("score") or {}).get("fulltime") or {}
            hg, ag = ft.get("home"), ft.get("away")
            # Only fallback to goals for FT matches. For AET/PEN, skip if
            # fulltime is missing — goals field includes extra time.
            if hg is None or ag is None:
                if status_short in ("AET", "PEN", "FT_PEN"):
                    log.warning("fixture-settle: %s is %s but fulltime score missing — skipping", mid, status_short)
                    continue
                g = fx.get("goals", {})
                hg, ag = g.get("home"), g.get("away")
            if status_short in _FINISHED and hg is not None and ag is not None:
                result = "home" if hg > ag else "away" if ag > hg else "draw"
                with db() as conn:
                    conn.execute(
                        "UPDATE fixtures SET result = ?, home_goals = ?, away_goals = ? WHERE match_id = ?",
                        (result, int(hg), int(ag), mid),
                    )
                log.info("fixture-settle: %s %s vs %s → %s-%s",
                         mid, r["home_team"], r["away_team"], hg, ag)
                settled += 1
    # Pre-fetch xG stats for all settled WC fixtures so they're cached
    # when the next sync runs. Without this, the sync hits rate limits
    # trying to fetch stats mid-run and falls back to qualifier data.
    if settled > 0:
        stats_fetched = 0
        with db() as conn:
            wc_settled = conn.execute(
                "SELECT match_id FROM fixtures WHERE league = 'world_cup' AND result IS NOT NULL AND match_id LIKE 'af-%'"
            ).fetchall()
        async with httpx.AsyncClient() as client:
            for r in wc_settled:
                fid = int(r["match_id"].removeprefix("af-"))
                try:
                    await api_football.fixture_statistics(client, fid, force=False)
                    stats_fetched += 1
                except Exception:
                    pass
        log.info("fixture-settle: pre-fetched stats for %d settled WC fixtures", stats_fetched)

    log.info("fixture-settle: settled %d/%d fixtures", settled, len(rows))
    return {"settled": settled, "checked": len(rows)}


async def _wrap(name: str, coro_factory):
    """Adapter — APScheduler wants a no-arg async fn; automation_runner
    wants (name, fn) where fn is also no-arg async."""
    import automation_runner
    return await automation_runner.run_task(name, coro_factory)


async def job_nightly_model_validation():
    import automation_tasks
    return await _wrap("model_validation", automation_tasks.task_model_validation)

async def job_nightly_auto_calibration():
    import automation_tasks
    return await _wrap("auto_calibration", automation_tasks.task_auto_calibration)

async def job_nightly_wc_data_prep():
    import automation_tasks
    return await _wrap("wc_data_prep", automation_tasks.task_wc_data_prep)

async def job_nightly_system_health():
    import automation_tasks
    return await _wrap("system_health", automation_tasks.task_system_health)

async def job_nightly_feature_verification():
    import automation_tasks
    return await _wrap("feature_verification", automation_tasks.task_feature_verification)

async def job_nightly_wc_configuration():
    import automation_tasks
    return await _wrap("wc_configuration", automation_tasks.task_wc_configuration)

async def job_nightly_real_money_performance():
    import automation_tasks
    return await _wrap("real_money_performance", automation_tasks.task_real_money_performance)

async def job_nightly_readiness_score():
    import automation_tasks
    return await _wrap("readiness_score", automation_tasks.task_readiness_score)

async def job_nightly_early_wc_opportunities():
    import automation_tasks
    return await _wrap("early_wc_opportunities", automation_tasks.task_early_wc_opportunities)


async def job_consec_failure_alert():
    """06:00 NY — if any nightly task has 3 consecutive FAILs, urgent email.
    Spec section 2: '3 consecutive failures trigger urgent 6am alert email'.
    DEFERRED rows don't count; they reset the streak."""
    import automation_runner
    tasks = [
        "model_validation", "auto_calibration", "wc_data_prep", "system_health",
        "feature_verification", "wc_configuration", "real_money_performance",
        "readiness_score", "early_wc_opportunities",
    ]
    escalated = []
    for t in tasks:
        n = automation_runner.consecutive_failures(t)
        if n >= 3:
            escalated.append((t, n))
    if not escalated:
        log.info("scheduler: 06:00 consec-failure check — all clear")
        return
    body_lines = [
        "URGENT — three or more consecutive nightly failures detected.",
        "",
        "Tasks needing attention:",
    ]
    for t, n in escalated:
        last = automation_runner.latest_run(t) or {}
        body_lines.append(f"  - {t}: {n} consecutive FAILs")
        if last.get("error_message"):
            body_lines.append(f"      last error: {last['error_message'][:200]}")
    body = "\n".join(body_lines)
    try:
        digest.send("BetEdge — URGENT: nightly automation failures", body)
    except Exception:
        log.exception("scheduler: 06:00 consec-failure email send crashed")


async def job_morning_report():
    """08:00 NY — render the morning automation report and email it.
    Combines:
      - overnight task pass/fail rollup (automation_log today)
      - real-money status (digest's existing block via /stats)
      - readiness score + days-to-kickoff
      - manual action items (placeholder for spec 5)
    """
    import automation_runner
    runs = automation_runner.todays_runs()
    today = datetime.now(timezone.utc).date()
    days_to_kickoff = (datetime(2026, 6, 11).date() - today).days

    # Roll up readiness from the most recent task run. The task always
    # returns status=PASS (the task itself ran fine); the GREEN/AMBER/RED
    # health flag is parsed from the summary string ("8/29 (27.6%) · RED · 38 days").
    readiness = next((r for r in reversed(runs) if r["task_name"] == "readiness_score"), None)
    pct = "?"
    flag = "?"
    if readiness and readiness.get("result_summary"):
        s = readiness["result_summary"]
        if "(" in s and "%" in s:
            try:
                pct = s.split("(")[1].split("%")[0]
            except Exception:
                pass
        for tag in ("GREEN", "AMBER", "RED"):
            if tag in s:
                flag = tag
                break

    body_lines = []
    body_lines.append("=== OVERNIGHT COMPLETIONS ===")
    if runs:
        for r in runs:
            icon = "✅" if r["status"] == "PASS" else "⏸" if r["status"] == "DEFERRED" else "❌"
            body_lines.append(f"  {icon} {r['task_name']}: {r['result_summary']}")
    else:
        body_lines.append("  (no automation tasks logged today)")

    # Real money status — reuse the existing renderer indirectly by calling /stats
    try:
        # local read — same module path as the FastAPI process
        with db() as conn:
            cash = conn.execute(
                """
                SELECT COALESCE(SUM(CASE WHEN status IN ('won','lost') THEN profit ELSE 0 END), 0) AS pnl,
                       COALESCE(SUM(CASE WHEN status='won' THEN 1 ELSE 0 END), 0) AS won,
                       COALESCE(SUM(CASE WHEN status='lost' THEN 1 ELSE 0 END), 0) AS lost
                FROM bets_placed WHERE is_paper = 0
                """
            ).fetchone()
        body_lines.append("")
        body_lines.append("=== REAL MONEY STATUS ===")
        body_lines.append(f"  Total P&L: {cash['pnl']:+.2f}")
        body_lines.append(f"  Won/Lost:  {cash['won']}/{cash['lost']}")
    except Exception:
        log.exception("morning_report: real-money block failed")

    body_lines.append("")
    body_lines.append("=== READINESS SCORE ===")
    body_lines.append(f"  {pct}% — {flag} — {days_to_kickoff} days to kickoff")

    body = "\n".join(body_lines)
    subject = f"BetEdge — {today.isoformat()} — WC Readiness {pct}% · {days_to_kickoff} days to kickoff"
    try:
        digest.send(subject, body)
    except Exception:
        log.exception("morning_report: send crashed")


async def job_clv_feedback_eval():
    """23:50 NY — refresh rolling 10-bet CLV avg + flip digest send hour
    if needed. Self-cal Piece 5."""
    log.info("scheduler: 23:50 CLV feedback eval")
    try:
        import clv_feedback
        return clv_feedback.evaluate_and_update()
    except Exception:
        log.exception("scheduler: CLV feedback eval crashed")
        return None


async def job_weekly_tactical_suppressor_detect():
    """Sunday 04:15 NY — recompute tactical_suppressors from settled
    matches. Self-cal Piece 4 + Fix 2 (auto-detection layer)."""
    log.info("scheduler: Sunday 04:15 tactical-suppressor auto-detect")
    try:
        import tactical_suppressors
        seeded = tactical_suppressors.seed_manual_entries()
        result = tactical_suppressors.auto_detect_from_results()
        result["manual_seeded_now"] = seeded
        return result
    except Exception:
        log.exception("scheduler: tactical suppressor auto-detect crashed")
        return None


async def job_real_trade_audit():
    """02:45 NY-local — refresh the real_trade_audit table after the 02:30
    auto-settle pass. Spec 1.7: per-cash-bet comparison against paper
    counterpart (odds delta, stake delta, missing counterpart). Cheap
    SQL-only pass; no external API calls."""
    log.info("scheduler: 02:45 real-trade audit refresh")
    try:
        import real_trade_audit
        return real_trade_audit.refresh_all()
    except Exception:
        log.exception("scheduler: real-trade audit crashed")
        return None


async def job_pre_kickoff_clv():
    """Every 30 min — capture closing lines from the LIVE odds API for bets
    kicking off within 45 minutes. Covers alternate totals (3.5, 4.5, etc.)
    and BTTS that the historical API misses."""
    try:
        result = await clv_tracker.sweep_pre_kickoff_closing_lines(LEAGUE_TO_SPORT_KEY)
        if result["captured"] or result["errored"]:
            log.info("scheduler: pre-kickoff CLV: %s", result)
    except Exception:
        log.exception("scheduler: pre-kickoff CLV sweep crashed")


async def job_closing_lines_and_pnl():
    """23:55 NY: sweep closing lines, snapshot P&L, then run the self-eval
    pipeline (result logging + 5 bias checks per league). Each step wrapped so
    a single failure doesn't cascade."""
    log.info("scheduler: 23:55 closing-line sweep + daily P&L + self-eval")
    try:
        sweep = await clv_tracker.sweep_closing_lines(LEAGUE_TO_SPORT_KEY)
        log.info("scheduler: closing-line sweep: %s", sweep)
    except Exception:
        log.exception("scheduler: closing-line sweep crashed")
    try:
        await _job_daily_pnl()
    except Exception:
        log.exception("scheduler: daily P&L snapshot crashed")
    try:
        eval_out = await self_eval.run_nightly()
        log.info("scheduler: self-eval ran: %s", eval_out)
    except Exception:
        log.exception("scheduler: self-eval crashed")


async def _job_daily_pnl():
    today = datetime.now(timezone.utc).date().isoformat()
    with db() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS n,
                   SUM(profit) AS profit,
                   SUM(stake)  AS staked,
                   AVG(clv)    AS avg_clv,
                   AVG(edge_at_placement) AS avg_edge
            FROM bets_placed
            WHERE date(timestamp) = ?
              AND status IN ('won','lost')
            """,
            (today,),
        ).fetchone()

        starting = float(os.getenv("BANKROLL", "1000"))
        bankroll_now = conn.execute(
            "SELECT COALESCE(SUM(profit),0) FROM bets_placed WHERE status IN ('won','lost')"
        ).fetchone()[0]
        bankroll = starting + (bankroll_now or 0)
        roi = (row["profit"] or 0) / row["staked"] if row["staked"] else 0.0

        conn.execute(
            """
            INSERT INTO daily_stats (date, bankroll, bets_placed_count, profit, roi, avg_clv, avg_edge)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(date) DO UPDATE SET
                bankroll = excluded.bankroll,
                bets_placed_count = excluded.bets_placed_count,
                profit = excluded.profit,
                roi = excluded.roi,
                avg_clv = excluded.avg_clv,
                avg_edge = excluded.avg_edge
            """,
            (today, bankroll, row["n"] or 0, row["profit"] or 0, roi, row["avg_clv"], row["avg_edge"]),
        )


# ---------- end-of-day cron status email ----------


# Friendly labels for the daily status email — covers every job_id in the
# schedule below. New jobs added to the schedule should also get a label
# here; the listener falls back to the raw id if missing.
async def job_activate_opponent_adjusted_xg():
    """One-shot — fires at 2026-07-20 00:01 NY-local (post-World-Cup Final).

    Flips the OPPONENT_ADJUSTED_XG flag in the box's .env and emails a
    notification + a kickoff for the auto-backtest. Until this fires, the
    table is populated nightly but the model uses raw xG.
    """
    log.info("scheduler: activating Fix B — opponent-adjusted xG")
    try:
        env_path = os.path.join(os.path.dirname(__file__), "..", ".env")
        env_path = os.path.abspath(env_path)
        # Set or update the flag in .env
        try:
            existing = open(env_path).read() if os.path.exists(env_path) else ""
        except Exception:
            existing = ""
        new_lines = []
        replaced = False
        for line in existing.splitlines():
            if line.startswith("OPPONENT_ADJUSTED_XG="):
                new_lines.append("OPPONENT_ADJUSTED_XG=true")
                replaced = True
            else:
                new_lines.append(line)
        if not replaced:
            new_lines.append("OPPONENT_ADJUSTED_XG=true")
        try:
            with open(env_path, "w") as f:
                f.write("\n".join(new_lines) + "\n")
            log.info("Fix B activation: wrote OPPONENT_ADJUSTED_XG=true to %s", env_path)
        except Exception as e:
            log.exception("Fix B activation: writing .env failed: %s", e)

        # Notify via email — flag the operator that a service restart is
        # required before the model actually picks up the change.
        try:
            subject = "BetEdge — Fix B activated (opponent-adjusted xG)"
            body = (
                "OPPONENT_ADJUSTED_XG was set to true in .env at the scheduled\n"
                "time. The running uvicorn process needs a restart to pick up\n"
                "the new env value:\n\n"
                "  sudo systemctl restart betedge.service\n\n"
                "After restart, the next sync will use opponent-strength-\n"
                "adjusted xG. Run a backtest to compare:\n\n"
                "  cd /opt/betedge/backend && .venv/bin/python3 backtest.py\n\n"
                "If accuracy regresses, set OPPONENT_ADJUSTED_XG=false and\n"
                "restart again to revert."
            )
            digest.send(subject, body)
        except Exception as e:
            log.exception("Fix B activation email failed: %s", e)
    except Exception:
        log.exception("Fix B activation crashed")


_JOB_LABELS = {
    "sync_epl":             "EPL sync",
    "sync_ucl":             "UCL sync",
    "sync_ucl_final":       "UCL Final sync",
    "sync_uel":             "Europa League sync",
    "sync_world_cup":       "World Cup sync",
    "sync_la_liga":         "La Liga sync",
    "pre_kickoff_clv":      "Pre-kickoff CLV capture (live odds)",
    "auto_paper_bets":      "Auto-place paper bets (BTTS + totals over)",
    "morning_ev":           "Morning EV pre-warm",
    "morning_digest":       "Morning digest email",
    "closing_and_pnl":      "Closing lines + daily P&L + self-eval",
    "weekly_accuracy":      "Weekly accuracy snapshot",
    "monthly_calibration":  "Monthly calibration check",
    "wc_nightly_check":     "World Cup nightly check",
    "daily_status_email":   "Daily cron status email",
    "activate_fix_b":       "Activate Fix B (opponent-adjusted xG)",
    "auto_settle":          "Auto-settle open bets",
    "real_trade_audit":     "Real-trade audit refresh",
    "nightly_model_validation":       "Nightly: model validation",
    "nightly_auto_calibration":       "Nightly: auto-calibration",
    "nightly_wc_data_prep":           "Nightly: WC data prep",
    "nightly_system_health":          "Nightly: system health",
    "nightly_feature_verification":   "Nightly: feature verification",
    "nightly_wc_configuration":       "Nightly: WC configuration",
    "nightly_real_money_performance": "Nightly: real-money performance",
    "nightly_readiness_score":        "Nightly: readiness score",
    "nightly_early_wc_opportunities": "Nightly: early WC opportunities",
    "consec_failure_alert":           "Consec-failure alert (06:00)",
    "morning_report":                 "Morning automation report (08:00)",
}


def _log_job_outcome(event):
    """APScheduler listener — write one row per job execution to cron_log.
    Catches both EVENT_JOB_EXECUTED and EVENT_JOB_ERROR; success flag is
    derived from whether `event.exception` is set."""
    success = event.exception is None
    error_msg = None if success else str(event.exception)[:1000]
    duration_ms = None
    try:
        with db() as conn:
            conn.execute(
                """
                INSERT INTO cron_log (job_id, finished_at, success, error_msg, duration_ms)
                VALUES (?, datetime('now'), ?, ?, ?)
                """,
                (event.job_id, 1 if success else 0, error_msg, duration_ms),
            )
    except Exception as e:
        log.exception("cron_log insert failed for %s: %s", event.job_id, e)


async def job_daily_status_email():
    """End-of-day cadence: summarize today's cron_log rows in a single
    pass/fail email. Runs at 23:58 NY-local — three minutes after the
    closing_and_pnl job that's typically the last 'real' job of the day."""
    log.info("scheduler: daily status email")
    try:
        from datetime import datetime as _dt
        from zoneinfo import ZoneInfo
        ny_now = _dt.now(ZoneInfo(TIMEZONE))
        today_short = ny_now.strftime("%b ") + str(ny_now.day)
        # We want today in the box's clock terms. cron_log uses datetime('now')
        # which is UTC, so a 23:58 NY job runs at 03:58/04:58 UTC the NEXT day.
        # Use a 24h lookback window to capture today's NY-local runs reliably.
        with db() as conn:
            rows = conn.execute(
                """
                SELECT job_id, finished_at, success, error_msg
                FROM cron_log
                WHERE finished_at >= datetime('now', '-24 hours')
                ORDER BY finished_at
                """,
            ).fetchall()
        rows = [dict(r) for r in rows]
        passed = [r for r in rows if r["success"]]
        failed = [r for r in rows if not r["success"]]

        if failed:
            subject = f"BetEdge cron status — {today_short} — {len(failed)} job{'s' if len(failed) != 1 else ''} FAILED ({len(passed)}/{len(rows)} OK)"
        elif rows:
            subject = f"BetEdge cron status — {today_short} — all {len(rows)} jobs OK"
        else:
            subject = f"BetEdge cron status — {today_short} — no jobs ran in the last 24h"

        lines = [f"=== CRON STATUS — last 24h ==="]
        if not rows:
            lines.append("No jobs ran. Scheduler may be down — check `systemctl status betedge`.")
        for r in rows:
            label = _JOB_LABELS.get(r["job_id"], r["job_id"])
            mark = "✓" if r["success"] else "✗"
            ts = (r["finished_at"] or "")[5:16].replace(" ", " @ ")  # 'MM-DD @ HH:MM'
            lines.append(f"  {mark} {label:<40} {ts} UTC")
            if not r["success"] and r["error_msg"]:
                lines.append(f"      → {r['error_msg'][:200]}")

        if failed:
            lines.append("")
            lines.append("Investigate failed jobs via:")
            lines.append("  sudo journalctl -u betedge -n 200 | grep -i error")

        body = "\n".join(lines)
        result = digest.send(subject, body)
        log.info("daily status email: sent=%s reason=%s", result.get("sent"), result.get("reason"))
    except Exception:
        log.exception("daily status email crashed")


# ---------- lifecycle ----------


def build() -> AsyncIOScheduler:
    s = AsyncIOScheduler(timezone=TIMEZONE)
    schedule = [
        ("sync_epl",         _league_sync_job("epl"),       CronTrigger(hour=0,  minute=0,  timezone=TIMEZONE)),
        # UCL games are played Tue/Wed (matchdays) — only sync those mornings.
        # The Final is a fixed date (Sat 2026-05-30 in Budapest); a one-shot
        # cron at 00:00 NY-local on that date pulls fresh data the morning of.
        ("sync_ucl",         _league_sync_job("ucl"),       CronTrigger(day_of_week="tue,wed", hour=1, minute=0, timezone=TIMEZONE)),
        ("sync_ucl_final",   _league_sync_job("ucl"),       CronTrigger(year=2026, month=5, day=30, hour=0, minute=0, timezone=TIMEZONE)),
        ("sync_uel",         _league_sync_job("uel"),       CronTrigger(hour=2,  minute=0,  timezone=TIMEZONE)),
        ("sync_la_liga",     _league_sync_job("la_liga"),   CronTrigger(hour=0,  minute=30, timezone=TIMEZONE)),
        ("auto_settle",           job_auto_settle_open_bets,     CronTrigger(hour=2,  minute=30, timezone=TIMEZONE)),
        # Settle ALL past fixtures + pre-fetch their xG stats BEFORE the
        # WC sync runs, so the model has real data instead of fallbacks.
        ("settle_fixtures",        job_settle_fixtures,          CronTrigger(hour=2,  minute=45, timezone=TIMEZONE)),
        ("sync_world_cup",         _league_sync_job("world_cup"), CronTrigger(hour=3,  minute=0,  timezone=TIMEZONE)),
        # Mid-day: same pattern — settle first, then sync.
        ("settle_fixtures_midday", job_settle_fixtures,          CronTrigger(hour=11, minute=45, timezone=TIMEZONE)),
        ("sync_world_cup_midday",  _league_sync_job("world_cup"), CronTrigger(hour=12, minute=0,  timezone=TIMEZONE)),
        ("real_trade_audit",      job_real_trade_audit,          CronTrigger(hour=2,  minute=45, timezone=TIMEZONE)),
        # Spec section 2 — 9-task nightly automation pipeline (May 31 → June 10).
        # All tasks log to automation_log; deferred tasks re-arm automatically
        # when their preconditions are met. The 06:00 watchdog escalates any
        # 3-consecutive-failure streak to email; the 08:00 morning report
        # rolls up overnight pass/fail, real-money status, and readiness score.
        ("nightly_model_validation",       job_nightly_model_validation,       CronTrigger(hour=0,  minute=0,  timezone=TIMEZONE)),
        ("nightly_auto_calibration",       job_nightly_auto_calibration,       CronTrigger(hour=0,  minute=30, timezone=TIMEZONE)),
        ("nightly_clv_feedback",           job_clv_feedback_eval,              CronTrigger(hour=23, minute=50, timezone=TIMEZONE)),
        ("weekly_tactical_suppressor",     job_weekly_tactical_suppressor_detect, CronTrigger(day_of_week="sun", hour=4, minute=15, timezone=TIMEZONE)),
        ("nightly_wc_data_prep",           job_nightly_wc_data_prep,           CronTrigger(hour=1,  minute=0,  timezone=TIMEZONE)),
        ("nightly_system_health",          job_nightly_system_health,          CronTrigger(hour=1,  minute=30, timezone=TIMEZONE)),
        ("nightly_feature_verification",   job_nightly_feature_verification,   CronTrigger(hour=2,  minute=0,  timezone=TIMEZONE)),
        ("nightly_wc_configuration",       job_nightly_wc_configuration,       CronTrigger(hour=2,  minute=15, timezone=TIMEZONE)),
        ("nightly_real_money_performance", job_nightly_real_money_performance, CronTrigger(hour=3,  minute=0,  timezone=TIMEZONE)),
        ("nightly_readiness_score",        job_nightly_readiness_score,        CronTrigger(hour=4,  minute=0,  timezone=TIMEZONE)),
        ("nightly_early_wc_opportunities", job_nightly_early_wc_opportunities, CronTrigger(hour=5,  minute=0,  timezone=TIMEZONE)),
        ("consec_failure_alert",           job_consec_failure_alert,           CronTrigger(hour=6,  minute=0,  timezone=TIMEZONE)),
        ("morning_report",                 job_morning_report,                 CronTrigger(hour=8,  minute=0,  timezone=TIMEZONE)),
        ("morning_ev",            job_morning_ev,                CronTrigger(hour=6,  minute=0,  timezone=TIMEZONE)),
        ("auto_paper_bets",       job_auto_paper_bets,           CronTrigger(hour=6,  minute=15, timezone=TIMEZONE)),
        ("morning_digest",        job_morning_digest,            CronTrigger(hour=8,  minute=0,  timezone=TIMEZONE)),
        ("pre_kickoff_clv",       job_pre_kickoff_clv,           IntervalTrigger(minutes=30)),
        ("closing_and_pnl",       job_closing_lines_and_pnl,     CronTrigger(hour=23, minute=55, timezone=TIMEZONE)),
        # End-of-day pass/fail summary email — runs 3 min after closing_and_pnl
        # so the last 'real' job's outcome is in the cron_log it reads from.
        ("daily_status_email",    job_daily_status_email,        CronTrigger(hour=23, minute=58, timezone=TIMEZONE)),
        # Weekly Sun 04:00 NY (right after the daily syncs and before the digest)
        ("weekly_accuracy",       job_weekly_accuracy_snapshot,  CronTrigger(day_of_week="sun", hour=4, minute=0, timezone=TIMEZONE)),
        # Monthly 1st 04:00 NY
        ("monthly_calibration",   job_monthly_calibration_check, CronTrigger(day=1,            hour=4, minute=0, timezone=TIMEZONE)),
        # Nightly 04:30 NY — silent unless WC phase transitioned
        ("wc_nightly_check",      job_nightly_wc_check,          CronTrigger(hour=4, minute=30, timezone=TIMEZONE)),
        # Fix B (opponent-adjusted xG) auto-activation — one-shot at
        # 2026-07-20 00:01 NY-local (post-World-Cup-Final). Flips the
        # OPPONENT_ADJUSTED_XG flag in .env and emails the operator.
        # After this fires it never re-triggers (year/month/day all match
        # exactly once).
        ("activate_fix_b",        job_activate_opponent_adjusted_xg, CronTrigger(year=2026, month=7, day=20, hour=0, minute=1, timezone=TIMEZONE)),
    ]
    for jid, fn, trig in schedule:
        s.add_job(fn, trig, id=jid, replace_existing=True)
    s.add_listener(_log_job_outcome, EVENT_JOB_EXECUTED | EVENT_JOB_ERROR)
    return s


def start() -> dict:
    global _scheduler
    if _scheduler and _scheduler.running:
        return {"running": True, "note": "already running"}
    _scheduler = build()
    _scheduler.start()
    return status()


def stop() -> dict:
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        _scheduler = None
        return {"running": False}
    return {"running": False, "note": "was not running"}


def status() -> dict:
    if not _scheduler or not _scheduler.running:
        return {"running": False, "enabled_at_boot": ENABLED, "timezone": TIMEZONE}
    jobs = []
    for j in _scheduler.get_jobs():
        jobs.append({
            "id": j.id,
            "next_run": j.next_run_time.isoformat() if j.next_run_time else None,
            "trigger": str(j.trigger),
        })
    return {"running": True, "enabled_at_boot": ENABLED, "timezone": TIMEZONE, "jobs": jobs}


def auto_start_if_enabled() -> dict:
    if ENABLED:
        return start()
    return {"running": False, "enabled_at_boot": False, "note": "set SCHEDULER_ENABLED=true to auto-start"}
