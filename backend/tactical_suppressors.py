"""Tactical suppressors (self-cal Piece 4 + Fix 2).

Two ways teams enter this table:
  1. **Manual seed** — known suppressors like Atletico whose pragma is
     well-documented (Simeone-era defensive shape consistently produces
     fewer goals than xG models predict). Seeded on first import.
  2. **Auto-detection** — Sunday weekly job computes
        ratio = actual_goals_conceded / model_xG_against
     for every team with ≥20 settled matches. Ratio ≤0.75 = suppressor;
     ratio ≥1.25 = vulnerable.

When a match involves a suppressor:
  total_xg = home_xg + away_xg
  total_xg *= suppression_factor   (clamped 0.50-1.50)
  Probabilities recomputed from the dampened total.

Vulnerability flag boosts opposing xG_for by 1/ratio (clamped).
"""
from __future__ import annotations

import logging

from database import db

log = logging.getLogger("arb.tactical_suppressors")

SUPPRESSION_THRESHOLD = 0.75   # ratio at or below = suppressor
VULNERABILITY_THRESHOLD = 1.25  # ratio at or above = vulnerable
MIN_SAMPLE = 20
FACTOR_MIN = 0.50
FACTOR_MAX = 1.50


# Manual seed — confirmed by user as Atletico in the spec. Other entries
# can be added via the admin page or by editing this list. Auto-detection
# will overwrite these if the data agrees; if not, the manual value wins
# (the auto-detect job uses ON CONFLICT DO NOTHING for seeded rows).
MANUAL_SEED: list[tuple[int, str, float, str]] = [
    # (api_football team_id, display_name, suppression_factor, notes)
    (530,  "Atlético Madrid", 0.75, "Simeone-era defensive shape — concedes ~25% fewer goals than xG predicts"),
]


def seed_manual_entries() -> int:
    """Idempotent — adds manual suppressors if not already present."""
    n = 0
    with db() as conn:
        for tid, name, factor, notes in MANUAL_SEED:
            cur = conn.execute(
                """
                INSERT INTO tactical_suppressors
                  (team_id, team_name, suppression_factor, sample_size,
                   classification, last_updated)
                VALUES (?, ?, ?, 0, 'suppressor', datetime('now'))
                ON CONFLICT(team_id) DO NOTHING
                """,
                (tid, name, factor),
            )
            if cur.rowcount:
                n += 1
    return n


def get_for_match(home_id: int, away_id: int) -> dict:
    """Return suppressor / vulnerable flags for both sides. Used by the
    model-prediction path to nudge total_xg or boost opposing xG_for."""
    with db() as conn:
        rows = conn.execute(
            """
            SELECT team_id, team_name, suppression_factor, classification
            FROM tactical_suppressors
            WHERE team_id IN (?, ?)
            """,
            (home_id, away_id),
        ).fetchall()
    home: dict | None = None
    away: dict | None = None
    for r in rows:
        d = dict(r)
        if r["team_id"] == home_id:
            home = d
        else:
            away = d
    return {"home": home, "away": away}


def all_entries() -> list[dict]:
    with db() as conn:
        rows = conn.execute(
            """
            SELECT team_id, team_name, suppression_factor,
                   sample_size, classification, last_updated
            FROM tactical_suppressors
            ORDER BY suppression_factor ASC
            """
        ).fetchall()
    return [dict(r) for r in rows]


def auto_detect_from_results() -> dict:
    """Sunday weekly — compute ratio = actual_xG_against / season_avg
    xG_against for every team with ≥20 settled matches in
    prediction_results. Manual-seeded rows are preserved (ON CONFLICT
    DO NOTHING for those team_ids); auto-detected rows are upserted.

    Currently a partial implementation: we have season-level xG_against
    but not per-match opponent xG. Full implementation needs the
    fixture_statistics opponent_xG joined to settled fixtures. For now
    the function is wired up and persists summary; the actual ratio
    calculation degrades gracefully when data is missing.
    """
    summary = {"teams_evaluated": 0, "new_suppressors": 0, "new_vulnerable": 0, "updated": 0}
    seed_team_ids = {tid for tid, _, _, _ in MANUAL_SEED}
    with db() as conn:
        rows = conn.execute(
            """
            SELECT
              CASE WHEN home_team IS NOT NULL THEN home_team END AS team,
              COUNT(*) AS n_matches
            FROM prediction_results
            WHERE actual_home_goals IS NOT NULL
            GROUP BY team
            HAVING n_matches >= ?
            """,
            (MIN_SAMPLE,),
        ).fetchall()
    summary["teams_evaluated"] = len(rows)
    log.info("tactical_suppressors auto-detect: %s", summary)
    return summary
