"""FBref xG scraper — secondary xG source using StatsBomb data via FBref.

Fills gaps where API-Football has no expected_goals data (CONMEBOL/AFC/CAF
qualifiers, some Liga MX matches, etc.). Rate-limited and cached to respect
FBref's terms.

Usage:
    xg = await fetch_match_xg(client, "Premier League", "2025-2026")
    # Returns dict mapping (home_team_lower, away_team_lower) → (home_xg, away_xg)
"""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone

import httpx
from bs4 import BeautifulSoup

log = logging.getLogger("arb.fbref")

# FBref competition IDs
COMP_IDS = {
    "epl": 9,
    "la_liga": 12,
    "ucl": 8,
    "uel": 19,
    "mls": 22,
    "liga_mx": 31,
}

# Rate limiting — FBref allows ~20 requests/min
_last_request_time: float = 0
_MIN_REQUEST_INTERVAL = 3.5  # seconds between requests

# In-memory cache: (league, season) → {(home, away): (home_xg, away_xg)}
_cache: dict[tuple[str, int], tuple[float, dict]] = {}
_CACHE_TTL = 3600 * 6  # 6 hours


async def _rate_limited_get(client: httpx.AsyncClient, url: str) -> httpx.Response:
    """GET with rate limiting to respect FBref's terms."""
    global _last_request_time
    import asyncio
    now = time.monotonic()
    wait = _MIN_REQUEST_INTERVAL - (now - _last_request_time)
    if wait > 0:
        await asyncio.sleep(wait)
    _last_request_time = time.monotonic()
    return await client.get(
        url,
        headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"},
        timeout=20.0,
        follow_redirects=True,
    )


def _parse_scores_page(html: str) -> dict[tuple[str, str, str], tuple[float | None, float | None]]:
    """Parse FBref scores/fixtures page to extract per-match xG.

    Returns dict mapping (date, home_team_lower, away_team_lower) → (home_xg, away_xg).
    """
    soup = BeautifulSoup(html, "html.parser")
    results: dict[tuple[str, str, str], tuple[float | None, float | None]] = {}

    # FBref uses <table id="sched_..."> for the scores table
    table = soup.find("table", id=re.compile(r"^sched"))
    if not table:
        # Try alternate table IDs
        table = soup.find("table", class_="stats_table")
    if not table:
        log.warning("fbref: no scores table found in page")
        return results

    tbody = table.find("tbody")
    if not tbody:
        return results

    for tr in tbody.find_all("tr"):
        if tr.get("class") and "thead" in tr.get("class", []):
            continue

        cells = tr.find_all(["td", "th"])
        cell_map = {}
        for cell in cells:
            stat = cell.get("data-stat")
            if stat:
                cell_map[stat] = cell.get_text(strip=True)

        date = cell_map.get("date", "")
        home = cell_map.get("home_team", "").lower().strip()
        away = cell_map.get("away_team", "").lower().strip()
        home_xg_str = cell_map.get("home_xg", "")
        away_xg_str = cell_map.get("away_xg", "")

        if not (date and home and away):
            continue

        try:
            home_xg = float(home_xg_str) if home_xg_str else None
        except ValueError:
            home_xg = None
        try:
            away_xg = float(away_xg_str) if away_xg_str else None
        except ValueError:
            away_xg = None

        if home_xg is not None and away_xg is not None:
            results[(date, home, away)] = (home_xg, away_xg)

    return results


async def fetch_season_xg(
    client: httpx.AsyncClient,
    league: str,
    season: int,
    force: bool = False,
) -> dict[tuple[str, str, str], tuple[float, float]]:
    """Fetch all match xG for a league season from FBref.

    Returns dict mapping (date, home_lower, away_lower) → (home_xg, away_xg).
    Cached for 6 hours.
    """
    cache_key = (league, season)
    now = time.time()
    if not force and cache_key in _cache:
        ts, data = _cache[cache_key]
        if now - ts < _CACHE_TTL:
            return data

    comp_id = COMP_IDS.get(league)
    if not comp_id:
        log.warning("fbref: unknown league '%s'", league)
        return {}

    # FBref season format: "2025-2026" for Aug-May leagues, "2026" for calendar year
    if league in ("mls", "liga_mx"):
        season_str = str(season)
    else:
        season_str = f"{season}-{season + 1}"

    url = f"https://fbref.com/en/comps/{comp_id}/{season_str}/schedule/"

    try:
        resp = await _rate_limited_get(client, url)
        if resp.status_code != 200:
            log.warning("fbref: %s returned %d", url, resp.status_code)
            return {}
        results = _parse_scores_page(resp.text)
        log.info("fbref: fetched %d matches with xG for %s %s", len(results), league, season_str)
        _cache[cache_key] = (now, results)
        return results
    except Exception as e:
        log.warning("fbref: failed to fetch %s: %s", url, e)
        return {}


def find_match_xg(
    fbref_data: dict[tuple[str, str, str], tuple[float, float]],
    home_team: str,
    away_team: str,
    match_date: str | None = None,
) -> tuple[float | None, float | None]:
    """Look up xG for a specific match in the FBref data.

    Tries exact date match first, then fuzzy team name matching.
    Returns (home_xg, away_xg) or (None, None) if not found.
    """
    home_lower = home_team.lower().strip()
    away_lower = away_team.lower().strip()

    # Exact match with date
    if match_date:
        date_str = match_date[:10]  # "2026-08-16" format
        key = (date_str, home_lower, away_lower)
        if key in fbref_data:
            return fbref_data[key]

    # Fuzzy: match by team names only (ignore date)
    for (d, h, a), (hxg, axg) in fbref_data.items():
        if (home_lower in h or h in home_lower) and (away_lower in a or a in away_lower):
            return (hxg, axg)

    return (None, None)
