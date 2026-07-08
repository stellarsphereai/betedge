"""SQLite schema + simple connection helpers."""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager

DB_PATH = os.getenv("DB_PATH", "betedge.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS model_predictions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id TEXT UNIQUE,
    home_team TEXT,
    away_team TEXT,
    league TEXT,
    kickoff_time TEXT,
    home_win_pct REAL,
    draw_pct REAL,
    away_win_pct REAL,
    btts_yes_pct REAL,
    home_xg REAL,
    away_xg REAL,
    confidence TEXT,
    score_matrix_json TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS bets_placed (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id TEXT,
    home_team TEXT,
    away_team TEXT,
    bet_type TEXT,
    book TEXT,
    odds_at_placement REAL,
    closing_odds REAL,
    stake REAL,
    edge_at_placement REAL,
    timestamp TEXT DEFAULT (datetime('now')),
    status TEXT DEFAULT 'open',
    is_paper INTEGER DEFAULT 0,
    profit REAL,
    clv REAL
);

CREATE TABLE IF NOT EXISTS fixtures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id TEXT UNIQUE,
    home_team TEXT,
    away_team TEXT,
    league TEXT,
    kickoff_time TEXT,
    result TEXT,
    home_goals INTEGER,
    away_goals INTEGER
);

CREATE TABLE IF NOT EXISTS daily_stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT UNIQUE,
    bankroll REAL,
    bets_placed_count INTEGER,
    profit REAL,
    roi REAL,
    avg_clv REAL,
    avg_edge REAL
);

CREATE TABLE IF NOT EXISTS api_quota (
    date TEXT PRIMARY KEY,
    calls INTEGER NOT NULL DEFAULT 0,
    warning_sent INTEGER NOT NULL DEFAULT 0
);

-- Per-league weekly accuracy snapshots so we can trend Brier / win-rate over
-- time, not just the current rolling number.
CREATE TABLE IF NOT EXISTS accuracy_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_date TEXT NOT NULL,
    league TEXT NOT NULL,
    n_settled INTEGER NOT NULL,
    avg_brier REAL,
    win_rate REAL,
    n_clv_samples INTEGER,
    avg_clv REAL,
    UNIQUE(snapshot_date, league)
);

-- Per-league model config. Tunable knobs live here (not in code) so the
-- gamma/blend/anomaly-threshold values can be adjusted without a deploy.
-- Seeded on init_db with the values from the spec; existing rows are left
-- alone so manual edits stick.
CREATE TABLE IF NOT EXISTS league_config (
    league_key TEXT PRIMARY KEY,
    league_id INTEGER,
    league_name TEXT,
    gamma REAL NOT NULL,
    recent_weight REAL NOT NULL,
    season_weight REAL NOT NULL,
    anomaly_edge_threshold REAL NOT NULL,
    sharp_book_divergence_threshold REAL NOT NULL,
    last_updated TEXT DEFAULT (datetime('now'))
);

-- Per-match result logging — populated by the 23:55 self-eval job. One row
-- per finished match where a prediction existed. Drives rolling-accuracy
-- and Brier metrics, plus the bias checks below.
CREATE TABLE IF NOT EXISTS prediction_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id TEXT UNIQUE,
    league_id INTEGER,
    league_key TEXT,
    home_team TEXT,
    away_team TEXT,
    kickoff_time TEXT,
    predicted_outcome TEXT,
    actual_outcome TEXT,
    correct INTEGER,
    predicted_home_prob REAL,
    predicted_draw_prob REAL,
    predicted_away_prob REAL,
    actual_home_goals INTEGER,
    actual_away_goals INTEGER,
    brier_score REAL,
    home_xg_predicted REAL,
    away_xg_predicted REAL,
    home_xg_error REAL,
    away_xg_error REAL,
    league_gamma_used REAL,
    penalties_applied TEXT,        -- JSON list (e.g. '["rest_tired","scorer_out"]')
    season_blend_used REAL,
    anomaly_flagged INTEGER,
    home_team_prior3 TEXT,         -- 'WWL', 'LLL', etc — for form-recency bias check
    away_team_prior3 TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

-- Bias log — one row per detector firing or per "all-clear" run. Read by the
-- dashboard's Model Health panel and the morning digest.
CREATE TABLE IF NOT EXISTS bias_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    check_name TEXT NOT NULL,
    league_id INTEGER,
    league_key TEXT,
    sample_size INTEGER,
    expected_rate REAL,
    actual_rate REAL,
    deviation REAL,
    flagged INTEGER,
    severity TEXT,                 -- 'info' | 'warn' | 'critical'
    suggested_adjustment TEXT,
    description TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

-- Anomaly log — every prediction or EV bet that trips a sanity check (edge
-- too high, sharp-book divergence, penalty stack, form vs season divergence,
-- gamma invariant violation) lands here for audit + the dashboard tab.
CREATE TABLE IF NOT EXISTS anomaly_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id TEXT,
    home_team TEXT,
    away_team TEXT,
    anomaly_type TEXT NOT NULL,
    description TEXT,
    edge_shown REAL,
    model_prob REAL,
    book_implied REAL,
    created_at TEXT DEFAULT (datetime('now')),
    was_bet_placed INTEGER DEFAULT 0,
    -- Dedup key: "{match_id}:{outcome}:{anomaly_type}:{YYYY-MM-DD}". Same
    -- (match × outcome × type × day) only fires once. Without this
    -- MARKET_CONSENSUS_DIVERGENCE was logging once per book per pipeline
    -- run — 84 entries for one match/day. NULL allowed for legacy rows.
    -- The UNIQUE index is created in _backfill_anomaly_dedup after the
    -- legacy-row backfill — creating it in SCHEMA would fail on existing
    -- DBs because CREATE TABLE IF NOT EXISTS won't add the column to a
    -- table that already exists, and the index would reference a missing
    -- column at startup.
    dedup_key TEXT
);

-- Per-book bankroll balances. Seeded from BALANCE_<BOOK> env vars on
-- startup (INSERT OR IGNORE so existing balances persist across restarts).
-- Updated when real-money bets settle: balance += profit. Paper bets do
-- not move real-account balances.
CREATE TABLE IF NOT EXISTS book_balance (
    book_key TEXT PRIMARY KEY,           -- 'fanduel', 'draftkings', 'espnbet', etc.
    display_name TEXT,                   -- 'FanDuel', 'DraftKings', 'ESPN Bet', etc.
    balance_usd REAL NOT NULL DEFAULT 0,
    initial_balance_usd REAL NOT NULL DEFAULT 0,
    updated_at TEXT DEFAULT (datetime('now'))
);

-- Per-team attack / defense ratings, normalized to league average.
-- Powers Fix B (opponent-strength-adjusted xG). Computed nightly from the
-- season-to-date stats we already pull during data_sync. Activated by the
-- OPPONENT_ADJUSTED_XG env flag — table populates regardless so we can
-- backtest before flipping the switch.
-- Manager-change tracking (Addition 3). Nightly job pulls API-Football's
-- coach endpoint per team; on a detected change, records the date and
-- counts post-change games. data_sync.py weights pre-change games at
-- 0.10 and flags LOW confidence when post-change sample < 5.
CREATE TABLE IF NOT EXISTS manager_changes (
    team_id INTEGER PRIMARY KEY,
    team_name TEXT,
    old_manager TEXT,
    new_manager TEXT,
    change_date TEXT,
    games_since_change INTEGER NOT NULL DEFAULT 0,
    last_checked TEXT DEFAULT (datetime('now'))
);

-- Per-market calibration factors (self-calibration spec piece 1).
-- The 00:30 nightly job computes actual_rate / model_avg_pct for each
-- (market, outcome[, line]) bucket and stores the multiplicative
-- correction. Edge calculator multiplies model_prob by the factor at
-- /ev-bets time so cash bets are graded against calibrated model probs.
-- Factors only "apply" when sample_size >= MIN and factor in [0.70, 1.30]
-- — outside that range usually means a data problem, not a calibration need.
CREATE TABLE IF NOT EXISTS market_calibration_factors (
    cal_key TEXT PRIMARY KEY,           -- "btts:yes" / "totals:2.5:over" / "h2h:home"
    market TEXT NOT NULL,
    outcome TEXT NOT NULL,
    market_line REAL,
    model_avg_pct REAL NOT NULL,
    actual_rate REAL NOT NULL,
    calibration_factor REAL NOT NULL,
    sample_size INTEGER NOT NULL,
    applied INTEGER NOT NULL DEFAULT 0,
    last_updated TEXT DEFAULT (datetime('now'))
);

-- One row per +EV bet whose model_prob got multiplicatively adjusted by
-- a factor from market_calibration_factors. Used by the morning digest
-- + admin page to show calibration provenance and to monitor drift.
CREATE TABLE IF NOT EXISTS calibration_applications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bet_id INTEGER,                     -- nullable — fires per-/ev-bets call too
    cal_key TEXT,
    raw_model_prob REAL,
    calibration_factor REAL,
    calibrated_prob REAL,
    edge_before REAL,
    edge_after REAL,
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_cal_app_created ON calibration_applications(created_at);

-- Tactical suppressors (piece 4) — teams whose actual goals conceded
-- consistently come in below xG. Auto-detected weekly from settled
-- matches; a low ratio (≤0.75) suppresses opposing xG_for predictions
-- against this team. A high ratio (≥1.25) flags vulnerability.
CREATE TABLE IF NOT EXISTS tactical_suppressors (
    team_id INTEGER PRIMARY KEY,
    team_name TEXT,
    suppression_factor REAL NOT NULL,   -- actual_xGA / model_xGA — clamped [0.5, 1.5]
    sample_size INTEGER NOT NULL,
    classification TEXT NOT NULL,       -- 'suppressor' | 'vulnerable' | 'neutral'
    last_updated TEXT DEFAULT (datetime('now'))
);

-- CLV feedback state (piece 5) — drives whether the morning digest fires
-- at 06:00 (early-bet timing) or 08:00 (default). Single-row table,
-- updated by the nightly CLV-rolling job.
CREATE TABLE IF NOT EXISTS clv_feedback_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),  -- always row 1
    rolling_clv_avg REAL,
    sample_size INTEGER,
    digest_send_hour INTEGER NOT NULL DEFAULT 8,  -- 6 or 8
    timing_changed_at TEXT,
    consecutive_negative_at_6am INTEGER NOT NULL DEFAULT 0,
    last_updated TEXT DEFAULT (datetime('now'))
);
INSERT OR IGNORE INTO clv_feedback_state (id, digest_send_hour) VALUES (1, 8);

-- Spec section 2 — nightly automation pipeline.
-- One row per task per run. Status = 'PASS' / 'FAIL' / 'DEFERRED' / 'SKIP'.
-- Three consecutive FAIL rows for the same task_name trigger the 6am
-- urgent alert email (handled by automation_runner.consecutive_failures).
CREATE TABLE IF NOT EXISTS automation_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_date TEXT NOT NULL,             -- 'YYYY-MM-DD' UTC
    task_name TEXT NOT NULL,
    status TEXT NOT NULL,
    result_summary TEXT,
    error_message TEXT,
    duration_seconds REAL,
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_automation_log_run ON automation_log(run_date, task_name);
CREATE INDEX IF NOT EXISTS idx_automation_log_task_created ON automation_log(task_name, created_at);

-- Spec 2.8 — 29-item World Cup readiness checklist. Each row is one
-- criterion with a current status. The readiness-score task rolls the
-- whole table up to a percentage (completed/29 * 100) and into the
-- daily morning report.
CREATE TABLE IF NOT EXISTS wc_readiness_checklist (
    item_id TEXT PRIMARY KEY,           -- '1.1', '2.3', etc.
    category TEXT NOT NULL,             -- 'model' | 'system' | 'features' | 'real_money' | 'wc_data' | 'wc_config' | 'fix_b'
    label TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',  -- 'pending' | 'pass' | 'fail' | 'manual'
    priority TEXT NOT NULL DEFAULT 'normal', -- 'normal' | 'low' (low = July 20 target, hidden from June digest)
    target_date TEXT,                    -- ISO date, used by digest gate
    manual_required INTEGER NOT NULL DEFAULT 0,
    notes TEXT,
    last_checked TEXT
);

-- Real-trade audit (spec 1.7) — for each cash bet, capture whether a
-- paper counterpart existed, how the executed odds and stake compared
-- to that counterpart, and an overall "execution quality" score.
-- Refreshed nightly via a scheduler job so the morning digest + admin
-- page reflect today's audit.
CREATE TABLE IF NOT EXISTS real_trade_audit (
    bet_id INTEGER PRIMARY KEY,         -- 1:1 with bets_placed.id
    paper_bet_id INTEGER,               -- matching paper bet, if found
    odds_diff REAL,                     -- real_odds - paper_odds
    stake_diff REAL,                    -- real_stake - paper_kelly_stake
    odds_flag INTEGER NOT NULL DEFAULT 0,   -- 1 if |odds_diff| > 0.10
    stake_flag INTEGER NOT NULL DEFAULT 0,  -- 1 if |stake_diff| > 5
    no_paper_flag INTEGER NOT NULL DEFAULT 0,
    quality TEXT,                       -- 'green' / 'amber' / 'red'
    notes TEXT,
    last_audited TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS team_ratings (
    team_id INTEGER NOT NULL,
    league_id INTEGER NOT NULL,
    season INTEGER NOT NULL,
    team_name TEXT,
    attack_rating REAL,    -- ~1.0 = league average; >1 better attack
    defense_rating REAL,   -- ~1.0 = league average; >1 better defense
    overall_rating REAL,
    games_played INTEGER,
    last_updated TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (team_id, league_id, season)
);

-- Cron job execution log — APScheduler EVENT_JOB_EXECUTED / EVENT_JOB_ERROR
-- listener writes one row per job run. End-of-day status email reads from
-- this to summarize the day's pass/fail.
CREATE TABLE IF NOT EXISTS cron_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    finished_at TEXT NOT NULL DEFAULT (datetime('now')),
    success INTEGER NOT NULL,
    error_msg TEXT,
    duration_ms INTEGER
);

-- Match analysis — Claude Haiku 4.5 generated narratives, cached 30 minutes.
-- Daily-budget enforcement counts rows in this table by date(created_at).
CREATE TABLE IF NOT EXISTS match_analysis (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id TEXT NOT NULL,
    analysis_text TEXT NOT NULL,
    anomalies_found INTEGER DEFAULT 0,
    critical_flags INTEGER DEFAULT 0,
    claude_model_used TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cost_usd REAL,
    cache_expires_at TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_bets_match ON bets_placed(match_id);
CREATE INDEX IF NOT EXISTS idx_predictions_match ON model_predictions(match_id);
CREATE INDEX IF NOT EXISTS idx_anomaly_match ON anomaly_log(match_id);
CREATE INDEX IF NOT EXISTS idx_anomaly_created ON anomaly_log(created_at);
CREATE INDEX IF NOT EXISTS idx_pred_results_league ON prediction_results(league_key, kickoff_time);
CREATE INDEX IF NOT EXISTS idx_pred_results_team ON prediction_results(home_team, away_team);
CREATE INDEX IF NOT EXISTS idx_bias_log_created ON bias_log(created_at);
CREATE INDEX IF NOT EXISTS idx_match_analysis_match ON match_analysis(match_id, cache_expires_at);
CREATE INDEX IF NOT EXISTS idx_match_analysis_created ON match_analysis(created_at);
CREATE INDEX IF NOT EXISTS idx_cron_log_finished ON cron_log(finished_at);
"""


def _predictions_has_unique_match_id(conn: sqlite3.Connection) -> bool:
    """True iff model_predictions has a UNIQUE index on (match_id) alone."""
    indexes = conn.execute("PRAGMA index_list('model_predictions')").fetchall()
    for _, idx_name, is_unique, *_ in indexes:
        if not is_unique:
            continue
        cols = conn.execute(f"PRAGMA index_info('{idx_name}')").fetchall()
        if [c[2] for c in cols] == ["match_id"]:
            return True
    return False


def _migrate_predictions_unique_match_id(conn: sqlite3.Connection) -> int:
    """Drop duplicate predictions (keep newest per match_id) and rebuild the table
    with UNIQUE(match_id). Returns rows kept."""
    conn.execute(
        """
        DELETE FROM model_predictions WHERE id NOT IN (
            SELECT MAX(id) FROM model_predictions GROUP BY match_id
        )
        """
    )
    conn.execute("ALTER TABLE model_predictions RENAME TO _model_predictions_old")
    conn.executescript(
        """
        CREATE TABLE model_predictions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            match_id TEXT UNIQUE,
            home_team TEXT,
            away_team TEXT,
            league TEXT,
            kickoff_time TEXT,
            home_win_pct REAL,
            draw_pct REAL,
            away_win_pct REAL,
            home_xg REAL,
            away_xg REAL,
            confidence TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_predictions_match ON model_predictions(match_id);
        """
    )
    conn.execute(
        """
        INSERT INTO model_predictions
          (id, match_id, home_team, away_team, league, kickoff_time,
           home_win_pct, draw_pct, away_win_pct, home_xg, away_xg, confidence, created_at)
        SELECT id, match_id, home_team, away_team, league, kickoff_time,
               home_win_pct, draw_pct, away_win_pct, home_xg, away_xg, confidence, created_at
        FROM _model_predictions_old
        """
    )
    cur = conn.execute("SELECT COUNT(*) FROM model_predictions")
    kept = cur.fetchone()[0]
    conn.execute("DROP TABLE _model_predictions_old")
    return kept


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info('{table}')").fetchall()
    return any(r[1] == column for r in rows)


def _add_column_if_missing(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    if not _column_exists(conn, table, column):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def _backfill_anomaly_dedup(conn: sqlite3.Connection) -> None:
    """One-time pass: backfill `dedup_key` on existing anomaly_log rows, then
    delete duplicates keeping the lowest id per key. The UNIQUE index is in
    SCHEMA so creating it before this runs would fail on the existing dupes.
    Safe to re-run — the WHERE clause skips already-keyed rows.
    """
    # Backfill keys for legacy rows. We don't have outcome stored, so derive
    # a stable discriminator from (model_prob, book_implied) — same logic as
    # _dedup_key in anomaly.py for legacy rows.
    conn.execute(
        """
        UPDATE anomaly_log
           SET dedup_key = match_id || ':' ||
                           'm' || CAST(ROUND(COALESCE(model_prob,0)*1000) AS INT) || '-' ||
                           'b' || CAST(ROUND(COALESCE(book_implied,0)*1000) AS INT) || ':' ||
                           anomaly_type || ':' ||
                           DATE(created_at)
         WHERE dedup_key IS NULL
        """
    )
    # De-dupe: keep min(id) per key.
    conn.execute(
        """
        DELETE FROM anomaly_log
         WHERE id NOT IN (
             SELECT MIN(id) FROM anomaly_log
             WHERE dedup_key IS NOT NULL
             GROUP BY dedup_key
         )
        """
    )
    # Full (non-partial) UNIQUE index. SQLite's UPSERT (ON CONFLICT(dedup_key)
    # DO UPDATE) requires a real UNIQUE constraint or non-partial unique
    # index — partial indexes are silently ignored as conflict targets and
    # produce "ON CONFLICT clause does not match any PRIMARY KEY or UNIQUE
    # constraint". SQLite considers NULLs distinct from each other inside
    # UNIQUE indexes, so any backfilled rows that ended up with NULL won't
    # collide.
    conn.execute("DROP INDEX IF EXISTS idx_anomaly_dedup")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_anomaly_dedup ON anomaly_log(dedup_key)"
    )


def init_db(path: str | None = None) -> None:
    p = path or DB_PATH
    with sqlite3.connect(p) as conn:
        conn.executescript(SCHEMA)
        if not _predictions_has_unique_match_id(conn):
            _migrate_predictions_unique_match_id(conn)
        # Anomaly dedup: add column + UNIQUE index, backfill, drop dupes.
        _add_column_if_missing(conn, "anomaly_log", "dedup_key", "TEXT")
        _backfill_anomaly_dedup(conn)
        # Trend / form-breakpoint columns (Additions 1 + 2).
        for col, decl in (
            ("home_attack_trend",         "REAL"),
            ("home_defense_trend",        "REAL"),
            ("away_attack_trend",         "REAL"),
            ("away_defense_trend",        "REAL"),
            ("trend_adjustment_applied",  "INTEGER DEFAULT 0"),
            ("form_breakpoint_detected",  "INTEGER DEFAULT 0"),
            ("form_breakpoint_team",      "TEXT"),
            ("breakpoint_ratio",          "REAL"),
            ("blend_overridden",          "INTEGER DEFAULT 0"),
            ("blend_used",                "TEXT"),
        ):
            _add_column_if_missing(conn, "model_predictions", col, decl)
        # Additive migrations for derived markets
        _add_column_if_missing(conn, "model_predictions", "btts_yes_pct", "REAL")
        _add_column_if_missing(conn, "model_predictions", "score_matrix_json", "TEXT")
        # Track which market/line each paper bet came from so we can dedupe in the UI
        _add_column_if_missing(conn, "bets_placed", "market", "TEXT")
        _add_column_if_missing(conn, "bets_placed", "market_line", "REAL")
        # WC phase tracking — used by the nightly WC cron to detect transitions
        _add_column_if_missing(conn, "accuracy_snapshots", "phase", "TEXT")
        # Self-eval — capture the per-league knobs used at prediction time so
        # the 23:55 result logger can backfill prediction_results rows.
        _add_column_if_missing(conn, "model_predictions", "penalties_json", "TEXT")
        _add_column_if_missing(conn, "model_predictions", "gamma_used", "REAL")
        _add_column_if_missing(conn, "model_predictions", "season_blend_used", "REAL")
        _add_column_if_missing(conn, "model_predictions", "anomaly_flagged", "INTEGER DEFAULT 0")
        # Per-team form snapshot — JSON arrays of last-N xG values + the
        # weighted attack/defense numbers fed to predict(). Captured at
        # prediction time so AI analysis + bias detection don't need to
        # re-fetch from API-Football. NULL on rows from before this migration.
        _add_column_if_missing(conn, "model_predictions", "home_games_xg_for", "TEXT")
        _add_column_if_missing(conn, "model_predictions", "home_games_xg_against", "TEXT")
        _add_column_if_missing(conn, "model_predictions", "away_games_xg_for", "TEXT")
        _add_column_if_missing(conn, "model_predictions", "away_games_xg_against", "TEXT")
        _add_column_if_missing(conn, "model_predictions", "home_attack_weighted", "REAL")
        _add_column_if_missing(conn, "model_predictions", "home_defense_weighted", "REAL")
        _add_column_if_missing(conn, "model_predictions", "away_attack_weighted", "REAL")
        _add_column_if_missing(conn, "model_predictions", "away_defense_weighted", "REAL")
        _add_column_if_missing(conn, "model_predictions", "home_rest_days", "INTEGER")
        _add_column_if_missing(conn, "model_predictions", "away_rest_days", "INTEGER")
        _add_column_if_missing(conn, "model_predictions", "home_penalties_applied", "TEXT")
        _add_column_if_missing(conn, "model_predictions", "away_penalties_applied", "TEXT")
        _add_column_if_missing(conn, "model_predictions", "home_season_avg_for", "REAL")
        _add_column_if_missing(conn, "model_predictions", "home_season_avg_against", "REAL")
        _add_column_if_missing(conn, "model_predictions", "away_season_avg_for", "REAL")
        _add_column_if_missing(conn, "model_predictions", "away_season_avg_against", "REAL")
        # Seed league_config with defaults if rows are missing. Existing rows
        # are left alone so manual edits via the table aren't clobbered.
        _seed_league_config(conn)
        conn.commit()


# (league_key, league_id, league_name, gamma, recent_weight, season_weight,
#  anomaly_edge_threshold, sharp_book_divergence_threshold)
LEAGUE_CONFIG_SEED: list[tuple] = [
    ("epl",       39, "Premier League",       1.30, 0.60, 0.40, 0.15, 0.20),
    ("ucl",        2, "UEFA Champions League", 1.25, 0.50, 0.50, 0.12, 0.15),
    ("uel",        3, "UEFA Europa League",    1.28, 0.60, 0.40, 0.15, 0.20),
    ("world_cup",  1, "FIFA World Cup",        1.20, 0.70, 0.30, 0.10, 0.15),
    ("la_liga",  140, "La Liga",               1.30, 0.60, 0.40, 0.15, 0.20),
]


def _seed_league_config(conn: sqlite3.Connection) -> None:
    for row in LEAGUE_CONFIG_SEED:
        conn.execute(
            """
            INSERT INTO league_config
              (league_key, league_id, league_name, gamma,
               recent_weight, season_weight,
               anomaly_edge_threshold, sharp_book_divergence_threshold)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(league_key) DO NOTHING
            """,
            row,
        )


@contextmanager
def db(path: str | None = None):
    p = path or DB_PATH
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()
