"""Time-aware backtest of the Dixon-Coles model on UEFA Champions League 2023-24.

Tests on knockout-stage matches (R16 / QF / SF / F) using each team's earlier
group-stage UCL games as the prior xG window. Mirrors the EPL backtest but on
elite-vs-elite knockout football, where we expect the model to be over-confident
because last-5 domestic xG dominated the EPL test fixtures.

Run directly:
    python3 backtest_ucl.py
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path

import httpx

import api_football
import model

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("backtest.ucl")

LEAGUE = api_football.UCL_LEAGUE_ID = 2  # ensure constant exists for re-use
SEASON = 2023
FROM_DATE = "2023-09-01"
TO_DATE = "2024-06-30"
TEST_ROUND_PREFIXES = ("Round of 16", "Quarter-finals", "Semi-finals", "Final")
RECENT_FORM_WINDOW = 10
MIN_PRIOR_MATCHES = 3


async def load_all_fixtures(client) -> list[dict]:
    fixtures = await api_football.fixtures_by_date_range(client, LEAGUE, SEASON, FROM_DATE, TO_DATE)
    log.info("UCL %d-%02d fixtures: %d", SEASON, SEASON + 1 - 2000, len(fixtures))
    return fixtures


async def load_all_stats(client, fixtures: list[dict]) -> dict[int, list[dict]]:
    """Pull stats for every fixture (cached on disk so subsequent runs are free)."""
    stats: dict[int, list[dict]] = {}
    for fx in fixtures:
        fid = fx["fixture"]["id"]
        try:
            stats[fid] = await api_football.fixture_statistics(client, fid)
        except api_football.PlanError as e:
            log.warning("plan error on stats for %d: %s", fid, e)
    log.info("loaded stats for %d fixtures", len(stats))
    return stats


def _team_recent_xg(
    team_id: int,
    target_date: datetime,
    fixtures: list[dict],
    stats_by_fixture: dict[int, list[dict]],
    n: int = RECENT_FORM_WINDOW,
) -> tuple[list[float], list[float]]:
    """Same-competition prior xG for a team, most recent first, n cap."""
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
    """Average xG_for / xG_against across every loaded fixture before
    target_date — leak-free season-blend surrogate (uses the same UCL group
    + KO corpus we already have)."""
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


def _is_test_round(label: str | None) -> bool:
    return bool(label and any(label.startswith(p) for p in TEST_ROUND_PREFIXES))


def _is_knockout(label: str | None) -> bool:
    if not label:
        return False
    s = label.lower()
    return any(k in s for k in ("round of", "quarter", "semi", "final"))


def _outcome(fx: dict) -> str | None:
    g = fx.get("goals") or {}
    h, a = g.get("home"), g.get("away")
    if h is None or a is None:
        return None
    return "home" if h > a else "away" if a > h else "draw"


def _brier(predicted: dict, actual_outcome: str) -> float:
    targets = {"home": 0.0, "draw": 0.0, "away": 0.0}
    targets[actual_outcome] = 1.0
    return sum((predicted[k] - targets[k]) ** 2 for k in targets)


async def run_backtest() -> dict:
    async with httpx.AsyncClient() as client:
        fixtures = await load_all_fixtures(client)
        # Drop qualifying rounds — those are between weaker clubs and pollute the prior xG.
        fixtures = [
            fx for fx in fixtures
            if "Qualifying" not in (fx.get("league", {}).get("round") or "")
            and "Preliminary" not in (fx.get("league", {}).get("round") or "")
            and "Play-offs" not in (fx.get("league", {}).get("round") or "")
        ]
        stats_cache = await load_all_stats(client, fixtures)

    test_fixtures = [fx for fx in fixtures if _is_test_round(fx.get("league", {}).get("round"))]
    log.info("test fixtures (KO stage): %d", len(test_fixtures))

    rows: list[dict] = []
    for fx in test_fixtures:
        d = datetime.fromisoformat(fx["fixture"]["date"].replace("Z", "+00:00"))
        h = fx["teams"]["home"]
        a = fx["teams"]["away"]
        actual = _outcome(fx)
        if actual is None:
            continue

        h_xf, h_xa = _team_recent_xg(h["id"], d, fixtures, stats_cache)
        a_xf, a_xa = _team_recent_xg(a["id"], d, fixtures, stats_cache)
        if len(h_xf) < MIN_PRIOR_MATCHES or len(a_xf) < MIN_PRIOR_MATCHES:
            continue

        h_szn_for, h_szn_against = _season_to_date_avg(h["id"], d, fixtures, stats_cache)
        a_szn_for, a_szn_against = _season_to_date_avg(a["id"], d, fixtures, stats_cache)

        home_form = model.TeamForm(
            name=h["name"], xg_for=h_xf, xg_against=h_xa, games_played=len(h_xf),
            season_avg_for=h_szn_for, season_avg_against=h_szn_against,
        )
        away_form = model.TeamForm(
            name=a["name"], xg_for=a_xf, xg_against=a_xa, games_played=len(a_xf),
            season_avg_for=a_szn_for, season_avg_against=a_szn_against,
        )
        knockout = _is_knockout(fx["league"]["round"])
        pred = model.predict(home_form, away_form, knockout=knockout)

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
                "btts_yes_pct": pred.btts_yes_pct,
                "brier": round(_brier(probs, actual), 4),
            }
        )

    n = len(rows)
    if n == 0:
        return {"n": 0, "note": "no scorable fixtures"}

    correct = sum(1 for r in rows if r["correct_winner"])
    avg_brier = sum(r["brier"] for r in rows) / n

    bins = [(0, 0.4), (0.4, 0.5), (0.5, 0.6), (0.6, 0.7), (0.7, 1.01)]
    calibration: list[dict] = []
    for lo, hi in bins:
        bucket = [r for r in rows if lo <= r["probs"][r["predicted_winner"]] < hi]
        if not bucket:
            continue
        hit = sum(1 for r in bucket if r["correct_winner"]) / len(bucket)
        avg_p = sum(r["probs"][r["predicted_winner"]] for r in bucket) / len(bucket)
        calibration.append(
            {"bucket": f"{lo:.2f}-{hi:.2f}", "n": len(bucket),
             "model_avg": round(avg_p, 3), "actual_hit_rate": round(hit, 3)}
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
    print(f"\n{'=' * 78}")
    print(f"UCL 2023-24 backtest — knockout stage  (n={result.get('n', 0)})")
    print("=" * 78)
    if result.get("n", 0) == 0:
        print(result.get("note") or "no data")
    else:
        print(f"Winner accuracy : {result['winner_accuracy'] * 100:.2f}%  ({result['correct_winner']}/{result['n']})")
        print(f"Avg Brier       : {result['avg_brier']}  (uninformed three-class ≈ 0.667)")
        print()
        print("Calibration (predicted-prob bucket → actual hit rate):")
        for c in result["calibration"]:
            bar = "▇" * int(c["actual_hit_rate"] * 30)
            print(f"  {c['bucket']:<10}  n={c['n']:>2}  model={c['model_avg']:>5.3f}  actual={c['actual_hit_rate']:>5.3f}  {bar}")
        print()
        print("Per-fixture detail (first 10):")
        for r in result["rows"][:10]:
            tag = "✓" if r["correct_winner"] else "✗"
            print(
                f"  {tag} {r['round']:<14} {r['home'][:18]:<18} {r['score']['home']}-{r['score']['away']:<2} {r['away'][:18]:<18}  "
                f"pred={r['predicted_winner']:<5} ({r['probs'][r['predicted_winner']]*100:>5.1f}%)  brier={r['brier']:.3f}"
            )
        out = Path(__file__).parent / "backtest_ucl_result.json"
        out.write_text(json.dumps(result, indent=2))
        print(f"\nFull results → {out}")
