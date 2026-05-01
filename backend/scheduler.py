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

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

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


# ---------- lifecycle ----------


def build() -> AsyncIOScheduler:
    s = AsyncIOScheduler(timezone=TIMEZONE)
    schedule = [
        ("sync_epl",         _league_sync_job("epl"),       CronTrigger(hour=0,  minute=0,  timezone=TIMEZONE)),
        ("sync_ucl",         _league_sync_job("ucl"),       CronTrigger(hour=1,  minute=0,  timezone=TIMEZONE)),
        ("sync_uel",         _league_sync_job("uel"),       CronTrigger(hour=2,  minute=0,  timezone=TIMEZONE)),
        ("sync_world_cup",   _league_sync_job("world_cup"), CronTrigger(hour=3,  minute=0,  timezone=TIMEZONE)),
        ("morning_ev",            job_morning_ev,                CronTrigger(hour=6,  minute=0,  timezone=TIMEZONE)),
        ("morning_digest",        job_morning_digest,            CronTrigger(hour=8,  minute=0,  timezone=TIMEZONE)),
        ("closing_and_pnl",       job_closing_lines_and_pnl,     CronTrigger(hour=23, minute=55, timezone=TIMEZONE)),
        # Weekly Sun 04:00 NY (right after the daily syncs and before the digest)
        ("weekly_accuracy",       job_weekly_accuracy_snapshot,  CronTrigger(day_of_week="sun", hour=4, minute=0, timezone=TIMEZONE)),
        # Monthly 1st 04:00 NY
        ("monthly_calibration",   job_monthly_calibration_check, CronTrigger(day=1,            hour=4, minute=0, timezone=TIMEZONE)),
        # Nightly 04:30 NY — silent unless WC phase transitioned
        ("wc_nightly_check",      job_nightly_wc_check,          CronTrigger(hour=4, minute=30, timezone=TIMEZONE)),
    ]
    for jid, fn, trig in schedule:
        s.add_job(fn, trig, id=jid, replace_existing=True)
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
