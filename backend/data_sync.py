"""Daily data sync orchestrator.

Pulls fixtures + per-team xG history + injuries + top scorers from API-Football,
runs the Dixon-Coles model, and upserts predictions. Wired to the scheduler's
00:00 job and the manual /sync-data/run endpoint.

Free-tier API-Football blocks the current 2025-26 EPL season AND the `last`
parameter — both raise PlanError. We surface these as structured failures so
the dashboard / digest can show "blocked: paid plan needed" without crashing.
"""
from __future__ import annotations

import logging
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Iterable

import httpx

import anomaly
import api_football
import calibrate_engine
import league_config
import model
import team_aliases
from database import db

log = logging.getLogger("arb.sync")

LEAGUE_TO_API_FOOTBALL = {
    "epl": api_football.EPL_LEAGUE_ID,
    "ucl": 2,   # UEFA Champions League
    "uel": 3,   # UEFA Europa League
    "world_cup": api_football.WORLD_CUP_LEAGUE_ID,
}

LOOKAHEAD_DAYS = 7
RECENT_FORM_WINDOW = 10  # last 10 matches blended with season-long averages


def _current_season(league: str, today: datetime | None = None) -> int:
    """API-Football season convention: year the season started."""
    today = today or datetime.now(timezone.utc)
    if league == "world_cup":
        return today.year  # WC2026 = season 2026
    # EPL: Aug→May. After July use this calendar year, else previous.
    return today.year if today.month >= 8 else today.year - 1


def _outcome_from_score(home_goals: int | None, away_goals: int | None) -> str | None:
    if home_goals is None or away_goals is None:
        return None
    if home_goals > away_goals:
        return "home"
    if away_goals > home_goals:
        return "away"
    return "draw"


def _store_fixture(conn: sqlite3.Connection, fx: dict, league: str) -> None:
    g = fx.get("goals") or {}
    conn.execute(
        """
        INSERT INTO fixtures (match_id, home_team, away_team, league, kickoff_time, result, home_goals, away_goals)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(match_id) DO UPDATE SET
            home_team = excluded.home_team,
            away_team = excluded.away_team,
            kickoff_time = excluded.kickoff_time,
            result = excluded.result,
            home_goals = excluded.home_goals,
            away_goals = excluded.away_goals
        """,
        (
            f"af-{fx['fixture']['id']}",
            team_aliases.canonical(fx["teams"]["home"]["name"]),
            team_aliases.canonical(fx["teams"]["away"]["name"]),
            league,
            fx["fixture"]["date"],
            _outcome_from_score(g.get("home"), g.get("away")),
            g.get("home"),
            g.get("away"),
        ),
    )


def _team_xg_history(
    team_id: int,
    recent: list[dict],
    stats_cache: dict[int, list[dict]],
    n: int = RECENT_FORM_WINDOW,
) -> tuple[list[float], list[float]]:
    """From a team's recent fixtures + cached stats, return (xg_for, xg_against)
    most recent first."""
    rows: list[tuple[str, float, float]] = []
    for fx in recent:
        fid = fx["fixture"]["id"]
        stats = stats_cache.get(fid)
        if not stats:
            continue
        h_id = fx["teams"]["home"]["id"]
        a_id = fx["teams"]["away"]["id"]
        if team_id not in (h_id, a_id):
            continue
        xg_self = api_football.expected_goals_for(stats, team_id)
        opp_id = a_id if team_id == h_id else h_id
        xg_opp = api_football.expected_goals_for(stats, opp_id)
        if xg_self is None or xg_opp is None:
            continue
        rows.append((fx["fixture"]["date"], xg_self, xg_opp))
    rows.sort(key=lambda r: r[0], reverse=True)
    rows = rows[:n]
    return [r[1] for r in rows], [r[2] for r in rows]


def _rest_days_for_team(team_id: int, target_kickoff: str, recent: list[dict]) -> int:
    target = datetime.fromisoformat(target_kickoff.replace("Z", "+00:00"))
    last_played: datetime | None = None
    for fx in recent:
        d = datetime.fromisoformat(fx["fixture"]["date"].replace("Z", "+00:00"))
        if d >= target:
            continue
        if last_played is None or d > last_played:
            last_played = d
    if last_played is None:
        return 4  # safe default
    return max(1, (target - last_played).days)


def _scorer_out_per_team(injuries: list[dict], scorers: list[dict]) -> dict[int, bool]:
    """True if any of a team's top-3 scorers is on the current injury list."""
    top3_by_team: dict[int, set[int]] = defaultdict(set)
    for s in scorers[:60]:  # API returns ~top scorers across the league
        team_id = s.get("statistics", [{}])[0].get("team", {}).get("id")
        player_id = s.get("player", {}).get("id")
        if team_id and player_id and len(top3_by_team[team_id]) < 3:
            top3_by_team[team_id].add(player_id)

    out: dict[int, bool] = {}
    injured_player_ids: set[int] = {
        i.get("player", {}).get("id") for i in injuries if i.get("player", {}).get("id")
    }
    for team_id, top3 in top3_by_team.items():
        out[team_id] = bool(top3 & injured_player_ids)
    return out


def _is_knockout(round_label: str | None) -> bool:
    if not round_label:
        return False
    r = round_label.lower()
    return any(k in r for k in ("knockout", "round of", "quarter", "semi", "final"))


async def sync_daily(league: str = "epl", force: bool = False) -> dict:
    league_id = LEAGUE_TO_API_FOOTBALL.get(league)
    if league_id is None:
        return {"ok": False, "reason": f"unknown league: {league}"}

    today = datetime.now(timezone.utc).date()
    from_date = today.isoformat()
    to_date = (today + timedelta(days=LOOKAHEAD_DAYS)).isoformat()
    season = _current_season(league)

    summary: dict = {
        "ok": False, "league": league, "season": season,
        "from_date": from_date, "to_date": to_date,
        "fixtures": 0, "predictions_upserted": 0,
        "skipped_for_data": 0, "errors": [],
    }

    # Per-league model knobs (gamma, season_blend) come from league_config.
    # WC additionally has calibrated rho/ko_damping persisted in
    # model_params_wc.json — overlay those onto the league_config baseline so
    # both signals apply.
    model_params = league_config.params_for_league(league)
    if league == "world_cup":
        wc_overlay = calibrate_engine.load_wc_params()
        # Keep league_config's gamma + season_blend, take rho/ko_damping
        # from the WC calibrated overlay.
        import dataclasses
        model_params = dataclasses.replace(
            model_params,
            rho=wc_overlay.rho,
            ko_draw_damping=wc_overlay.ko_draw_damping,
        )

    async with httpx.AsyncClient() as client:
        # 1. Fixtures in the lookahead window
        try:
            fixtures = await api_football.fixtures_by_date_range(
                client, league_id, season, from_date, to_date, force=force
            )
        except api_football.PlanError as e:
            summary["errors"].append(f"fixtures: paid plan needed ({e})")
            return summary
        summary["fixtures"] = len(fixtures)

        with db() as conn:
            for fx in fixtures:
                _store_fixture(conn, fx, league)

        if not fixtures:
            summary["ok"] = True
            return summary

        # 2. League-level injury / scorer data
        scorer_out_map: dict[int, bool] = {}
        try:
            inj = await api_football.injuries(client, league_id, season, force=force)
            scrs = await api_football.top_scorers(client, league_id, season, force=force)
            scorer_out_map = _scorer_out_per_team(inj, scrs)
        except api_football.PlanError as e:
            summary["errors"].append(f"injuries/scorers blocked ({e}); top_scorer_out=False")

        # 3. Per-team last-N fixtures (paid-only `last` param)
        team_ids = {fx["teams"]["home"]["id"] for fx in fixtures} | {fx["teams"]["away"]["id"] for fx in fixtures}
        team_recent: dict[int, list[dict]] = {}
        try:
            for tid in team_ids:
                team_recent[tid] = await api_football.team_recent_fixtures(
                    client, tid, last=RECENT_FORM_WINDOW, league=league_id, season=season,
                    force=force,
                )
        except api_football.PlanError as e:
            summary["errors"].append(f"team-recent blocked ({e})")
            return summary

        # 4. Stats per unique recent fixture (xG)
        unique_fixture_ids: set[int] = set()
        for recent in team_recent.values():
            for rfx in recent:
                unique_fixture_ids.add(rfx["fixture"]["id"])

        stats_cache: dict[int, list[dict]] = {}
        for fid in unique_fixture_ids:
            try:
                stats_cache[fid] = await api_football.fixture_statistics(client, fid, force=force)
            except api_football.PlanError as e:
                summary["errors"].append(f"stats for {fid} blocked ({e})")

        # 4b. Per-team season averages (goals for/against per match) — feed
        # the blend in model.team_strengths so a hot/cold 10-game stretch is
        # tempered by the team's full-season baseline.
        season_avg: dict[int, tuple[float | None, float | None]] = {}
        for tid in team_ids:
            try:
                ts = await api_football.team_statistics(client, tid, league_id, season, force=force)
                season_avg[tid] = api_football.season_avg_goals(ts)
            except api_football.PlanError as e:
                summary["errors"].append(f"team_stats {tid} blocked ({e})")
                season_avg[tid] = (None, None)

        # 5. Run model and upsert
        for fx in fixtures:
            home_id = fx["teams"]["home"]["id"]
            away_id = fx["teams"]["away"]["id"]
            home_xg_for, home_xg_against = _team_xg_history(home_id, team_recent.get(home_id, []), stats_cache)
            away_xg_for, away_xg_against = _team_xg_history(away_id, team_recent.get(away_id, []), stats_cache)
            if len(home_xg_for) < 3 or len(away_xg_for) < 3:
                summary["skipped_for_data"] += 1
                continue

            kickoff_iso = fx["fixture"]["date"]
            h_season_for, h_season_against = season_avg.get(home_id, (None, None))
            a_season_for, a_season_against = season_avg.get(away_id, (None, None))
            home_form = model.TeamForm(
                name=team_aliases.canonical(fx["teams"]["home"]["name"]),
                xg_for=home_xg_for,
                xg_against=home_xg_against,
                rest_days=_rest_days_for_team(home_id, kickoff_iso, team_recent.get(home_id, [])),
                top_scorer_out=scorer_out_map.get(home_id, False),
                games_played=len(home_xg_for),
                season_avg_for=h_season_for,
                season_avg_against=h_season_against,
            )
            away_form = model.TeamForm(
                name=team_aliases.canonical(fx["teams"]["away"]["name"]),
                xg_for=away_xg_for,
                xg_against=away_xg_against,
                rest_days=_rest_days_for_team(away_id, kickoff_iso, team_recent.get(away_id, [])),
                top_scorer_out=scorer_out_map.get(away_id, False),
                games_played=len(away_xg_for),
                season_avg_for=a_season_for,
                season_avg_against=a_season_against,
            )
            knockout = league == "world_cup" and _is_knockout(fx.get("league", {}).get("round"))
            prediction = model.predict(home_form, away_form, knockout=knockout, params=model_params, league_id=league_id)

            match_id = f"af-{fx['fixture']['id']}"

            # Prediction-level anomaly checks: penalty stack (>1 penalty same
            # team) + form-vs-season divergence (>40% relative). Bet-level
            # checks (edge thresholds, sharp-book disagreement) run later in
            # /ev-bets when book prices are available.
            flags: list[anomaly.Anomaly] = []
            flags += anomaly.detect_penalty_stack(prediction, match_id=match_id)
            flags += anomaly.detect_form_divergence(home_form, "home", prediction, match_id=match_id)
            flags += anomaly.detect_form_divergence(away_form, "away", prediction, match_id=match_id)
            if flags:
                anomaly.log_many(flags)

            # Capture the per-league knobs that were applied to this
            # prediction. The 23:55 self-eval reads these to backfill the
            # prediction_results row when the match settles.
            penalties_combined = sorted(set(
                prediction.home_penalties_applied + prediction.away_penalties_applied
            ))
            anomaly_flagged_int = 1 if flags else 0
            import json as _json
            with db() as conn:
                conn.execute(
                    """
                    INSERT INTO model_predictions
                        (match_id, home_team, away_team, league, kickoff_time,
                         home_win_pct, draw_pct, away_win_pct, btts_yes_pct,
                         home_xg, away_xg, confidence, score_matrix_json,
                         penalties_json, gamma_used, season_blend_used, anomaly_flagged)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(match_id) DO UPDATE SET
                        home_team         = excluded.home_team,
                        away_team         = excluded.away_team,
                        league            = excluded.league,
                        kickoff_time      = excluded.kickoff_time,
                        home_win_pct      = excluded.home_win_pct,
                        draw_pct          = excluded.draw_pct,
                        away_win_pct      = excluded.away_win_pct,
                        btts_yes_pct      = excluded.btts_yes_pct,
                        home_xg           = excluded.home_xg,
                        away_xg           = excluded.away_xg,
                        confidence        = excluded.confidence,
                        score_matrix_json = excluded.score_matrix_json,
                        penalties_json    = excluded.penalties_json,
                        gamma_used        = excluded.gamma_used,
                        season_blend_used = excluded.season_blend_used,
                        anomaly_flagged   = excluded.anomaly_flagged,
                        created_at        = datetime('now')
                    """,
                    (match_id, home_form.name, away_form.name, league, kickoff_iso,
                     prediction.home_win_pct, prediction.draw_pct, prediction.away_win_pct,
                     prediction.btts_yes_pct,
                     prediction.home_xg, prediction.away_xg, prediction.confidence,
                     _json.dumps(prediction.score_matrix),
                     _json.dumps(penalties_combined),
                     model_params.home_gamma,
                     model_params.season_blend,
                     anomaly_flagged_int),
                )
            summary["predictions_upserted"] += 1

    summary["ok"] = True
    return summary
