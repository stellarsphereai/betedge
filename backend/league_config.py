"""Per-league model + anomaly tuning, sourced from the league_config SQLite
table.

Why a runtime table and not a constants module? Tournament dynamics shift
season-to-season (UCL home advantage, EL knockout, WC neutral venues), and
the user wanted tuning without redeploys. Reads are cached for a short
window so the dashboard's polling doesn't keep hammering SQLite.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

import model
from database import db


_CACHE_TTL_S = 60.0
_cache: dict[str, dict] = {}
_cache_loaded_at = 0.0


@dataclass(frozen=True)
class LeagueThresholds:
    """Per-league anomaly thresholds — exposed separately from ModelParams
    because they only matter to the detector, not the math model."""
    edge_threshold: float       # flag edge above this on FD/DK (Anomaly 1a)
    sharp_divergence: float     # model vs FD/DK delta above this (Anomaly 2)


def _load_all() -> dict[str, dict]:
    """Read every league_config row and shape it into a {league_key: row} map.
    Returns empty dict if the table doesn't exist yet (first-boot edge case)."""
    try:
        with db() as conn:
            rows = conn.execute(
                """
                SELECT league_key, league_id, league_name, gamma,
                       recent_weight, season_weight,
                       anomaly_edge_threshold, sharp_book_divergence_threshold
                FROM league_config
                """
            ).fetchall()
    except Exception:
        return {}
    return {r["league_key"]: dict(r) for r in rows}


def _refresh_if_stale() -> dict[str, dict]:
    global _cache, _cache_loaded_at
    if not _cache or (time.time() - _cache_loaded_at) > _CACHE_TTL_S:
        _cache = _load_all()
        _cache_loaded_at = time.time()
    return _cache


def get_row(league_key: str) -> dict | None:
    """Raw league_config row for a league. None if not seeded yet."""
    return _refresh_if_stale().get((league_key or "").lower())


def params_for_league(league_key: str) -> model.ModelParams:
    """Build a ModelParams from the league_config row. Falls back to
    DEFAULT_PARAMS when the league isn't seeded yet (e.g. test environment)."""
    row = get_row(league_key)
    if not row:
        return model.DEFAULT_PARAMS
    base = model.DEFAULT_PARAMS
    return model.ModelParams(
        rho=base.rho,
        game_weights=base.game_weights,
        rest_tired_penalty=base.rest_tired_penalty,
        injured_scorer_penalty=base.injured_scorer_penalty,
        ko_draw_damping=base.ko_draw_damping,
        home_gamma=row["gamma"],
        penalty_floor=base.penalty_floor,
        season_blend=row["recent_weight"],
    )


def thresholds_for_league(league_key: str) -> LeagueThresholds:
    """Anomaly thresholds for a league. Falls back to EPL-equivalent defaults
    (15% edge, 20pp divergence) when the league isn't in the table."""
    row = get_row(league_key)
    if not row:
        return LeagueThresholds(edge_threshold=0.15, sharp_divergence=0.20)
    return LeagueThresholds(
        edge_threshold=row["anomaly_edge_threshold"],
        sharp_divergence=row["sharp_book_divergence_threshold"],
    )


def all_rows() -> list[dict]:
    """For the admin endpoint — list every seeded league config row."""
    return list(_refresh_if_stale().values())
