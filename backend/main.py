"""FastAPI app — Phase A endpoints.

Implements all eight endpoints from the spec, wired against the running
SQLite + Odds API. The model still relies on caller-supplied TeamForm
(real Understat/API-Football fetchers come in Phase B), so /run-model
accepts xG payloads in the request body.
"""
from __future__ import annotations

import json
import os
import secrets
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, Field

import anomaly
import api_quota
import book_balance
import calibrate
import calibrate_engine
import league_config
import match_analysis
import self_eval
import wc_calibrate

import accuracy
import api_football
import backtest
import clv_tracker
import data_sync
import digest
import team_aliases
import ev_calculator
import historical_lines
import kelly
import line_shopper
import model
import odds_client
import scheduler as sched
import understat
from database import db, init_db

load_dotenv()

LEAGUE_MODE = os.getenv("LEAGUE_MODE", "epl").lower()
BANKROLL = float(os.getenv("BANKROLL", "1000"))
MIN_EDGE = float(os.getenv("MIN_EDGE", "0.03"))
MAX_STAKE_PCT = float(os.getenv("MAX_STAKE_PCT", "0.02"))

# WC-specific guardrails (real-money betting on a sparse, structurally weaker
# corpus). Tighter floor on edge, smaller stake cap, only HIGH-confidence
# predictions, model must agree with market consensus, and a daily loss cap
# that locks out further actionable WC bets once breached.
WC_MIN_EDGE = float(os.getenv("WC_MIN_EDGE", "0.08"))
WC_MAX_STAKE_PCT = float(os.getenv("WC_MAX_STAKE_PCT", "0.005"))
WC_HIGH_CONFIDENCE_ONLY = os.getenv("WC_HIGH_CONFIDENCE_ONLY", "true").strip().lower() == "true"
WC_REQUIRE_MARKET_AGREEMENT = os.getenv("WC_REQUIRE_MARKET_AGREEMENT", "true").strip().lower() == "true"
WC_DAILY_LOSS_CAP_PCT = float(os.getenv("WC_DAILY_LOSS_CAP_PCT", "0.02"))

LEAGUE_TO_SPORT_KEY = {
    "epl": "soccer_epl",
    "ucl": "soccer_uefa_champs_league",
    "uel": "soccer_uefa_europa_league",
    "world_cup": "soccer_fifa_world_cup",
}


def _league_risk_config(league: str, base_min_edge: float) -> dict:
    """Per-league risk knobs. WC tightens edge floor and stake cap, gates on
    confidence, and requires the model to agree with the market on direction."""
    if league == "world_cup":
        return {
            "min_edge": max(base_min_edge, WC_MIN_EDGE),
            "max_stake_pct": WC_MAX_STAKE_PCT,
            "high_confidence_only": WC_HIGH_CONFIDENCE_ONLY,
            "require_market_agreement": WC_REQUIRE_MARKET_AGREEMENT,
            "daily_loss_cap_pct": WC_DAILY_LOSS_CAP_PCT,
            "real_money": True,
        }
    return {
        "min_edge": base_min_edge,
        "max_stake_pct": MAX_STAKE_PCT,
        "high_confidence_only": False,
        "require_market_agreement": False,
        "daily_loss_cap_pct": None,
        "real_money": False,
    }


def _wc_loss_today() -> float:
    """Net WC P&L for today (negative if losing). Used to decide whether to
    lock new WC bets behind the daily-loss cap."""
    today = datetime.now(timezone.utc).date().isoformat()
    with db() as conn:
        row = conn.execute(
            """
            SELECT COALESCE(SUM(b.profit), 0) AS pnl
            FROM bets_placed b
            JOIN model_predictions p ON p.match_id = b.match_id
            WHERE p.league = 'world_cup'
              AND date(b.timestamp) = ?
              AND b.status IN ('won', 'lost')
            """,
            (today,),
        ).fetchone()
    return float(row["pnl"] or 0.0)


def _market_argmax(consensus_for_match: dict, market: str, line: float | None) -> str | None:
    if not consensus_for_match:
        return None
    if market == "totals":
        node = (consensus_for_match.get("totals") or {}).get(str(line))
    else:
        node = consensus_for_match.get(market)
    if not node:
        return None
    return max(node, key=node.get)


def _devig_avg_for_offers(per_book: dict[str, dict[str, float]]) -> dict[str, float] | None:
    """Average of per-book de-vigged implied probabilities. Returns None if no
    book had a usable price set."""
    sums: dict[str, float] = {}
    n = 0
    for _book, odds in per_book.items():
        implied = {k: 1.0 / v for k, v in odds.items() if v and v > 1.0}
        total = sum(implied.values())
        if total <= 0:
            continue
        for k, v in implied.items():
            sums[k] = sums.get(k, 0.0) + (v / total)
        n += 1
    if n == 0:
        return None
    return {k: round(v / n, 4) for k, v in sums.items()}


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    book_balance.seed_from_env()  # idempotent — won't overwrite existing balances
    sched.auto_start_if_enabled()
    yield
    sched.stop()


# --- Admin Basic-auth ---------------------------------------------------------
# Manual syncs are NEVER exposed to anonymous callers or to the dashboard. The
# only path is /admin/* under HTTP Basic auth, intended for emergency use.

_basic_auth = HTTPBasic(auto_error=False)


def admin_auth(credentials: HTTPBasicCredentials = Depends(_basic_auth)) -> str:
    expected_user = os.getenv("ADMIN_USER", "admin")
    expected_pass = os.getenv("ADMIN_PASSWORD", "")
    if not expected_pass:
        raise HTTPException(503, "ADMIN_PASSWORD not set in .env")
    if not credentials:
        raise HTTPException(
            401, "auth required", headers={"WWW-Authenticate": "Basic"}
        )
    ok_user = secrets.compare_digest(credentials.username, expected_user)
    ok_pass = secrets.compare_digest(credentials.password, expected_pass)
    if not (ok_user and ok_pass):
        raise HTTPException(
            401, "auth failed", headers={"WWW-Authenticate": "Basic"}
        )
    return credentials.username


app = FastAPI(lifespan=lifespan, title="BetEdge NY")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------- request/response models ----------


class TeamFormInput(BaseModel):
    name: str
    xg_for: list[float]
    xg_against: list[float]
    rest_days: int = 4
    top_scorer_out: bool = False


class MatchInput(BaseModel):
    match_id: str
    home: TeamFormInput
    away: TeamFormInput
    knockout: bool = False
    league: str = "epl"
    kickoff_time: str | None = None


class RunModelInput(BaseModel):
    matches: list[MatchInput]


class BetInput(BaseModel):
    match_id: str
    home_team: str
    away_team: str
    bet_type: str = Field(..., description="home/draw/away/yes/no/over/under")
    book: str
    odds_at_placement: float
    stake: float
    edge_at_placement: float = 0.0
    is_paper: bool | None = None  # if None, derive from LEAGUE_MODE
    market: str | None = None         # h2h / btts / totals
    market_line: float | None = None  # only for totals


# ---------- internal helpers ----------


def _store_prediction(match: MatchInput, prediction: model.MatchPrediction) -> int:
    with db() as conn:
        conn.execute(
            """
            INSERT INTO model_predictions
                (match_id, home_team, away_team, league, kickoff_time,
                 home_win_pct, draw_pct, away_win_pct, home_xg, away_xg, confidence)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(match_id) DO UPDATE SET
                home_team    = excluded.home_team,
                away_team    = excluded.away_team,
                league       = excluded.league,
                kickoff_time = excluded.kickoff_time,
                home_win_pct = excluded.home_win_pct,
                draw_pct     = excluded.draw_pct,
                away_win_pct = excluded.away_win_pct,
                home_xg      = excluded.home_xg,
                away_xg      = excluded.away_xg,
                confidence   = excluded.confidence,
                created_at   = datetime('now')
            """,
            (
                match.match_id, prediction.home_team, prediction.away_team,
                match.league, match.kickoff_time,
                prediction.home_win_pct, prediction.draw_pct, prediction.away_win_pct,
                prediction.home_xg, prediction.away_xg, prediction.confidence,
            ),
        )
        cur = conn.execute(
            "SELECT id FROM model_predictions WHERE match_id = ?", (match.match_id,)
        )
        return cur.fetchone()[0]


def _to_team_form(payload: TeamFormInput) -> model.TeamForm:
    return model.TeamForm(
        name=payload.name,
        xg_for=list(payload.xg_for),
        xg_against=list(payload.xg_against),
        rest_days=payload.rest_days,
        top_scorer_out=payload.top_scorer_out,
        games_played=len(payload.xg_for),
    )


# ---------- endpoints ----------


@app.get("/")
async def root():
    return {
        "status": "ok",
        "mode": LEAGUE_MODE,
        "endpoints": [
            "/predictions", "/ev-bets", "/bets", "/stats", "/fixtures",
            "/run-model", "/digest-preview", "/send-digest",
        ],
    }


@app.post("/run-model")
async def run_model(payload: RunModelInput):
    """Run Dixon-Coles on a batch of fixtures and persist the predictions."""
    results = []
    for match in payload.matches:
        prediction = model.predict(
            _to_team_form(match.home),
            _to_team_form(match.away),
            knockout=match.knockout,
            league_id=data_sync.LEAGUE_TO_API_FOOTBALL.get(match.league),
        )
        pred_id = _store_prediction(match, prediction)
        results.append({"id": pred_id, **prediction.__dict__})
    return {"count": len(results), "predictions": results}


@app.get("/predictions")
async def get_predictions(limit: int = Query(50, ge=1, le=500)):
    with db() as conn:
        rows = conn.execute(
            """
            SELECT * FROM model_predictions
            ORDER BY created_at DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return {"count": len(rows), "predictions": [dict(r) for r in rows]}


# In-process TTL cache for /ev-bets responses, keyed by (league, bankroll,
# min_edge). The Odds API meters by markets×regions per call and we burn a
# pile of credits per /ev-bets invocation, so dashboard polling and digest
# pre-warm both ride this cache. `?force=true` bypasses it.
_EV_CACHE_TTL_S = float(os.getenv("EV_CACHE_TTL_S", "1800"))  # 30 min default
_EV_CACHE: dict[tuple, tuple[float, dict]] = {}


@app.get("/ev-bets")
async def get_ev_bets(
    bankroll: float = Query(BANKROLL, ge=100.0, le=1_000_000.0),
    min_edge: float = Query(MIN_EDGE, ge=0.0, le=0.5),
    league: str | None = None,
    force: bool = Query(False, description="bypass the TTL cache and re-hit the Odds API"),
):
    """Cross today's predictions with current Odds API prices and emit +EV bets.

    Cached for EV_CACHE_TTL_S (default 1800s = 30 min) per (league, bankroll,
    min_edge). Pass `?force=true` to bypass the cache."""
    league = (league or LEAGUE_MODE).lower()
    sport_key = LEAGUE_TO_SPORT_KEY.get(league)
    if not sport_key:
        raise HTTPException(400, f"unknown league: {league}")

    cache_key = (league, round(bankroll, 2), round(min_edge, 4))
    if not force:
        hit = _EV_CACHE.get(cache_key)
        if hit and (time.time() - hit[0]) < _EV_CACHE_TTL_S:
            cached = dict(hit[1])
            cached["cache"] = {
                "hit": True,
                "age_s": round(time.time() - hit[0], 1),
                "ttl_s": _EV_CACHE_TTL_S,
            }
            return cached

    risk = _league_risk_config(league, min_edge)
    effective_min_edge = risk["min_edge"]

    # Real-money guardrail: lock out further actionable WC bets once today's
    # cumulative WC loss breaches the daily cap. Bets are still surfaced (so
    # the user sees what's available) but `actionable=false` with a reason.
    lockout_reason: str | None = None
    if risk["real_money"] and risk["daily_loss_cap_pct"]:
        loss_today = _wc_loss_today()
        cap = -bankroll * risk["daily_loss_cap_pct"]
        if loss_today <= cap:
            lockout_reason = (
                f"daily WC loss cap hit: {loss_today:.2f} ≤ {cap:.2f} "
                f"({risk['daily_loss_cap_pct']:.1%} of {bankroll:.0f})"
            )

    with db() as conn:
        # match_id is UNIQUE in model_predictions, so plain WHERE returns at
        # most one row per match. ORDER BY for stable iteration.
        preds = conn.execute(
            """
            SELECT match_id, home_team, away_team, home_win_pct, draw_pct,
                   away_win_pct, btts_yes_pct, score_matrix_json, confidence
            FROM model_predictions
            WHERE league = ?
            ORDER BY created_at DESC
            """,
            (league,),
        ).fetchall()

    if not preds:
        return {"count": 0, "league": league, "bets": [], "note": "no predictions for league"}

    # Cross-source team names disagree (Bayern Munich / Bayern München, Atlético /
    # Atletico, etc.) so we join on a normalized key, not raw strings.
    def _key(home: str, away: str) -> tuple[str, str]:
        return (team_aliases.normalize_key(home), team_aliases.normalize_key(away))

    async with httpx.AsyncClient() as client:
        try:
            raw = await odds_client.fetch_odds(client, sport_key)
        except Exception as e:
            raise HTTPException(502, f"Odds API error: {e}")

        # BTTS isn't on the sport-level odds endpoint — fetch per-fixture and merge
        # it in. Only do it for fixtures we actually have predictions for to keep
        # quota cost bounded.
        pred_keys = {_key(p["home_team"], p["away_team"]) for p in preds}
        for m in raw:
            if _key(m.get("home_team"), m.get("away_team")) not in pred_keys:
                continue
            try:
                btts_books = await odds_client.fetch_event_btts(client, sport_key, m["id"])
                odds_client.merge_btts_into_match(m, btts_books)
            except Exception:
                pass  # btts is best-effort; absence just means no btts EV rows

    by_pair: dict[tuple[str, str], dict] = {
        _key(m["home_team"], m["away_team"]): m for m in raw
    }

    # Skip in-play / completed fixtures: pre-match model probabilities are
    # meaningless against live in-play odds (state-dependent), and pre-match
    # markets that have already triggered get suspended by the books anyway.
    now_utc = datetime.now(timezone.utc)

    bets: list[dict] = []
    for p in preds:
        match = by_pair.get(_key(p["home_team"], p["away_team"]))
        if not match:
            continue
        commence = match.get("commence_time")
        if commence:
            try:
                if datetime.fromisoformat(commence.replace("Z", "+00:00")) <= now_utc:
                    continue
            except ValueError:
                pass

        # WC: only HIGH-confidence predictions clear the gate (the corpus is
        # too sparse + structurally different to bet real money on MED/LOW).
        if risk["high_confidence_only"] and (p["confidence"] or "").upper() != "HIGH":
            continue

        markets = odds_client.parse_all_markets(match)

        # h2h scan
        ev_bets: list[ev_calculator.EVBet] = []
        offer_lookup: dict[tuple[str, float | None], dict[str, dict[str, float]]] = {}

        if markets["h2h"]:
            ev_bets += ev_calculator.find_ev_bets_market(
                market="h2h", market_label=None, market_line=None,
                match_id=p["match_id"],
                home_team=p["home_team"], away_team=p["away_team"],
                model_probs={"home": p["home_win_pct"], "draw": p["draw_pct"], "away": p["away_win_pct"]},
                confidence=p["confidence"],
                offers_by_book=markets["h2h"],
                min_edge=effective_min_edge,
                outcomes=("home", "draw", "away"),
            )
            offer_lookup[("h2h", None)] = markets["h2h"]

        # btts scan
        btts_yes = p["btts_yes_pct"]
        if markets["btts"] and btts_yes is not None:
            ev_bets += ev_calculator.find_ev_bets_market(
                market="btts", market_label="BTTS", market_line=None,
                match_id=p["match_id"],
                home_team=p["home_team"], away_team=p["away_team"],
                model_probs={"yes": btts_yes, "no": 1.0 - btts_yes},
                confidence=p["confidence"],
                offers_by_book=markets["btts"],
                min_edge=effective_min_edge,
                outcomes=("yes", "no"),
            )
            offer_lookup[("btts", None)] = markets["btts"]

        # totals scan — derive over% from stored score matrix per line the books offer
        if markets["totals"] and p["score_matrix_json"]:
            try:
                matrix = json.loads(p["score_matrix_json"])
            except Exception:
                matrix = None
            if matrix:
                for line, by_book in markets["totals"].items():
                    over = sum(matrix[h][a] for h in range(len(matrix)) for a in range(len(matrix[0])) if (h + a) > line)
                    ev_bets += ev_calculator.find_ev_bets_market(
                        market="totals", market_label=f"Over/Under {line}", market_line=line,
                        match_id=p["match_id"],
                        home_team=p["home_team"], away_team=p["away_team"],
                        model_probs={"over": over, "under": 1.0 - over},
                        confidence=p["confidence"],
                        offers_by_book=by_book,
                        min_edge=effective_min_edge,
                        outcomes=("over", "under"),
                    )
                    offer_lookup[("totals", line)] = by_book

        for b in ev_bets:
            offers = offer_lookup.get((b.market, b.market_line)) or {}
            outcome_offers = {bk: o[b.outcome] for bk, o in offers.items() if b.outcome in o}
            shop = line_shopper.best_line(outcome_offers, opening_odds=None, edge=b.edge)
            kelly_stake_full = kelly.kelly_stake(b.edge, b.decimal_odds, bankroll, risk["max_stake_pct"])
            # Cap by per-book balance for the recommended book. If the book
            # isn't tracked (e.g. Caesars/BetRivers), available is None and
            # we skip the cap. Stake-reduced bets carry a flag the UI shows.
            recommended_book = shop.best_book if shop else b.book
            book_key = book_balance.normalize_book(recommended_book)
            available = book_balance.get_balance(book_key) if book_key else None
            stake_reduced = False
            top_up_book = None
            if available is not None and kelly_stake_full > available:
                stake = float(round(max(0.0, available)))
                if stake > 0:
                    stake_reduced = True
                    top_up_book = recommended_book
            else:
                stake = kelly_stake_full

            # De-vigged market consensus across all books — used both for
            # the WC market-agreement gate and for the new
            # MARKET_CONSENSUS_DIVERGENCE anomaly.
            avg = _devig_avg_for_offers(offers)
            consensus_prob = (avg or {}).get(b.outcome) if avg else None

            # Market-agreement gate (WC): drop bets where the model's pick
            # disagrees with the de-vigged market consensus on direction —
            # books are sharper than us on this corpus, so a hard disagreement
            # is more likely a model error than an inefficiency.
            if risk["require_market_agreement"] and avg:
                market_pick = max(avg, key=avg.get)
                if market_pick != b.outcome:
                    continue

            row = {
                **b.__dict__,
                "best_book": shop.best_book if shop else b.book,
                "best_odds": shop.best_odds if shop else b.decimal_odds,
                "second_book": shop.second_book if shop else None,
                "second_odds": shop.second_odds if shop else None,
                "timing": shop.timing if shop else "GREEN",
                "league": league,
                "paper_only": not risk["real_money"],
                "commence_time": match.get("commence_time"),
                "kelly_stake_full": kelly_stake_full,
                "stake_reduced_low_balance": stake_reduced,
                "top_up_book": top_up_book,
                "book_balance_available": available,
            }

            # Anomaly detection: edge thresholds + sharp-book divergence. Each
            # anomaly carries action hints — `excludes_bet` (PHANTOM_EDGE) and
            # `downgrades_to_low` (EDGE_HIGH) — that mutate the row before it
            # ships to the dashboard. Persisted to anomaly_log so the digest
            # excluder + Anomalies tab can read them.
            bet_flags: list[anomaly.Anomaly] = []
            bet_flags += anomaly.detect_edge_anomalies(row, league=league)
            bet_flags += anomaly.detect_sharp_divergence(row, match_consensus=None, league=league)
            bet_flags += anomaly.detect_market_consensus_divergence(row, consensus_prob, league=league)
            anomaly_excluded = any(f.excludes_bet for f in bet_flags)
            anomaly_downgrade = any(f.downgrades_to_low for f in bet_flags)
            if anomaly_downgrade:
                row["confidence"] = "LOW"
            row["anomaly_flags"] = [
                {
                    "type": f.anomaly_type,
                    "description": f.description,
                    "excludes_bet": f.excludes_bet,
                    "downgrades_to_low": f.downgrades_to_low,
                }
                for f in bet_flags
            ]
            if bet_flags:
                anomaly.log_many(bet_flags)

            actionable = (lockout_reason is None) and (not anomaly_excluded)
            row["stake"] = stake if actionable else 0.0
            row["actionable"] = actionable
            if not actionable:
                row["lockout_reason"] = (
                    lockout_reason
                    if lockout_reason
                    else "phantom-edge anomaly excluded this bet"
                )
            bets.append(row)

    bets.sort(key=lambda x: x["edge"], reverse=True)

    # Per-match market consensus + model view: a parallel pair of structures
    # so the dashboard can show "what the market thinks" and "what the model
    # thinks" for every outcome, even ones with no +EV bet against them.
    match_consensus: dict[str, dict] = {}
    match_model_view: dict[str, dict] = {}
    for p in preds:
        m = by_pair.get(_key(p["home_team"], p["away_team"]))
        if not m:
            continue
        markets = odds_client.parse_all_markets(m)

        view: dict = {}
        if markets["h2h"]:
            avg = _devig_avg_for_offers(markets["h2h"])
            if avg:
                view["h2h"] = avg
        if markets["btts"]:
            avg = _devig_avg_for_offers(markets["btts"])
            if avg:
                view["btts"] = avg
        if markets["totals"]:
            view["totals"] = {}
            for line, by_book in markets["totals"].items():
                avg = _devig_avg_for_offers(by_book)
                if avg:
                    view["totals"][str(line)] = avg
            if not view["totals"]:
                view.pop("totals")

        if view:
            match_consensus[p["match_id"]] = view

        # Mirror structure for model view, populated only for the markets the
        # books are pricing (so the comparison aligns line-by-line).
        model_view: dict = {}
        if "h2h" in view and p["home_win_pct"] is not None:
            model_view["h2h"] = {
                "home": round(p["home_win_pct"], 4),
                "draw": round(p["draw_pct"], 4),
                "away": round(p["away_win_pct"], 4),
            }
        if "btts" in view and p["btts_yes_pct"] is not None:
            yes = p["btts_yes_pct"]
            model_view["btts"] = {"yes": round(yes, 4), "no": round(1.0 - yes, 4)}
        if "totals" in view and p["score_matrix_json"]:
            try:
                matrix = json.loads(p["score_matrix_json"])
            except Exception:
                matrix = None
            if matrix:
                model_view["totals"] = {}
                for line_str in view["totals"].keys():
                    line = float(line_str)
                    over = sum(
                        matrix[h][a]
                        for h in range(len(matrix))
                        for a in range(len(matrix[0]))
                        if (h + a) > line
                    )
                    model_view["totals"][line_str] = {
                        "over": round(over, 4),
                        "under": round(1.0 - over, 4),
                    }
        if model_view:
            match_model_view[p["match_id"]] = model_view
    response = {
        "count": len(bets), "league": league, "bankroll": bankroll,
        "bets": bets,
        "match_consensus": match_consensus,
        "match_model_view": match_model_view,
        "risk": {
            "min_edge": risk["min_edge"],
            "max_stake_pct": risk["max_stake_pct"],
            "real_money": risk["real_money"],
            "high_confidence_only": risk["high_confidence_only"],
            "require_market_agreement": risk["require_market_agreement"],
            "daily_loss_cap_pct": risk["daily_loss_cap_pct"],
            "lockout_reason": lockout_reason,
        },
        "cache": {"hit": False, "age_s": 0.0, "ttl_s": _EV_CACHE_TTL_S},
    }
    _EV_CACHE[cache_key] = (time.time(), response)
    return response


def _model_prob_for_bet(bet: dict) -> float | None:
    """Look up the model's probability for the bet's exact (market, outcome, line)."""
    market = (bet.get("market") or "h2h").lower()
    bet_type = (bet.get("bet_type") or "").lower()
    if market == "h2h":
        return {
            "home": bet.get("home_win_pct"),
            "draw": bet.get("draw_pct"),
            "away": bet.get("away_win_pct"),
        }.get(bet_type)
    if market == "btts":
        yes = bet.get("btts_yes_pct")
        if yes is None:
            return None
        return yes if bet_type == "yes" else round(1.0 - yes, 4)
    if market == "totals":
        matrix_json = bet.get("score_matrix_json")
        line = bet.get("market_line")
        if not matrix_json or line is None:
            return None
        try:
            matrix = json.loads(matrix_json)
        except Exception:
            return None
        over = sum(
            matrix[h][a]
            for h in range(len(matrix))
            for a in range(len(matrix[0]))
            if (h + a) > line
        )
        return round(over, 4) if bet_type == "over" else round(1.0 - over, 4)
    return None


@app.get("/bets")
async def list_bets(status: str | None = None, limit: int = Query(100, ge=1, le=1000)):
    """Bets joined with fixture result + model prediction so the UI can show
    placement-implied / closing-implied / model probabilities side-by-side."""
    q = """
        SELECT b.*,
               f.result      AS fixture_result,
               f.home_goals  AS fixture_home_goals,
               f.away_goals  AS fixture_away_goals,
               p.kickoff_time AS match_kickoff,
               p.league       AS match_league,
               p.home_win_pct, p.draw_pct, p.away_win_pct,
               p.btts_yes_pct, p.score_matrix_json
        FROM bets_placed b
        LEFT JOIN fixtures f         ON f.match_id = b.match_id
        LEFT JOIN model_predictions p ON p.match_id = b.match_id
    """
    params: tuple = ()
    if status:
        q += " WHERE b.status = ?"
        params = (status,)
    q += " ORDER BY b.timestamp DESC LIMIT ?"
    params = (*params, limit)
    with db() as conn:
        rows = conn.execute(q, params).fetchall()

    out: list[dict] = []
    for r in rows:
        d = dict(r)
        # Raw implied probabilities (no de-vig — matches what the user paid for / saw)
        if d.get("odds_at_placement"):
            d["placement_implied_prob"] = round(1.0 / d["odds_at_placement"], 4)
        if d.get("closing_odds"):
            d["closing_implied_prob"] = round(1.0 / d["closing_odds"], 4)
        d["model_prob"] = _model_prob_for_bet(d)
        # Drop heavy fields the UI doesn't need
        d.pop("score_matrix_json", None)
        out.append(d)
    return {"count": len(out), "bets": out}


@app.post("/bets")
async def create_bet(payload: BetInput):
    is_paper = payload.is_paper if payload.is_paper is not None else LEAGUE_MODE == "epl"
    bet_id = clv_tracker.log_bet(
        match_id=payload.match_id,
        home_team=payload.home_team,
        away_team=payload.away_team,
        bet_type=payload.bet_type,
        book=payload.book,
        odds_at_placement=payload.odds_at_placement,
        stake=payload.stake,
        edge_at_placement=payload.edge_at_placement,
        is_paper=is_paper,
        market=payload.market,
        market_line=payload.market_line,
    )
    return {"id": bet_id, "is_paper": is_paper}


@app.get("/stats")
async def get_stats():
    weekly = clv_tracker.weekly_report()
    accuracy_report = accuracy.model_accuracy_report()
    return {
        "bankroll": BANKROLL,
        "league_mode": LEAGUE_MODE,
        "min_edge": MIN_EDGE,
        "max_stake_pct": MAX_STAKE_PCT,
        "weekly": weekly,
        "accuracy": accuracy_report,
    }


@app.get("/fixtures")
async def get_fixtures(league: str | None = None, limit: int = Query(50, ge=1, le=500)):
    q = "SELECT * FROM fixtures"
    params: tuple = ()
    if league:
        q += " WHERE league = ?"
        params = (league,)
    q += " ORDER BY kickoff_time ASC LIMIT ?"
    params = (*params, limit)
    with db() as conn:
        rows = conn.execute(q, params).fetchall()
    return {"count": len(rows), "fixtures": [dict(r) for r in rows]}


async def _digest_payloads():
    """Two-section digest: today (next 24h) + look-ahead (24h-7d)."""
    today = await get_best_bets(
        league="all", bankroll=BANKROLL, min_edge=MIN_EDGE, limit=3,
        kickoff_within_hours=24, kickoff_after_hours=0,
    )
    week = await get_best_bets(
        league="all", bankroll=BANKROLL, min_edge=MIN_EDGE, limit=3,
        kickoff_within_hours=168, kickoff_after_hours=24,
    )
    stats = await get_stats()
    return today, week, stats


@app.get("/digest-preview")
async def digest_preview():
    today, week, stats = await _digest_payloads()
    subject, body = digest.render(
        {"bets": today["bets"]}, stats, week_payload={"bets": week["bets"]},
    )
    return {"subject": subject, "body": body, "to": os.getenv("DIGEST_EMAIL")}


@app.post("/send-digest")
async def send_digest():
    today, week, stats = await _digest_payloads()
    subject, body = digest.render(
        {"bets": today["bets"]}, stats, week_payload={"bets": week["bets"]},
    )
    result = digest.send(subject, body)
    if not result["sent"]:
        raise HTTPException(502, result["reason"])
    return result


# ---------- Phase B: data fetchers + backtest ----------


@app.post("/backtest")
async def run_backtest_endpoint():
    """Run the EPL 2023-24 model backtest. First run ~1-2 min as it fills cache."""
    return await backtest.run_backtest()


@app.get("/sync-data/status")
async def sync_data_status():
    """Report data-source availability so the user can see what's blocked."""
    sample = None
    async with httpx.AsyncClient() as client:
        try:
            sample = await api_football.fixtures_for_round(
                client, api_football.EPL_LEAGUE_ID, 2023, "Regular Season - 38"
            )
            api_fb = {"available": True, "sample_count": len(sample)}
        except Exception as e:
            api_fb = {"available": False, "reason": str(e)}

    return {
        "api_football": api_fb,
        "understat": {"available": False, "reason": understat.UNAVAILABLE_REASON},
        "odds_api_historical": {
            "note": "free tier returns 401; use clv_tracker.set_closing() manually until upgrade"
        },
        "backtest_endpoint": "/backtest",
    }


@app.post("/sync-data/closing-line")
async def capture_closing_line(sport_key: str, timestamp: str):
    """Raw historical-odds fetch. Free tier returns 'unavailable'; paid plan unlocks it."""
    async with httpx.AsyncClient() as client:
        return await historical_lines.fetch_historical_odds(client, sport_key, timestamp)


class MarkResultInput(BaseModel):
    home_goals: int | None = None
    away_goals: int | None = None
    result: str | None = Field(None, description="home / draw / away (legacy h2h-only)")


@app.post("/bets/{bet_id}/auto-mark")
async def auto_mark_result_for_bet(bet_id: int):
    """Fetch the match's final score from API-Football and settle every
    open bet on that match. Match IDs in our DB are 'af-<fixture_id>' —
    we strip the prefix and look up the single fixture.

    Returns 400 with a clear 'match still in progress' message if the
    fixture isn't FT yet (the UI shows this as a toast)."""
    with db() as conn:
        bet = conn.execute(
            "SELECT match_id FROM bets_placed WHERE id = ?", (bet_id,)
        ).fetchone()
    if not bet:
        raise HTTPException(404, f"bet {bet_id} not found")
    match_id = bet["match_id"] or ""
    if not match_id.startswith("af-"):
        raise HTTPException(
            400,
            f"can't auto-fetch — match_id {match_id!r} isn't an API-Football fixture",
        )
    try:
        fixture_id = int(match_id.removeprefix("af-"))
    except ValueError:
        raise HTTPException(400, f"can't parse fixture id from {match_id!r}")

    import httpx
    async with httpx.AsyncClient() as client:
        try:
            fixture = await api_football.fetch_fixture(client, fixture_id)
        except api_football.PlanError as e:
            raise HTTPException(502, f"API-Football plan error: {e}")
        except Exception as e:
            raise HTTPException(502, f"failed to reach API-Football: {e}")

    if not fixture:
        raise HTTPException(404, f"fixture {fixture_id} not found in API-Football")

    status_short = (fixture.get("fixture", {}).get("status", {}).get("short") or "").upper()
    if status_short != "FT":
        long_status = fixture.get("fixture", {}).get("status", {}).get("long") or status_short
        elapsed = fixture.get("fixture", {}).get("status", {}).get("elapsed")
        suffix = f" ({elapsed}')" if elapsed else ""
        raise HTTPException(
            409,
            f"Match is still going on — status: {long_status}{suffix}. Try again after full time.",
        )

    home_goals = fixture.get("goals", {}).get("home")
    away_goals = fixture.get("goals", {}).get("away")
    if home_goals is None or away_goals is None:
        raise HTTPException(502, "fixture is FT but goals data missing from API response")

    try:
        result = clv_tracker.mark_match_result(
            match_id, home_goals=int(home_goals), away_goals=int(away_goals),
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {
        "status": "FT",
        "home_goals": int(home_goals),
        "away_goals": int(away_goals),
        **result,
    }


@app.post("/bets/{bet_id}/mark-result")
async def mark_result_for_bet(bet_id: int, payload: MarkResultInput):
    """Settle the bet's match. With home_goals + away_goals supplied, settles
    every open paper bet on this fixture (h2h, btts, totals). Without goals
    (just `result`), settles h2h bets only — kept for legacy compatibility."""
    with db() as conn:
        bet = conn.execute(
            "SELECT match_id FROM bets_placed WHERE id = ?", (bet_id,)
        ).fetchone()
    if not bet:
        raise HTTPException(404, f"bet {bet_id} not found")
    try:
        return clv_tracker.mark_match_result(
            bet["match_id"],
            home_goals=payload.home_goals,
            away_goals=payload.away_goals,
            result=payload.result,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/bets/{bet_id}/set-paper")
async def set_bet_paper(bet_id: int, value: bool):
    """Flip a bet between paper and cash (real money) mode. Only allowed
    while status='open' — settled bets already moved (or didn't move) the
    book balance, and retroactively flipping would diverge book balances
    from reality."""
    with db() as conn:
        row = conn.execute(
            "SELECT status, is_paper FROM bets_placed WHERE id = ?", (bet_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, f"bet {bet_id} not found")
        if row["status"] != "open":
            raise HTTPException(
                400,
                f"can't change mode on a {row['status']} bet — settled bets are locked",
            )
        conn.execute(
            "UPDATE bets_placed SET is_paper = ? WHERE id = ?",
            (1 if value else 0, bet_id),
        )
    return {"ok": True, "id": bet_id, "is_paper": value}


@app.delete("/bets/{bet_id}")
async def delete_bet(bet_id: int):
    """Hard-delete a logged bet. Used by the paper-trade-log "remove" button:
    when a user logs a bet by mistake (or changes their mind), this clears
    the row so the bet reappears in the +EV grid and drops out of portfolio
    summaries on the next refresh."""
    with db() as conn:
        cur = conn.execute("DELETE FROM bets_placed WHERE id = ?", (bet_id,))
        if cur.rowcount == 0:
            raise HTTPException(404, f"bet {bet_id} not found")
    return {"deleted": True, "id": bet_id}


@app.post("/bets/capture-closing-sweep")
async def capture_closing_sweep():
    """Walk every open paper bet whose match has kicked off and capture its
    closing line via the (paid) historical Odds API. Idempotent."""
    return await clv_tracker.sweep_closing_lines(LEAGUE_TO_SPORT_KEY)


@app.post("/bets/{bet_id}/capture-closing")
async def capture_closing_for_bet(bet_id: int, league: str | None = None):
    """Pull the historical odds snapshot at the bet's kickoff for the same
    (book, outcome), persist closing_odds + CLV. Idempotent."""
    league_key = league or LEAGUE_MODE
    sport_key = LEAGUE_TO_SPORT_KEY.get(league_key)
    if not sport_key:
        raise HTTPException(400, f"unknown league: {league_key}")
    result = await clv_tracker.capture_closing_for_bet(bet_id, sport_key)
    if not result.get("ok"):
        raise HTTPException(400, result.get("reason", "capture failed"))
    return result


def _bet_label(home: str, away: str, market: str | None, market_line, bet_type: str | None) -> str:
    m = (market or "h2h").lower()
    t = (bet_type or "").lower()
    if m == "h2h":
        if t == "home": return f"{home} vs {away} · {home}"
        if t == "away": return f"{home} vs {away} · {away}"
        return f"{home} vs {away} · Draw"
    if m == "btts":
        return f"{home} vs {away} · BTTS {t.capitalize()}"
    if m == "totals":
        return f"{home} vs {away} · {t.capitalize()} {market_line}"
    return f"{home} vs {away} · {t}"


@app.get("/stats/timeseries")
async def stats_timeseries():
    """Bankroll trajectory + per-bet CLV points with bet labels for the chart."""
    with db() as conn:
        # Bankroll runs off settled bets (need profit). CLV is independent — show
        # any bet where closing line was captured, including still-open ones.
        bankroll_rows = conn.execute(
            """
            SELECT timestamp, profit
            FROM bets_placed
            WHERE status IN ('won', 'lost')
            ORDER BY timestamp ASC
            """
        ).fetchall()
        clv_rows = conn.execute(
            """
            SELECT timestamp, clv, status, home_team, away_team,
                   market, market_line, bet_type
            FROM bets_placed
            WHERE clv IS NOT NULL
            ORDER BY timestamp ASC
            """
        ).fetchall()

    bankroll = BANKROLL
    bankroll_pts = [{"t": "start", "bankroll": bankroll}]
    for r in bankroll_rows:
        bankroll += r["profit"] or 0
        bankroll_pts.append({"t": r["timestamp"], "bankroll": round(bankroll, 2)})

    clv_pts = [
        {
            "t": r["timestamp"],
            "clv": r["clv"],
            "won": r["status"] == "won",
            "label": _bet_label(
                r["home_team"], r["away_team"],
                r["market"], r["market_line"], r["bet_type"],
            ),
        }
        for r in clv_rows
    ]
    return {"bankroll": bankroll_pts, "clv": clv_pts, "starting_bankroll": BANKROLL}


@app.get("/quota")
async def quota_status():
    """Public read-only view of today's API quota."""
    return api_quota.state()


@app.get("/scheduler/status")
async def scheduler_status():
    """Public — read-only. Start/stop is admin-only."""
    return sched.status()


@app.get("/model-health")
async def model_health(league: str | None = None):
    """Rolling accuracy + Brier vs backtest baseline + active bias alerts.
    Backs the dashboard's Model Health panel."""
    return self_eval.health_summary(league=league)


@app.get("/league-config")
async def league_config_list():
    """Per-league model knobs from the league_config table — gamma, blend
    weights, and anomaly thresholds. Edit via SQL if you need to retune."""
    return {"leagues": league_config.all_rows()}


@app.get("/anomalies")
async def anomalies_list(limit: int = Query(200, ge=1, le=1000)):
    """Today's anomaly_log rows for the dashboard's Anomalies tab + the header
    badge. Newest first. Use the count_today field for the badge."""
    return {
        "count_today": anomaly.count_today(),
        "anomalies": anomaly.recent(limit=limit),
    }


# --- Best bets across leagues ------------------------------------------------

LEAGUES_FOR_BEST_BETS = ("epl", "ucl", "uel", "world_cup")


def _bet_is_excluded(b: dict) -> bool:
    """A bet is excluded from the top-3 ranking if it's flagged as
    actionable=False (PHANTOM_EDGE excludes outright) or carries any
    anomaly that excludes-bet / downgrades-to-low (per-league EDGE_HIGH
    threshold). Per-league thresholds were already applied at /ev-bets
    time, so we just enforce the flag here."""
    if not b.get("actionable", True):
        return True
    for f in (b.get("anomaly_flags") or []):
        if f.get("excludes_bet") or f.get("downgrades_to_low"):
            return True
    return False


@app.get("/best-bets")
async def get_best_bets(
    league: str = Query("all", description="all | epl | ucl | uel | world_cup"),
    bankroll: float = Query(BANKROLL, ge=100.0, le=1_000_000.0),
    min_edge: float = Query(MIN_EDGE, ge=0.0, le=0.5),
    limit: int = Query(3, ge=1, le=10),
    kickoff_within_hours: int = Query(
        48, ge=1, le=336,
        description="Only consider matches kicking off within this many hours of now. "
                    "Default 48h so 'best bets' means 'imminent' rather than 'this week'.",
    ),
    kickoff_after_hours: int = Query(
        0, ge=0, le=336,
        description="Lower bound — only consider matches kicking off AT LEAST this many "
                    "hours from now. Use with kickoff_within_hours to slice 'today' (0-24) "
                    "vs 'next week' (24-168).",
    ),
):
    """Top-N best bets across one or all tracked leagues, ranked by edge.

    Filters to matches with `commence_time` between now and now+kickoff_within_hours
    so the grid surfaces actionable picks rather than week-out futures the model
    happens to have priced. Per-league anomaly thresholds are applied at /ev-bets
    time, so a UCL bet at 11% edge survives (UCL threshold 12%) while a World Cup
    bet at 11% edge is filtered (WC threshold 10%).
    """
    from datetime import datetime, timedelta, timezone as _tz
    league = (league or "all").lower()
    leagues = LEAGUES_FOR_BEST_BETS if league == "all" else (league,)
    now = datetime.now(_tz.utc)
    lower = now + timedelta(hours=kickoff_after_hours)
    upper = now + timedelta(hours=kickoff_within_hours)

    def _within_window(commence_time: str | None) -> bool:
        if not commence_time:
            return False
        try:
            dt = datetime.fromisoformat(commence_time.replace("Z", "+00:00"))
        except ValueError:
            return False
        return lower <= dt <= upper

    # Dedupe by (match_id, market, market_line, outcome) — /ev-bets can return
    # the same bet row more than once across its internal market passes; keep
    # the row with the highest edge.
    deduped: dict[tuple, dict] = {}
    skipped_window = 0
    for lg in leagues:
        try:
            r = await get_ev_bets(bankroll=bankroll, min_edge=min_edge, league=lg)
        except HTTPException:
            continue  # league not configured / no data → skip
        for b in r.get("bets", []) or []:
            if _bet_is_excluded(b):
                continue
            if not _within_window(b.get("commence_time")):
                skipped_window += 1
                continue
            b.setdefault("league", lg)
            key = (
                b.get("match_id"),
                (b.get("market") or "h2h"),
                b.get("market_line"),
                b.get("outcome"),
            )
            existing = deduped.get(key)
            if existing is None or (b.get("edge") or 0) > (existing.get("edge") or 0):
                deduped[key] = b

    merged = sorted(deduped.values(), key=lambda x: x.get("edge", 0.0), reverse=True)
    top = merged[:limit]
    return {
        "league_filter": league,
        "leagues_considered": list(leagues),
        "leagues_in_top": sorted({b.get("league") for b in top if b.get("league")}),
        "count_considered": len(merged),
        "count_returned": len(top),
        "kickoff_within_hours": kickoff_within_hours,
        "skipped_outside_window": skipped_window,
        "bets": top,
    }


# --- Book balances -----------------------------------------------------------


@app.get("/book-balances")
async def book_balances():
    """Per-book bankroll snapshot. Includes total + warning level per book
    (ok / amber <$50 / red <$20). Backs the dashboard header strip."""
    balances = book_balance.get_all()
    total = sum(float(b.get("balance_usd") or 0.0) for b in balances)
    return {
        "total": round(total, 2),
        "amber_threshold": book_balance.LOW_AMBER_USD,
        "red_threshold": book_balance.LOW_RED_USD,
        "books": balances,
    }


# --- Match analysis (Claude) -------------------------------------------------


@app.get("/match-analysis/{match_id}")
async def match_analysis_get(match_id: str, force: bool = False):
    """Per-match Haiku 4.5 analysis. Cached 30 min in SQLite; daily-cap'd."""
    return await match_analysis.analyze_match(match_id, force=force)


# --- Portfolio ---------------------------------------------------------------

def _ev_for_bet(stake: float | None, edge: float | None) -> float:
    """Expected $ value of a single bet. Edge is fractional (0.06 = 6%)."""
    if stake is None or edge is None:
        return 0.0
    return float(stake) * float(edge)


@app.get("/portfolio/summary")
async def portfolio_summary(
    league: str | None = None,
    is_paper: bool | None = None,
):
    """Aggregate portfolio metrics across bets_placed.

    Filters: optional league + paper/real. is_paper omitted = both kinds.
    Returns the four summary cards plus rollups (avg_edge, avg_clv, win_rate)
    used by the model-health badges.
    """
    where = []
    params: list = []
    if league:
        where.append("p.league = ?")
        params.append(league)
    if is_paper is not None:
        where.append("b.is_paper = ?")
        params.append(1 if is_paper else 0)
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""

    with db() as conn:
        rows = conn.execute(
            f"""
            SELECT b.id, b.stake, b.odds_at_placement, b.edge_at_placement,
                   b.status, b.profit, b.clv, b.bet_type, b.market,
                   b.is_paper, p.league
            FROM bets_placed b
            LEFT JOIN model_predictions p ON p.match_id = b.match_id
            {where_sql}
            """,
            tuple(params),
        ).fetchall()
    bets = [dict(r) for r in rows]

    total_invested = sum((b["stake"] or 0) for b in bets)
    open_bets = [b for b in bets if b["status"] == "open"]
    settled_bets = [b for b in bets if b["status"] in ("won", "lost")]
    void_bets = [b for b in bets if b["status"] == "void"]

    realized_pnl = sum((b["profit"] or 0) for b in settled_bets)
    settled_stake = sum((b["stake"] or 0) for b in settled_bets)
    realized_pct = (realized_pnl / settled_stake) if settled_stake > 0 else 0.0

    expected_pnl = sum(_ev_for_bet(b["stake"], b["edge_at_placement"]) for b in bets)

    # Open-bet outcome range. Best = all open bets win, worst = all lose.
    open_best = sum(
        ((b["odds_at_placement"] or 1.0) - 1.0) * (b["stake"] or 0)
        for b in open_bets
    )
    open_worst = -sum((b["stake"] or 0) for b in open_bets)

    starting_bankroll = BANKROLL
    current_value_best = starting_bankroll + realized_pnl + open_best
    current_value_worst = starting_bankroll + realized_pnl + open_worst

    edges = [b["edge_at_placement"] for b in bets if b["edge_at_placement"] is not None]
    clvs = [b["clv"] for b in settled_bets if b["clv"] is not None]
    won = sum(1 for b in settled_bets if b["status"] == "won")
    win_rate = (won / len(settled_bets)) if settled_bets else 0.0

    return {
        "league": league,
        "is_paper": is_paper,
        "starting_bankroll": starting_bankroll,
        "total_invested": round(total_invested, 2),
        "open_bets_count": len(open_bets),
        "settled_bets_count": len(settled_bets),
        "void_bets_count": len(void_bets),
        "realized_pnl": round(realized_pnl, 2),
        "realized_pct": round(realized_pct, 4),
        "expected_pnl": round(expected_pnl, 2),
        "current_value_best": round(current_value_best, 2),
        "current_value_worst": round(current_value_worst, 2),
        "avg_edge": round(sum(edges) / len(edges), 4) if edges else 0.0,
        "avg_clv": round(sum(clvs) / len(clvs), 4) if clvs else 0.0,
        "win_rate": round(win_rate, 4),
        "roi": round(realized_pct, 4),
    }


def _kelly_growth_curve(
    starting_bankroll: float,
    edge: float,
    n_bets: int,
    kelly_fraction: float,
    avg_decimal_odds: float = 2.0,
) -> list[dict]:
    """Compound bankroll growth assuming a constant edge and constant odds.

    For each bet we stake `f * bankroll` where f = kelly_fraction × Kelly_full
    and Kelly_full = edge / (b) with b = avg_decimal_odds - 1. Per-bet expected
    log-growth ≈ f × edge — small-fraction approximation, matches half/full
    Kelly textbooks closely enough for projection-table purposes.
    """
    b = max(0.05, avg_decimal_odds - 1.0)
    full_kelly = max(0.0, edge / b)
    f = kelly_fraction * full_kelly
    r = f * edge  # per-bet compound rate (~edge × f for small f)
    out = []
    bankroll = starting_bankroll
    for n in range(1, n_bets + 1):
        bankroll = bankroll * (1.0 + r)
        out.append({"n": n, "bankroll": round(bankroll, 2)})
    return out


@app.get("/portfolio/projection")
async def portfolio_projection(
    matches: int = Query(64, ge=1, le=500),
    stake: float = Query(20.0, ge=1.0, le=10_000.0),
    edge: float = Query(0.06, ge=0.0, le=0.50),
    bets_per_match: float = Query(1.5, ge=0.1, le=10.0),
    avg_decimal_odds: float = Query(2.0, ge=1.05, le=20.0),
    starting_bankroll: float = Query(1000.0, ge=10.0, le=1_000_000.0),
):
    """Projected ROI + Kelly growth + 3 scenarios for the portfolio calculator.

    `edge` is fractional (0.06 = 6%). Best/worst sweep edge ±50%, and the
    "with variance" worst case bakes in a 60% loss rate to show the downside
    when the model is wrong despite an apparent edge.
    """
    total_bets = int(round(matches * bets_per_match))
    total_staked = total_bets * stake
    expected_profit = total_staked * edge
    expected_roi = edge
    expected_bankroll = starting_bankroll + expected_profit

    best_case = total_staked * (edge * 1.5)
    worst_case = total_staked * (edge * 0.5)
    # Variance-down case: model wrong on 60% of bets despite edge.
    # Per bet at avg_decimal_odds: win → +stake*(odds-1), lose → -stake.
    b = avg_decimal_odds - 1.0
    variance_loss_rate = 0.60
    variance_pnl = total_bets * (
        (1 - variance_loss_rate) * stake * b
        - variance_loss_rate * stake
    )

    half_kelly = _kelly_growth_curve(starting_bankroll, edge, total_bets, 0.5, avg_decimal_odds)
    full_kelly = _kelly_growth_curve(starting_bankroll, edge, total_bets, 1.0, avg_decimal_odds)

    # Pick out fixed milestones for the table (capped at total_bets).
    milestones = [n for n in (20, 40, 64, 100, 200) if n <= total_bets]
    if total_bets not in milestones and total_bets > 0:
        milestones.append(total_bets)
    growth_table = []
    for n in milestones:
        full = full_kelly[n - 1]["bankroll"] if n - 1 < len(full_kelly) else None
        half = half_kelly[n - 1]["bankroll"] if n - 1 < len(half_kelly) else None
        growth_table.append({
            "n": n,
            "full_kelly": full,
            "half_kelly": half,
            "full_pct": round((full / starting_bankroll - 1) * 100, 1) if full else None,
            "half_pct": round((half / starting_bankroll - 1) * 100, 1) if half else None,
        })

    scenarios = [
        {
            "name": "model_works_as_backtested",
            "label": "Model works as backtested",
            "win_rate": 0.75,
            "edge": max(0.04, edge),
            "roi": round(max(0.04, edge) * (total_staked / starting_bankroll), 4),
            "expected_profit": round(total_staked * max(0.04, edge), 2),
            "tone": "good",
        },
        {
            "name": "model_slightly_worse",
            "label": "Model slightly worse than backtest",
            "win_rate": 0.62,
            "edge": max(0.0, edge * 0.66),
            "roi": round(max(0.0, edge * 0.66) * (total_staked / starting_bankroll), 4),
            "expected_profit": round(total_staked * max(0.0, edge * 0.66), 2),
            "tone": "warn",
        },
        {
            "name": "model_underperforms",
            "label": "Model underperforms",
            "win_rate": 0.52,
            "edge": -max(0.005, edge * 0.33),
            "roi": round(-max(0.005, edge * 0.33) * (total_staked / starting_bankroll), 4),
            "expected_profit": round(-total_staked * max(0.005, edge * 0.33), 2),
            "note": "Model needs recalibration",
            "tone": "bad",
        },
    ]

    return {
        "inputs": {
            "matches": matches,
            "stake": stake,
            "edge": edge,
            "bets_per_match": bets_per_match,
            "avg_decimal_odds": avg_decimal_odds,
            "starting_bankroll": starting_bankroll,
        },
        "summary": {
            "total_bets": total_bets,
            "total_staked": round(total_staked, 2),
            "expected_profit": round(expected_profit, 2),
            "expected_roi": round(expected_roi, 4),
            "expected_bankroll": round(expected_bankroll, 2),
            "best_case": round(best_case, 2),
            "worst_case": round(worst_case, 2),
            "variance_pnl": round(variance_pnl, 2),
        },
        "kelly_growth_table": growth_table,
        "kelly_curves": {
            "half_kelly": half_kelly,
            "full_kelly": full_kelly,
        },
        "scenarios": scenarios,
    }


# --- Admin endpoints ----------------------------------------------------------
# Manual sync controls live only under /admin/* and require Basic auth.

@app.post("/admin/sync", dependencies=[Depends(admin_auth)])
async def admin_sync(
    background_tasks: BackgroundTasks,
    league: str | None = None,
    force: bool = False,
):
    """Emergency manual sync. Runs in the background — returns immediately so
    the request doesn't time out (a full sync takes ~3 min). Watch quota usage
    and predictions table for progress."""
    league_key = league or LEAGUE_MODE
    background_tasks.add_task(data_sync.sync_daily, league=league_key, force=force)
    return {
        "queued": True,
        "league": league_key,
        "force": force,
        "note": "sync running in background; takes ~3 min. Refresh /quota or /predictions to see progress.",
    }


@app.post("/admin/scheduler/start", dependencies=[Depends(admin_auth)])
async def admin_scheduler_start():
    return sched.start()


@app.post("/admin/scheduler/stop", dependencies=[Depends(admin_auth)])
async def admin_scheduler_stop():
    return sched.stop()


@app.get("/admin/calibration-status", dependencies=[Depends(admin_auth)])
async def admin_calibration_status():
    """Per regular league: settled prediction count + grid-search eligibility."""
    return calibrate.calibration_status()


@app.post("/admin/calibrate", dependencies=[Depends(admin_auth)])
async def admin_run_calibration_check():
    """Manually fire the monthly calibration check now (instead of waiting
    for the 1st of the month). Idempotent — does not modify model.py."""
    payload = calibrate.run_monthly_calibration_check()
    # Email the report so the admin sees it the same way the cron does
    subject, body = digest.render_monthly_calibration(payload)
    email_result = digest.send(subject, body)
    return {"check": payload, "email": email_result}


@app.post("/admin/accuracy-snapshot", dependencies=[Depends(admin_auth)])
async def admin_run_accuracy_snapshot():
    """Manually fire the weekly accuracy snapshot now."""
    payload = calibrate.write_weekly_snapshot()
    subject, body = digest.render_weekly_accuracy(payload)
    email_result = digest.send(subject, body)
    return {"snapshot": payload, "email": email_result}


@app.get("/admin/accuracy-history", dependencies=[Depends(admin_auth)])
async def admin_accuracy_history(league: str | None = None, limit: int = Query(52, ge=1, le=520)):
    """Accuracy snapshots time series for trend analysis (newest first)."""
    q = "SELECT * FROM accuracy_snapshots"
    params: tuple = ()
    if league:
        q += " WHERE league = ?"
        params = (league,)
    q += " ORDER BY snapshot_date DESC LIMIT ?"
    params = (*params, limit)
    with db() as conn:
        rows = conn.execute(q, params).fetchall()
    return {"count": len(rows), "snapshots": [dict(r) for r in rows]}


# --- World Cup calibration (separate from regular leagues) -------------------

@app.get("/admin/wc/status", dependencies=[Depends(admin_auth)])
async def admin_wc_status():
    """Tournament phase + settled count + which step to fire next."""
    return wc_calibrate.status()


@app.post("/admin/wc/snapshot", dependencies=[Depends(admin_auth)])
async def admin_wc_snapshot():
    """Persist a WC accuracy snapshot row + email a summary. Use during the
    tournament after each match day, or on demand."""
    payload = wc_calibrate.write_wc_snapshot()
    snap = payload["snapshot"]
    # Reuse the phase-report email — it covers the same ground
    subject, body = digest.render_wc_phase_report({
        "phase": snap["phase"],
        "n_settled": snap["n_settled"],
        "n_min_for_grid_search": wc_calibrate.N_MIN_FOR_GRID_SEARCH,
        "eligible": snap["n_settled"] >= wc_calibrate.N_MIN_FOR_GRID_SEARCH,
        "snapshot": snap,
        "grid_search": None,
    })
    email = digest.send(subject, body)
    return {"snapshot": payload, "email": email}


@app.post("/admin/wc/calibrate-from-ucl-proxy", dependencies=[Depends(admin_auth)])
async def admin_wc_calibrate_proxy(apply: bool = False):
    """Pre-tournament one-time calibration using UCL knockouts as a proxy.

    apply=false (default): grid-search the cached UCL 2023-24 knockouts and
    return ranked results — review only, nothing persisted.
    apply=true: persist the lowest-Brier params to model_params_wc.json so
    the next /admin/sync?league=world_cup picks them up automatically."""
    return await wc_calibrate.run_proxy_calibration_from_ucl_knockouts(apply=apply)


@app.post("/admin/wc/post-phase-check", dependencies=[Depends(admin_auth)])
async def admin_wc_post_phase_check(phase: str = "knockouts"):
    """Run after group stage / quarters / final to snapshot + email a report.
    `phase` ∈ pre_tournament | group_stage | knockouts | concluded."""
    payload = wc_calibrate.run_post_phase_check(phase=phase)
    subject, body = digest.render_wc_phase_report(payload)
    email = digest.send(subject, body)
    return {"check": payload, "email": email}


@app.get("/admin/health", dependencies=[Depends(admin_auth)])
async def admin_health():
    """Aggregate status — useful before deciding to fire a manual sync."""
    return {
        "scheduler": sched.status(),
        "quota": api_quota.state(),
        "league_mode": LEAGUE_MODE,
        "min_edge": MIN_EDGE,
        "wc_safeguards": {
            "min_edge": WC_MIN_EDGE,
            "max_stake_pct": WC_MAX_STAKE_PCT,
            "high_confidence_only": WC_HIGH_CONFIDENCE_ONLY,
            "require_market_agreement": WC_REQUIRE_MARKET_AGREEMENT,
            "daily_loss_cap_pct": WC_DAILY_LOSS_CAP_PCT,
            "wc_params_present": calibrate_engine.has_wc_params(),
        },
    }


@app.get("/backtest-result")
async def get_backtest_result():
    """Return the last saved backtest_result.json (written by backtest.py CLI)."""
    p = Path(__file__).parent / "backtest_result.json"
    if not p.exists():
        return {"available": False, "note": "run `python3 backtest.py` to generate"}
    return {"available": True, **json.loads(p.read_text())}
