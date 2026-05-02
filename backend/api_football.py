"""API-Football client (v3.football.api-sports.io).

Disk-cached: every successful response is stored under .cache/api_football/
keyed by URL+params. Re-runs of the backtest don't re-burn quota.

Free plan: 10 req/min, 100 req/day, seasons 2022-2024.
We pace at ~7s between calls and back off on 429.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from pathlib import Path
from urllib.parse import quote

import httpx
from dotenv import load_dotenv

# Make standalone scripts (e.g. backtest.py) work without their own load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")

log = logging.getLogger("arb.apifb")

BASE = "https://v3.football.api-sports.io"
EPL_LEAGUE_ID = 39
WORLD_CUP_LEAGUE_ID = 1

CACHE_DIR = Path(__file__).parent / ".cache" / "api_football"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

_MIN_INTERVAL_S = 7.0
_last_call_at = 0.0

# Cached responses older than this are treated as stale and re-fetched. Set
# CACHE_TTL_S in .env to change. 6h default = predictions written at 00:00
# always have fresh fixture/stats data even if cache files exist on disk.
CACHE_TTL_S = int(os.getenv("CACHE_TTL_S", "21600"))


class PlanError(RuntimeError):
    """API-Football refused the call because of a free-plan restriction."""


def _cache_key(path: str, params: dict) -> Path:
    h = hashlib.sha1((path + json.dumps(params, sort_keys=True)).encode()).hexdigest()[:16]
    return CACHE_DIR / f"{h}.json"


async def _throttle():
    global _last_call_at
    elapsed = time.monotonic() - _last_call_at
    if elapsed < _MIN_INTERVAL_S:
        await asyncio.sleep(_MIN_INTERVAL_S - elapsed)
    _last_call_at = time.monotonic()


async def _get(client: httpx.AsyncClient, path: str, params: dict, force: bool = False) -> dict:
    cache_path = _cache_key(path, params)
    if not force and cache_path.exists():
        age = time.time() - cache_path.stat().st_mtime
        if age < CACHE_TTL_S:
            return json.loads(cache_path.read_text())

    # Pre-call quota gate. Imported lazily to keep module import cycle-free.
    import api_quota
    api_quota.check_or_raise()

    qs = "&".join(f"{k}={quote(str(v), safe='')}" for k, v in params.items())
    url = f"{BASE}{path}?{qs}"
    headers = {"x-apisports-key": os.getenv("API_FOOTBALL_KEY", "")}

    last_status = None
    for attempt in range(4):
        await _throttle()
        r = await client.get(url, headers=headers, timeout=30.0)
        last_status = r.status_code
        if r.status_code == 429 or (r.status_code == 403 and attempt < 3):
            wait = 65  # API-Football's per-minute window resets after 60s
            log.warning("HTTP %s on %s — waiting %ds (attempt %d)", r.status_code, path, wait, attempt + 1)
            await asyncio.sleep(wait)
            continue
        r.raise_for_status()
        data = r.json()
        if data.get("errors"):
            errs = data["errors"]
            if isinstance(errs, dict):
                for k, v in errs.items():
                    if k.lower() == "plan":
                        log.warning("API-Football plan error for %s: %s", path, v)
                        raise PlanError(str(v))
        cache_path.write_text(json.dumps(data))
        # Successful network call — bump the quota counter (skipped on cache hits)
        api_quota.increment()
        return data

    raise RuntimeError(f"API-Football: gave up after retries (last status={last_status}) on {path}")


async def fixtures_for_round(
    client: httpx.AsyncClient, league: int, season: int, round_label: str, force: bool = False
) -> list[dict]:
    data = await _get(client, "/fixtures",
                      {"league": league, "season": season, "round": round_label}, force=force)
    return data.get("response", []) or []


async def fixtures_by_date_range(
    client: httpx.AsyncClient, league: int, season: int, from_date: str, to_date: str,
    force: bool = False,
) -> list[dict]:
    """Fixtures with kickoff in [from_date, to_date]. Both ISO yyyy-mm-dd."""
    data = await _get(
        client, "/fixtures",
        {"league": league, "season": season, "from": from_date, "to": to_date},
        force=force,
    )
    return data.get("response", []) or []


async def fetch_fixture(
    client: httpx.AsyncClient, fixture_id: int, force: bool = True,
) -> dict | None:
    """Single fixture by ID — used by /bets/{id}/auto-mark to read the score
    + status when a paper bet is being settled. Default force=True because
    'is the match finished yet?' is the whole point of the call; a stale
    cache hit would defeat it."""
    data = await _get(client, "/fixtures", {"id": fixture_id}, force=force)
    rows = data.get("response", []) or []
    return rows[0] if rows else None


async def team_recent_fixtures(
    client: httpx.AsyncClient,
    team_id: int,
    last: int = 5,
    league: int | None = None,
    season: int | None = None,
    force: bool = False,
) -> list[dict]:
    """A team's last N completed fixtures. Pass league+season to keep cup ties
    against lower-division clubs out of the xG sample (otherwise xG inflates)."""
    params: dict = {"team": team_id, "last": last}
    if league is not None:
        params["league"] = league
    if season is not None:
        params["season"] = season
    data = await _get(client, "/fixtures", params, force=force)
    return data.get("response", []) or []


async def fixture_statistics(client: httpx.AsyncClient, fixture_id: int, force: bool = False) -> list[dict]:
    data = await _get(client, "/fixtures/statistics", {"fixture": fixture_id}, force=force)
    return data.get("response", []) or []


async def injuries(client: httpx.AsyncClient, league: int, season: int, force: bool = False) -> list[dict]:
    data = await _get(client, "/injuries", {"league": league, "season": season}, force=force)
    return data.get("response", []) or []


async def top_scorers(client: httpx.AsyncClient, league: int, season: int, force: bool = False) -> list[dict]:
    data = await _get(client, "/players/topscorers", {"league": league, "season": season}, force=force)
    return data.get("response", []) or []


async def team_statistics(
    client: httpx.AsyncClient, team_id: int, league: int, season: int, force: bool = False
) -> dict:
    """Aggregate team stats for a (team, league, season). The fields we use:
    goals.for.average.{home,away,total} and goals.against.average.{...}.
    Returns the inner `response` dict (not wrapped)."""
    data = await _get(
        client, "/teams/statistics",
        {"team": team_id, "league": league, "season": season},
        force=force,
    )
    return data.get("response", {}) or {}


def season_avg_goals(stats: dict) -> tuple[float | None, float | None]:
    """Pull (avg goals_for_per_match, avg goals_against_per_match) from a
    /teams/statistics payload. Falls back to None when the season hasn't
    produced enough data yet."""
    if not stats:
        return None, None
    try:
        gf = stats.get("goals", {}).get("for", {}).get("average", {}).get("total")
        ga = stats.get("goals", {}).get("against", {}).get("average", {}).get("total")
        gf_f = float(gf) if gf not in (None, "") else None
        ga_f = float(ga) if ga not in (None, "") else None
        return gf_f, ga_f
    except (TypeError, ValueError):
        return None, None


def is_plan_error(payload: dict) -> str | None:
    """Return the plan error message if API-Football refused the call due to free-tier limits."""
    errs = payload.get("errors") if isinstance(payload, dict) else None
    if isinstance(errs, dict):
        for k, v in errs.items():
            if k.lower() == "plan":
                return str(v)
    return None


def expected_goals_for(stats: list[dict], team_id: int) -> float | None:
    for ts in stats:
        if ts.get("team", {}).get("id") == team_id:
            for s in ts.get("statistics", []):
                if s.get("type") == "expected_goals":
                    v = s.get("value")
                    try:
                        return float(v) if v is not None else None
                    except (TypeError, ValueError):
                        return None
    return None


def winner_outcome(fixture: dict) -> str | None:
    g = fixture.get("goals") or {}
    h, a = g.get("home"), g.get("away")
    if h is None or a is None:
        return None
    if h > a:
        return "home"
    if a > h:
        return "away"
    return "draw"
