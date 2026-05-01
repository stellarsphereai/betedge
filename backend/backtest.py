"""Time-aware backtest of the Dixon-Coles model on EPL 2023-24.

For each test fixture we:
  1. Find that fixture's date.
  2. Pull each team's most recent N completed matches BEFORE that date.
  3. Read xG-for / xG-against from API-Football fixture statistics.
  4. Run model.predict on the resulting TeamForm objects.
  5. Compare prediction to actual result, score Brier + winner correctness.

We pre-cache stats for rounds 33-38 once (under the daily quota), then run.

Run directly:
    python3 backtest.py
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import httpx

import api_football
import model

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("backtest")

SEASON = 2023
# Need enough prior rounds for the 10-game recent-form window + a season-
# to-date sample for the season-blend feature in model.team_strengths.
# 16 rounds gives every test team ≥10 matches of priors and ≥10 of season-
# to-date — small but representative.
ROUNDS_TO_LOAD = [f"Regular Season - {n}" for n in range(23, 39)]  # 23..38
TEST_ROUNDS = {"Regular Season - 37", "Regular Season - 38"}
RECENT_FORM_WINDOW = 10
MIN_PRIOR_MATCHES = 3


async def load_round(client, round_label: str) -> list[dict]:
    fixtures = await api_football.fixtures_for_round(
        client, api_football.EPL_LEAGUE_ID, SEASON, round_label
    )
    log.info("round %r → %d fixtures", round_label, len(fixtures))
    return fixtures


async def load_all_fixtures(client) -> list[dict]:
    out: list[dict] = []
    for r in ROUNDS_TO_LOAD:
        out.extend(await load_round(client, r))
    return out


async def load_all_stats(client, fixtures: list[dict]) -> dict[int, list[dict]]:
    stats_by_fixture: dict[int, list[dict]] = {}
    for fx in fixtures:
        fid = fx["fixture"]["id"]
        stats_by_fixture[fid] = await api_football.fixture_statistics(client, fid)
    log.info("loaded stats for %d fixtures", len(stats_by_fixture))
    return stats_by_fixture


def _team_recent_xg(
    team_id: int,
    target_date: datetime,
    fixtures: list[dict],
    stats_by_fixture: dict[int, list[dict]],
    n: int = RECENT_FORM_WINDOW,
) -> tuple[list[float], list[float]]:
    """Return (xg_for, xg_against) for the team's last n matches before target_date,
    most recent first."""
    prior: list[tuple[datetime, float, float]] = []
    for fx in fixtures:
        d = datetime.fromisoformat(fx["fixture"]["date"].replace("Z", "+00:00"))
        if d >= target_date:
            continue
        h_id = fx["teams"]["home"]["id"]
        a_id = fx["teams"]["away"]["id"]
        if team_id not in (h_id, a_id):
            continue
        stats = stats_by_fixture.get(fx["fixture"]["id"])
        if not stats:
            continue
        xg_self = api_football.expected_goals_for(stats, team_id)
        opp_id = a_id if team_id == h_id else h_id
        xg_opp = api_football.expected_goals_for(stats, opp_id)
        if xg_self is None or xg_opp is None:
            continue
        prior.append((d, xg_self, xg_opp))

    prior.sort(key=lambda t: t[0], reverse=True)
    prior = prior[:n]
    return [p[1] for p in prior], [p[2] for p in prior]


def _season_to_date_avg(
    team_id: int,
    target_date: datetime,
    fixtures: list[dict],
    stats_by_fixture: dict[int, list[dict]],
) -> tuple[float | None, float | None]:
    """Average xG_for / xG_against for the team across every match in the
    loaded corpus that kicked off before target_date. Leak-free season blend
    surrogate — uses the same fixtures the recent-window pulls from."""
    fors: list[float] = []
    against: list[float] = []
    for fx in fixtures:
        d = datetime.fromisoformat(fx["fixture"]["date"].replace("Z", "+00:00"))
        if d >= target_date:
            continue
        h_id = fx["teams"]["home"]["id"]
        a_id = fx["teams"]["away"]["id"]
        if team_id not in (h_id, a_id):
            continue
        stats = stats_by_fixture.get(fx["fixture"]["id"])
        if not stats:
            continue
        xg_self = api_football.expected_goals_for(stats, team_id)
        opp_id = a_id if team_id == h_id else h_id
        xg_opp = api_football.expected_goals_for(stats, opp_id)
        if xg_self is None or xg_opp is None:
            continue
        fors.append(xg_self)
        against.append(xg_opp)
    if not fors:
        return None, None
    return sum(fors) / len(fors), sum(against) / len(against)


def brier(predicted: dict, actual_outcome: str) -> float:
    targets = {"home": 0.0, "draw": 0.0, "away": 0.0}
    if actual_outcome in targets:
        targets[actual_outcome] = 1.0
    return sum((predicted[k] - targets[k]) ** 2 for k in targets)


async def run_backtest() -> dict:
    async with httpx.AsyncClient() as client:
        all_fixtures = await load_all_fixtures(client)
        stats_by_fixture = await load_all_stats(client, all_fixtures)

    test_fixtures = [
        fx for fx in all_fixtures if fx["league"]["round"] in TEST_ROUNDS
    ]
    log.info("test fixtures: %d", len(test_fixtures))

    rows: list[dict] = []
    for fx in test_fixtures:
        d = datetime.fromisoformat(fx["fixture"]["date"].replace("Z", "+00:00"))
        h = fx["teams"]["home"]
        a = fx["teams"]["away"]
        actual = api_football.winner_outcome(fx)
        if actual is None:
            continue

        h_xf, h_xa = _team_recent_xg(h["id"], d, all_fixtures, stats_by_fixture)
        a_xf, a_xa = _team_recent_xg(a["id"], d, all_fixtures, stats_by_fixture)

        if len(h_xf) < MIN_PRIOR_MATCHES or len(a_xf) < MIN_PRIOR_MATCHES:
            log.debug("skip %s vs %s — insufficient prior data", h["name"], a["name"])
            continue

        h_szn_for, h_szn_against = _season_to_date_avg(h["id"], d, all_fixtures, stats_by_fixture)
        a_szn_for, a_szn_against = _season_to_date_avg(a["id"], d, all_fixtures, stats_by_fixture)

        home_form = model.TeamForm(
            name=h["name"], xg_for=h_xf, xg_against=h_xa, games_played=len(h_xf),
            season_avg_for=h_szn_for, season_avg_against=h_szn_against,
        )
        away_form = model.TeamForm(
            name=a["name"], xg_for=a_xf, xg_against=a_xa, games_played=len(a_xf),
            season_avg_for=a_szn_for, season_avg_against=a_szn_against,
        )
        pred = model.predict(home_form, away_form, knockout=False)

        probs = {"home": pred.home_win_pct, "draw": pred.draw_pct, "away": pred.away_win_pct}
        winner = max(probs, key=probs.get)
        rows.append(
            {
                "fixture_id": fx["fixture"]["id"],
                "date": fx["fixture"]["date"],
                "round": fx["league"]["round"],
                "home": h["name"],
                "away": a["name"],
                "score": fx["goals"],
                "actual": actual,
                "predicted_winner": winner,
                "correct_winner": winner == actual,
                "probs": probs,
                "home_xg": pred.home_xg,
                "away_xg": pred.away_xg,
                "brier": round(brier(probs, actual), 4),
            }
        )

    n = len(rows)
    if n == 0:
        return {"n": 0, "note": "no scorable fixtures"}

    correct = sum(1 for r in rows if r["correct_winner"])
    avg_brier = sum(r["brier"] for r in rows) / n

    # Calibration buckets — when model says "home wins with X%", does it actually?
    bins = [(0, 0.3), (0.3, 0.4), (0.4, 0.5), (0.5, 0.6), (0.6, 0.7), (0.7, 1.01)]
    calibration: list[dict] = []
    for lo, hi in bins:
        bucket = [r for r in rows if lo <= r["probs"][r["predicted_winner"]] < hi]
        if not bucket:
            continue
        hit = sum(1 for r in bucket if r["correct_winner"]) / len(bucket)
        avg_p = sum(r["probs"][r["predicted_winner"]] for r in bucket) / len(bucket)
        calibration.append(
            {"bucket": f"{lo:.2f}-{hi:.2f}", "n": len(bucket), "model_avg": round(avg_p, 3), "actual_hit_rate": round(hit, 3)}
        )

    return {
        "n": n,
        "correct_winner": correct,
        "winner_accuracy": round(correct / n, 4),
        "avg_brier": round(avg_brier, 4),
        "calibration": calibration,
        "rows": rows,
    }


if __name__ == "__main__":
    result = asyncio.run(run_backtest())
    print(f"\n{'=' * 72}")
    print(f"Backtest: EPL 2023-24, rounds 37-38 (n={result.get('n', 0)})")
    print("=" * 72)
    if result.get("n", 0) == 0:
        print(result.get("note") or "no data")
    else:
        print(f"Winner accuracy : {result['winner_accuracy'] * 100:.2f}%  ({result['correct_winner']}/{result['n']})")
        print(f"Avg Brier       : {result['avg_brier']}  (lower = better; 0.6 ≈ uninformed)")
        print()
        print("Calibration (predicted-prob bucket → actual hit rate):")
        for c in result["calibration"]:
            bar = "▇" * int(c["actual_hit_rate"] * 30)
            print(f"  {c['bucket']:<10}  n={c['n']:>2}  model={c['model_avg']:>5.3f}  actual={c['actual_hit_rate']:>5.3f}  {bar}")
        print()
        print("Sample rows:")
        for r in result["rows"][:6]:
            tag = "✓" if r["correct_winner"] else "✗"
            print(
                f"  {tag} {r['home'][:18]:<18} {r['score']['home']}-{r['score']['away']:<2} {r['away'][:18]:<18}  "
                f"pred={r['predicted_winner']:<5} ({r['probs'][r['predicted_winner']]*100:>5.1f}%)  brier={r['brier']:.3f}"
            )
        # write full results for inspection
        out = Path(__file__).parent / "backtest_result.json"
        out.write_text(json.dumps(result, indent=2))
        print(f"\nFull results → {out}")
