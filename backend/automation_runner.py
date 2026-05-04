"""Spec section 2 — nightly automation pipeline runner.

Each task in the pipeline gets wrapped by `run_task(name, fn)` which:
  1. Times the call,
  2. Catches any exception and converts it to a FAIL row,
  3. Persists status (PASS / FAIL / DEFERRED / SKIP) + result_summary +
     error_message + duration_seconds into automation_log,
  4. Returns the summary dict so the caller (scheduler / on-demand
     endpoint) can chain decisions.

`consecutive_failures(task_name)` returns the number of consecutive most-
recent FAIL rows for one task — the 6am urgent-alert job uses this to
decide whether to escalate ("3 consecutive nights" per spec).

Status taxonomy:
  - PASS      — task completed; result_summary describes what happened
  - FAIL      — task threw or returned a status='FAIL' dict
  - DEFERRED  — task returned status='DEFERRED' (e.g. WC data prep
                before May 31 — not an error, just nothing to do yet)
  - SKIP      — task was disabled by a feature flag or config

Tasks should return a dict like:
    {"status": "PASS", "summary": "...", "metrics": {...}}
    {"status": "FAIL", "summary": "...", "error": "..."}
    {"status": "DEFERRED", "summary": "..."}
"""
from __future__ import annotations

import json
import logging
import time
import traceback
from datetime import datetime, timezone
from typing import Awaitable, Callable

from database import db

log = logging.getLogger("arb.automation")

VALID_STATUSES = {"PASS", "FAIL", "DEFERRED", "SKIP"}


async def run_task(name: str, fn: Callable[[], Awaitable[dict]]) -> dict:
    """Run one nightly task end-to-end with structured logging. `fn` is an
    async callable returning a dict shaped like {status, summary, ...}.
    """
    started = time.time()
    today = datetime.now(timezone.utc).date().isoformat()
    status = "FAIL"
    summary_text = ""
    error_text: str | None = None
    metrics: dict = {}
    try:
        result = await fn()
        if not isinstance(result, dict):
            raise TypeError(f"task {name!r} returned {type(result).__name__}, expected dict")
        status = (result.get("status") or "PASS").upper()
        if status not in VALID_STATUSES:
            raise ValueError(f"task {name!r} returned bad status {status!r}")
        summary_text = str(result.get("summary") or "")
        metrics = {k: v for k, v in result.items() if k not in ("status", "summary", "error")}
        error_text = result.get("error")
    except Exception as e:
        log.exception("automation: task %s crashed", name)
        status = "FAIL"
        summary_text = summary_text or f"crashed: {type(e).__name__}"
        error_text = traceback.format_exc()[-2000:]
    duration = time.time() - started

    # Persist + structured-log even on success, so the morning report can
    # reconstruct the night's run without a separate stream.
    log.info("automation: %s → %s (%.2fs) — %s", name, status, duration, summary_text)
    with db() as conn:
        conn.execute(
            """
            INSERT INTO automation_log
              (run_date, task_name, status, result_summary, error_message, duration_seconds)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (today, name, status, summary_text[:500], error_text[:2000] if error_text else None, round(duration, 3)),
        )
    return {
        "task_name": name, "status": status, "summary": summary_text,
        "duration_seconds": round(duration, 3), "metrics": metrics,
    }


def consecutive_failures(task_name: str) -> int:
    """How many consecutive most-recent FAIL rows this task has logged.
    Used by the 6am urgent-alert job (spec: 3 consecutive failures escalate).
    """
    with db() as conn:
        rows = conn.execute(
            """
            SELECT status FROM automation_log
            WHERE task_name = ?
            ORDER BY id DESC LIMIT 10
            """,
            (task_name,),
        ).fetchall()
    n = 0
    for r in rows:
        if r["status"] == "FAIL":
            n += 1
        else:
            break
    return n


def todays_runs() -> list[dict]:
    """All automation_log rows from today, newest first. Drives the morning
    report's "OVERNIGHT COMPLETIONS" section."""
    today = datetime.now(timezone.utc).date().isoformat()
    with db() as conn:
        rows = conn.execute(
            """
            SELECT task_name, status, result_summary, error_message,
                   duration_seconds, created_at
            FROM automation_log
            WHERE run_date = ?
            ORDER BY id ASC
            """,
            (today,),
        ).fetchall()
    return [dict(r) for r in rows]


def latest_run(task_name: str) -> dict | None:
    """Most recent run for a task, regardless of date — used by readiness
    scoring to find the most recent PASS/FAIL signal even when today's run
    hasn't fired yet."""
    with db() as conn:
        row = conn.execute(
            """
            SELECT * FROM automation_log
            WHERE task_name = ? ORDER BY id DESC LIMIT 1
            """,
            (task_name,),
        ).fetchone()
    return dict(row) if row else None
