"""Pre-dashboard anomaly detector.

Runs on every prediction + every EV bet before they reach the user. Surfaces
five classes of guardrail trip-wires the model can hit when inputs are noisy,
the corpus is too small, or a future code change silently regresses a fix.

  1. EDGE_HIGH       — edge > league-specific threshold on FanDuel/DraftKings
                       (15% EPL/EL, 12% UCL, 10% WC — sharper books, lower bar)
  2. PHANTOM_EDGE    — edge > 25% on any book (cross-league constant)
  3. SHARP_DIVERGE   — model vs FD/DK implied differs by > league threshold
                       (20pp EPL/EL, 15pp UCL/WC)
  4. PENALTY_STACK   — multiple penalties applied to the same team
  5. FORM_DIVERGE    — last-5 xG diverges > 40% from season average

Anomaly 5 in the spec (gamma invariant) lives in model.predict() as a runtime
assertion that raises ModelInvariantError — it halts the run rather than
flowing through this detector, so it's not handled here.

Each detector returns a list of `Anomaly` records; persist with `log_many()`.
EDGE_HIGH downgrades the bet's confidence to LOW. PHANTOM_EDGE excludes the
bet from recommendations entirely (`actionable=False`).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from database import db
import league_config


# ---- thresholds -----------------------------------------------------------

# Cross-league constants. Per-league overrides for EDGE_HIGH and SHARP_DIVERGE
# come from the league_config table via league_config.thresholds_for_league().
EDGE_PHANTOM_THRESHOLD = 0.25   # any book — phantom edge gate is league-agnostic
FORM_DIVERGE_THRESHOLD = 0.40   # 40% relative — same threshold across leagues

# How far the model can drift from the de-vigged book consensus before we
# treat it as a model error rather than a real edge. Per-league because
# corpus quality varies — UCL/WC are smaller, sharper markets so we tighten.
MARKET_CONSENSUS_THRESHOLDS_PP = {
    "epl":       0.06,
    "ucl":       0.05,
    "uel":       0.06,
    "world_cup": 0.04,
}
DEFAULT_MARKET_CONSENSUS_THRESHOLD_PP = 0.06

# Books treated as "sharp" for the divergence check. Match book TITLES the way
# odds_client emits them (BOOK_TITLE_OVERRIDES + Odds API .title).
SHARP_BOOK_TITLES = {"FanDuel", "DraftKings"}

# ---- types ----------------------------------------------------------------


@dataclass
class Anomaly:
    """One trip-wire firing on one (match × bet) tuple."""
    anomaly_type: str
    description: str
    match_id: str
    home_team: str
    away_team: str
    edge_shown: float | None = None
    model_prob: float | None = None
    book_implied: float | None = None
    # Action hints consumed by the /ev-bets pipeline:
    excludes_bet: bool = False        # PHANTOM_EDGE — actionable=false
    downgrades_to_low: bool = False   # EDGE_HIGH — confidence='LOW'
    extras: dict = field(default_factory=dict)  # free-form payload (e.g. penalties)


# ---- per-bet detectors ----------------------------------------------------


def detect_edge_anomalies(bet: dict, league: str | None = None) -> list[Anomaly]:
    """Anomalies 1a + 1b. Inspects the bet's edge against the league's edge
    threshold (from league_config) for sharp books, plus the cross-league
    phantom threshold of 25%."""
    out: list[Anomaly] = []
    edge = bet.get("edge")
    if edge is None:
        return out
    book = (bet.get("book") or "").strip()
    edge_threshold = league_config.thresholds_for_league(league or "").edge_threshold
    common = dict(
        match_id=bet["match_id"],
        home_team=bet["home_team"],
        away_team=bet["away_team"],
        edge_shown=edge,
        model_prob=bet.get("model_prob"),
        book_implied=bet.get("true_implied_prob"),
    )

    outcome = bet.get("outcome")
    if edge > EDGE_PHANTOM_THRESHOLD:
        out.append(Anomaly(
            anomaly_type="PHANTOM_EDGE",
            description=(
                f"{edge*100:.1f}% edge on {book or 'unknown book'} exceeds the "
                f"{EDGE_PHANTOM_THRESHOLD*100:.0f}% phantom threshold — "
                f"likely a model error, not market mispricing"
            ),
            excludes_bet=True,
            extras={"outcome": outcome},
            **common,
        ))
        return out  # phantom dominates; skip the EDGE_HIGH check

    if edge > edge_threshold and book in SHARP_BOOK_TITLES:
        out.append(Anomaly(
            anomaly_type="EDGE_HIGH",
            description=(
                f"{edge*100:.1f}% edge on {book} exceeds the "
                f"{edge_threshold*100:.0f}% {(league or 'default').upper()} "
                f"market-efficiency threshold — sharp book disagreement "
                f"implies model over-confidence"
            ),
            downgrades_to_low=True,
            extras={"outcome": outcome},
            **common,
        ))
    return out


def detect_sharp_divergence(
    bet: dict, match_consensus: dict | None, league: str | None = None
) -> list[Anomaly]:
    """Anomaly 2. Per-bet check: does the model probability differ from the
    de-vigged FD/DK implied probability by more than the league's
    sharp_divergence threshold (20pp EPL/EL, 15pp UCL/WC)?"""
    out: list[Anomaly] = []
    model_p = bet.get("model_prob")
    if model_p is None:
        return out
    book = (bet.get("book") or "").strip()
    if book not in SHARP_BOOK_TITLES:
        return out
    book_p = bet.get("true_implied_prob")
    if book_p is None:
        return out
    delta = abs(model_p - book_p)
    threshold = league_config.thresholds_for_league(league or "").sharp_divergence
    if delta < threshold:
        return out
    out.append(Anomaly(
        anomaly_type="SHARP_DIVERGE",
        description=(
            f"Sharp book disagreement: model {model_p*100:.0f}% vs "
            f"{book} implies {book_p*100:.0f}% ({delta*100:.0f}pp gap, "
            f"{(league or 'default').upper()} threshold {threshold*100:.0f}pp) — "
            f"verify model inputs before betting"
        ),
        match_id=bet["match_id"],
        home_team=bet["home_team"],
        away_team=bet["away_team"],
        edge_shown=bet.get("edge"),
        model_prob=model_p,
        book_implied=book_p,
        extras={"outcome": bet.get("outcome")},
    ))
    return out


def detect_market_consensus_divergence(
    bet: dict,
    consensus_prob: float | None,
    league: str | None = None,
) -> list[Anomaly]:
    """MARKET_CONSENSUS_DIVERGENCE.

    Catches model-vs-market disagreements where it's not one outlier book —
    every book agrees and our model is the one out on its own. We compare
    `model_prob` against the de-vigged average across all books offering
    this outcome (`consensus_prob`); if the gap exceeds the league's
    threshold, downgrade to LOW so the bet drops out of /best-bets.

    The `excludes_bet=False` choice is deliberate: we still want the row to
    show up in the +EV grid with the flag visible, so the user can inspect
    the disagreement and decide for themselves. Top-3 / digest exclude it
    via the existing downgrades-to-low filter.
    """
    out: list[Anomaly] = []
    model_p = bet.get("model_prob")
    if model_p is None or consensus_prob is None:
        return out
    threshold = MARKET_CONSENSUS_THRESHOLDS_PP.get(
        (league or "").lower(), DEFAULT_MARKET_CONSENSUS_THRESHOLD_PP
    )
    delta = model_p - consensus_prob
    if abs(delta) <= threshold:
        return out
    direction = "above" if delta > 0 else "below"
    out.append(Anomaly(
        anomaly_type="MARKET_CONSENSUS_DIVERGENCE",
        description=(
            f"Model {model_p * 100:.1f}% vs market consensus {consensus_prob * 100:.1f}% "
            f"({delta * 100:+.1f}pp gap, {direction} consensus) — exceeds the "
            f"{(league or 'default').upper()} threshold of {threshold * 100:.0f}pp. "
            f"Whole market disagrees with the model on this outcome."
        ),
        match_id=bet.get("match_id", ""),
        home_team=bet.get("home_team", ""),
        away_team=bet.get("away_team", ""),
        edge_shown=bet.get("edge"),
        model_prob=model_p,
        book_implied=consensus_prob,
        downgrades_to_low=True,  # excluded from top 3 / digest, still visible in grid
        extras={"outcome": bet.get("outcome")},
    ))
    return out


# ---- per-prediction detectors --------------------------------------------


def detect_penalty_stack(prediction, match_id: str = "") -> list[Anomaly]:
    """Anomaly 3. Reads `prediction.{home,away}_penalties_applied` set by
    model.predict(). Fires when more than one penalty hits the same team."""
    out: list[Anomaly] = []
    for side, pens, mult in (
        ("home", prediction.home_penalties_applied, prediction.home_penalty_multiplier),
        ("away", prediction.away_penalties_applied, prediction.away_penalty_multiplier),
    ):
        if len(pens) <= 1:
            continue
        floor_note = " · penalty cap applied" if prediction.penalty_floor_applied and mult == 0.85 else ""
        reduction = (1.0 - mult) * 100
        out.append(Anomaly(
            anomaly_type="PENALTY_STACK",
            description=(
                f"{side.capitalize()} team has {len(pens)} stacked penalties "
                f"({', '.join(pens)}); combined attack multiplier {mult:.3f} "
                f"(−{reduction:.1f}%){floor_note}"
            ),
            match_id=match_id,
            home_team=prediction.home_team,
            away_team=prediction.away_team,
            extras={
                "side": side,
                "penalties": pens,
                "combined_multiplier": mult,
                "reduction_pct": round(reduction, 2),
                "floor_applied": prediction.penalty_floor_applied,
            },
        ))
    return out


def detect_form_divergence(form, side: str, prediction, match_id: str = "") -> list[Anomaly]:
    """Anomaly 4. Compares the unweighted last-5 xG average against the season
    average passed in via TeamForm.season_avg_for / season_avg_against. Fires
    when relative divergence exceeds 40% on either xG or xGA."""
    out: list[Anomaly] = []
    if form.season_avg_for is None or form.season_avg_against is None:
        return out
    xg_for = form.xg_for[:5]
    xga = form.xg_against[:5]
    if len(xg_for) < 3 or len(xga) < 3:
        return out
    last5_for = sum(xg_for) / len(xg_for)
    last5_against = sum(xga) / len(xga)
    season_for = form.season_avg_for
    season_against = form.season_avg_against

    def _rel(a: float, b: float) -> float:
        if b <= 0:
            return 0.0
        return abs(a - b) / b

    triggered = []
    if _rel(last5_for, season_for) > FORM_DIVERGE_THRESHOLD:
        triggered.append(("xG", last5_for, season_for))
    if _rel(last5_against, season_against) > FORM_DIVERGE_THRESHOLD:
        triggered.append(("xGA", last5_against, season_against))

    for metric, recent, season in triggered:
        delta_pct = (recent - season) / season * 100
        out.append(Anomaly(
            anomaly_type="FORM_DIVERGE",
            description=(
                f"{side.capitalize()} {metric} recent form diverges from season baseline: "
                f"season avg {season:.2f} vs last-5 avg {recent:.2f} "
                f"({delta_pct:+.0f}%) — recent stretch may be variance, not signal"
            ),
            match_id=match_id,
            home_team=prediction.home_team,
            away_team=prediction.away_team,
            extras={
                "side": side,
                "metric": metric,
                "season_avg": round(season, 3),
                "last5_avg": round(recent, 3),
                "delta_pct": round(delta_pct, 1),
            },
        ))
    return out


# ---- persistence ----------------------------------------------------------


def _dedup_key(r: "Anomaly") -> str:
    """Per-day per-(match, outcome, type) key. Outcome is pulled from the
    bet's identity tuple stored in `extras['outcome']` if the caller put it
    there, otherwise we derive from `(model_prob, book_implied)` to keep
    home/draw/away distinct (different outcomes have different prob pairs).
    """
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).date().isoformat()
    outcome = (r.extras or {}).get("outcome") if hasattr(r, "extras") else None
    if not outcome:
        # Fall back to a discriminator that varies across home/draw/away
        # without having to thread `outcome` through every call site.
        outcome = f"m{round((r.model_prob or 0)*1000)}-b{round((r.book_implied or 0)*1000)}"
    return f"{r.match_id}:{outcome}:{r.anomaly_type}:{today}"


def log_many(rows: Iterable[Anomaly]) -> int:
    """Append rows to anomaly_log. Returns the number of writes performed
    (inserts + value-refreshing updates).

    Same anomaly on the same match × outcome × day fires once per day, but
    when it re-fires we UPSERT — overwriting model_prob / book_implied /
    edge / description with the latest values. Without the upsert path,
    a stale entry from an earlier (pre-deploy / pre-resync) run stays in
    the log forever and the AI Analysis ingests the stale numbers.
    """
    rows = list(rows)
    if not rows:
        return 0
    written = 0
    with db() as conn:
        for r in rows:
            cur = conn.execute(
                """
                INSERT INTO anomaly_log
                  (match_id, home_team, away_team, anomaly_type, description,
                   edge_shown, model_prob, book_implied, dedup_key)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(dedup_key) DO UPDATE SET
                  description  = excluded.description,
                  edge_shown   = excluded.edge_shown,
                  model_prob   = excluded.model_prob,
                  book_implied = excluded.book_implied
                """,
                (r.match_id, r.home_team, r.away_team, r.anomaly_type, r.description,
                 r.edge_shown, r.model_prob, r.book_implied, _dedup_key(r)),
            )
            if cur.rowcount > 0:
                written += 1
    return written


def recent(limit: int = 200, since_iso: str | None = None) -> list[dict]:
    """Read recent anomalies for the dashboard tab. `since_iso` defaults to
    the start of UTC today when None, capping the listing to today's flags."""
    from datetime import datetime, timezone
    if since_iso is None:
        since_iso = datetime.now(timezone.utc).date().isoformat()
    with db() as conn:
        rows = conn.execute(
            """
            SELECT * FROM anomaly_log
            WHERE created_at >= ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (since_iso, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def count_today() -> int:
    """Lightweight count for the header badge."""
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).date().isoformat()
    with db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM anomaly_log WHERE created_at >= ?",
            (today,),
        ).fetchone()
    return int(row["n"] or 0)


def excluded_match_ids_today() -> set[str]:
    """Match IDs flagged with PHANTOM_EDGE or EDGE_HIGH today. Used by the
    digest renderer to keep flagged matches out of recommendation tables."""
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).date().isoformat()
    with db() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT match_id FROM anomaly_log
            WHERE created_at >= ?
              AND anomaly_type IN ('PHANTOM_EDGE', 'EDGE_HIGH')
              AND match_id IS NOT NULL AND match_id != ''
            """,
            (today,),
        ).fetchall()
    return {r["match_id"] for r in rows}
