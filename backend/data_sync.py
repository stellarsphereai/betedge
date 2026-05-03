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
import re
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


_KNOCKOUT_ROUND_TOKENS = ("Round of 16", "Quarter-finals", "Semi-finals", "Final")
_GROUP_ROUND_TOKENS = ("Group Stage", "League Phase")
_GROUP_STAGE_DISCOUNT = 0.70   # downweight group-stage xG when blending


def _is_ucl_knockout_round(round_label: str | None) -> bool:
    if not round_label:
        return False
    return any(tok in round_label for tok in _KNOCKOUT_ROUND_TOKENS)


def _is_ucl_group_round(round_label: str | None) -> bool:
    if not round_label:
        return False
    return any(tok in round_label for tok in _GROUP_ROUND_TOKENS)


def _team_xg_history(
    team_id: int,
    recent: list[dict],
    stats_cache: dict[int, list[dict]],
    n: int = RECENT_FORM_WINDOW,
    *,
    knockout_only_for_ucl: bool = False,
    opponent_ratings: dict[int, dict] | None = None,
) -> tuple[list[float], list[float], dict]:
    """From a team's recent fixtures + cached stats, return (xg_for, xg_against)
    most recent first.

    When `knockout_only_for_ucl=True`, the model is predicting a UCL knockout
    fixture and group-stage data is unreliable for that prediction (group
    stage often features elite teams running up scores against weaker
    opposition; those numbers don't carry into knockout legs). Behavior:
      - If ≥3 knockout-stage fixtures available → use ONLY those.
      - Else → blend, discounting group-stage xG by 0.70 and signaling
        `fallback_used=True` so the caller can downgrade confidence.

    Returns (xg_for, xg_against, info) where info has:
      ko_count, group_count, fallback_used, mode ('knockout_only' or 'discounted_blend' or 'all')
    """
    # Lazy import to avoid cycles + keep this importable in standalone tests.
    try:
        import team_ratings as _team_ratings_mod
    except Exception:
        _team_ratings_mod = None
    apply_opponent_adj = (
        opponent_ratings is not None
        and _team_ratings_mod is not None
        and _team_ratings_mod.opponent_adjustment_enabled()
    )

    rows: list[tuple[str, float, float, str]] = []
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
        # Fix B: per-game opponent-strength adjustment.
        if apply_opponent_adj:
            opp_r = (opponent_ratings or {}).get(opp_id) or {}
            xg_self, xg_opp = _team_ratings_mod.adjust_xg(
                xg_self, xg_opp,
                opponent_attack_rating=opp_r.get("attack"),
                opponent_defense_rating=opp_r.get("defense"),
            )
        round_label = fx.get("league", {}).get("round") or ""
        rows.append((fx["fixture"]["date"], xg_self, xg_opp, round_label))
    rows.sort(key=lambda r: r[0], reverse=True)

    info = {"ko_count": 0, "group_count": 0, "fallback_used": False, "mode": "all"}
    if not knockout_only_for_ucl:
        rows = rows[:n]
        return [r[1] for r in rows], [r[2] for r in rows], info

    # UCL knockout path
    ko_rows = [r for r in rows if _is_ucl_knockout_round(r[3])]
    info["ko_count"] = len(ko_rows)
    info["group_count"] = sum(1 for r in rows if _is_ucl_group_round(r[3]))

    if len(ko_rows) >= 3:
        info["mode"] = "knockout_only"
        ko_rows = ko_rows[:n]
        return [r[1] for r in ko_rows], [r[2] for r in ko_rows], info

    # Fallback: blend with group-stage games discounted to 70%.
    info["mode"] = "discounted_blend"
    info["fallback_used"] = True
    capped = rows[:n]
    xg_for, xg_against = [], []
    for date, self_xg, opp_xg, round_label in capped:
        if _is_ucl_group_round(round_label):
            xg_for.append(self_xg * _GROUP_STAGE_DISCOUNT)
            xg_against.append(opp_xg * _GROUP_STAGE_DISCOUNT)
        else:
            xg_for.append(self_xg)
            xg_against.append(opp_xg)
    return xg_for, xg_against, info


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


_EPL_ROUND_RE = re.compile(r"(?i)regular season\s*-\s*(\d{1,2})")


def _epl_gameweek(round_label: str | None) -> int | None:
    """Extract the gameweek number from API-Football's round label.
    Examples: 'Regular Season - 36' → 36; 'Relegation - 1' → None.
    Used by the late-season tightening below — late-season EPL form
    matters more than the dragging season average that includes Aug–Oct.
    """
    if not round_label:
        return None
    m = _EPL_ROUND_RE.search(round_label)
    return int(m.group(1)) if m else None


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
    import dataclasses
    model_params = league_config.params_for_league(league)
    if league == "world_cup":
        wc_overlay = calibrate_engine.load_wc_params()
        # Keep league_config's gamma + season_blend, take rho/ko_damping
        # from the WC calibrated overlay.
        model_params = dataclasses.replace(
            model_params,
            rho=wc_overlay.rho,
            ko_draw_damping=wc_overlay.ko_draw_damping,
        )
    elif league == "ucl" and calibrate_engine.has_league_params("ucl"):
        # Same overlay shape for UCL — calibrated rho + ko_damping from
        # grid_search_ucl_knockouts. The model's structural over-confidence on
        # UCL knockouts is exactly what these params are tuned to fix.
        ucl_overlay = calibrate_engine.load_league_params("ucl")
        model_params = dataclasses.replace(
            model_params,
            rho=ucl_overlay.rho,
            ko_draw_damping=ucl_overlay.ko_draw_damping,
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
        team_names: dict[int, str] = {}
        for fx in fixtures:
            for side in ("home", "away"):
                t = fx["teams"][side]
                team_names[t["id"]] = team_aliases.canonical(t["name"])
        for tid in team_ids:
            try:
                ts = await api_football.team_statistics(client, tid, league_id, season, force=force)
                season_avg[tid] = api_football.season_avg_goals(ts)
            except api_football.PlanError as e:
                summary["errors"].append(f"team_stats {tid} blocked ({e})")
                season_avg[tid] = (None, None)

        # 4c. Refresh team_ratings — opponent-strength scores normalized to
        # league average (Fix B). The model only USES these when
        # OPPONENT_ADJUSTED_XG=true, but we populate the table either way so
        # the post-July-20 backtest has data to compare against.
        try:
            import team_ratings
            ratings_rows = []
            for tid in team_ids:
                f, a = season_avg.get(tid, (None, None))
                if f is None or a is None:
                    continue
                ratings_rows.append({
                    "team_id": tid,
                    "team_name": team_names.get(tid),
                    "season_xg_for": f,
                    "season_xg_against": a,
                    "games_played": RECENT_FORM_WINDOW,  # rough — refined later
                })
            n = team_ratings.upsert_ratings_for_league(league_id, season, ratings_rows)
            summary["ratings_upserted"] = n
        except Exception as e:
            log.exception("team_ratings upsert failed: %s", e)
            summary["errors"].append(f"team_ratings: {e}")

        # 5. Run model and upsert
        # Pull rating snapshot once per league/season — used by Fix B's
        # opponent-strength xG adjustment when the env flag is on.
        try:
            import team_ratings
            ratings_snapshot = team_ratings.get_ratings(league_id, season)
        except Exception:
            ratings_snapshot = {}
        for fx in fixtures:
            home_id = fx["teams"]["home"]["id"]
            away_id = fx["teams"]["away"]["id"]
            # For UCL knockout fixtures, restrict each team's xG window to
            # knockout-stage games only (or fall back to a group-stage-
            # discounted blend if <3 KO games available).
            this_round = fx.get("league", {}).get("round") or ""
            is_ucl_knockout = league == "ucl" and _is_knockout(this_round)
            home_xg_for, home_xg_against, home_xg_info = _team_xg_history(
                home_id, team_recent.get(home_id, []), stats_cache,
                knockout_only_for_ucl=is_ucl_knockout,
                opponent_ratings=ratings_snapshot,
            )
            away_xg_for, away_xg_against, away_xg_info = _team_xg_history(
                away_id, team_recent.get(away_id, []), stats_cache,
                knockout_only_for_ucl=is_ucl_knockout,
                opponent_ratings=ratings_snapshot,
            )
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
            # Knockout detection now also applies to UCL / UEL — the
            # ko_draw_damping path (draw-leak to home/away) and the
            # season-blend tightening below both rely on it.
            knockout = (
                league in ("world_cup", "ucl", "uel")
                and _is_knockout(fx.get("league", {}).get("round"))
            )
            # Knockout-stage tightening for UCL/UEL: the 10-game form window
            # is dominated by group-stage matches where elite teams ran up
            # scores against weaker opponents. Knockout legs are tactically
            # tighter and books prior-weight that. Lean more on the season
            # average (0.30 recent, 0.70 season) for these matches.
            match_params = model_params
            if knockout and league in ("ucl", "uel"):
                match_params = dataclasses.replace(match_params, season_blend=0.30)
            # EPL late-season tightening — by gameweek 30+ the 10-game window
            # captures roughly current shape, but the season baseline still
            # carries Aug–Oct results from before injuries / mid-season form
            # changes. Push more weight onto the recent window:
            #   GW <= 29: default 0.60 recent / 0.40 season
            #   GW 30-35: 0.70 / 0.30
            #   GW 36-38: 0.75 / 0.25
            if league == "epl":
                gw = _epl_gameweek(this_round)
                if gw is not None:
                    if gw >= 36:
                        match_params = dataclasses.replace(match_params, season_blend=0.75)
                    elif gw >= 30:
                        match_params = dataclasses.replace(match_params, season_blend=0.70)
            prediction = model.predict(home_form, away_form, knockout=knockout, params=match_params, league_id=league_id)

            # When the UCL knockout filter fell back to discounted-blend
            # (because either team had <3 knockout-stage games available),
            # the input data is structurally weaker — force LOW confidence
            # so downstream consumers (Top 3, digest, recommendations)
            # weight the prediction less.
            if is_ucl_knockout and (home_xg_info["fallback_used"] or away_xg_info["fallback_used"]):
                prediction.confidence = "LOW"

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
            # Per-team form snapshot — same numbers fed to predict() so the
            # AI analysis + bias detection don't have to re-fetch from the
            # API. Weighted attack/defense are recomputed here from the same
            # arrays + game_weights so they stay consistent with what the
            # model actually saw.
            home_atk, home_def = model.team_strengths(home_form, match_params)
            away_atk, away_def = model.team_strengths(away_form, match_params)
            with db() as conn:
                conn.execute(
                    """
                    INSERT INTO model_predictions
                        (match_id, home_team, away_team, league, kickoff_time,
                         home_win_pct, draw_pct, away_win_pct, btts_yes_pct,
                         home_xg, away_xg, confidence, score_matrix_json,
                         penalties_json, gamma_used, season_blend_used, anomaly_flagged,
                         home_games_xg_for, home_games_xg_against,
                         away_games_xg_for, away_games_xg_against,
                         home_attack_weighted, home_defense_weighted,
                         away_attack_weighted, away_defense_weighted,
                         home_rest_days, away_rest_days,
                         home_penalties_applied, away_penalties_applied,
                         home_season_avg_for, home_season_avg_against,
                         away_season_avg_for, away_season_avg_against)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(match_id) DO UPDATE SET
                        home_team               = excluded.home_team,
                        away_team               = excluded.away_team,
                        league                  = excluded.league,
                        kickoff_time            = excluded.kickoff_time,
                        home_win_pct            = excluded.home_win_pct,
                        draw_pct                = excluded.draw_pct,
                        away_win_pct            = excluded.away_win_pct,
                        btts_yes_pct            = excluded.btts_yes_pct,
                        home_xg                 = excluded.home_xg,
                        away_xg                 = excluded.away_xg,
                        confidence              = excluded.confidence,
                        score_matrix_json       = excluded.score_matrix_json,
                        penalties_json          = excluded.penalties_json,
                        gamma_used              = excluded.gamma_used,
                        season_blend_used       = excluded.season_blend_used,
                        anomaly_flagged         = excluded.anomaly_flagged,
                        home_games_xg_for       = excluded.home_games_xg_for,
                        home_games_xg_against   = excluded.home_games_xg_against,
                        away_games_xg_for       = excluded.away_games_xg_for,
                        away_games_xg_against   = excluded.away_games_xg_against,
                        home_attack_weighted    = excluded.home_attack_weighted,
                        home_defense_weighted   = excluded.home_defense_weighted,
                        away_attack_weighted    = excluded.away_attack_weighted,
                        away_defense_weighted   = excluded.away_defense_weighted,
                        home_rest_days          = excluded.home_rest_days,
                        away_rest_days          = excluded.away_rest_days,
                        home_penalties_applied  = excluded.home_penalties_applied,
                        away_penalties_applied  = excluded.away_penalties_applied,
                        home_season_avg_for     = excluded.home_season_avg_for,
                        home_season_avg_against = excluded.home_season_avg_against,
                        away_season_avg_for     = excluded.away_season_avg_for,
                        away_season_avg_against = excluded.away_season_avg_against,
                        created_at              = datetime('now')
                    """,
                    (match_id, home_form.name, away_form.name, league, kickoff_iso,
                     prediction.home_win_pct, prediction.draw_pct, prediction.away_win_pct,
                     prediction.btts_yes_pct,
                     prediction.home_xg, prediction.away_xg, prediction.confidence,
                     _json.dumps(prediction.score_matrix),
                     _json.dumps(penalties_combined),
                     match_params.home_gamma,
                     match_params.season_blend,
                     anomaly_flagged_int,
                     _json.dumps(home_form.xg_for),
                     _json.dumps(home_form.xg_against),
                     _json.dumps(away_form.xg_for),
                     _json.dumps(away_form.xg_against),
                     round(home_atk, 4), round(home_def, 4),
                     round(away_atk, 4), round(away_def, 4),
                     home_form.rest_days, away_form.rest_days,
                     _json.dumps(prediction.home_penalties_applied),
                     _json.dumps(prediction.away_penalties_applied),
                     home_form.season_avg_for, home_form.season_avg_against,
                     away_form.season_avg_for, away_form.season_avg_against),
                )
            summary["predictions_upserted"] += 1

    summary["ok"] = True
    return summary
