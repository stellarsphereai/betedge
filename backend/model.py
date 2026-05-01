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


def team_strengths(form: TeamForm, params: ModelParams = DEFAULT_PARAMS) -> tuple[float, float]:
    """Return (attack, defense). Blends time-decayed recent xG with season-long
    averages when both sides are present (season_blend = weight on recent)."""
    recent_for = _weighted_avg(form.xg_for, params.game_weights)
    recent_against = _weighted_avg(form.xg_against, params.game_weights)
    if form.season_avg_for is not None and form.season_avg_against is not None:
        b = params.season_blend
        return (
            recent_for * b + form.season_avg_for * (1 - b),
            recent_against * b + form.season_avg_against * (1 - b),
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
) -> MatchPrediction:
    p = params or DEFAULT_PARAMS
    h_atk, h_def = team_strengths(home, p)
    a_atk, a_def = team_strengths(away, p)

    # Stack rest + scorer-out penalties as a single multiplier per team, then
    # floor the combined effect at penalty_floor (default 0.85x base) so an
    # already-weak team can never get penalized more than 15%.
    rest_diff = home.rest_days - away.rest_days
    home_pen = 1.0
    away_pen = 1.0
    home_pens: list[str] = []
    away_pens: list[str] = []
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

    # Home-field advantage: γ applied to home_xg only (Dixon-Coles convention).
    home_xg = max(0.05, h_atk * a_def * p.home_gamma)
    away_xg = max(0.05, a_atk * h_def)

    # ANOMALY 5 — runtime invariant: home_xg MUST include p.home_gamma. Compare
    # against the same expression with gamma=1; the two must differ unless the
    # 0.05 floor saturated. Catches future refactors that silently drop γ.
    _no_gamma = max(0.05, h_atk * a_def)
    _with_gamma = max(0.05, h_atk * a_def * p.home_gamma)
    if abs(home_xg - _with_gamma) > 1e-9 or (
        p.home_gamma != 1.0 and h_atk * a_def > 0.05 / p.home_gamma
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

    games = min(len(home.xg_for), len(away.xg_for))
    no_inj = not (home.top_scorer_out or away.top_scorer_out)
    rested = abs(rest_diff) < 3
    if games >= 5 and no_inj and rested:
        confidence = "HIGH"
    elif games >= 3:
        confidence = "MEDIUM"
    else:
        confidence = "LOW"

    rounded_matrix = [[round(p, 5) for p in row] for row in matrix]
    btts_yes = _btts_yes_from_matrix(matrix)

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
        confidence=confidence,
        score_matrix=rounded_matrix,
        home_penalties_applied=home_pens,
        away_penalties_applied=away_pens,
        home_penalty_multiplier=round(home_pen_floored, 4),
        away_penalty_multiplier=round(away_pen_floored, 4),
        penalty_floor_applied=floor_applied,
        home_gamma_used=p.home_gamma,
    )
