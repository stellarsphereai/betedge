"""Daily API call quota tracking.

Persists a per-day call counter in SQLite. Every API-Football request increments
it; before firing a request we check we're not over the daily limit. When the
counter crosses a warning threshold for the first time on a given day, an
email warning is sent (once per day).

Designed to fail-safe: if the quota row can't be read, we *allow* the call
rather than block. Saving the user from a runaway script is the priority,
not pedantic accounting.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

from database import db

log = logging.getLogger("arb.quota")

DEFAULT_DAILY_LIMIT = int(os.getenv("API_QUOTA_DAILY_LIMIT", "6000"))
DEFAULT_WARN_THRESHOLD = int(os.getenv("API_QUOTA_WARN_THRESHOLD", "5000"))


class QuotaExceeded(RuntimeError):
    """Raised when we hit the daily API call limit."""


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def today_count() -> int:
    try:
        with db() as conn:
            row = conn.execute(
                "SELECT calls FROM api_quota WHERE date = ?", (_today(),)
            ).fetchone()
        return row["calls"] if row else 0
    except Exception:
        return 0


def state() -> dict:
    """Snapshot of today's quota state for the admin/status panel."""
    today = _today()
    try:
        with db() as conn:
            row = conn.execute(
                "SELECT calls, warning_sent FROM api_quota WHERE date = ?", (today,)
            ).fetchone()
    except Exception as e:
        return {"date": today, "calls": 0, "limit": DEFAULT_DAILY_LIMIT, "error": str(e)}

    calls = row["calls"] if row else 0
    return {
        "date": today,
        "calls": calls,
        "limit": DEFAULT_DAILY_LIMIT,
        "warn_threshold": DEFAULT_WARN_THRESHOLD,
        "remaining": max(0, DEFAULT_DAILY_LIMIT - calls),
        "warning_sent": bool(row["warning_sent"]) if row else False,
        "exceeded": calls >= DEFAULT_DAILY_LIMIT,
    }


def check_or_raise() -> None:
    """Pre-call gate. Raises QuotaExceeded if today's count is at/above limit."""
    if today_count() >= DEFAULT_DAILY_LIMIT:
        raise QuotaExceeded(
            f"daily API quota of {DEFAULT_DAILY_LIMIT} reached — sync blocked until midnight UTC"
        )


def increment() -> int:
    """Add 1 to today's counter. Returns the new value. If we just crossed the
    warn threshold, dispatch a one-shot email."""
    today = _today()
    new_count: int
    should_warn = False
    try:
        with db() as conn:
            conn.execute(
                """
                INSERT INTO api_quota (date, calls) VALUES (?, 1)
                ON CONFLICT(date) DO UPDATE SET calls = calls + 1
                """,
                (today,),
            )
            row = conn.execute(
                "SELECT calls, warning_sent FROM api_quota WHERE date = ?", (today,)
            ).fetchone()
            new_count = row["calls"]
            if (
                new_count >= DEFAULT_WARN_THRESHOLD
                and not row["warning_sent"]
            ):
                conn.execute(
                    "UPDATE api_quota SET warning_sent = 1 WHERE date = ?", (today,)
                )
                should_warn = True
    except Exception as e:
        log.warning("quota increment failed: %s", e)
        return 0

    if should_warn:
        _dispatch_warning_email(new_count)
    return new_count


def _dispatch_warning_email(used: int) -> None:
    """Lazy import digest to avoid a hard dependency at module load."""
    try:
        import digest
        subject = f"BetEdge NY: API quota warning — {used}/{DEFAULT_DAILY_LIMIT}"
        body = (
            f"Today's API-Football call count crossed the warning threshold of "
            f"{DEFAULT_WARN_THRESHOLD}.\n\n"
            f"Used so far  : {used}\n"
            f"Daily limit  : {DEFAULT_DAILY_LIMIT}\n"
            f"Remaining    : {DEFAULT_DAILY_LIMIT - used}\n\n"
            f"All scheduled syncs will be blocked once the limit is reached. "
            f"They resume automatically at midnight UTC.\n"
        )
        result = digest.send(subject, body)
        log.info("quota warning email: %s", result)
    except Exception as e:
        log.warning("could not send quota warning email: %s", e)
