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

# Per-league fixture-lookahead window. EPL's weekly cadence makes a short
# horizon fine; tournaments with concentrated schedules need longer windows
# so all fixtures (group + early knockout) land in a single sync. WC's 35
# days covers the entire group stage in one pull and continues catching
# knockouts as they're scheduled into the API.
LOOKAHEAD_DAYS_BY_LEAGUE = {"epl": 7, "ucl": 14, "uel": 14, "world_cup": 35}
LOOKAHEAD_DAYS_DEFAULT = 7
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


# Knockout-round token detection is shared across UCL, UEL, and WC since
# all three use the same labels ("Round of 16", "Quarter-finals", etc.)
# at the API-Football layer. Functions retain the historic _ucl_ prefix
# for backwards compatibility with imports elsewhere in the file.
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
    # "final" alone matches "Final Round" (group stage matchday 3) — require
    # it to be the WHOLE label or preceded by a space after "semi"/"quarter".
    # Explicit tokens that unambiguously mean knockout stage:
    if any(k in r for k in ("knockout", "round of", "quarter-final", "semi-final")):
        return True
    # "Final" as a standalone round (e.g. "Final", "The Final") but NOT
    # "Final Round" or "Group ... Final" which are group stage labels.
    if "final" in r and "round" not in r and "group" not in r:
        return True
    return False


def _is_neutral_venue_final(round_label: str | None, league: str) -> bool:
    """UCL/UEL/WC finals (championship + 3rd-place) are at neutral venues —
    the nominal home side gets no real home-field advantage there, so the
    league's home_gamma multiplier should be overridden to 1.0 for these
    fixtures. Semi-finals and quarter-finals stay two-legged with normal
    home advantage and are excluded."""
    if league not in ("ucl", "uel", "world_cup"):
        return False
    r = (round_label or "").lower()
    if not r or "semi" in r or "quarter" in r or "round of" in r:
        return False
    return "final" in r


async def sync_daily(league: str = "epl", force: bool = False, lookahead_days: int | None = None) -> dict:
    league_id = LEAGUE_TO_API_FOOTBALL.get(league)
    if league_id is None:
        return {"ok": False, "reason": f"unknown league: {league}"}

    today = datetime.now(timezone.utc).date()
    from_date = today.isoformat()
    window = lookahead_days if lookahead_days is not None else LOOKAHEAD_DAYS_BY_LEAGUE.get(league, LOOKAHEAD_DAYS_DEFAULT)
    to_date = (today + timedelta(days=window)).isoformat()
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

        # 3. Per-team last-N fixtures + their stats.
        # Club leagues use API-Football's generic last-N-per-team lookup
        # filtered by league+season so cup ties against lower-division
        # opposition don't inflate xG.
        # WC sources from the qualifier corpus instead — the per-team
        # last-N lookup returns zero or mostly-untracked friendlies for
        # most national teams, and the corpus is the data set the model
        # was designed around (also what calibrate_engine grid-searched
        # against). Single corpus call covers all 32 WC participants and
        # populates both team_recent and stats_cache in one pass.
        team_ids = {fx["teams"]["home"]["id"] for fx in fixtures} | {fx["teams"]["away"]["id"] for fx in fixtures}
        # Build team_names early — needed by the WC confederation-strength
        # discount (step 4 of the fallback pipeline) before season_avg runs.
        team_names: dict[int, str] = {}
        for fx in fixtures:
            for side in ("home", "away"):
                t = fx["teams"][side]
                team_names[t["id"]] = team_aliases.canonical(t["name"])
        team_recent: dict[int, list[dict]] = {}
        stats_cache: dict[int, list[dict]] = {}
        # WC-only: goals-based season averages computed from the qualifier
        # corpus, used as a fallback xG signal for non-UEFA teams whose
        # corpus fixtures don't carry expected_goals (CONMEBOL/AFC/CAF/
        # CONCACAF/OFC qualifiers all return 0% xG coverage in API-Football).
        # Empty {} for other leagues; the per-fixture loop only consults
        # this when league == "world_cup".
        wc_goal_avg: dict[int, tuple[float, float]] = {}
        if league == "world_cup":
            import qualifier_corpus
            from collections import defaultdict
            try:
                corpus_fixtures, corpus_stats = await qualifier_corpus.load_full_corpus()
            except api_football.PlanError as e:
                summary["errors"].append(f"qualifier corpus blocked ({e})")
                return summary
            stats_cache = corpus_stats
            # Feed settled WC fixtures back into the team-form history so
            # group-stage results influence knockout-stage predictions.
            # The qualifier corpus alone is frozen at pre-tournament data;
            # without this merge, Argentina's R16 prediction would not
            # incorporate their group-stage performance. API-Football has
            # full xG coverage for WC matches (the desert is qualifiers
            # for non-UEFA regions), so settled WC games bring real signal.
            with db() as conn:
                settled_rows = conn.execute(
                    "SELECT match_id FROM fixtures "
                    "WHERE league='world_cup' AND result IS NOT NULL"
                ).fetchall()
            seen_fids = {fx["fixture"]["id"] for fx in corpus_fixtures}
            for r in settled_rows:
                mid = r["match_id"]
                if not (mid and mid.startswith("af-")):
                    continue
                try:
                    fid = int(mid[3:])
                except ValueError:
                    continue
                if fid in seen_fids:
                    continue
                try:
                    fx = await api_football.fetch_fixture(client, fid, force=False)
                except Exception as e:
                    summary["errors"].append(f"settled WC fetch {fid}: {e}")
                    continue
                if fx is None:
                    continue
                corpus_fixtures.append(fx)
                seen_fids.add(fid)
                try:
                    stats_cache[fid] = await api_football.fixture_statistics(
                        client, fid, force=False
                    )
                except api_football.PlanError as e:
                    summary["errors"].append(f"settled WC stats {fid} blocked ({e})")
            grouped: dict[int, list[dict]] = defaultdict(list)
            for fx in corpus_fixtures:
                home_id = fx["teams"]["home"]["id"]
                away_id = fx["teams"]["away"]["id"]
                if home_id in team_ids:
                    grouped[home_id].append(fx)
                if away_id in team_ids:
                    grouped[away_id].append(fx)
            # Goal averages for the fallback path. Four-step pipeline:
            #   (1) compute per-source-league baseline goals/team/match
            #   (2) rescale each team's raw avg from source-league units
            #       to WC context (CONMEBOL ~1.0 g/team is normal there
            #       but understates expected WC scoring; UEFA quals ~1.5
            #       overshoots slightly)
            #   (3) Bayesian shrinkage toward the international baseline
            #       so thin-sample teams regress to sanity
            #   (4) Confederation-strength discount — qualifier goals
            #       against weak confederations don't translate to WC.
            #       AFC/CONCACAF/OFC teams that scored freely against
            #       regional minnows get their fallback xG dampened;
            #       their xG-against gets inflated (they'll concede more
            #       at WC level). UEFA teams pass through unchanged.
            #
            # Without the rescale step, Ecuador's 0.78 goals/game CONMEBOL
            # avg crosses through Dixon-Coles vs Curaçao to ~0.1 xG and
            # the model gives 80% draw. With rescale to WC baseline, the
            # same "below-average team in their qualifying region" gets
            # mapped to a sensible international scoring rate.
            INTL_BASELINE = 1.4  # goals per team per match in WC fixtures
            FULL_WEIGHT_GAMES = 10

            # Step 4: confederation quality multipliers applied to the
            # rescaled+shrunk fallback. Values < 1.0 on attack mean "your
            # qualifier goals overstate WC attacking output"; values > 1.0
            # on defense mean "you'll concede more at WC than in qualifiers."
            # UEFA is the reference (1.0/1.0); others are discounted based
            # on historical WC group-stage performance vs qualification stats.
            CONFED_QUALITY = {
                #                  (attack_mult, defense_mult)
                "UEFA":      (1.00, 1.00),
                "CONMEBOL":  (0.90, 1.05),
                "CAF":       (0.80, 1.15),
                "AFC":       (0.75, 1.20),
                "CONCACAF":  (0.85, 1.10),
                "OFC":       (0.65, 1.30),
            }
            CONFED_DEFAULT = (0.80, 1.15)

            # Build tid → confederation lookup from wc_qualified_teams.
            # Index by both raw name and normalize_key to handle aliases
            # (e.g. "Ivory Coast" vs "Côte d'Ivoire").
            import wc_qualified_teams
            _name_to_confed: dict[str, str] = {}
            for _qt in wc_qualified_teams.WC_2026_QUALIFIED:
                _name_to_confed[_qt.name.lower()] = _qt.confederation
                _name_to_confed[team_aliases.normalize_key(_qt.name)] = _qt.confederation
            league_goals: dict[int, list[int]] = defaultdict(list)
            for fx in corpus_fixtures:
                g = fx.get("goals") or {}
                h, a = g.get("home"), g.get("away")
                if h is None or a is None:
                    continue
                lg_id = (fx.get("league") or {}).get("id")
                if lg_id is not None:
                    league_goals[lg_id].extend([h, a])
            league_baseline = {
                lg: (sum(vals) / len(vals)) if vals else INTL_BASELINE
                for lg, vals in league_goals.items()
            }

            for tid in team_ids:
                fxs = sorted(grouped.get(tid, []),
                             key=lambda f: f["fixture"]["date"], reverse=True)
                team_recent[tid] = fxs[:RECENT_FORM_WINDOW]
                gf, ga, ngames = 0, 0, 0
                league_appearances: dict[int, int] = defaultdict(int)
                for fx in fxs:
                    g = fx.get("goals") or {}
                    h, a = g.get("home"), g.get("away")
                    if h is None or a is None:
                        continue
                    if fx["teams"]["home"]["id"] == tid:
                        gf += h; ga += a
                    else:
                        gf += a; ga += h
                    ngames += 1
                    lg_id = (fx.get("league") or {}).get("id")
                    if lg_id is not None:
                        league_appearances[lg_id] += 1
                if ngames == 0:
                    continue
                raw_for = gf / ngames
                raw_against = ga / ngames
                # Weighted source baseline across the source leagues this
                # team actually played in. Defaults to INTL_BASELINE if a
                # league's baseline is missing or zero.
                total_app = sum(league_appearances.values()) or 1
                src_baseline = sum(
                    (league_baseline.get(lg) or INTL_BASELINE) * n
                    for lg, n in league_appearances.items()
                ) / total_app
                if src_baseline <= 0:
                    src_baseline = INTL_BASELINE
                scale = INTL_BASELINE / src_baseline
                rescaled_for = raw_for * scale
                rescaled_against = raw_against * scale
                # Cap the data weight at 80% so the international baseline
                # always retains ≥20% influence. Qualifier opposition is
                # structurally weaker than WC group-stage — even 10 games
                # against minnows shouldn't fully override the prior.
                # (Tunisia conceded 0 goals in 10 CAF qualifiers; without
                # the cap, shrunk_against = 0.0, producing phantom edges.)
                MAX_DATA_WEIGHT = 0.80
                w = min(ngames, FULL_WEIGHT_GAMES) / FULL_WEIGHT_GAMES * MAX_DATA_WEIGHT
                shrunk_for = w * rescaled_for + (1 - w) * INTL_BASELINE
                shrunk_against = w * rescaled_against + (1 - w) * INTL_BASELINE
                # Floor: no WC team's fallback xG should be below 0.30
                # (even the weakest WC side creates some chances / concedes
                # some goals at tournament level).
                WC_XG_FLOOR = 0.30
                shrunk_for = max(shrunk_for, WC_XG_FLOOR)
                shrunk_against = max(shrunk_against, WC_XG_FLOOR)
                # Step 4: confederation-strength discount
                tname = team_names.get(tid) or ""
                confed = (
                    _name_to_confed.get(tname.lower())
                    or _name_to_confed.get(team_aliases.normalize_key(tname))
                )
                atk_mult, def_mult = CONFED_QUALITY.get(confed or "", CONFED_DEFAULT)
                wc_goal_avg[tid] = (
                    shrunk_for * atk_mult,
                    shrunk_against * def_mult,
                )
        else:
            try:
                for tid in team_ids:
                    team_recent[tid] = await api_football.team_recent_fixtures(
                        client, tid, last=RECENT_FORM_WINDOW,
                        league=league_id, season=season,
                        force=force,
                    )
            except api_football.PlanError as e:
                summary["errors"].append(f"team-recent blocked ({e})")
                return summary
            unique_fixture_ids: set[int] = set()
            for recent in team_recent.values():
                for rfx in recent:
                    unique_fixture_ids.add(rfx["fixture"]["id"])
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
            # Tournament-knockout filter: for UCL / UEL / WC knockout
            # fixtures, restrict each team's xG window to knockout-stage
            # games only (or fall back to a group-stage-discounted blend
            # if <3 KO games available). Group-stage matches where strong
            # teams blow out minnows produce inflated xG that doesn't
            # transfer to knockout opposition.
            this_round = fx.get("league", {}).get("round") or ""
            is_tournament_knockout = (
                league in ("ucl", "uel", "world_cup")
                and _is_knockout(this_round)
            )
            if league == "world_cup":
                log.info("round-check: %s v %s round=%r knockout=%s",
                         fx["teams"]["home"]["name"], fx["teams"]["away"]["name"],
                         this_round, is_tournament_knockout)
            home_xg_for, home_xg_against, home_xg_info = _team_xg_history(
                home_id, team_recent.get(home_id, []), stats_cache,
                knockout_only_for_ucl=is_tournament_knockout,
                opponent_ratings=ratings_snapshot,
            )
            away_xg_for, away_xg_against, away_xg_info = _team_xg_history(
                away_id, team_recent.get(away_id, []), stats_cache,
                knockout_only_for_ucl=is_tournament_knockout,
                opponent_ratings=ratings_snapshot,
            )
            # WC fallback — API-Football only carries expected_goals for
            # UEFA WC qualifiers; CONMEBOL/AFC/CAF/CONCACAF/OFC return
            # 0% xG coverage. For sides whose xG history came back short,
            # blend whatever real xG samples we do have with the
            # qualifier-corpus goal average. As actual WC matches settle
            # the team accumulates real xG samples, so the weight on real
            # ramps up and the fallback recedes:
            #   0 real samples → full fallback (pre-tournament / non-UEFA opener)
            #   1 real sample  → 30% real / 70% fallback (after game 1)
            #   2 real samples → 60% real / 40% fallback (after game 2)
            #   3+ real samples → pure real xG, no fallback (R16 onward for non-UEFA)
            # European teams hit the 3+ bucket immediately from qualifier
            # xG and never see the blend or fallback.
            # Reduced from 5→3: fallback data shouldn't fill the same depth
            # as real match data — fewer synthetic samples means the model's
            # time-decay weights cover less ground, appropriately limiting
            # the fallback's influence on team_strengths().
            FALLBACK_REPLICATION = 3
            WC_BLEND_WEIGHT = {0: 0.0, 1: 0.30, 2: 0.60}
            # Spread applied to replicated values so the model doesn't see
            # "perfect consistency" from synthetic data.  ±8% of the mean
            # gives three distinct values while keeping the average stable.
            _FALLBACK_SPREAD = (1.08, 1.00, 0.92)

            def _wc_blend(xg_for, xg_against, avg, info):
                n = len(xg_for)
                if league != "world_cup" or n >= 3 or avg is None:
                    return xg_for, xg_against
                w = WC_BLEND_WEIGHT.get(n, 0.0)
                mean_for = (sum(xg_for) / n) if n else 0.0
                mean_against = (sum(xg_against) / n) if n else 0.0
                blended_for = w * mean_for + (1 - w) * avg[0]
                blended_against = w * mean_against + (1 - w) * avg[1]
                info["fallback_used"] = True
                info["mode"] = (
                    "season_avg_goals_fallback" if n == 0
                    else f"wc_blend_{n}sample_{int(w*100)}pct_real"
                )
                return (
                    [blended_for * s for s in _FALLBACK_SPREAD],
                    [blended_against * s for s in _FALLBACK_SPREAD],
                )

            home_xg_for, home_xg_against = _wc_blend(
                home_xg_for, home_xg_against, wc_goal_avg.get(home_id), home_xg_info
            )
            away_xg_for, away_xg_against = _wc_blend(
                away_xg_for, away_xg_against, wc_goal_avg.get(away_id), away_xg_info
            )
            if len(home_xg_for) < 3 or len(away_xg_for) < 3:
                summary["skipped_for_data"] += 1
                continue

            kickoff_iso = fx["fixture"]["date"]
            h_season_for, h_season_against = season_avg.get(home_id, (None, None))
            a_season_for, a_season_against = season_avg.get(away_id, (None, None))
            # Fix A part 2 (now also covers UEL + WC knockouts): for any
            # tournament knockout match, the season aggregate is dominated
            # by group-stage results where elite teams ran up scores
            # against weaker opponents. The 30/70 recent/season blend
            # below would otherwise pull the prediction back into
            # "group-stage" territory. Override the season component to
            # the same knockout-only xG history we already filtered for
            # the recent window — the team's full body of knockout work
            # IS the right baseline for a knockout match.
            if is_tournament_knockout:
                if home_xg_for and home_xg_against:
                    h_season_for     = sum(home_xg_for) / len(home_xg_for)
                    h_season_against = sum(home_xg_against) / len(home_xg_against)
                if away_xg_for and away_xg_against:
                    a_season_for     = sum(away_xg_for) / len(away_xg_for)
                    a_season_against = sum(away_xg_against) / len(away_xg_against)
            # Addition 1 — trend detection (last-3 vs prior-4 means).
            # Trend adjustments are multiplicative scalings of the form
            # arrays; downstream weighted-average + Poisson math then
            # naturally produces the trend-adjusted home_xg / away_xg.
            import trend_detection
            home_trend = trend_detection.compute_trends(home_xg_for, home_xg_against)
            away_trend = trend_detection.compute_trends(away_xg_for, away_xg_against)
            home_xg_for_adj, home_xg_against_adj = trend_detection.apply_trend_to_arrays(
                home_xg_for, home_xg_against, home_trend,
            )
            away_xg_for_adj, away_xg_against_adj = trend_detection.apply_trend_to_arrays(
                away_xg_for, away_xg_against, away_trend,
            )

            # Addition 2 — form-breakpoint detection. We test attack
            # (xg_for) and defense (xg_against) for both teams; if EITHER
            # side has a >25% deviation between last-5 and prev-5, we
            # override season_blend to 0.80/0.20 so the recent window
            # dominates. Pick the strongest detected breakpoint to
            # surface in the prediction record.
            breakpoint_overall = None
            breakpoint_team = None
            for side_name, xgf, xga in (
                ("home_attack",  home_xg_for, []),
                ("home_defense", [], home_xg_against),
                ("away_attack",  away_xg_for, []),
                ("away_defense", [], away_xg_against),
            ):
                bp = trend_detection.detect_breakpoint(xgf or xga)
                if bp.detected:
                    cand_team = team_aliases.canonical(
                        fx["teams"]["home" if side_name.startswith("home") else "away"]["name"]
                    )
                    # Prefer the largest-magnitude breakpoint
                    if (breakpoint_overall is None
                            or abs(bp.ratio - 1.0) > abs(breakpoint_overall.ratio - 1.0)):
                        breakpoint_overall = bp
                        breakpoint_overall.side = side_name
                        breakpoint_team = cand_team

            # Lineup check: if the match kicks off within 90 minutes, try
            # to fetch lineups and flag key-player absences. This overrides
            # the injury-list-based top_scorer_out with real lineup data
            # (rotation, tactical benching, late injuries not on the list).
            home_scorer_out = scorer_out_map.get(home_id, False)
            away_scorer_out = scorer_out_map.get(away_id, False)
            try:
                kickoff_dt = datetime.fromisoformat(kickoff_iso.replace("Z", "+00:00"))
                minutes_to_kickoff = (kickoff_dt - datetime.now(timezone.utc)).total_seconds() / 60
                if 0 < minutes_to_kickoff < 90:
                    lineups = await api_football.fixture_lineups(client, fx["fixture"]["id"])
                    if lineups:
                        # Get top-3 scorer IDs per team from the scorers list
                        top3: dict[int, list[int]] = defaultdict(list)
                        for s in (scrs if 'scrs' in dir() else []):
                            tid = s.get("statistics", [{}])[0].get("team", {}).get("id")
                            pid = s.get("player", {}).get("id")
                            if tid and pid and len(top3[tid]) < 3:
                                top3[tid].append(pid)
                        for tid, flag_attr in [(home_id, "home"), (away_id, "away")]:
                            if top3.get(tid):
                                missing = api_football.lineup_missing_key_players(lineups, tid, top3[tid])
                                if missing:
                                    if tid == home_id:
                                        home_scorer_out = True
                                    else:
                                        away_scorer_out = True
                                    log.info("lineup: %s missing key player(s) %s", team_names.get(tid), missing)
            except Exception:
                pass  # lineup check is best-effort

            home_form = model.TeamForm(
                name=team_aliases.canonical(fx["teams"]["home"]["name"]),
                xg_for=home_xg_for_adj,
                xg_against=home_xg_against_adj,
                rest_days=_rest_days_for_team(home_id, kickoff_iso, team_recent.get(home_id, [])),
                top_scorer_out=home_scorer_out,
                games_played=len(home_xg_for_adj),
                season_avg_for=h_season_for,
                season_avg_against=h_season_against,
            )
            away_form = model.TeamForm(
                name=team_aliases.canonical(fx["teams"]["away"]["name"]),
                xg_for=away_xg_for_adj,
                xg_against=away_xg_against_adj,
                rest_days=_rest_days_for_team(away_id, kickoff_iso, team_recent.get(away_id, [])),
                top_scorer_out=away_scorer_out,
                games_played=len(away_xg_for_adj),
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
            if knockout and league in ("ucl", "uel", "world_cup"):
                match_params = dataclasses.replace(match_params, season_blend=0.30)
            # EPL late-season tightening — by gameweek 30+ the 10-game window
            # captures roughly current shape, but the season baseline still
            # carries Aug–Oct results from before injuries / mid-season form
            # changes. Push more weight onto the recent window:
            #   GW <= 29: default 0.60 recent / 0.40 season
            #   GW 30-34: 0.70 / 0.30
            #   GW 35+:   0.80 / 0.20  (Addition 5 — late-season override)
            if league == "epl":
                gw = _epl_gameweek(this_round)
                if gw is not None:
                    if gw >= 35:
                        match_params = dataclasses.replace(match_params, season_blend=0.80)
                    elif gw >= 30:
                        match_params = dataclasses.replace(match_params, season_blend=0.70)
            # Addition 2 — form-breakpoint blend override. When either
            # side has a >25% recent-vs-prior swing, lean hard on the
            # recent window (80/20). Stronger override than EPL late-
            # season tightening; runs after it so it wins the tie.
            blend_overridden = False
            if breakpoint_overall is not None:
                match_params = dataclasses.replace(match_params, season_blend=trend_detection.BREAKPOINT_BLEND)
                blend_overridden = True
            # Neutral-venue override — every WC 2026 match is at a neutral
            # venue (USA/Canada/Mexico). The nominal "home" side in FIFA's
            # designation gets no real crowd/travel advantage, so override
            # home_gamma to 1.0. UCL/UEL finals also get this treatment.
            # Host nations (USA, Canada, Mexico) get a small residual
            # gamma (1.08) since they do have genuine home crowds.
            if league == "world_cup":
                home_name = team_aliases.canonical(fx["teams"]["home"]["name"])
                if home_name in ("USA", "Canada", "Mexico"):
                    match_params = dataclasses.replace(match_params, home_gamma=1.08)
                else:
                    match_params = dataclasses.replace(match_params, home_gamma=1.0)
            elif _is_neutral_venue_final(this_round, league):
                match_params = dataclasses.replace(match_params, home_gamma=1.0)
            blend_used = f"{int(round(match_params.season_blend*100))}/{int(round((1-match_params.season_blend)*100))}"
            prediction = model.predict(home_form, away_form, knockout=knockout, params=match_params, league_id=league_id)

            # Fix 2 — tactical suppressor adjustment. When either team is
            # in tactical_suppressors with classification='suppressor',
            # multiply the predicted xGs by the suppression factor before
            # we hand the prediction off to consumers. This dampens BTTS
            # Yes / Over probabilities for low-scoring tactical sides
            # (e.g. Atlético Madrid 0.75) without re-running the full
            # Dixon-Coles solve. Skip for h2h-only consumers since 1X2
            # is preserved via the existing prediction.
            try:
                import tactical_suppressors
                supp = tactical_suppressors.get_for_match(home_id, away_id)
                supp_match = supp["home"] or supp["away"]
                if supp_match and supp_match.get("classification") == "suppressor":
                    factor = float(supp_match.get("suppression_factor") or 1.0)
                    factor = max(0.5, min(1.5, factor))
                    prediction.home_xg = round(prediction.home_xg * factor, 3)
                    prediction.away_xg = round(prediction.away_xg * factor, 3)
                    prediction.btts_yes_pct = round(prediction.btts_yes_pct * factor, 4)
                    prediction.btts_no_pct = round(1.0 - prediction.btts_yes_pct, 4)
                    prediction.tactical_suppressor_applied = True
                    prediction.suppressor_team = supp_match.get("team_name")
                    prediction.suppressor_factor = factor
            except Exception:
                log.exception("tactical suppressor adjustment failed")

            # When the UCL knockout filter fell back to discounted-blend
            # (because either team had <3 knockout-stage games available),
            # the input data is structurally weaker — force LOW confidence
            # so downstream consumers (Top 3, digest, recommendations)
            # weight the prediction less.
            if is_tournament_knockout and (home_xg_info["fallback_used"] or away_xg_info["fallback_used"]):
                prediction.confidence = "LOW"

            # WC fallback confidence downgrade — when either side used the
            # confederation-average fallback, cap confidence since part of
            # the prediction relies on qualifier goals rather than match xG.
            # Only force LOW when BOTH sides are on full fallback (0 real
            # samples each). When one side has real data (e.g. Portugal 6
            # games vs Congo DR 0 games), the prediction is still primarily
            # driven by the well-scouted side → MEDIUM is appropriate.
            if league == "world_cup" and (home_xg_info.get("fallback_used") or away_xg_info.get("fallback_used")):
                if prediction.confidence == "HIGH":
                    prediction.confidence = "MEDIUM"
                home_full_fb = home_xg_info.get("mode") == "season_avg_goals_fallback"
                away_full_fb = away_xg_info.get("mode") == "season_avg_goals_fallback"
                if home_full_fb and away_full_fb:
                    prediction.confidence = "LOW"

            # Addition 3 — manager-change LOW confidence override.
            try:
                import manager_changes
                for tid in (home_id, away_id):
                    force_low, _note = manager_changes.should_force_low_confidence(tid)
                    if force_low:
                        prediction.confidence = "LOW"
                        break
            except Exception:
                pass

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
            home_atk, home_def = model.team_strengths(home_form, match_params, league_id=league_id)
            away_atk, away_def = model.team_strengths(away_form, match_params, league_id=league_id)
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
                         away_season_avg_for, away_season_avg_against,
                         home_attack_trend, home_defense_trend,
                         away_attack_trend, away_defense_trend,
                         trend_adjustment_applied,
                         form_breakpoint_detected, form_breakpoint_team,
                         breakpoint_ratio, blend_overridden, blend_used)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                        home_attack_trend       = excluded.home_attack_trend,
                        home_defense_trend      = excluded.home_defense_trend,
                        away_attack_trend       = excluded.away_attack_trend,
                        away_defense_trend      = excluded.away_defense_trend,
                        trend_adjustment_applied= excluded.trend_adjustment_applied,
                        form_breakpoint_detected= excluded.form_breakpoint_detected,
                        form_breakpoint_team    = excluded.form_breakpoint_team,
                        breakpoint_ratio        = excluded.breakpoint_ratio,
                        blend_overridden        = excluded.blend_overridden,
                        blend_used              = excluded.blend_used,
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
                     away_form.season_avg_for, away_form.season_avg_against,
                     home_trend.attack_trend, home_trend.defense_trend,
                     away_trend.attack_trend, away_trend.defense_trend,
                     int(home_trend.applied or away_trend.applied),
                     int(breakpoint_overall is not None),
                     breakpoint_team,
                     breakpoint_overall.ratio if breakpoint_overall else None,
                     int(blend_overridden), blend_used),
                )
            summary["predictions_upserted"] += 1
            # Invalidate cached AI analysis so the next view regenerates
            # with the fresh prediction data.
            try:
                import match_analysis
                match_analysis.invalidate_cache(match_id)
            except Exception:
                pass

    summary["ok"] = True
    return summary
