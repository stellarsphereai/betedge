"""AI-powered match analysis via Claude Haiku 4.5.

A user-triggered button on each match card calls this. We pull the stored
prediction + anomaly_log for the match, build a structured prompt, and ask
Haiku to produce a six-section analysis (TEAM FORM / XG / MODEL INPUTS /
BET VERDICTS / ANOMALY FLAGS / FINAL VERDICT).

Cost control:
- 30-minute SQLite cache per match_id (avoids duplicate calls)
- Daily budget cap (default 50 calls), counted across the match_analysis
  table for today (UTC date)
- Haiku 4.5 pricing: $1/M input, $5/M output ≈ $0.001-0.003 per call

Prompt caching (cache_control on system prompt) is intentionally skipped:
Haiku 4.5's minimum cacheable prefix is 4096 tokens, and our system prompt
is ~250 tokens. The annotation would silently no-op.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone

import anthropic
from fastapi import HTTPException

import model
from database import db

log = logging.getLogger("arb.analysis")

MODEL_NAME = "claude-haiku-4-5"
DEFAULT_DAILY_CAP = 50
CACHE_TTL_S = 30 * 60               # 30 minutes
MAX_OUTPUT_TOKENS = 1024            # ~400-600 expected, room to spare
INPUT_PRICE_PER_M = 1.0             # USD per 1M tokens
OUTPUT_PRICE_PER_M = 5.0

SYSTEM_PROMPT = (
    "You are a sports betting assistant analyzing match predictions and "
    "specific bet recommendations for a beginner. After analyzing the "
    "match, analyze EACH specific bet, then give ONE clear final "
    "recommendation.\n\n"
    "Style rules:\n"
    "- Plain English. Never use words like: asymmetric, pipeline, "
    "suppression, anchored, defensible, jibes, compressed.\n"
    "- Always explain what something means immediately after saying it.\n"
    "- Use concrete dollar amounts and percentages — never abstract concepts.\n"
    "- Write like you are texting a smart friend who knows nothing about "
    "betting models.\n"
    "- Be direct. Never say 'it depends' or 'consider your risk tolerance'. "
    "Pick one bet or skip all.\n"
    "- Maximum 2 sentences per point.\n\n"
    "Sharp / soft book classification (use these labels in your analysis):\n"
    "- SHARP books (efficient pricing, edges here are meaningful): FanDuel, "
    "DraftKings\n"
    "- DECENT books (middle of the road): ESPN Bet, Fanatics, Caesars\n"
    "- SOFT books (looser pricing, edges less reliable): Bally Bet, BetRivers\n\n"
    "When analyzing each bet, consider:\n"
    "1. Is the edge size believable for this market and book type?\n"
    "2. Does the break-even point leave enough buffer above the model "
    "probability? Use 'comfortable' (>3 points), 'thin' (1-3 points), "
    "'razor thin' (<1 point).\n"
    "3. Sharp or soft book?\n"
    "4. Kelly stake — high conviction (full cap, $20), medium ($10-15), "
    "low (<$10)?\n"
    "5. Concentration risk — is there already a bet on this match today?\n"
    "6. Daily bet limit — how many bets logged today; room for another within "
    "the 3-bet daily limit?\n"
    "7. Do multiple bets on this card tell a consistent story or contradict?\n\n"
    "RULES for your final recommendation:\n"
    "- Never recommend more than ONE bet per match.\n"
    "- Never recommend ANY bet if 3 bets are already logged today (at the "
    "daily cap).\n"
    "- Never recommend a draw bet unless ALL THREE: edge ≥ 5%, book is "
    "FanDuel or DraftKings, Kelly stake ≥ $15.\n"
    "- If all bets are on soft books with small Kelly stakes, recommend "
    "SKIP ALL.\n\n"
    "Use this exact section structure with these exact headings on their "
    "own lines:\n\n"
    "🔍 QUICK SUMMARY\n"
    "Three sentences. What this match looks like. What the model expects. "
    "Whether it's worth betting — yes or no.\n\n"
    "⚽ TEAM FORM\n"
    "Both teams' recent form in plain English. Any injuries or rest concerns.\n\n"
    "🔢 XG CHECK\n"
    "Are the xG numbers believable? Compare to season averages.\n\n"
    "✅ BET BY BET ANALYSIS\n"
    "For each bet recommended, render this exact block:\n\n"
    "[Market] — [Outcome] — [Book] — [Odds]\n"
    "Edge: X% — [believable / suspicious]\n"
    "Break-even: X% — model says X% — buffer: X points "
    "[comfortable / thin / razor thin]\n"
    "Book: [Sharp / Decent / Soft] — [what this means]\n"
    "Kelly: $X — [high / medium / low conviction]\n"
    "Consistency: [does this bet line up with the overall match prediction?]\n"
    "VERDICT: BET IT / CAUTION / SKIP\n\n"
    "🚨 ANOMALY FLAGS (only if confirmed issues exist)\n"
    "Plain-English explanation. Only flag CRITICAL when a specific data "
    "value in the input PROVES it (penalties applied, gamma, season blend, "
    "etc.) — don't flag based on hunches about the output.\n\n"
    "═══════════════════════════════\n"
    "MY RECOMMENDATION\n"
    "═══════════════════════════════\n\n"
    "PLACE THIS BET:\n"
    "Market: [market name]\n"
    "Outcome: [outcome]\n"
    "Book: [book name]\n"
    "Odds: [american odds]\n"
    "Stake: $[amount]\n"
    "Why: [one sentence plain English reason]\n\n"
    "SKIP THESE:\n"
    "[Market 1]: [one sentence why]\n"
    "[Market 2]: [one sentence why]\n\n"
    "OVERALL: [one of these three exact options, including emoji]\n"
    "  ✅ BET IT — clear edge, log now\n"
    "  ⚠️ CAUTION — log small or skip\n"
    "  ❌ SKIP ALL — not worth betting this match today\n\n"
    "If SKIP ALL, replace the PLACE THIS BET block with one sentence "
    "explaining why none of the bets are worth placing today.\n\n"
    "═══════════════════════════════"
)


# --- helpers -----------------------------------------------------------------


def _today_call_count() -> int:
    """Count today's analysis calls for daily-budget enforcement."""
    today = datetime.now(timezone.utc).date().isoformat()
    with db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM match_analysis WHERE date(created_at) = ?",
            (today,),
        ).fetchone()
    return int(row["n"] or 0)


def _cached_analysis(match_id: str) -> dict | None:
    """Return cached analysis if cache_expires_at > now."""
    with db() as conn:
        row = conn.execute(
            """
            SELECT * FROM match_analysis
            WHERE match_id = ? AND cache_expires_at > datetime('now')
            ORDER BY created_at DESC LIMIT 1
            """,
            (match_id,),
        ).fetchone()
    return dict(row) if row else None


def _league_avg_for_key(league_key: str | None) -> float:
    """Map our string league key (epl/ucl/uel/world_cup) to the API-Football
    integer ID then look up LEAGUE_AVG_GOALS. Falls back to default."""
    mapping = {"epl": 39, "ucl": 2, "uel": 3, "world_cup": 1}
    league_id = mapping.get((league_key or "").lower())
    return model.league_avg_goals(league_id)


def _decimal_to_american(d: float | None) -> str:
    if not d or d <= 1:
        return "—"
    if d >= 2:
        return f"+{round((d - 1) * 100)}"
    return f"{round(-100 / (d - 1))}"


def _format_ev_bet_for_prompt(b: dict) -> str:
    market = (b.get("market") or "h2h").upper()
    outcome = b.get("outcome") or ""
    line = f" {b['market_line']}" if b.get("market_line") is not None else ""
    book = b.get("best_book") or b.get("book") or ""
    decimal = b.get("best_odds") or b.get("decimal_odds") or 0
    model_pct = (b.get("model_prob") or 0) * 100
    market_pct = (b.get("true_implied_prob") or 0) * 100
    edge_pct = (b.get("edge") or 0) * 100
    stake = b.get("stake") or 0
    timing = b.get("timing") or "GREEN"
    coverage = b.get("book_coverage") or 0
    min_cov = b.get("min_book_coverage") or 0
    coverage_str = f"  coverage {coverage}/7 (min {min_cov})" if coverage else ""
    break_even = (1.0 / decimal * 100) if decimal else 0
    buffer = model_pct - break_even
    return (
        f"  - {market} — {outcome.upper()}{line} — {book} — "
        f"{decimal:.2f} ({_decimal_to_american(decimal)})\n"
        f"      market_implied: {market_pct:.1f}%   model: {model_pct:.1f}%   "
        f"edge: {edge_pct:.2f}%\n"
        f"      break_even: {break_even:.1f}%   buffer: {buffer:+.1f}pp   "
        f"kelly_stake: ${stake:.0f}   timing: {timing}{coverage_str}"
    )


def _build_user_prompt(
    prediction: dict,
    anomalies: list[dict],
    ev_bets: list[dict] | None = None,
    existing_bets: list[dict] | None = None,
    todays_bets_count: int = 0,
    book_balances: list[dict] | None = None,
    max_bets_per_day: int = 3,
    max_stake_per_bet: int = 20,
) -> str:
    """Assemble the per-match prompt from stored prediction + anomaly_log
    + the new operational context (ev bets, existing bets, daily counts,
    book balances)."""
    p = prediction
    league_avg = _league_avg_for_key(p.get("league"))

    # Penalties: stored as JSON in penalties_json. Render as a comma list.
    penalties_str = "none"
    raw = p.get("penalties_json")
    if raw:
        try:
            obj = json.loads(raw)
            # Accept either list[str] or {home: [...], away: [...]}
            if isinstance(obj, list):
                penalties_str = ", ".join(obj) or "none"
            elif isinstance(obj, dict):
                parts = []
                for side in ("home", "away"):
                    if obj.get(side):
                        parts.append(f"{side}: {', '.join(obj[side])}")
                penalties_str = "; ".join(parts) or "none"
        except Exception:
            pass

    if anomalies:
        anom_lines = [
            f"- {a.get('anomaly_type')}: {a.get('description') or '(no description)'}"
            for a in anomalies
        ]
        anom_str = "\n".join(anom_lines)
    else:
        anom_str = "None flagged."

    home_xg = p.get("home_xg") or 0.0
    away_xg = p.get("away_xg") or 0.0
    total_xg = home_xg + away_xg

    def _arr(field: str) -> str:
        raw = p.get(field)
        if not raw:
            return "(not stored — older prediction; will be available after next sync)"
        try:
            arr = json.loads(raw)
        except Exception:
            return "(unparseable)"
        return ", ".join(f"{x:.2f}" for x in arr) if arr else "(empty)"

    def _per_side_pens(field: str) -> str:
        raw = p.get(field)
        if not raw:
            return "none"
        try:
            arr = json.loads(raw)
        except Exception:
            return "none"
        return ", ".join(arr) if arr else "none"

    home = p.get("home_team")
    away = p.get("away_team")

    return (
        f"Analyze this match prediction:\n\n"
        f"Match: {home} vs {away}\n"
        f"League: {(p.get('league') or 'unknown').upper()}\n"
        f"Kickoff: {p.get('kickoff_time')}\n\n"
        f"MODEL OUTPUT:\n"
        f"Home win: {(p.get('home_win_pct') or 0) * 100:.1f}%\n"
        f"Draw: {(p.get('draw_pct') or 0) * 100:.1f}%\n"
        f"Away win: {(p.get('away_win_pct') or 0) * 100:.1f}%\n"
        f"BTTS Yes: {(p.get('btts_yes_pct') or 0) * 100:.1f}%\n"
        f"Home xG: {home_xg:.2f}\n"
        f"Away xG: {away_xg:.2f}\n"
        f"Total xG: {total_xg:.2f}  (league avg ≈ {league_avg * 2:.2f} per match)\n"
        f"Confidence: {p.get('confidence') or 'UNKNOWN'}\n\n"
        f"HOME TEAM — {home}:\n"
        f"Last 10 xG for (most recent first): {_arr('home_games_xg_for')}\n"
        f"Last 10 xG against:                  {_arr('home_games_xg_against')}\n"
        f"Weighted attack:  {p.get('home_attack_weighted')}\n"
        f"Weighted defense: {p.get('home_defense_weighted')}\n"
        f"Season avg xG for / against: {p.get('home_season_avg_for')} / "
        f"{p.get('home_season_avg_against')}\n"
        f"Rest days: {p.get('home_rest_days')}\n"
        f"Penalties applied: {_per_side_pens('home_penalties_applied')}\n\n"
        f"AWAY TEAM — {away}:\n"
        f"Last 10 xG for (most recent first): {_arr('away_games_xg_for')}\n"
        f"Last 10 xG against:                  {_arr('away_games_xg_against')}\n"
        f"Weighted attack:  {p.get('away_attack_weighted')}\n"
        f"Weighted defense: {p.get('away_defense_weighted')}\n"
        f"Season avg xG for / against: {p.get('away_season_avg_for')} / "
        f"{p.get('away_season_avg_against')}\n"
        f"Rest days: {p.get('away_rest_days')}\n"
        f"Penalties applied: {_per_side_pens('away_penalties_applied')}\n\n"
        f"MODEL ADJUSTMENTS:\n"
        f"Gamma (home-field) applied: {p.get('gamma_used')}\n"
        f"Combined penalties this match: {penalties_str}\n"
        f"Season blend (recent vs season avg): {p.get('season_blend_used')}\n"
        f"League-avg goals per team per game: {league_avg}\n\n"
        f"ANOMALIES DETECTED IN PIPELINE:\n{anom_str}\n\n"
        f"BETS THE EV CALCULATOR FOUND ON THIS MATCH:\n"
        f"{_render_ev_bets_block(ev_bets)}\n\n"
        f"BETS ALREADY LOGGED ON THIS MATCH TODAY:\n"
        f"{_render_existing_bets_block(existing_bets)}\n\n"
        f"DAILY LIMITS:\n"
        f"  bets logged today (across all matches): {todays_bets_count}\n"
        f"  max bets per day: {max_bets_per_day}\n"
        f"  max stake per bet: ${max_stake_per_bet}\n"
        f"  remaining bets allowed today: {max(0, max_bets_per_day - todays_bets_count)}\n\n"
        f"ACCOUNT BALANCES:\n"
        f"{_render_balances_block(book_balances)}\n\n"
        f"Now do the full analysis per the system prompt's structure. "
        f"After analyzing the match, analyze EACH bet listed above, then "
        f"give ONE clear final recommendation. Apply ALL the rules: never "
        f"more than one bet per match, respect the daily limit, never a "
        f"draw bet under the strict criteria, label every book sharp/decent/"
        f"soft, never hedge. The final 'MY RECOMMENDATION' section must "
        f"appear exactly as specified in the system prompt with the equals-"
        f"line dividers."
    )


def _render_ev_bets_block(ev_bets: list[dict] | None) -> str:
    if not ev_bets:
        return "  (no +EV bets found on this match — recommend SKIP ALL)"
    lines = []
    for b in ev_bets:
        if not b.get("actionable", True):
            continue  # PHANTOM_EDGE / lockout — don't even surface
        lines.append(_format_ev_bet_for_prompt(b))
    return "\n".join(lines) if lines else "  (no actionable bets after anomaly filter)"


def _render_existing_bets_block(existing_bets: list[dict] | None) -> str:
    if not existing_bets:
        return "  (none)"
    lines = []
    for b in existing_bets:
        market = (b.get("market") or "h2h").upper()
        outcome = b.get("bet_type") or ""
        line = f" {b['market_line']}" if b.get("market_line") is not None else ""
        book = b.get("book") or ""
        odds = b.get("odds_at_placement") or 0
        stake = b.get("stake") or 0
        kind = "PAPER" if b.get("is_paper") else "CASH"
        lines.append(
            f"  - {market} {outcome.upper()}{line} on {book} @ {odds:.2f} "
            f"({_decimal_to_american(odds)})  ${stake:.0f}  ({kind})"
        )
    return "\n".join(lines)


def _render_balances_block(book_balances: list[dict] | None) -> str:
    if not book_balances:
        return "  (balances not loaded)"
    return "\n".join(
        f"  {b.get('display_name', '?'):<14} ${(b.get('balance_usd') or 0):.0f}"
        for b in book_balances
    )


def _has_critical_flag(text: str) -> bool:
    """The 'ANOMALY FLAGS' section is conditional — Claude only includes
    it when a confirmed issue is found in the input data. Presence of the
    section heading is the signal that the fix-it banner should show."""
    upper = (text or "").upper()
    if "ANOMALY FLAGS" not in upper:
        return False
    # Heuristic: section is present but body just says 'none detected' →
    # not actually critical. Drop the false positive.
    after = upper.split("ANOMALY FLAGS", 1)[1][:200]
    if "NONE" in after and "DETECT" in after:
        return False
    return True


# --- main entry --------------------------------------------------------------


async def analyze_match(
    match_id: str,
    force: bool = False,
    *,
    ev_bets: list[dict] | None = None,
    existing_bets: list[dict] | None = None,
    todays_bets_count: int = 0,
    book_balances: list[dict] | None = None,
    max_bets_per_day: int = 3,
    max_stake_per_bet: int = 20,
) -> dict:
    """Get-or-generate analysis for a match.

    Returns the cached analysis if one exists and `cache_expires_at > now`,
    unless `force=True`. Enforces the daily budget cap before calling Claude.
    """
    if not force:
        cached = _cached_analysis(match_id)
        if cached:
            cached["cached"] = True
            cached["tokens_used"] = (cached.get("input_tokens") or 0) + (cached.get("output_tokens") or 0)
            return cached

    daily_cap = int(os.getenv("MAX_ANALYSIS_CALLS_PER_DAY", str(DEFAULT_DAILY_CAP)))
    today_count = _today_call_count()
    if today_count >= daily_cap:
        raise HTTPException(
            429,
            f"Analysis budget reached for today ({today_count}/{daily_cap}). "
            f"Resets at UTC midnight.",
        )

    with db() as conn:
        pred_row = conn.execute(
            "SELECT * FROM model_predictions WHERE match_id = ?", (match_id,)
        ).fetchone()
        if not pred_row:
            raise HTTPException(404, f"No prediction found for match_id={match_id}")
        prediction = dict(pred_row)
        anom_rows = conn.execute(
            """
            SELECT anomaly_type, description, edge_shown, model_prob, book_implied
            FROM anomaly_log WHERE match_id = ?
            ORDER BY created_at DESC LIMIT 20
            """,
            (match_id,),
        ).fetchall()
    anomalies = [dict(r) for r in anom_rows]

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise HTTPException(
            503,
            "ANTHROPIC_API_KEY not configured. Add it to /opt/betedge/backend/.env "
            "and restart betedge.service.",
        )

    user_prompt = _build_user_prompt(
        prediction, anomalies,
        ev_bets=ev_bets,
        existing_bets=existing_bets,
        todays_bets_count=todays_bets_count,
        book_balances=book_balances,
        max_bets_per_day=max_bets_per_day,
        max_stake_per_bet=max_stake_per_bet,
    )
    client = anthropic.AsyncAnthropic(api_key=api_key)

    try:
        response = await client.messages.create(
            model=MODEL_NAME,
            max_tokens=MAX_OUTPUT_TOKENS,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        )
    except anthropic.AuthenticationError:
        raise HTTPException(503, "Invalid ANTHROPIC_API_KEY — rotate and retry")
    except anthropic.RateLimitError:
        raise HTTPException(429, "Anthropic rate limit hit; try again in a moment")
    except anthropic.APIStatusError as e:
        log.exception("Anthropic API error: %s", e)
        raise HTTPException(502, f"Claude API error ({e.status_code}): {e.message}")
    except anthropic.APIConnectionError:
        raise HTTPException(503, "Cannot reach Anthropic API — check egress")

    analysis_text = "".join(b.text for b in response.content if getattr(b, "type", None) == "text")
    if not analysis_text:
        raise HTTPException(502, "Claude returned empty content")

    input_tokens = int(response.usage.input_tokens or 0)
    output_tokens = int(response.usage.output_tokens or 0)
    cost_usd = (input_tokens * INPUT_PRICE_PER_M + output_tokens * OUTPUT_PRICE_PER_M) / 1_000_000.0
    critical = _has_critical_flag(analysis_text)
    has_anom_section = critical  # the new prompt only includes the section on issues

    expires_at = (datetime.now(timezone.utc) + timedelta(seconds=CACHE_TTL_S)).strftime("%Y-%m-%d %H:%M:%S")

    with db() as conn:
        cur = conn.execute(
            """
            INSERT INTO match_analysis
              (match_id, analysis_text, anomalies_found, critical_flags,
               claude_model_used, input_tokens, output_tokens, cost_usd,
               cache_expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                match_id, analysis_text,
                1 if has_anom_section else 0,
                1 if critical else 0,
                MODEL_NAME, input_tokens, output_tokens, round(cost_usd, 6),
                expires_at,
            ),
        )
        analysis_id = cur.lastrowid

    return {
        "id": analysis_id,
        "match_id": match_id,
        "analysis_text": analysis_text,
        "anomalies_found": has_anom_section,
        "critical_flags": critical,
        "claude_model_used": MODEL_NAME,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "tokens_used": input_tokens + output_tokens,
        "cost_usd": round(cost_usd, 6),
        "cache_expires_at": expires_at,
        "cached": False,
    }
