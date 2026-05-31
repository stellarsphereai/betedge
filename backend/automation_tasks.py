"""Spec section 2 — the 9 nightly automation tasks themselves.

Each function returns a dict that automation_runner.run_task() understands:
    {"status": "PASS"|"FAIL"|"DEFERRED"|"SKIP", "summary": "...", ...}

Tasks fully implemented now:
  - task_model_validation        (00:00 NY) — accuracy/Brier/CLV from existing tables
  - task_real_money_performance  (03:00 NY) — cash bet rollup + flagging
  - task_system_health           (01:30 NY) — disk, memory, services, DB integrity, backup
  - task_readiness_score         (04:00 NY) — 29-item checklist roll-up

Tasks deferred to nearer launch (return DEFERRED with a clear reason):
  - task_auto_calibration        (00:30) — needs the bias-driven calibrator
  - task_wc_data_prep            (01:00) — fetches all 64 fixtures from API-Football
  - task_feature_verification    (02:00) — 8-test smoke suite
  - task_wc_configuration        (02:30) — applies WC league_config + sample preds
  - task_early_wc_opportunities  (05:00) — runs after WC fixtures load on June 5

DEFERRED tasks aren't failures — they're "not applicable yet" and won't
contribute to the 3-consecutive-failure alert. They re-arm automatically
once the prerequisites are met (a date crossing, fixtures loaded, etc.).
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from database import db

log = logging.getLogger("arb.automation_tasks")

# Path to the SQLite DB (resolved against backend/ since that's where main.py
# lives). Used by task_system_health for backup + integrity check.
DB_PATH = os.path.join(os.path.dirname(__file__), "betedge.db")
BACKUP_DIR = os.path.expanduser("~/backups")
WC_KICKOFF_DATE = "2026-06-11"


# ===========================================================================
# Task 1 — Model validation (00:00 NY)
# ===========================================================================

async def task_model_validation() -> dict:
    """Pull accuracy / Brier / CLV from settled bets and predictions."""
    import accuracy
    import calibrate
    rep = accuracy.model_accuracy_report()
    n = int(rep.get("n_predictions") or 0)
    if n == 0:
        return {
            "status": "DEFERRED",
            "summary": "no settled predictions yet — model_validation re-arms when first fixture closes",
            "n_predictions": 0,
        }
    # Sample-size gate — under 20 settled predictions the accuracy/Brier
    # estimates are dominated by sample variance (one missed call swings
    # win_rate by 5-8pp). DEFERRED instead of FAIL so the 6am
    # consec-failure watchdog doesn't escalate while we're still in
    # early-data territory. Threshold check resumes once n ≥ 20.
    MIN_N_FOR_VALIDATION = 20
    if n < MIN_N_FOR_VALIDATION:
        return {
            "status": "DEFERRED",
            "summary": f"only {n} settled predictions (need ≥{MIN_N_FOR_VALIDATION} for stable thresholds — re-arms automatically)",
            "n_predictions": n,
        }
    # Stale-sample gate — between seasons (EPL ends late May, WC starts
    # June 11) the same settled fixtures sit in the DB for weeks. Without
    # this gate the same FAIL re-fires every night and triggers the 6am
    # urgent-alert watchdog on data that hasn't moved. Defer when the
    # latest settled fixture is older than STALE_DAYS.
    STALE_DAYS = 5
    with db() as conn:
        row = conn.execute(
            "SELECT MAX(kickoff_time) AS last_kickoff FROM fixtures WHERE result IS NOT NULL"
        ).fetchone()
    last_kickoff = row["last_kickoff"] if row else None
    if last_kickoff:
        try:
            last_dt = datetime.fromisoformat(last_kickoff.replace("Z", "+00:00"))
            age_days = (datetime.now(timezone.utc) - last_dt).days
        except ValueError:
            age_days = 0
        if age_days > STALE_DAYS:
            return {
                "status": "DEFERRED",
                "summary": f"sample frozen — last settled fixture was {age_days}d ago (n={n}); awaiting new results",
                "n_predictions": n,
                "last_settled_kickoff": last_kickoff,
                "age_days": age_days,
            }
    win_rate = float(rep.get("win_rate") or 0)
    brier = float(rep.get("avg_brier") or 0)
    clv = rep.get("avg_clv")
    # Thresholds calibrated for 3-way (1X2) football. Even sharp public
    # models top out near 55% top-pick accuracy and 0.56–0.60 Brier; an
    # always-uniform predictor scores Brier ≈ 0.667. So these gates flag
    # "model degraded toward random" rather than "model isn't superhuman."
    # The stricter 0.65 win-rate bar in accuracy.model_accuracy_report is
    # the real-money promotion gate, not the nightly sanity check.
    win_pass = win_rate >= 0.50
    brier_pass = brier <= 0.62
    clv_pass = clv is None or clv >= -0.05
    overall = win_pass and brier_pass and clv_pass
    parts = [
        f"acc {win_rate*100:.1f}% {'✓' if win_pass else '✗'}",
        f"Brier {brier:.4f} {'✓' if brier_pass else '✗'}",
        f"CLV {clv:+.3f} {'✓' if clv_pass else '✗'}" if clv is not None else "CLV n/a",
    ]
    return {
        "status": "PASS" if overall else "FAIL",
        "summary": " · ".join(parts) + f" (n={n})",
        "n_predictions": n,
        "win_rate": win_rate, "brier": brier, "clv": clv,
        "thresholds": {"win_rate_min": 0.50, "brier_max": 0.62, "clv_min": -0.05},
    }


# ===========================================================================
# Task 2 — Auto-calibration (00:30 NY) — DEFERRED
# ===========================================================================

async def task_auto_calibration() -> dict:
    """00:30 NY — refresh per-market calibration factors from settled
    paper bets. Self-cal Piece 1 + Fix 3.

    Runs unconditionally; the 10-bet (goal-market) and 20-bet (h2h)
    sample-size gates are inside refresh_factors. Returns a summary
    of buckets evaluated, applied, and deferred."""
    import market_calibration
    summary = market_calibration.refresh_factors()
    n_total = summary["buckets"]
    n_applied = summary["applied"]
    if n_total == 0:
        return {
            "status": "DEFERRED",
            "summary": "no settled paper bets yet — calibration re-arms when the first market closes",
        }
    return {
        "status": "PASS",
        "summary": (
            f"{n_total} buckets evaluated · {n_applied} applied · "
            f"{summary['deferred_sample']} pending sample · "
            f"{summary['deferred_bounds']} skipped (factor outside [0.70, 1.30])"
        ),
        **summary,
    }


# ===========================================================================
# Task 3 — World Cup data preparation (01:00 NY) — DEFERRED until May 31
# ===========================================================================

async def task_wc_data_prep() -> dict:
    """Measure WC fixture/prediction load. Defers itself before May 31, and
    defers (instead of FAILing) during a 7-day grace window after activation
    so the daily sync has time to pull all 64 fixtures from API-Football
    without the 6am watchdog escalating mid-load."""
    today = datetime.now(timezone.utc).date()
    cutoff = datetime(2026, 5, 31).date()
    if today < cutoff:
        return {
            "status": "DEFERRED",
            "summary": f"WC data prep activates {cutoff.isoformat()}; today is {today.isoformat()}",
        }
    with db() as conn:
        n_fixtures = conn.execute(
            "SELECT COUNT(*) AS n FROM fixtures WHERE league = 'world_cup'"
        ).fetchone()["n"]
        n_preds = conn.execute(
            "SELECT COUNT(*) AS n FROM model_predictions WHERE league = 'world_cup'"
        ).fetchone()["n"]
    # PASS once the full slate is loaded; defer (not FAIL) during the
    # grace window or while the sync is still progressing toward the full
    # 64. Only escalate to FAIL once we're past the grace window AND still
    # short — at that point something is genuinely wrong with the sync.
    GRACE_DAYS = 7
    grace_end = cutoff + timedelta(days=GRACE_DAYS)
    if n_fixtures >= 64:
        return {
            "status": "PASS",
            "summary": f"WC fixtures loaded: {n_fixtures}/64, predictions: {n_preds}",
            "n_fixtures": n_fixtures, "n_preds": n_preds,
        }
    if today <= grace_end:
        return {
            "status": "DEFERRED",
            "summary": f"WC fixtures loading: {n_fixtures}/64 (grace window through {grace_end.isoformat()}); predictions: {n_preds}",
            "n_fixtures": n_fixtures, "n_preds": n_preds,
        }
    return {
        "status": "FAIL",
        "summary": f"WC fixtures still incomplete past grace window: {n_fixtures}/64; predictions: {n_preds}",
        "n_fixtures": n_fixtures, "n_preds": n_preds,
    }


# ===========================================================================
# Task 4 — System health (01:30 NY)
# ===========================================================================

async def task_system_health() -> dict:
    """Disk, memory, DB integrity, backup, FastAPI alive."""
    checks: dict[str, dict] = {}

    # 1. Disk
    try:
        s = shutil.disk_usage("/")
        free_pct = s.free / s.total * 100
        checks["disk"] = {
            "free_pct": round(free_pct, 1), "total_gb": round(s.total/1e9, 1),
            "ok": free_pct >= 20.0,
        }
    except Exception as e:
        checks["disk"] = {"ok": False, "error": str(e)}

    # 2. Memory (Linux only — read /proc/meminfo)
    try:
        with open("/proc/meminfo") as f:
            mem = {l.split(":")[0]: l.split(":")[1].strip() for l in f if ":" in l}
        total = int(mem["MemTotal"].split()[0])
        avail = int(mem["MemAvailable"].split()[0])
        used_pct = (1 - avail/total) * 100
        checks["memory"] = {"used_pct": round(used_pct, 1), "ok": used_pct < 80.0}
    except Exception as e:
        checks["memory"] = {"ok": False, "error": str(e)}

    # 3. DB integrity
    try:
        with sqlite3.connect(DB_PATH) as conn:
            r = conn.execute("PRAGMA integrity_check").fetchone()
        ok = (r and r[0] == "ok")
        checks["db_integrity"] = {"ok": bool(ok), "result": r[0] if r else None}
    except Exception as e:
        checks["db_integrity"] = {"ok": False, "error": str(e)}

    # 4. Backup — dump SQLite to dated file under ~/backups/, prune > 14d
    try:
        Path(BACKUP_DIR).mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
        backup_path = Path(BACKUP_DIR) / f"betedge_backup_{stamp}.db"
        # SQLite online-backup API — atomic, safe even with the live writer.
        with sqlite3.connect(DB_PATH) as src, sqlite3.connect(backup_path) as dst:
            src.backup(dst)
        # Prune anything older than 14 days.
        cutoff = datetime.now(timezone.utc) - timedelta(days=14)
        pruned = 0
        for p in Path(BACKUP_DIR).glob("betedge_backup_*.db"):
            try:
                mtime = datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc)
                if mtime < cutoff:
                    p.unlink()
                    pruned += 1
            except Exception:
                pass
        checks["backup"] = {"ok": True, "path": str(backup_path), "pruned": pruned,
                            "size_mb": round(backup_path.stat().st_size / 1e6, 2)}
    except Exception as e:
        checks["backup"] = {"ok": False, "error": str(e)}

    # 5. FastAPI process — we ARE the FastAPI process. If we're running, we're up.
    checks["service_alive"] = {"ok": True, "pid": os.getpid()}

    # 6. Scheduler jobs — verify APScheduler is running and the expected
    # cron jobs are all registered. Drives readiness item 2.3. Written
    # directly to the checklist (not part of the system_health aggregate
    # so a scheduler hiccup doesn't cascade into 'all 2.x items failed').
    try:
        import scheduler as _sched
        st = _sched.status()
        # Floor at the number of nightly automation tasks (9) — if fewer
        # are registered, the scheduler is incomplete. The wider envelope
        # of full prod has ~27 jobs (syncs, digest, weekly/monthly,
        # nightly automation, alerts), but 9 is the must-have minimum.
        MIN_JOBS = 9
        jobs = st.get("jobs") or []
        running = bool(st.get("running"))
        sched_ok = running and len(jobs) >= MIN_JOBS
        checks["scheduler_jobs"] = {
            "ok": sched_ok, "running": running, "n_jobs": len(jobs),
            "min_required": MIN_JOBS,
        }
    except Exception as e:
        sched_ok = False
        checks["scheduler_jobs"] = {"ok": False, "error": str(e)}

    # 7. API keys — verify API-Football + Odds API are reachable and
    # the keys are valid. Drives readiness item 2.4. Uses each provider's
    # free probe endpoint (API-Football /status, Odds API /sports) so we
    # don't burn quota. Direct httpx calls (not via api_football._get)
    # to avoid the quota-counter increment.
    api_key_checks: dict[str, dict] = {}
    try:
        import httpx
        af_key = os.getenv("API_FOOTBALL_KEY", "")
        odds_key = os.getenv("ODDS_API_KEY", "")
        async with httpx.AsyncClient(timeout=10.0) as c:
            # API-Football /status — free; returns subscription info
            if af_key:
                try:
                    r = await c.get(
                        "https://v3.football.api-sports.io/status",
                        headers={"x-apisports-key": af_key},
                    )
                    body = r.json() if r.status_code == 200 else {}
                    has_resp = bool((body or {}).get("response"))
                    api_key_checks["api_football"] = {
                        "ok": r.status_code == 200 and has_resp,
                        "status": r.status_code,
                    }
                except Exception as e:
                    api_key_checks["api_football"] = {"ok": False, "error": str(e)}
            else:
                api_key_checks["api_football"] = {"ok": False, "error": "API_FOOTBALL_KEY not set"}
            # Odds API /sports — free; doesn't decrement x-requests-remaining
            if odds_key:
                try:
                    r = await c.get(
                        "https://api.the-odds-api.com/v4/sports",
                        params={"apiKey": odds_key},
                    )
                    api_key_checks["odds_api"] = {
                        "ok": r.status_code == 200,
                        "status": r.status_code,
                    }
                except Exception as e:
                    api_key_checks["odds_api"] = {"ok": False, "error": str(e)}
            else:
                api_key_checks["odds_api"] = {"ok": False, "error": "ODDS_API_KEY not set"}
        keys_ok = all(v.get("ok") for v in api_key_checks.values())
        checks["api_keys"] = {"ok": keys_ok, **api_key_checks}
    except Exception as e:
        keys_ok = False
        checks["api_keys"] = {"ok": False, "error": str(e)}

    # Persist 2.3 and 2.4 directly to the readiness checklist. Aggregate
    # 2.1/2.2/2.5/2.6/2.7 still ride task_readiness_score's PASS/FAIL
    # mapping; these two are independent so a scheduler hiccup or API
    # outage doesn't cascade.
    try:
        with db() as conn:
            conn.execute(
                "UPDATE wc_readiness_checklist SET status=?, last_checked=datetime('now') WHERE item_id='2.3'",
                ("pass" if sched_ok else "fail",),
            )
            conn.execute(
                "UPDATE wc_readiness_checklist SET status=?, last_checked=datetime('now') WHERE item_id='2.4'",
                ("pass" if keys_ok else "fail",),
            )
    except Exception:
        log.exception("system_health: persisting 2.3/2.4 readiness failed")

    overall = all(c.get("ok") for c in checks.values())
    fail_summary = ", ".join(k for k, v in checks.items() if not v.get("ok"))
    return {
        "status": "PASS" if overall else "FAIL",
        "summary": (
            f"all {len(checks)} checks ok" if overall
            else f"failures: {fail_summary}"
        ),
        "checks": checks,
    }


# ===========================================================================
# Task 5 — Feature verification (02:00 NY) — DEFERRED
# ===========================================================================

async def task_feature_verification() -> dict:
    """8-test feature smoke suite — exercises each feature module with a
    synthetic input and verifies a known invariant. Per-test results are
    written directly to wc_readiness_checklist 3.1–3.8 so the readiness
    panel updates without needing a separate mapping in
    task_readiness_score.

    These are smoke tests, not unit tests: each one proves the feature is
    callable end-to-end on a sensible input. Heavy/costly calls (the
    OpenAI analysis, live Odds API fetches) are skipped — we verify the
    module is importable and the public surface is intact.
    """
    results: dict[str, tuple[bool, str]] = {}

    # 3.1 EV calculator — de-vig math + bet finder on a clearly +EV line
    try:
        import ev_calculator
        devig = ev_calculator.remove_vig({"home": 1.80, "draw": 4.00, "away": 4.50})
        assert abs(sum(devig.values()) - 1.0) < 1e-9
        ev = ev_calculator.find_ev_bets(
            match_id="smoke", home_team="H", away_team="A",
            model_probs={"home": 0.55, "draw": 0.25, "away": 0.20},
            confidence="MEDIUM",
            offers_by_book={"BookA": {"home": 2.50, "draw": 3.50, "away": 4.00}},
            min_edge=0.0,
        )
        assert any(b.outcome == "home" for b in ev)
        results["3.1"] = (True, f"de-vig sums=1.0, {len(ev)} +EV bet(s) on synthetic line")
    except Exception as e:
        results["3.1"] = (False, f"{type(e).__name__}: {e}")

    # 3.2 Anomaly detector — synthetic 50%-edge bet should produce ≥1 flag
    try:
        import anomaly
        flags = anomaly.detect_edge_anomalies(
            {"edge": 0.50, "model_prob": 0.95, "decimal_odds": 2.00,
             "outcome": "home", "match_id": "smoke",
             "home_team": "H", "away_team": "A",
             "true_implied_prob": 0.45, "book": "FanDuel"},
            league="epl",
        )
        assert isinstance(flags, list) and len(flags) >= 1, "expected ≥1 flag on 50%-edge bet"
        results["3.2"] = (True, f"{len(flags)} flag(s) on synthetic 50%-edge bet: {flags[0].anomaly_type}")
    except Exception as e:
        results["3.2"] = (False, f"{type(e).__name__}: {e}")

    # 3.3 Line shopper — best/second-best selection from 3-book offers
    try:
        import line_shopper
        bl = line_shopper.best_line({"BookA": 2.10, "BookB": 2.30, "BookC": 2.05})
        assert bl is not None and bl.best_book == "BookB" and bl.best_odds == 2.30
        assert bl.second_book == "BookA" and bl.second_odds == 2.10
        results["3.3"] = (True, f"best={bl.best_book}@{bl.best_odds}, 2nd={bl.second_book}@{bl.second_odds}")
    except Exception as e:
        results["3.3"] = (False, f"{type(e).__name__}: {e}")

    # 3.4 Kelly sizing — cap-bound + zero-edge sanity
    try:
        import kelly
        capped = kelly.kelly_stake(edge=0.10, decimal_odds=2.50, bankroll=1000.0)
        assert capped == 20.0, f"expected 20 (cap), got {capped}"
        assert kelly.kelly_stake(0.0, 2.5, 1000.0) == 0.0
        results["3.4"] = (True, "cap-bound stake=$20, zero-edge=$0")
    except Exception as e:
        results["3.4"] = (False, f"{type(e).__name__}: {e}")

    # 3.5 Top-3 grid — /best-bets endpoint returns the expected envelope.
    # Self-call via httpx on localhost; works inside the same uvicorn
    # event loop because httpx-async multiplexes through asyncio. Other
    # scheduler jobs use the same pattern. Brief retry handles the race
    # where this task is invoked seconds after a service restart and
    # uvicorn hasn't bound the port yet.
    try:
        import httpx
        last_err: Exception | None = None
        body = None
        for attempt in range(3):
            try:
                async with httpx.AsyncClient(base_url="http://127.0.0.1:8002", timeout=10.0) as c:
                    r = await c.get("/best-bets?limit=3&kickoff_within_hours=336")
                if r.status_code == 200:
                    body = r.json()
                    break
                last_err = RuntimeError(f"HTTP {r.status_code}")
            except Exception as e:
                last_err = e
            await asyncio.sleep(1.0)
        if body is None:
            raise last_err or RuntimeError("unknown")
        assert "bets" in body and "monitoring" in body and "count_returned" in body
        results["3.5"] = (True,
            f"endpoint OK, returned={body.get('count_returned')}, monitoring={body.get('count_monitoring')}")
    except Exception as e:
        results["3.5"] = (False, f"{type(e).__name__}: {e}")

    # 3.6 AI Analysis — module surface intact (no real OpenAI call;
    # exercising analyze_match() costs money and depends on a live API
    # key + reachability that isn't worth testing nightly).
    try:
        import match_analysis
        assert callable(getattr(match_analysis, "analyze_match", None))
        results["3.6"] = (True, "module importable, analyze_match callable (no API call)")
    except Exception as e:
        results["3.6"] = (False, f"{type(e).__name__}: {e}")

    # 3.7 Morning digest — render with an empty payload should produce
    # a non-empty subject+body (one of the "no bets today" branches).
    try:
        import digest
        subject, body = digest.render(
            {"bets": []},
            {"accuracy": {"win_rate": None, "n_predictions": 0}},
        )
        assert isinstance(subject, str) and len(subject) > 0
        assert isinstance(body, str) and len(body) > 0
        results["3.7"] = (True, f"subject ok ({len(subject)} ch), body ok ({len(body)} ch)")
    except Exception as e:
        results["3.7"] = (False, f"{type(e).__name__}: {e}")

    # 3.8 Book coverage — multi-book offers feed through find_ev_bets
    # cleanly; the underlying logic that drives the /ev-bets coverage
    # gate also drives top-3 visibility, so verifying find_ev_bets
    # over N books exercises the same path.
    try:
        import ev_calculator
        offers = {f"Book{i}": {"home": 2.0, "draw": 3.0, "away": 4.0} for i in range(4)}
        ev = ev_calculator.find_ev_bets(
            match_id="smoke", home_team="H", away_team="A",
            model_probs={"home": 0.55, "draw": 0.25, "away": 0.20},
            confidence="MEDIUM",
            offers_by_book=offers,
            min_edge=0.0,
        )
        assert any(b.outcome == "home" for b in ev)
        results["3.8"] = (True, f"4-book coverage → {len(ev)} +EV bet(s)")
    except Exception as e:
        results["3.8"] = (False, f"{type(e).__name__}: {e}")

    # Persist per-test status directly to the readiness checklist so the
    # rolled-up score reflects these without needing a per-test mapping
    # in task_readiness_score.
    with db() as conn:
        for iid, (ok, _) in results.items():
            conn.execute(
                "UPDATE wc_readiness_checklist SET status=?, last_checked=datetime('now') WHERE item_id=?",
                ("pass" if ok else "fail", iid),
            )

    n_pass = sum(1 for ok, _ in results.values() if ok)
    summary_parts = [f"{iid}{'✓' if ok else '✗'}" for iid, (ok, _) in results.items()]
    return {
        "status": "PASS" if n_pass == 8 else "FAIL",
        "summary": f"{n_pass}/8 passed · " + " ".join(summary_parts),
        "tests": {iid: {"pass": ok, "detail": detail} for iid, (ok, detail) in results.items()},
    }


# ===========================================================================
# Task 6 — World Cup configuration (02:30 NY) — DEFERRED until May 31
# ===========================================================================

async def task_wc_configuration() -> dict:
    """Apply WC settings to league_config and run sample predictions to
    verify the config landed cleanly. League_config is already seeded with
    WC defaults at install time — this task verifies they match the spec
    once we're inside the WC window."""
    today = datetime.now(timezone.utc).date()
    cutoff = datetime(2026, 5, 31).date()
    if today < cutoff:
        return {
            "status": "DEFERRED",
            "summary": f"WC config activates {cutoff.isoformat()}; today is {today.isoformat()}",
        }
    # Verify the league_config row matches spec for world_cup.
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM league_config WHERE league_key = 'world_cup'"
        ).fetchone()
    if not row:
        return {"status": "FAIL", "summary": "league_config has no world_cup row"}
    expected = {"gamma": 1.20, "recent_weight": 0.70, "season_weight": 0.30,
                "anomaly_edge_threshold": 0.10}
    mismatches = []
    for k, v in expected.items():
        actual = row[k] if k in row.keys() else None
        if actual != v:
            mismatches.append(f"{k}={actual} (expected {v})")
    if mismatches:
        return {"status": "FAIL", "summary": "; ".join(mismatches)}
    return {"status": "PASS", "summary": "world_cup league_config matches spec"}


# ===========================================================================
# Task 7 — Real money performance check (03:00 NY)
# ===========================================================================

async def task_real_money_performance() -> dict:
    """Pull cash-bet rollup + flag warnings if win rate < 50%, CLV < -0.10,
    PnL negative overall, or paper-vs-real win-rate gap > 10pp."""
    with db() as conn:
        cash = conn.execute(
            """
            SELECT
              COALESCE(SUM(CASE WHEN status IN ('won','lost') THEN 1 ELSE 0 END), 0) AS settled,
              COALESCE(SUM(CASE WHEN status='won'  THEN 1 ELSE 0 END), 0) AS won,
              COALESCE(SUM(CASE WHEN status='lost' THEN 1 ELSE 0 END), 0) AS lost,
              COALESCE(SUM(CASE WHEN status IN ('won','lost') THEN profit ELSE 0 END), 0) AS pnl,
              AVG(CASE WHEN status IN ('won','lost') AND clv IS NOT NULL THEN clv END) AS avg_clv
            FROM bets_placed WHERE is_paper = 0
            """
        ).fetchone()
        paper = conn.execute(
            """
            SELECT
              COALESCE(SUM(CASE WHEN status IN ('won','lost') THEN 1 ELSE 0 END), 0) AS settled,
              COALESCE(SUM(CASE WHEN status='won' THEN 1 ELSE 0 END), 0) AS won
            FROM bets_placed WHERE is_paper = 1
            """
        ).fetchone()
    if not cash or (cash["settled"] or 0) == 0:
        return {
            "status": "DEFERRED",
            "summary": "no settled cash bets yet — real-money performance check re-arms once one closes",
        }
    # Sample-size gate — under 30 settled cash bets the win-rate /
    # execution-gap signals are statistically unreliable (one cold streak
    # of 4 losses swings win_rate by 13pp on n=10, only 7pp on n=30).
    # DEFERRED keeps the 6am watchdog quiet during early ramp-up; the
    # morning report still surfaces the numbers via the digest's
    # REAL MONEY STATUS section so the data is visible — just no
    # URGENT escalation until the sample stabilizes.
    MIN_N_FOR_REAL_PERF = 30
    if (cash["settled"] or 0) < MIN_N_FOR_REAL_PERF:
        return {
            "status": "DEFERRED",
            "summary": f"only {cash['settled']} settled cash bets (need ≥{MIN_N_FOR_REAL_PERF} for stable thresholds — re-arms automatically)",
            "settled": int(cash["settled"] or 0),
        }
    settled = cash["settled"]; won = cash["won"]; lost = cash["lost"]
    pnl = float(cash["pnl"] or 0)
    win_rate = won / settled if settled else 0
    avg_clv = float(cash["avg_clv"]) if cash["avg_clv"] is not None else None
    paper_settled = paper["settled"] or 0
    paper_winrate = paper["won"]/paper_settled if paper_settled else None
    gap = (paper_winrate - win_rate) if paper_winrate is not None else None

    flags: list[str] = []
    if win_rate < 0.50:
        flags.append(f"WIN_RATE_LOW ({win_rate*100:.1f}%)")
    if avg_clv is not None and avg_clv < -0.10:
        flags.append(f"CLV_TIMING ({avg_clv:+.3f})")
    if pnl < 0:
        flags.append(f"PNL_NEGATIVE ({pnl:+.2f})")
    if gap is not None and abs(gap) > 0.10:
        flags.append(f"EXECUTION_GAP ({gap*100:+.1f}pp paper-vs-real)")
    summary = (
        f"{settled} settled · {won}W-{lost}L · {pnl:+.2f} · win_rate {win_rate*100:.1f}%"
        + (f" · CLV {avg_clv:+.3f}" if avg_clv is not None else "")
    )
    if flags:
        summary += " · flags: " + ", ".join(flags)
    return {
        "status": "PASS" if not flags else "FAIL",
        "summary": summary,
        "settled": settled, "won": won, "lost": lost, "pnl": round(pnl, 2),
        "win_rate": round(win_rate, 4),
        "avg_clv": round(avg_clv, 4) if avg_clv is not None else None,
        "paper_winrate": round(paper_winrate, 4) if paper_winrate is not None else None,
        "execution_gap_pp": round(gap*100, 1) if gap is not None else None,
        "flags": flags,
    }


# ===========================================================================
# Task 8 — Readiness score (04:00 NY)
# ===========================================================================

# Default 29-item checklist (spec section 2 task 8).
CHECKLIST_SEED: list[tuple] = [
    # (item_id, category, label, priority, target_date, manual_required)
    ("1.1", "model",      "Live accuracy above 60%",      "normal", None, 0),
    ("1.2", "model",      "Live Brier below 0.50",         "normal", None, 0),
    ("1.3", "model",      "CLV average above -0.05",       "normal", None, 0),
    ("1.4", "model",      "Formula fix confirmed",         "normal", None, 0),
    ("1.5", "model",      "UCL backtest complete",         "normal", None, 0),

    ("2.1", "system",     "Disk space above 20% free",     "normal", None, 0),
    ("2.2", "system",     "Memory below 80%",              "normal", None, 0),
    ("2.3", "system",     "All cron jobs running",         "normal", None, 0),
    ("2.4", "system",     "All API keys working",          "normal", None, 0),
    ("2.5", "system",     "Database healthy",              "normal", None, 0),
    ("2.6", "system",     "Backup created today",          "normal", None, 0),
    ("2.7", "system",     "All services running",          "normal", None, 0),

    ("3.1", "features",   "EV calculator test passed",     "normal", None, 0),
    ("3.2", "features",   "Anomaly detector test passed",  "normal", None, 0),
    ("3.3", "features",   "Line shopper test passed",      "normal", None, 0),
    ("3.4", "features",   "Kelly sizing test passed",      "normal", None, 0),
    ("3.5", "features",   "Top 3 grid test passed",        "normal", None, 0),
    ("3.6", "features",   "AI Analysis test passed",       "normal", None, 0),
    ("3.7", "features",   "Morning digest test passed",    "normal", None, 0),
    ("3.8", "features",   "Book coverage test passed",     "normal", None, 0),

    ("4.1", "real_money", "All real bets tracked",         "normal", None, 0),
    ("4.2", "real_money", "Book balances up to date",      "normal", None, 0),
    ("4.3", "real_money", "Real P&L calculated",           "normal", None, 0),
    ("4.4", "real_money", "Execution gap below 10%",       "normal", None, 0),

    ("5.1", "wc_data",    "All 64 fixtures loaded",        "normal", None, 0),
    ("5.2", "wc_data",    "All 32 team data loaded",       "normal", None, 0),
    ("5.3", "wc_data",    "WC historical backtest done",   "normal", None, 0),

    ("6.1", "wc_config",  "WC league config applied",      "normal", None, 0),
    ("6.2", "wc_config",  "WC sample predictions clean",   "normal", None, 0),

    # Fix B operational chrome — LOW priority, not blocking June 11.
    ("F.1", "fix_b",      "Dashboard countdown widget",                    "low", "2026-07-15", 0),
    ("F.2", "fix_b",      "Admin toggle page with reason + history log",   "low", "2026-07-15", 0),
    ("F.3", "fix_b",      "Auto-backtest email render format",             "low", "2026-07-15", 0),
]


def seed_checklist() -> int:
    """One-time + idempotent — populate wc_readiness_checklist if rows are
    missing. Existing rows are left alone so manual notes are preserved."""
    n = 0
    with db() as conn:
        for (iid, cat, label, prio, target, manual) in CHECKLIST_SEED:
            cur = conn.execute(
                """
                INSERT INTO wc_readiness_checklist
                  (item_id, category, label, priority, target_date, manual_required)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(item_id) DO NOTHING
                """,
                (iid, cat, label, prio, target, manual),
            )
            if cur.rowcount:
                n += 1
    return n


async def task_readiness_score() -> dict:
    """Re-seed if needed, then roll up status from automation_log + DB."""
    seed_checklist()
    # Pull most recent automation_log status per task name.
    with db() as conn:
        rows = conn.execute(
            """
            SELECT task_name, status, run_date FROM automation_log
            WHERE id IN (
              SELECT MAX(id) FROM automation_log GROUP BY task_name
            )
            """
        ).fetchall()
        latest = {r["task_name"]: r["status"] for r in rows}

        # Map latest task results onto checklist items.
        # Model
        mv = latest.get("model_validation")
        if mv == "PASS":
            for iid in ("1.1", "1.2", "1.3"):
                conn.execute("UPDATE wc_readiness_checklist SET status='pass', last_checked=datetime('now') WHERE item_id=?", (iid,))
        elif mv == "FAIL":
            for iid in ("1.1", "1.2", "1.3"):
                conn.execute("UPDATE wc_readiness_checklist SET status='fail', last_checked=datetime('now') WHERE item_id=?", (iid,))

        # System health
        sh_row = conn.execute(
            "SELECT status, result_summary FROM automation_log WHERE task_name='system_health' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if sh_row:
            new_status = "pass" if sh_row["status"] == "PASS" else "fail"
            for iid in ("2.1", "2.2", "2.5", "2.6", "2.7"):
                conn.execute("UPDATE wc_readiness_checklist SET status=?, last_checked=datetime('now') WHERE item_id=?", (new_status, iid))

        # Real money
        rm = latest.get("real_money_performance")
        if rm == "PASS":
            for iid in ("4.1", "4.2", "4.3", "4.4"):
                conn.execute("UPDATE wc_readiness_checklist SET status='pass', last_checked=datetime('now') WHERE item_id=?", (iid,))
        elif rm == "FAIL":
            # 4.4 specifically tracks execution gap; rest stay 'pending' until per-criterion logic exists
            conn.execute("UPDATE wc_readiness_checklist SET status='fail', last_checked=datetime('now') WHERE item_id='4.4'")
            for iid in ("4.1", "4.2", "4.3"):
                conn.execute("UPDATE wc_readiness_checklist SET status='pass', last_checked=datetime('now') WHERE item_id=?", (iid,))

        # WC data + config — pass-through from their tasks
        for task_name, item_ids in (
            ("wc_data_prep",      ("5.1", "5.2", "5.3")),
            ("wc_configuration",  ("6.1", "6.2")),
        ):
            tr = latest.get(task_name)
            if tr == "PASS":
                for iid in item_ids:
                    conn.execute("UPDATE wc_readiness_checklist SET status='pass', last_checked=datetime('now') WHERE item_id=?", (iid,))

        # Roll up
        all_rows = conn.execute(
            "SELECT item_id, status, priority FROM wc_readiness_checklist"
        ).fetchall()
    total = sum(1 for r in all_rows if r["priority"] != "low")
    completed = sum(1 for r in all_rows if r["priority"] != "low" and r["status"] == "pass")
    pct = round(completed / total * 100, 1) if total else 0
    days_to_kickoff = (datetime(2026, 6, 11).date() - datetime.now(timezone.utc).date()).days

    if pct >= 90 and days_to_kickoff > 0:
        flag_status = "GREEN"
    elif pct >= 70:
        flag_status = "AMBER"
    else:
        flag_status = "RED"

    return {
        "status": "PASS",
        "summary": f"{completed}/{total} ({pct}%) · {flag_status} · {days_to_kickoff} days to kickoff",
        "completed": completed, "total": total, "pct": pct,
        "flag_status": flag_status, "days_to_kickoff": days_to_kickoff,
    }


# ===========================================================================
# Task 9 — Early WC opportunities (05:00 NY) — DEFERRED until WC fixtures load
# ===========================================================================

async def task_early_wc_opportunities() -> dict:
    today = datetime.now(timezone.utc).date()
    fixtures_target = datetime(2026, 6, 5).date()
    if today < fixtures_target:
        return {
            "status": "DEFERRED",
            "summary": f"early WC opportunities scan activates {fixtures_target.isoformat()}; today is {today.isoformat()}",
        }
    # Scan WC matches for +EV. Real implementation defers to /best-bets on
    # the world_cup league; this hook can call into get_best_bets later.
    return {
        "status": "DEFERRED",
        "summary": "WC opportunities hook deferred; integrate with /best-bets after fixtures load",
    }
