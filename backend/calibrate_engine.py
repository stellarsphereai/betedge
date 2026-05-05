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

# Per-league calibrated-params files. WC is the original; UCL was added when
# the model showed structural over-confidence on UCL knockouts.
def _params_file(league_key: str) -> Path:
    return Path(__file__).parent / f"model_params_{league_key}.json"


# Where the calibrated WC params land. Loaded by wc_calibrate.load_wc_params()
# at sync time so WC predictions use them automatically.
WC_PARAMS_FILE = Path(__file__).parent / "model_params_wc.json"


def _evaluate_params(
    test_fixtures: list[dict],
    fixtures_all: list[dict],
    stats_cache: dict[int, list[dict]],
    params: model.ModelParams,
    league_id: int | None = None,
    min_priors: int | None = None,
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
        threshold = min_priors if min_priors is not None else backtest_ucl.MIN_PRIOR_MATCHES
        if len(h_xf) < threshold or len(a_xf) < threshold:
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
        pred = model.predict(home_form, away_form, knockout=knockout, params=params, league_id=league_id)
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
    baseline = _evaluate_params(test_fixtures, fixtures, stats_cache, model.DEFAULT_PARAMS, league_id=backtest_ucl.LEAGUE)

    results: list[dict] = []
    for rho, ko in product(rho_grid, ko_grid):
        params = model.ModelParams(rho=rho, ko_draw_damping=ko)
        score = _evaluate_params(test_fixtures, fixtures, stats_cache, params, league_id=backtest_ucl.LEAGUE)
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


async def grid_search_qualifier_corpus(
    rho_grid: tuple[float, ...] = RHO_GRID,
    ko_grid: tuple[float, ...] = KO_DAMPING_GRID,
) -> dict:
    """Walk RHO × KO_DAMPING against the WC qualifier corpus (matches
    involving any of the 48 qualified-pool teams). Reuses the same
    _evaluate_params() loop as the UCL-knockout version — only the corpus
    differs.

    Marks each test fixture with knockout=False since qualifier league
    matches mostly have flat round structure (group stages dominate);
    knockout-only inter-conf playoffs are a small subset and treated the
    same as group games for grid simplicity.
    """
    import qualifier_corpus
    log.info("calibrate: loading qualifier corpus...")
    fixtures, stats_cache = await qualifier_corpus.load_full_corpus()
    if not fixtures:
        return {"ok": False, "reason": "no qualifier fixtures fetched"}

    # We test on every fixture in the corpus that has a settled outcome.
    # _evaluate_params will skip ones without enough prior data automatically.
    test_fixtures = [
        fx for fx in fixtures
        if backtest_ucl._outcome(fx) is not None
    ]
    log.info(
        "calibrate: qualifier grid over %d test fixtures × %d×%d params",
        len(test_fixtures), len(rho_grid), len(ko_grid),
    )

    # International football has ~6-10 fixtures per team per year (vs 50+
    # for club football), so the default MIN_PRIOR_MATCHES=3 wipes out most
    # of the corpus. Drop to 2 — keeps the prior window meaningful but
    # admits ~3× more test fixtures.
    QUAL_MIN_PRIORS = 2
    baseline = _evaluate_params(test_fixtures, fixtures, stats_cache,
                                 model.DEFAULT_PARAMS, min_priors=QUAL_MIN_PRIORS)
    results: list[dict] = []
    for rho, ko in product(rho_grid, ko_grid):
        params = model.ModelParams(rho=rho, ko_draw_damping=ko)
        score = _evaluate_params(test_fixtures, fixtures, stats_cache,
                                  params, min_priors=QUAL_MIN_PRIORS)
        if score is None:
            continue
        results.append({"params": {"rho": rho, "ko_draw_damping": ko}, **score})

    if not results:
        return {"ok": False, "reason": "no scorable fixtures"}

    results.sort(key=lambda r: r["avg_brier"])
    best = results[0]
    return {
        "ok": True,
        "corpus": "wc_qualifier_pool",
        "n_combinations": len(results),
        "n_test_fixtures": baseline["n"] if baseline else 0,
        "n_corpus_fixtures": len(fixtures),
        "baseline": baseline,
        "best": best,
        "improvement_brier": round(baseline["avg_brier"] - best["avg_brier"], 4) if baseline else None,
        "top_5": results[:5],
        "all_results": results,
    }


def save_league_params(league_key: str, params: model.ModelParams, source: dict) -> dict:
    """Persist tuned params to model_params_<league>.json. The `source` dict
    rides along for provenance (which corpus, which Brier improvement, etc.)."""
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
    path = _params_file(league_key)
    path.write_text(json.dumps(payload, indent=2))
    return {"saved_to": str(path), "payload": payload}


def load_league_params(league_key: str) -> model.ModelParams:
    """Read calibrated params for one league, falling back to defaults."""
    path = _params_file(league_key)
    if not path.exists():
        return model.DEFAULT_PARAMS
    try:
        payload = json.loads(path.read_text())
        p = payload.get("params", {})
        return model.ModelParams(
            rho=p.get("rho", model.DEFAULT_PARAMS.rho),
            game_weights=tuple(p.get("game_weights", model.DEFAULT_PARAMS.game_weights)),
            rest_tired_penalty=p.get("rest_tired_penalty", model.DEFAULT_PARAMS.rest_tired_penalty),
            injured_scorer_penalty=p.get("injured_scorer_penalty", model.DEFAULT_PARAMS.injured_scorer_penalty),
            ko_draw_damping=p.get("ko_draw_damping", model.DEFAULT_PARAMS.ko_draw_damping),
        )
    except Exception as e:
        log.warning("failed to load %s params: %s — using defaults", league_key, e)
        return model.DEFAULT_PARAMS


def has_league_params(league_key: str) -> bool:
    return _params_file(league_key).exists()


# Backwards-compat aliases — wc_calibrate.py + scheduler import these names.
save_wc_params = lambda params, source: save_league_params("wc", params, source)
load_wc_params = lambda: load_league_params("wc")
has_wc_params = lambda: has_league_params("wc")
