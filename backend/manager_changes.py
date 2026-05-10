"""Manager-change tracking (Addition 3 — skeleton).

Full implementation requires API-Football's coaches endpoint
(/coachs?team=X) which returns coaching tenure history. For each team
we compare today's named coach against yesterday's stored value; on
mismatch we insert a manager_changes row with change_date=today and
games_since_change=0. Subsequent nightly runs increment the counter.

Currently scaffold only:
  - Table is in schema
  - apply_manager_change_filter(form, team_id) is the stub the predict
    pipeline calls — returns the form unchanged when no change is
    recorded; when a change exists with <5 post-change games, returns
    a flag that data_sync.py reads to force LOW confidence.
  - refresh_from_api(client) is a placeholder for the nightly fetch.
    Wired to the scheduler (Sunday 04:30) but currently a no-op.
"""
from __future__ import annotations

import logging

from database import db

log = logging.getLogger("arb.manager_changes")

POST_CHANGE_FULL_TRUST = 8       # games after change before pre-change games count fully
POST_CHANGE_LOW_CONF_THRESHOLD = 5
PRE_CHANGE_WEIGHT = 0.10         # spec — ~ignore pre-change games


def get_for_team(team_id: int) -> dict | None:
    with db() as conn:
        row = conn.execute(
            """
            SELECT team_id, team_name, old_manager, new_manager,
                   change_date, games_since_change, last_checked
            FROM manager_changes WHERE team_id = ?
            """,
            (team_id,),
        ).fetchone()
    return dict(row) if row else None


def should_force_low_confidence(team_id: int) -> tuple[bool, str | None]:
    """Spec rule: <5 post-change games → LOW confidence + reason note."""
    rec = get_for_team(team_id)
    if not rec:
        return False, None
    n = int(rec.get("games_since_change") or 0)
    if n < POST_CHANGE_LOW_CONF_THRESHOLD:
        return True, (
            f"Manager change {n} games ago — limited post-change data"
        )
    return False, None


async def refresh_from_api(client) -> dict:
    """Placeholder — fetch /coachs per team, detect changes, upsert.
    Real implementation pending API-Football endpoint exploration."""
    return {"checked": 0, "changes": 0, "note": "manager-change refresh deferred — placeholder"}
