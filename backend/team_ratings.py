"""Opponent-strength ratings + adjusted-xG helpers (Fix B foundation).

Status (as of 2026-05-03): the table is populated nightly so we always have
fresh ratings to backtest against, but the model itself reads `raw` xG by
default. Flipping `OPPONENT_ADJUSTED_XG=true` in `.env` and restarting the
service makes `_team_xg_history` adjust each game's xG before averaging.
The auto-flip is scheduled for 2026-07-20 (post-World Cup).

Adjustment formula:
  adjusted_xg_for     = raw_xg_for     × opponent_defense_rating
  adjusted_xg_against = raw_xg_against × opponent_attack_rating

Where ratings are normalized so 1.0 = league-average:
  attack_rating  = team_season_xg_for     / league_avg_xg_for
  defense_rating = league_avg_xg_against  / team_season_xg_against

A 4.0 xG_for vs a defense rated 0.65 (well below avg, weak) becomes
4.0 × 0.65 = 2.6. Same xG vs a 1.40 (strong) defense becomes 5.6 — the
model treats it as the more impressive performance it actually is.
"""
from __future__ import annotations

import logging
import os
from typing import Iterable

from database import db

log = logging.getLogger("arb.team_ratings")

# Read at module load — flipping the env var requires a service restart.
def opponent_adjustment_enabled() -> bool:
    return os.getenv("OPPONENT_ADJUSTED_XG", "false").strip().lower() in ("1", "true", "yes")


# ---- compute / store -------------------------------------------------------


def upsert_ratings_for_league(
    league_id: int,
    season: int,
    rows: Iterable[dict],
) -> int:
    """Insert/update one row per team for this (league_id, season). Each input
    dict must have: team_id, team_name, season_xg_for, season_xg_against,
    games_played. League averages are computed from the input batch (so the
    league baseline is always self-consistent with the teams we're rating).
    Returns count of upserted rows.
    """
    rows = [r for r in rows if r.get("season_xg_for") is not None and r.get("season_xg_against") is not None]
    if not rows:
        return 0

    league_avg_for = sum(r["season_xg_for"] for r in rows) / len(rows)
    league_avg_against = sum(r["season_xg_against"] for r in rows) / len(rows)
    if league_avg_for <= 0 or league_avg_against <= 0:
        return 0

    n = 0
    with db() as conn:
        for r in rows:
            attack = float(r["season_xg_for"]) / league_avg_for
            # Defense is "lower xGA = better team", so invert: league_avg / team
            defense = league_avg_against / max(0.05, float(r["season_xg_against"]))
            overall = (attack + defense) / 2.0
            conn.execute(
                """
                INSERT INTO team_ratings
                  (team_id, league_id, season, team_name, attack_rating,
                   defense_rating, overall_rating, games_played, last_updated)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                ON CONFLICT(team_id, league_id, season) DO UPDATE SET
                  team_name      = excluded.team_name,
                  attack_rating  = excluded.attack_rating,
                  defense_rating = excluded.defense_rating,
                  overall_rating = excluded.overall_rating,
                  games_played   = excluded.games_played,
                  last_updated   = datetime('now')
                """,
                (
                    int(r["team_id"]), int(league_id), int(season),
                    r.get("team_name"),
                    round(attack, 4), round(defense, 4), round(overall, 4),
                    int(r.get("games_played") or 0),
                ),
            )
            n += 1
    return n


# ---- read / apply ----------------------------------------------------------


def get_ratings(league_id: int, season: int) -> dict[int, dict]:
    """Return {team_id: {attack, defense, overall, games_played}} for the
    league/season. Empty dict if not populated yet."""
    with db() as conn:
        rows = conn.execute(
            """
            SELECT team_id, team_name, attack_rating, defense_rating, overall_rating, games_played
            FROM team_ratings WHERE league_id = ? AND season = ?
            """,
            (int(league_id), int(season)),
        ).fetchall()
    return {
        r["team_id"]: {
            "team_name": r["team_name"],
            "attack": r["attack_rating"],
            "defense": r["defense_rating"],
            "overall": r["overall_rating"],
            "games_played": r["games_played"],
        }
        for r in rows
    }


def adjust_xg(
    raw_xg_for: float,
    raw_xg_against: float,
    opponent_attack_rating: float | None,
    opponent_defense_rating: float | None,
) -> tuple[float, float]:
    """Apply opponent-strength adjustment to a single game's xG.

    Both perspectives normalize to "what this game would have looked like
    against a league-average opponent."

    xG_for: a high opponent_defense_rating means the opponent's defense is
    BETTER than league average (less xG conceded). Scoring against them is
    more impressive — multiply xG_for upward. Defense rating is built as
    `league_avg_against / team_xg_against`, so multiplication gives the
    right ratio (your scored xG × how much HARDER it was to score on this
    opponent vs. average).

    xG_against: a high opponent_attack_rating means the opponent's attack
    is BETTER than league average (more xG generated). Conceding to them
    is less bad — DIVIDE xG_against by their attack rating to scale the
    defensive performance back to "vs average attack." (Multiplying here
    would amplify in the wrong direction: it would make conceding to a
    strong attack look WORSE than the raw number, which is the opposite
    of normalization.)

    Returns (adjusted_xg_for, adjusted_xg_against). When ratings are missing
    (untracked opponent, missing data), returns the raw values unchanged.
    """
    # Clamp ratings to [0.5, 1.5] to avoid amplifying tiny denominators
    # — extreme outliers in early-season data shouldn't blow up the math.
    def _clamp(x: float | None) -> float | None:
        if x is None:
            return None
        return max(0.5, min(1.5, float(x)))

    od = _clamp(opponent_defense_rating)
    oa = _clamp(opponent_attack_rating)
    new_for = raw_xg_for * od if od is not None else raw_xg_for
    new_against = raw_xg_against / oa if oa is not None else raw_xg_against
    return new_for, new_against


# ---- nightly job (called from scheduler) -----------------------------------


def top_and_bottom(league_id: int, season: int, k: int = 5) -> dict:
    """For verification / dashboard — return top-k and bottom-k rated teams."""
    with db() as conn:
        rows = conn.execute(
            """
            SELECT team_name, ROUND(attack_rating, 3) AS atk,
                   ROUND(defense_rating, 3) AS dfn,
                   ROUND(overall_rating, 3) AS overall
            FROM team_ratings WHERE league_id = ? AND season = ?
            ORDER BY overall_rating DESC
            """,
            (int(league_id), int(season)),
        ).fetchall()
    rows = [dict(r) for r in rows]
    return {"top": rows[:k], "bottom": rows[-k:]}
