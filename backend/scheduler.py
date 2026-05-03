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
    "morning_ev":           "Morning EV pre-warm",
    "morning_digest":       "Morning digest email",
    "closing_and_pnl":      "Closing lines + daily P&L + self-eval",
    "weekly_accuracy":      "Weekly accuracy snapshot",
    "monthly_calibration":  "Monthly calibration check",
    "wc_nightly_check":     "World Cup nightly check",
    "daily_status_email":   "Daily cron status email",
    "activate_fix_b":       "Activate Fix B (opponent-adjusted xG)",
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
        ny_now = _dt.now(TIMEZONE)
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
        ("sync_world_cup",   _league_sync_job("world_cup"), CronTrigger(hour=3,  minute=0,  timezone=TIMEZONE)),
        ("morning_ev",            job_morning_ev,                CronTrigger(hour=6,  minute=0,  timezone=TIMEZONE)),
        ("morning_digest",        job_morning_digest,            CronTrigger(hour=8,  minute=0,  timezone=TIMEZONE)),
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
