"""WC qualifier corpus loader for the pre-tournament calibration grid.

Pulls fixtures from each confederation's WC 2026 qualifying league, filters
to matches involving teams in `wc_qualified_teams.WC_2026_QUALIFIED`, and
returns the fixture + stats payload shaped for `_evaluate_params()`.

Design decisions:
  - Team matching is by NAME (case-insensitive, with aliases). API-Football's
    /teams?search= would let us resolve numeric IDs but adds 48 round-trips
    we don't need — every fixture payload already carries `teams.home.name`
    and `teams.away.name`, so we match strings directly.
  - No filtering on competition stage. Quals + their playoff legs all count.
  - Stats are pulled only for matches that pass the team-name filter, so
    the API-call count is bounded by the qualified-team corpus size, not
    the full league size.
"""
from __future__ import annotations

import logging
from typing import Iterable

import httpx

import api_football
import wc_qualified_teams as W

log = logging.getLogger("arb.qualifier_corpus")

# (league_id, season) tuples covering all 2026 WC qualifying campaigns.
# Seasons confirmed against API-Football /leagues?search=Qualification.
QUALIFIER_LEAGUES: list[tuple[int, int, str]] = [
    # (league_id, season, label)
    (32, 2024, "UEFA"),
    (34, 2024, "CONMEBOL"),
    (34, 2026, "CONMEBOL (final round)"),
    (30, 2024, "AFC"),
    (30, 2026, "AFC (final rounds)"),
    (29, 2023, "CAF"),
    (29, 2025, "CAF (later rounds)"),
    (31, 2024, "CONCACAF"),
    (31, 2026, "CONCACAF (final round)"),
    (33, 2026, "OFC"),
    (37, 2026, "Inter-confederation playoffs"),
]


def _normalize(name: str) -> str:
    """Canonicalize a team name for matching. API-Football and FIFA disagree
    on a handful of names — this map covers the noisy ones in our pool."""
    s = (name or "").strip()
    aliases = {
        "USA": "United States",
        "United States Of America": "United States",
        "Usa": "United States",
        "Uae": "United Arab Emirates",
        "South Korea": "Korea Republic",
        "Korea Republic": "Korea Republic",
        "Czech Republic": "Czechia",
        "Czechia": "Czechia",
        "Ivory Coast": "Côte D'ivoire",
        "Côte D'ivoire": "Côte D'ivoire",
    }
    s = aliases.get(s, s)
    return s.casefold()


def _qualified_name_set() -> set[str]:
    return {_normalize(t.name) for t in W.WC_2026_QUALIFIED}


async def load_qualifier_fixtures(client: httpx.AsyncClient) -> list[dict]:
    """One /fixtures call per (league, season). Returns the union of all
    fixtures filtered to those involving at least one qualified team."""
    pool = _qualified_name_set()
    all_fx: list[dict] = []
    seen_ids: set[int] = set()
    for league_id, season, label in QUALIFIER_LEAGUES:
        try:
            fixtures = await api_football.fixtures_by_date_range(
                client, league=league_id, season=season,
                from_date=f"{season}-01-01", to_date=f"{season+1}-12-31",
            )
        except Exception as e:
            log.warning("qualifier_corpus: %s (league=%d season=%d) fetch failed: %s",
                        label, league_id, season, e)
            continue
        kept = 0
        for fx in fixtures:
            fid = fx["fixture"]["id"]
            if fid in seen_ids:
                continue
            home = _normalize(fx["teams"]["home"]["name"])
            away = _normalize(fx["teams"]["away"]["name"])
            if home in pool or away in pool:
                seen_ids.add(fid)
                all_fx.append(fx)
                kept += 1
        log.info("qualifier_corpus: %s — kept %d/%d fixtures",
                 label, kept, len(fixtures))
    log.info("qualifier_corpus: total unique fixtures involving qualified teams: %d",
             len(all_fx))
    return all_fx


async def load_stats_for_fixtures(client: httpx.AsyncClient, fixtures: list[dict]) -> dict[int, list[dict]]:
    """One /fixtures/statistics call per fixture. Cached on disk by
    api_football._cache_key, so re-runs after the first pull are free."""
    stats: dict[int, list[dict]] = {}
    for i, fx in enumerate(fixtures):
        fid = fx["fixture"]["id"]
        try:
            stats[fid] = await api_football.fixture_statistics(client, fid)
        except api_football.PlanError as e:
            log.warning("plan error on stats for %d: %s", fid, e)
            continue
        except Exception as e:
            log.warning("stats fetch failed for %d: %s", fid, e)
            continue
        if (i + 1) % 25 == 0:
            log.info("qualifier_corpus: stats progress %d/%d", i+1, len(fixtures))
    log.info("qualifier_corpus: loaded stats for %d/%d fixtures", len(stats), len(fixtures))
    return stats


async def load_full_corpus() -> tuple[list[dict], dict[int, list[dict]]]:
    """One-shot — fixtures + stats. Used by the grid search."""
    async with httpx.AsyncClient() as client:
        fixtures = await load_qualifier_fixtures(client)
        stats = await load_stats_for_fixtures(client, fixtures)
    return fixtures, stats
