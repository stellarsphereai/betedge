"""Dixon-Coles match prediction model.

Standard Poisson on weighted recent xG, plus the Dixon-Coles τ low-score
correction so 0-0/1-0/0-1/1-1 outcomes don't get under-priced. Time decay
follows the spec's discrete weights for the last 5 games.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from math import exp, factorial


HOME_GAMMA = 1.30  # home-field advantage multiplier on home_xg (Dixon-Coles γ)
# Combined attack penalty (rest + scorer-out) is floored at this multiplier so
# stacked penalties never erase more than 15% of base attack strength.
PENALTY_FLOOR = 0.85

# League-average goals per team per game. The xG formula divides by this so
# absolute attack/defense rates compose dimensionally as goals — not goals².
# Numbers are rolling multi-season EPL ≈ 1.40, UCL ≈ 1.35, EL ≈ 1.30, WC ≈ 1.20.
# Keys are API-Football league IDs.
LEAGUE_AVG_GOALS: dict[int, float] = {
    39: 1.40,   # EPL
    2:  1.35,   # UCL
    3:  1.30,   # Europa League
    1:  1.20,   # World Cup
}
LEAGUE_AVG_DEFAULT = 1.35


def league_avg_goals(league_id: int | None) -> float:
    """Look up league-average goals per team per game, with a 1.35 fallback for
    leagues we haven't measured (or when the caller didn't pass a league_id)."""
    if league_id is None:
        return LEAGUE_AVG_DEFAULT
    return LEAGUE_AVG_GOALS.get(league_id, LEAGUE_AVG_DEFAULT)


@dataclass(frozen=True)
class ModelParams:
    """Tunable Dixon-Coles parameters. Pass to predict() to override defaults
    (e.g. WC predictions load a calibrated set from model_params_wc.json)."""
    rho: float = -0.10                              # τ low-score correction
    # Time-decay weights, most recent first. 10-game window with a soft taper:
    # the latest game weighs full, then 0.10 step to game-2-ago and 0.05 steps
    # back to game-10-ago at 0.50.
    game_weights: tuple[float, ...] = (
        1.00, 0.90, 0.85, 0.80, 0.75, 0.70, 0.65, 0.60, 0.55, 0.50,
    )
    rest_tired_penalty: float = 0.92                # -8% attack when <2d less rest
    injured_scorer_penalty: float = 0.94            # -6% attack when top-3 scorer out
    ko_draw_damping: float = 0.85                   # knockout: dampen draw_prob
    home_gamma: float = HOME_GAMMA                  # home-field xG boost on home_xg
    penalty_floor: float = PENALTY_FLOOR            # stacked-penalty floor
    season_blend: float = 0.60                      # weight on recent form vs season mean
                                                    # (1.0 = recent only; 0.0 = season only)


DEFAULT_PARAMS = ModelParams()

# Backward-compat module-level constants (read-only — leave for any external readers)
GAME_WEIGHTS = list(DEFAULT_PARAMS.game_weights)
RHO = DEFAULT_PARAMS.rho
REST_TIRED_PENALTY = DEFAULT_PARAMS.rest_tired_penalty
INJURED_SCORER_PENALTY = DEFAULT_PARAMS.injured_scorer_penalty
KO_DRAW_DAMPING = DEFAULT_PARAMS.ko_draw_damping


@dataclass
class TeamForm:
    name: str
    xg_for: list[float]      # most recent first
    xg_against: list[float]  # most recent first
    rest_days: int = 4
    top_scorer_out: bool = False
    games_played: int = 5
    # Season-long averages (goals.for/against per match for the league/season).
    # When provided, team_strengths blends recent form with season mean per
    # `params.season_blend`. None on either side disables blending.
    season_avg_for: float | None = None
    season_avg_against: float | None = None

    @property
    def has_data(self) -> bool:
        return len(self.xg_for) > 0 and len(self.xg_against) > 0


@dataclass
class MatchPrediction:
    home_team: str
    away_team: str
    home_xg: float
    away_xg: float
    home_win_pct: float
    draw_pct: float
    away_win_pct: float
    btts_yes_pct: float
    btts_no_pct: float
    confidence: str
    score_matrix: list[list[float]]
    # Anomaly-detection scaffolding — surfaced so anomaly.py can flag stacked
    # penalties (Anomaly 3) and verify the gamma multiplier path (Anomaly 5)
    # without re-running the model.
    home_penalties_applied: list[str] = field(default_factory=list)
    away_penalties_applied: list[str] = field(default_factory=list)
    home_penalty_multiplier: float = 1.0
    away_penalty_multiplier: float = 1.0
    penalty_floor_applied: bool = False
    home_gamma_used: float = 1.0
    # Fix 1 — set when the Poisson BTTS Yes was dampened due to a side's
    # xG falling below the low-scoring threshold (away_xg < 1.0 or
    # home_xg < 1.0). False if no dampening fired.
    btts_low_xg_adjustment_applied: bool = False
    # Fix 2 — set when a tactical-suppressor team's factor was applied
    # to the total xG before computing 1X2 / BTTS / totals probabilities.
    tactical_suppressor_applied: bool = False
    suppressor_team: str | None = None
    suppressor_factor: float = 1.0

    def over_pct(self, line: float) -> float:
        """P(home_goals + away_goals > line). Works for any line we can resolve from the 6x6 matrix."""
        return _over_from_matrix(self.score_matrix, line)

    def under_pct(self, line: float) -> float:
        return 1.0 - self.over_pct(line)


class ModelInvariantError(RuntimeError):
    """Raised when a runtime invariant in model.predict() fails — currently
    the home-field gamma multiplier check (Anomaly 5). This halts the model
    run rather than silently producing un-gamma'd home_xg."""


def _btts_yes_from_matrix(matrix: list[list[float]]) -> float:
    return sum(matrix[h][a] for h in range(1, len(matrix)) for a in range(1, len(matrix[0])))


def _over_from_matrix(matrix: list[list[float]], line: float) -> float:
    """Sum of cells where home_goals + away_goals > line. For half-line totals this
    is exact; integer lines (1, 2, …) ignore the push case."""
    return sum(
        matrix[h][a]
        for h in range(len(matrix))
        for a in range(len(matrix[0]))
        if (h + a) > line
    )


def _weighted_avg(values: list[float], weights: tuple[float, ...]) -> float:
    if not values:
        return 0.0
    w = list(weights[: len(values)])
    return sum(v * x for v, x in zip(values, w)) / sum(w)


_SMALL_SAMPLE_THRESHOLD = 3  # teams with fewer games get reduced season-avg weight
_SEASON_AVG_CLAMP = 2.0      # clamp season avg to [league_avg / clamp, league_avg * clamp]

def team_strengths(form: TeamForm, params: ModelParams = DEFAULT_PARAMS,
                   league_id: int | None = None) -> tuple[float, float]:
    """Return (attack, defense). Blends time-decayed recent xG with season-long
    averages when both sides are present (season_blend = weight on recent).

    When a team has fewer than _SMALL_SAMPLE_THRESHOLD games played, the
    season average is unreliable (e.g. 2 WC matches → 3.0 goals/game noise).
    In that case, boost the recent-form weight so the noisy season average
    doesn't dominate the prediction.

    Season averages are also clamped to [league_avg/2, league_avg*2] so
    extreme values from small tournament samples can't distort predictions."""
    recent_for = _weighted_avg(form.xg_for, params.game_weights)
    recent_against = _weighted_avg(form.xg_against, params.game_weights)
    if form.season_avg_for and form.season_avg_against:
        b = params.season_blend
        if form.games_played < _SMALL_SAMPLE_THRESHOLD:
            b = 1.0 - (1.0 - b) * (form.games_played / _SMALL_SAMPLE_THRESHOLD)
        avg = league_avg_goals(league_id)
        lo = avg / _SEASON_AVG_CLAMP
        hi = avg * _SEASON_AVG_CLAMP
        sa_for = max(lo, min(hi, form.season_avg_for))
        sa_against = max(lo, min(hi, form.season_avg_against))
        return (
            recent_for * b + sa_for * (1 - b),
            recent_against * b + sa_against * (1 - b),
        )
    return (recent_for, recent_against)


def _poisson_pmf(k: int, lam: float) -> float:
    if lam <= 0:
        return 1.0 if k == 0 else 0.0
    return exp(-lam) * (lam**k) / factorial(k)


def _dc_tau(h: int, a: int, lh: float, la: float, rho: float) -> float:
    """Dixon-Coles low-score correction."""
    if h == 0 and a == 0:
        return 1.0 - lh * la * rho
    if h == 1 and a == 0:
        return 1.0 + la * rho
    if h == 0 and a == 1:
        return 1.0 + lh * rho
    if h == 1 and a == 1:
        return 1.0 - rho
    return 1.0


def predict(
    home: TeamForm,
    away: TeamForm,
    knockout: bool = False,
    params: ModelParams | None = None,
    league_id: int | None = None,
) -> MatchPrediction:
    p = params or DEFAULT_PARAMS
    league_avg = league_avg_goals(league_id)
    h_atk, h_def = team_strengths(home, p, league_id=league_id)
    a_atk, a_def = team_strengths(away, p, league_id=league_id)

    # Stack rest + scorer-out penalties as a single multiplier per team, then
    # floor the combined effect at penalty_floor (default 0.85x base) so an
    # already-weak team can never get penalized more than 15%.
    rest_diff = home.rest_days - away.rest_days
    home_pen = 1.0
    away_pen = 1.0
    home_pens: list[str] = []
    away_pens: list[str] = []
    # Rest penalty only applies when both teams have played recently
    # (within 14 days). Pre-tournament rest gaps of 200+ days are
    # meaningless — neither team is fatigued.
    REST_RELEVANCE_CAP = 14
    if home.rest_days <= REST_RELEVANCE_CAP and away.rest_days <= REST_RELEVANCE_CAP:
        if rest_diff < -2:
            home_pen *= p.rest_tired_penalty
            home_pens.append("rest_tired")
        elif rest_diff > 2:
            away_pen *= p.rest_tired_penalty
            away_pens.append("rest_tired")
    if home.top_scorer_out:
        home_pen *= p.injured_scorer_penalty
        home_pens.append("scorer_out")
    if away.top_scorer_out:
        away_pen *= p.injured_scorer_penalty
        away_pens.append("scorer_out")
    home_pen_floored = max(home_pen, p.penalty_floor)
    away_pen_floored = max(away_pen, p.penalty_floor)
    floor_applied = (home_pen_floored != home_pen) or (away_pen_floored != away_pen)
    h_atk *= home_pen_floored
    a_atk *= away_pen_floored

    # Dixon-Coles xG with league-average normalisation. Without the divide by
    # league_avg, multiplying two absolute goals/game rates produces goals² —
    # which over-predicts totals by ~50% on EPL (mean predicted ≈ 4.2 vs
    # actual ≈ 2.7). γ is applied to home_xg only (Dixon-Coles convention).
    home_xg = max(0.05, h_atk * a_def / league_avg * p.home_gamma)
    away_xg = max(0.05, a_atk * h_def / league_avg)

    # ANOMALY 5 — runtime invariant: home_xg MUST include p.home_gamma. Compare
    # against the same expression with gamma=1; the two must differ unless the
    # 0.05 floor saturated. Catches future refactors that silently drop γ.
    _base = h_atk * a_def / league_avg
    _no_gamma = max(0.05, _base)
    _with_gamma = max(0.05, _base * p.home_gamma)
    if abs(home_xg - _with_gamma) > 1e-9 or (
        p.home_gamma != 1.0 and _base > 0.05 / p.home_gamma
        and abs(_no_gamma - _with_gamma) < 1e-9
    ):
        raise ModelInvariantError(
            f"home_gamma={p.home_gamma} not applied to home_xg "
            f"(home_xg={home_xg}, expected≈{_with_gamma}, no-gamma={_no_gamma})"
        )

    matrix = [[0.0] * 6 for _ in range(6)]
    for h in range(6):
        for a in range(6):
            matrix[h][a] = (
                _poisson_pmf(h, home_xg)
                * _poisson_pmf(a, away_xg)
                * _dc_tau(h, a, home_xg, away_xg, p.rho)
            )

    # Renormalise (we truncated to 0-5)
    total = sum(sum(row) for row in matrix)
    if total > 0:
        matrix = [[c / total for c in row] for row in matrix]

    home_win = sum(matrix[h][a] for h in range(6) for a in range(6) if h > a)
    draw = sum(matrix[i][i] for i in range(6))
    away_win = sum(matrix[h][a] for h in range(6) for a in range(6) if a > h)

    if knockout:
        damped = draw * p.ko_draw_damping
        leak = draw - damped
        denom = home_win + away_win
        if denom > 0:
            home_win += leak * (home_win / denom)
            away_win += leak * (away_win / denom)
        draw = damped

    # Neutral-venue draw inflation — WC matches on neutral ground
    # historically produce ~28% draws vs Poisson model's typical 20-25%.
    # When home_gamma is 1.0 (neutral venue), inflate draw probability
    # by transferring mass from home_win and away_win proportionally.
    _NEUTRAL_DRAW_BOOST = 1.08  # +8% draw inflation (reduced from 12% after WC home-bias recal)
    if not knockout and p.home_gamma <= 1.05:
        boosted = min(draw * _NEUTRAL_DRAW_BOOST, 0.50)  # cap at 50%
        added = boosted - draw
        if added > 0 and (home_win + away_win) > 0:
            home_share = home_win / (home_win + away_win)
            home_win -= added * home_share
            away_win -= added * (1 - home_share)
            draw = boosted

    # Confidence rating reflects DATA quality, not roster quality. Top-scorer-
    # out is already priced into the prediction via injured_scorer_penalty
    # (-6% on attack), so don't double-count it here. A 3-day rest difference
    # (midweek → weekend EPL) is common — use ≤3, not <3.
    games = min(len(home.xg_for), len(away.xg_for))
    rested = abs(rest_diff) <= 3
    if games >= 5 and rested:
        confidence = "HIGH"
    elif games >= 3:
        confidence = "MEDIUM"
    else:
        confidence = "LOW"

    rounded_matrix = [[round(p, 5) for p in row] for row in matrix]
    btts_yes = _btts_yes_from_matrix(matrix)
    # Fix 1 — when one side has very low predicted xG, the Poisson-based
    # BTTS Yes calc tends to overstate the both-teams-score probability
    # (independence between the two scoring distributions doesn't hold
    # for low-scoring teams that often produce 0 across whole matches).
    # Empirically the model has been calling BTTS Yes on lopsided
    # matchups and losing — apply a multiplicative dampener and
    # renormalize implicitly (BTTS No = 1 - BTTS Yes).
    btts_low_xg_adjustment_applied = False
    if away_xg < 0.70:
        btts_yes *= 0.75
        btts_low_xg_adjustment_applied = True
    elif away_xg < 1.00:
        btts_yes *= 0.85
        btts_low_xg_adjustment_applied = True
    # Same dampener mirrored for low home_xg (rare but possible — heavy
    # away favorite at a struggling host).
    if home_xg < 0.70:
        btts_yes *= 0.75
        btts_low_xg_adjustment_applied = True
    elif home_xg < 1.00:
        btts_yes *= 0.85
        btts_low_xg_adjustment_applied = True
    # Fix 4 — global BTTS discount. The Poisson independence assumption
    # systematically overstates BTTS Yes (game state, tactics, and
    # defensive adjustments after conceding are not modeled). Settled
    # bets show 31% BTTS win rate vs model predictions of ~50-60%.
    # Apply a flat 0.85x discount on top of the low-xG dampener.
    _BTTS_GLOBAL_DISCOUNT = 0.85
    btts_yes *= _BTTS_GLOBAL_DISCOUNT

    btts_yes = max(0.0, min(1.0, btts_yes))

    return MatchPrediction(
        home_team=home.name,
        away_team=away.name,
        home_xg=round(home_xg, 3),
        away_xg=round(away_xg, 3),
        home_win_pct=round(home_win, 4),
        draw_pct=round(draw, 4),
        away_win_pct=round(away_win, 4),
        btts_yes_pct=round(btts_yes, 4),
        btts_no_pct=round(1.0 - btts_yes, 4),
        btts_low_xg_adjustment_applied=btts_low_xg_adjustment_applied,
        confidence=confidence,
        score_matrix=rounded_matrix,
        home_penalties_applied=home_pens,
        away_penalties_applied=away_pens,
        home_penalty_multiplier=round(home_pen_floored, 4),
        away_penalty_multiplier=round(away_pen_floored, 4),
        penalty_floor_applied=floor_applied,
        home_gamma_used=p.home_gamma,
    )
