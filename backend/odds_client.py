"""Thin Odds API client for the 7 NY-legal books we care about.

Caesars in The Odds API still uses the legacy williamhill_us key.
"""
from __future__ import annotations

import os

import httpx

ODDS_API_BASE = "https://api.the-odds-api.com/v4"

# Allowlist matches the seven NY-licensed sportsbooks specified in the spec.
# williamhill_us is included because the Odds API still uses that legacy key
# for Caesars data. BetMGM is intentionally omitted per spec.
NY_BOOKMAKER_KEYS = {
    "fanduel",
    "draftkings",
    "caesars",
    "williamhill_us",
    "espnbet",
    "betrivers",
    "fanatics",
    "ballybet",
}

BOOK_TITLE_OVERRIDES = {
    "espnbet": "ESPN Bet",
    "williamhill_us": "Caesars",
}


async def fetch_odds(
    client: httpx.AsyncClient,
    sport_key: str,
    regions: str = "us,us2",
    markets: str = "h2h,totals",
) -> list[dict]:
    """Sport-level odds. Note: btts is NOT supported on this endpoint — use
    fetch_event_btts() per fixture for that market."""
    api_key = os.getenv("ODDS_API_KEY", "")
    if not api_key:
        raise RuntimeError("ODDS_API_KEY not set")
    url = f"{ODDS_API_BASE}/sports/{sport_key}/odds"
    params = {
        "apiKey": api_key,
        "regions": regions,
        "markets": markets,
        "oddsFormat": "decimal",
    }
    r = await client.get(url, params=params, timeout=20.0)
    if r.status_code == 422:
        return []
    r.raise_for_status()
    return r.json()


async def fetch_event_btts(
    client: httpx.AsyncClient,
    sport_key: str,
    event_id: str,
    regions: str = "us,us2",
) -> list[dict]:
    """Per-fixture BTTS. Returns a list of {key, title, markets:[...]} bookmaker dicts
    that can be merged into the bookmakers list of the matching sport-level fixture."""
    api_key = os.getenv("ODDS_API_KEY", "")
    if not api_key:
        return []
    url = f"{ODDS_API_BASE}/sports/{sport_key}/events/{event_id}/odds"
    params = {
        "apiKey": api_key,
        "regions": regions,
        "markets": "btts",
        "oddsFormat": "decimal",
    }
    r = await client.get(url, params=params, timeout=20.0)
    if not r.is_success:
        return []
    data = r.json()
    return data.get("bookmakers", []) or []


def merge_btts_into_match(match: dict, btts_books: list[dict]) -> None:
    """Append btts markets onto the existing bookmakers list of `match` (in place)."""
    if not btts_books:
        return
    by_key: dict[str, dict] = {bm.get("key"): bm for bm in match.get("bookmakers", []) or []}
    for src in btts_books:
        k = src.get("key")
        btts_markets = [m for m in src.get("markets", []) if m.get("key") == "btts"]
        if not btts_markets:
            continue
        if k in by_key:
            by_key[k].setdefault("markets", []).extend(btts_markets)
        else:
            match.setdefault("bookmakers", []).append({
                "key": k, "title": src.get("title"), "markets": btts_markets,
            })


def offers_by_book(match: dict) -> dict[str, dict[str, float]]:
    """Reduce one Odds API match to {book_title: {'home','draw','away': odds}}.

    Filters to NY-legal books only. h2h only — kept for the legacy /ev-bets path
    that still uses 3-way joins. New code should use parse_all_markets().
    """
    home = match.get("home_team")
    away = match.get("away_team")
    out: dict[str, dict[str, float]] = {}
    for bm in match.get("bookmakers", []) or []:
        bm_key = (bm.get("key") or "").lower()
        if bm_key not in NY_BOOKMAKER_KEYS:
            continue
        title = BOOK_TITLE_OVERRIDES.get(bm_key) or bm.get("title") or bm_key
        prices: dict[str, float] = {}
        for market in bm.get("markets", []) or []:
            if market.get("key") != "h2h":
                continue
            for o in market.get("outcomes", []) or []:
                price = o.get("price")
                name = o.get("name")
                if not isinstance(price, (int, float)) or price <= 1.0:
                    continue
                if name == home:
                    prices["home"] = float(price)
                elif name == away:
                    prices["away"] = float(price)
                elif name and name.lower() == "draw":
                    prices["draw"] = float(price)
        if prices:
            out[title] = prices
    return out


def parse_all_markets(match: dict) -> dict:
    """Expand one Odds API match into a per-market view we can scan for EV.

    Returns:
        {
          "home_team": str,
          "away_team": str,
          "h2h":   { book: { "home": odds, "draw": odds, "away": odds } },
          "btts":  { book: { "yes": odds, "no": odds } },
          "totals":{ line(float): { book: { "over": odds, "under": odds } } },
        }
    Only NY-legal books are included.
    """
    home = match.get("home_team")
    away = match.get("away_team")
    h2h: dict[str, dict[str, float]] = {}
    btts: dict[str, dict[str, float]] = {}
    totals: dict[float, dict[str, dict[str, float]]] = {}

    for bm in match.get("bookmakers", []) or []:
        bm_key = (bm.get("key") or "").lower()
        if bm_key not in NY_BOOKMAKER_KEYS:
            continue
        title = BOOK_TITLE_OVERRIDES.get(bm_key) or bm.get("title") or bm_key

        for market in bm.get("markets", []) or []:
            mkey = market.get("key")
            outcomes = market.get("outcomes", []) or []

            if mkey == "h2h":
                row: dict[str, float] = {}
                for o in outcomes:
                    p, n = o.get("price"), o.get("name")
                    if not isinstance(p, (int, float)) or p <= 1.0:
                        continue
                    if n == home:                row["home"] = float(p)
                    elif n == away:              row["away"] = float(p)
                    elif (n or "").lower() == "draw": row["draw"] = float(p)
                if row:
                    h2h[title] = row

            elif mkey == "btts":
                row = {}
                for o in outcomes:
                    p, n = o.get("price"), (o.get("name") or "").lower()
                    if not isinstance(p, (int, float)) or p <= 1.0:
                        continue
                    if n == "yes": row["yes"] = float(p)
                    elif n == "no": row["no"]  = float(p)
                if row:
                    btts[title] = row

            elif mkey == "totals":
                # Group outcomes by point — Odds API can emit multiple lines for
                # the same book under the `totals` market on some sports.
                by_point: dict[float, dict[str, float]] = {}
                for o in outcomes:
                    p, n = o.get("price"), (o.get("name") or "").lower()
                    pt = o.get("point")
                    if not isinstance(p, (int, float)) or p <= 1.0 or pt is None:
                        continue
                    line = float(pt)
                    by_point.setdefault(line, {})
                    if n == "over":  by_point[line]["over"]  = float(p)
                    elif n == "under": by_point[line]["under"] = float(p)
                for line, row in by_point.items():
                    if "over" in row and "under" in row:
                        totals.setdefault(line, {})[title] = row

    return {
        "home_team": home,
        "away_team": away,
        "h2h": h2h,
        "btts": btts,
        "totals": totals,
    }
