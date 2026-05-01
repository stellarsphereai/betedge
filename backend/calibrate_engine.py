"""Grid-search engine for ModelParams.

Single engine, two callers:
  - wc_calibrate.run_proxy_calibration_from_ucl_knockouts() → uses cached
    UCL 2023-24 knockout fixtures as the calibration corpus
  - calibrate.run_grid_search_for_league() → will use stored TeamForm inputs
    on live predictions once we add input_json (Phase 2-bis)

For now only the UCL-knockout path is wired; the regular-league path returns
a structured "needs input storage" stub.
"""
from __future__ import annotations

import asyncio
import json
import logging
from itertools import product
from pathlib import Path

import httpx

import backtest_ucl
import model

log = logging.getLogger("arb.calibrate_engine")

# Search space for the proxy calibration. Kept compact so the search runs in
# seconds against the cached corpus. RHO and KO damping are the parameters with
# the most defensible-but-tunable physical meaning; game weights and the
# rest/injury penalties are left at defaults to keep dimensionality low.
RHO_GRID = (-0.20, -0.15, -0.10, -0.05, 0.0, 0.05)
KO_DAMPING_GRID = (0.65, 0.75, 0.85, 0.95, 1.00)

# Where the calibrated WC params land. Loaded by wc_calibrate.load_wc_params()
# at sync time so WC predictions use them automatically.
WC_PARAMS_FILE = Path(__file__).parent / "model_params_wc.json"


def _evaluate_params(
    test_fixtures: list[dict],
    fixtures_all: list[dict],
    stats_cache: dict[int, list[dict]],
    params: model.ModelParams,
) -> dict | None:
    """Run model.predict() with `params` against every test fixture; return
    aggregate Brier and winner-accuracy. None if no fixture had enough prior
    data to score."""
    rows: list[tuple[float, bool]] = []
    for fx in test_fixtures:
        from datetime import datetime
        d = datetime.fromisoformat(fx["fixture"]["date"].replace("Z", "+00:00"))
        h = fx["teams"]["home"]
        a = fx["teams"]["away"]
        actual = backtest_ucl._outcome(fx)
        if actual is None:
            continue
        h_xf, h_xa = backtest_ucl._team_recent_xg(h["id"], d, fixtures_all, stats_cache)
        a_xf, a_xa = backtest_ucl._team_recent_xg(a["id"], d, fixtures_all, stats_cache)
        if len(h_xf) < backtest_ucl.MIN_PRIOR_MATCHES or len(a_xf) < backtest_ucl.MIN_PRIOR_MATCHES:
            continue
        h_szn_for, h_szn_against = backtest_ucl._season_to_date_avg(h["id"], d, fixtures_all, stats_cache)
        a_szn_for, a_szn_against = backtest_ucl._season_to_date_avg(a["id"], d, fixtures_all, stats_cache)
        home_form = model.TeamForm(
            name=h["name"], xg_for=h_xf, xg_against=h_xa, games_played=len(h_xf),
            season_avg_for=h_szn_for, season_avg_against=h_szn_against,
        )
        away_form = model.TeamForm(
            name=a["name"], xg_for=a_xf, xg_against=a_xa, games_played=len(a_xf),
            season_avg_for=a_szn_for, season_avg_against=a_szn_against,
        )
        knockout = backtest_ucl._is_knockout(fx["league"]["round"])
        pred = model.predict(home_form, away_form, knockout=knockout, params=params)
        probs = {"home": pred.home_win_pct, "draw": pred.draw_pct, "away": pred.away_win_pct}
        target = {"home": 0, "draw": 0, "away": 0}
        target[actual] = 1
        brier = sum((probs[k] - target[k]) ** 2 for k in target)
        winner_correct = max(probs, key=probs.get) == actual
        rows.append((brier, winner_correct))

    if not rows:
        return None
    n = len(rows)
    return {
        "n": n,
        "avg_brier": sum(r[0] for r in rows) / n,
        "winner_accuracy": sum(1 for r in rows if r[1]) / n,
    }


async def grid_search_ucl_knockouts(rho_grid: tuple[float, ...] = RHO_GRID,
                                     ko_grid: tuple[float, ...] = KO_DAMPING_GRID) -> dict:
    """Walk RHO × KO_DAMPING against UCL 2023-24 knockouts. Returns ranked
    results with the lowest-Brier params on top."""
    async with httpx.AsyncClient() as client:
        fixtures = await backtest_ucl.load_all_fixtures(client)
    fixtures = [
        fx for fx in fixtures
        if "Qualifying" not in (fx.get("league", {}).get("round") or "")
        and "Preliminary" not in (fx.get("league", {}).get("round") or "")
        and "Play-offs" not in (fx.get("league", {}).get("round") or "")
    ]
    async with httpx.AsyncClient() as client:
        stats_cache = await backtest_ucl.load_all_stats(client, fixtures)

    test_fixtures = [
        fx for fx in fixtures
        if backtest_ucl._is_test_round(fx.get("league", {}).get("round"))
    ]
    log.info("calibrate: grid search over %d fixtures × %d×%d params",
             len(test_fixtures), len(rho_grid), len(ko_grid))

    # Baseline: current defaults
    baseline = _evaluate_params(test_fixtures, fixtures, stats_cache, model.DEFAULT_PARAMS)

    results: list[dict] = []
    for rho, ko in product(rho_grid, ko_grid):
        params = model.ModelParams(rho=rho, ko_draw_damping=ko)
        score = _evaluate_params(test_fixtures, fixtures, stats_cache, params)
        if score is None:
            continue
        results.append({
            "params": {"rho": rho, "ko_draw_damping": ko},
            **score,
        })

    if not results:
        return {"ok": False, "reason": "no scorable fixtures"}

    results.sort(key=lambda r: r["avg_brier"])
    best = results[0]
    return {
        "ok": True,
        "n_combinations": len(results),
        "n_test_fixtures": baseline["n"] if baseline else 0,
        "baseline": baseline,
        "best": best,
        "improvement_brier": round(baseline["avg_brier"] - best["avg_brier"], 4) if baseline else None,
        "top_5": results[:5],
        "all_results": results,
    }


def save_wc_params(params: model.ModelParams, source: dict) -> dict:
    """Persist tuned WC params to model_params_wc.json. The `source` dict is
    saved alongside so we have provenance (which corpus, which Brier, etc.)."""
    payload = {
        "params": {
            "rho": params.rho,
            "game_weights": list(params.game_weights),
            "rest_tired_penalty": params.rest_tired_penalty,
            "injured_scorer_penalty": params.injured_scorer_penalty,
            "ko_draw_damping": params.ko_draw_damping,
        },
        "source": source,
    }
    WC_PARAMS_FILE.write_text(json.dumps(payload, indent=2))
    return {"saved_to": str(WC_PARAMS_FILE), "payload": payload}


def load_wc_params() -> model.ModelParams:
    """Read the calibrated WC params from disk, falling back to defaults."""
    if not WC_PARAMS_FILE.exists():
        return model.DEFAULT_PARAMS
    try:
        payload = json.loads(WC_PARAMS_FILE.read_text())
        p = payload.get("params", {})
        return model.ModelParams(
            rho=p.get("rho", model.DEFAULT_PARAMS.rho),
            game_weights=tuple(p.get("game_weights", model.DEFAULT_PARAMS.game_weights)),
            rest_tired_penalty=p.get("rest_tired_penalty", model.DEFAULT_PARAMS.rest_tired_penalty),
            injured_scorer_penalty=p.get("injured_scorer_penalty", model.DEFAULT_PARAMS.injured_scorer_penalty),
            ko_draw_damping=p.get("ko_draw_damping", model.DEFAULT_PARAMS.ko_draw_damping),
        )
    except Exception as e:
        log.warning("failed to load WC params: %s — using defaults", e)
        return model.DEFAULT_PARAMS


def has_wc_params() -> bool:
    return WC_PARAMS_FILE.exists()
