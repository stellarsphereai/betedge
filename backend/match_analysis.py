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
    "You are an expert soccer betting analyst reviewing a prediction from a "
    "Poisson model. Your job is to:\n"
    "1. Explain the prediction in plain English\n"
    "2. Validate whether the inputs make sense\n"
    "3. Flag any anomalies or concerns\n"
    "4. Give an honest verdict on each bet\n"
    "5. Be direct and concise — no fluff\n\n"
    "Always structure your response in these exact sections, each on its "
    "own line and prefixed with the section name in CAPS followed by a colon. "
    "Section names must appear verbatim:\n"
    "TEAM FORM:\n"
    "XG ANALYSIS:\n"
    "MODEL INPUTS:\n"
    "BET VERDICTS:\n"
    "ANOMALY FLAGS:\n"
    "FINAL VERDICT:\n\n"
    "In the ANOMALY FLAGS section, prefix each flag with one of these "
    "exact tokens so the UI can color-code:\n"
    "- 'CRITICAL' for an issue that should halt betting on this match\n"
    "- 'WARNING' for something to monitor\n"
    "- 'INFO' for context worth noting\n"
    "Use 'CRITICAL' sparingly — only for genuine model issues, not minor "
    "data gaps."
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


def _build_user_prompt(prediction: dict, anomalies: list[dict]) -> str:
    """Assemble the per-match prompt from stored prediction + anomaly_log."""
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

    return (
        f"Analyze this match prediction:\n\n"
        f"Match: {p.get('home_team')} vs {p.get('away_team')}\n"
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
        f"MODEL ADJUSTMENTS:\n"
        f"Gamma (home-field) applied: {p.get('gamma_used')}\n"
        f"Penalties: {penalties_str}\n"
        f"Season blend (recent vs season avg): {p.get('season_blend_used')}\n"
        f"League-avg goals per team per game: {league_avg}\n\n"
        f"ANOMALIES DETECTED IN PIPELINE:\n{anom_str}\n\n"
        f"Note: Per-team form details (last-10 xG, attack/defense ratings, rest "
        f"days, injuries, season averages) are not exposed in the stored "
        f"prediction record. Analyze based on the model output, gamma/penalty/"
        f"blend choices, and pipeline anomalies above. If you'd flag a concern "
        f"that requires the missing detail to confirm, say so explicitly.\n\n"
        f"Explain this prediction. Validate the math. Flag anything that "
        f"warrants concern. Give a verdict on the match overall and on "
        f"betting it. Be honest about what the data here can and cannot tell us."
    )


def _has_critical_flag(text: str) -> bool:
    """The model is instructed to use the literal token CRITICAL only for
    genuine model issues. Substring match is good enough — we tolerate
    stylistic mentions; the user can always click through."""
    return "CRITICAL" in (text or "")


# --- main entry --------------------------------------------------------------


async def analyze_match(match_id: str, force: bool = False) -> dict:
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

    user_prompt = _build_user_prompt(prediction, anomalies)
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
    has_anom_section = "ANOMALY FLAGS" in analysis_text.upper()

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
