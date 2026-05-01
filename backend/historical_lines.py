"""Opening / closing line capture from The Odds API historical endpoint.

Free plan returns 401 HISTORICAL_UNAVAILABLE_ON_FREE_USAGE_PLAN, so this module
either succeeds (paid plan) or returns a clear unavailable status. Manual
closing-line entry via clv_tracker.set_closing() works regardless.
"""
from __future__ import annotations

import os

import httpx

ODDS_API_BASE = "https://api.the-odds-api.com/v4"


async def fetch_historical_odds(
    client: httpx.AsyncClient,
    sport_key: str,
    timestamp_iso: str,
    regions: str = "us,us2",
    markets: str = "h2h,totals",
) -> dict:
    api_key = os.getenv("ODDS_API_KEY", "")
    if not api_key:
        return {"available": False, "reason": "ODDS_API_KEY not set"}

    url = f"{ODDS_API_BASE}/historical/sports/{sport_key}/odds"
    params = {
        "apiKey": api_key,
        "regions": regions,
        "markets": markets,
        "oddsFormat": "decimal",
        "date": timestamp_iso,
    }
    return await _fetch(client, url, params)


async def fetch_historical_event_odds(
    client: httpx.AsyncClient,
    sport_key: str,
    event_id: str,
    timestamp_iso: str,
    regions: str = "us,us2",
    markets: str = "btts",
) -> dict:
    """Per-event historical endpoint — required for markets like BTTS that the
    bulk endpoint refuses (Odds API quirk)."""
    api_key = os.getenv("ODDS_API_KEY", "")
    if not api_key:
        return {"available": False, "reason": "ODDS_API_KEY not set"}
    url = f"{ODDS_API_BASE}/historical/sports/{sport_key}/events/{event_id}/odds"
    params = {
        "apiKey": api_key,
        "regions": regions,
        "markets": markets,
        "oddsFormat": "decimal",
        "date": timestamp_iso,
    }
    return await _fetch(client, url, params)


async def _fetch(client: httpx.AsyncClient, url: str, params: dict) -> dict:
    try:
        r = await client.get(url, params=params, timeout=20.0)
    except httpx.HTTPError as e:
        return {"available": False, "reason": f"network: {e}"}
    if r.status_code == 401:
        try:
            body = r.json()
            reason = body.get("message") or body.get("error_code")
        except Exception:
            reason = r.text[:200]
        return {"available": False, "paid_only": True, "reason": reason}
    if not r.is_success:
        return {"available": False, "reason": f"HTTP {r.status_code}: {r.text[:200]}"}
    return {"available": True, "data": r.json()}
