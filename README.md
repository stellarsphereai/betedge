# BetEdge NY

EV-based sports betting tool for NY-legal sportsbooks (FanDuel, DraftKings,
Caesars, BetRivers, Fanatics, ESPN Bet, Bally Bet) covering EPL (paper-trade
mode) and the 2026 World Cup (live recommendations).

> **Status: Phase A** — backend math + DB + FastAPI endpoints + console demo.
> Phases B–E (Understat scraper, frontend, scheduler/email, deploy scripts)
> not yet built.

## What's in this phase

- **Dixon-Coles prediction model** (`model.py`) — weighted recent xG with the
  τ low-score correction so draws aren't under-predicted. Rest-day penalty,
  injured-scorer penalty, knockout-stage draw damping.
- **EV calculator** (`ev_calculator.py`) — proportional vig removal per
  bookmaker, edge = model_prob − fair_implied_prob.
- **Kelly sizer** (`kelly.py`) — `(edge × bankroll) / odds`, capped at 2%
  bankroll, $5 floor.
- **Line shopper** (`line_shopper.py`) — best/second-best book per outcome,
  movement-vs-opening timing buckets (GREEN/AMBER/RED).
- **CLV tracker** (`clv_tracker.py`) — placement → closing line → settled.
- **Accuracy tracker** (`accuracy.py`) — Brier score, win rate, and a
  CLV-based "ready for real money" gate (50+ bets, positive avg CLV) on
  top of the spec's win-rate gate.
- **SQLite schema** (`database.py`) — `model_predictions`, `bets_placed`,
  `fixtures`, `daily_stats`.
- **FastAPI app** (`main.py`) — all eight endpoints from the spec wired up.
- **Demo** (`demo.py`) — runs the model on three EPL fixtures with mocked
  xG and prints predictions to verify math.

Phase A intentionally accepts xG via the API; Phase B replaces that with
Understat scraping + API-Football fixture lookup.

## Quick start

```bash
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# (1) verify the math
python3 demo.py

# (2) run the FastAPI server
uvicorn main:app --reload --port 8002
```

Endpoints:

| Method | Path | Notes |
|---|---|---|
| GET | `/predictions` | latest stored predictions |
| GET | `/ev-bets?bankroll=1000&min_edge=0.03&league=epl` | live +EV bets vs current Odds API prices |
| GET | `/bets` | all placed bets |
| POST | `/bets` | log a paper or real bet |
| GET | `/stats` | bankroll/CLV/accuracy summary |
| GET | `/fixtures` | upcoming fixtures (populated in Phase B) |
| POST | `/run-model` | run model on an explicit list of fixtures (xG supplied in body) |
| GET | `/digest-preview` | data the digest would render (Phase D builds the email) |
| POST | `/send-digest` | 501 until Phase D |

## Design notes worth knowing

- **Dixon-Coles vs vanilla Poisson.** The spec says "Dixon-Coles" but the
  math described is independent Poisson. We implemented the τ correction
  (the actual D-C contribution) on top so 0-0 / 1-0 / 0-1 / 1-1 outcomes
  aren't under-priced.
- **"Ready for real money" gate.** The spec gates on win rate ≥ 0.65 over
  20+ matches. That sample size has ±18% confidence — too noisy to be
  load-bearing. We surface that gate but ALSO enforce a CLV gate (≥50
  bets with positive avg CLV) before the readiness flag flips.
- **Validation philosophy.** CLV (closing-line value) is the primary
  signal in the dashboard. Win rate is informational only at small N.
- **NY books and ToS.** Sustained +EV play causes stake limits and account
  closures at every NY-legal sportsbook — they monitor for it. This tool
  doesn't try to disguise patterns.

## Sample run

```text
$ python3 demo.py
========================================================================
Dixon-Coles model demo — 3 EPL fixtures, mocked xG
========================================================================

▸ Manchester City vs Brentford (heavy fav)
  Expected goals : Manchester City 2.829  ·  Brentford 0.928
  Probabilities  : 71.51% home / 17.06% draw / 11.43% away
  Confidence     : HIGH
  Probability sum: 1.0000 (should be ≈ 1.0)

▸ Liverpool vs Chelsea (evenly matched)
  ...
```

(actual numbers depend on your weights / RHO; demo prints whatever the
current model produces).

## What's coming

| Phase | Scope |
|---|---|
| **B** | Understat scraper, API-Football client, fixture sync, opening/closing-line capture |
| **C** | React + Vite + Tailwind frontend with charts |
| **D** | APScheduler jobs (00:00 model, 06:00 EV, 08:00 digest, kickoff CLV, 23:55 P&L) + Gmail SMTP digest |
| **E** | `setup.sh`, nginx config, `betedge.service`, full Ubuntu 24.04 deploy guide |
