"""Understat scraper (currently broken — see note).

Phase B note: as of April 2026 Understat removed the `var datesData = JSON.parse(...)`
embedded JSON from team pages. Pages are static templates with no inline data
(~18 KB, no parseable variables). Until they restore the format or expose an
official endpoint, this module is a stub. The backtest pipeline uses
API-Football's per-fixture `expected_goals` instead.

Function signatures preserved so the orchestrator can swap back if Understat
reverts.
"""
from __future__ import annotations

import logging

import httpx

log = logging.getLogger("arb.understat")

UNAVAILABLE_REASON = (
    "Understat has stripped embedded match data from team pages "
    "(observed 2026-04). Falling back to API-Football expected_goals."
)


async def fetch_team_xg(client: httpx.AsyncClient, team_name: str, season: int = 2024) -> list[dict]:
    log.info("understat unavailable: %s", UNAVAILABLE_REASON)
    return []


def last_n_xg(matches, team_name, n=5):
    return [], []
