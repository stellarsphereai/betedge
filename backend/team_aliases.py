"""Team-name normalization across providers.

The Odds API, API-Football, and Kalshi all spell the same club differently
("Manchester City" / "Man City", "Wolverhampton Wanderers" / "Wolves",
"Brighton & Hove Albion" / "Brighton"). We collapse to a canonical form for
joining.
"""
from __future__ import annotations

import re
import unicodedata


def _fold_ascii(s: str) -> str:
    """ü → u, é → e, ñ → n. Lets aliases match regardless of source diacritics."""
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")

# Map any-known-alias → canonical form.
# Canonical = the most common Odds API spelling (drives the dashboard text).
_ALIASES = {
    "man city": "Manchester City",
    "manchester city fc": "Manchester City",
    "man united": "Manchester United",
    "manchester utd": "Manchester United",
    "manchester united fc": "Manchester United",
    "spurs": "Tottenham Hotspur",
    "tottenham": "Tottenham Hotspur",
    "wolves": "Wolverhampton Wanderers",
    "brighton": "Brighton & Hove Albion",
    "brighton hove albion": "Brighton & Hove Albion",
    "brighton and hove albion": "Brighton & Hove Albion",
    "newcastle": "Newcastle United",
    "newcastle utd": "Newcastle United",
    "west ham": "West Ham United",
    "leeds": "Leeds United",
    "leicester": "Leicester City",
    "ipswich": "Ipswich Town",
    "nott m forest": "Nottingham Forest",
    "nottm forest": "Nottingham Forest",
    "sheffield utd": "Sheffield United",
    "afc bournemouth": "Bournemouth",

    # UEFA / European clubs across language variants
    "bayern munchen": "Bayern Munich",
    "bayern munich fc": "Bayern Munich",
    "fc bayern munich": "Bayern Munich",
    "atletico madrid": "Atlético Madrid",
    "atletico de madrid": "Atlético Madrid",
    "club atletico de madrid": "Atlético Madrid",
    "atl madrid": "Atlético Madrid",
    "real madrid cf": "Real Madrid",
    "fc barcelona": "Barcelona",
    "barca": "Barcelona",
    "fc internazionale": "Inter Milan",
    "inter": "Inter Milan",
    "internazionale": "Inter Milan",
    "ac milan": "AC Milan",
    "milan": "AC Milan",
    "paris saint germain": "Paris Saint Germain",
    "psg": "Paris Saint Germain",
    "bvb": "Borussia Dortmund",
    "borussia dortmund": "Borussia Dortmund",

    # World Cup national teams — common variant collapses
    "us": "USA",
    "u s a": "USA",
    "united states": "USA",
    "korea republic": "South Korea",
    "republic of korea": "South Korea",
    "iran islamic republic of": "Iran",
    "ivory coast": "Côte d'Ivoire",
    "cote divoire": "Côte d'Ivoire",
    "czechia": "Czech Republic",
}


def _strip(name: str) -> str:
    """Lowercase, ASCII-fold, drop punctuation, collapse whitespace. No alias resolution."""
    if not name:
        return ""
    s = _fold_ascii(name).lower().strip()
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def canonical(name: str) -> str:
    """Return the canonical spelling for a team name from any provider."""
    if not name:
        return ""
    stripped = _strip(name)
    return _ALIASES.get(stripped, name.strip())


def normalize_key(name: str) -> str:
    """A lookup key: stripped canonical. Use for joining across data sources."""
    return _strip(canonical(name))
