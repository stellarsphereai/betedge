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
    was_bet_placed INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_bets_match ON bets_placed(match_id);
CREATE INDEX IF NOT EXISTS idx_predictions_match ON model_predictions(match_id);
CREATE INDEX IF NOT EXISTS idx_anomaly_match ON anomaly_log(match_id);
CREATE INDEX IF NOT EXISTS idx_anomaly_created ON anomaly_log(created_at);
CREATE INDEX IF NOT EXISTS idx_pred_results_league ON prediction_results(league_key, kickoff_time);
CREATE INDEX IF NOT EXISTS idx_pred_results_team ON prediction_results(home_team, away_team);
CREATE INDEX IF NOT EXISTS idx_bias_log_created ON bias_log(created_at);
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


def init_db(path: str | None = None) -> None:
    p = path or DB_PATH
    with sqlite3.connect(p) as conn:
        conn.executescript(SCHEMA)
        if not _predictions_has_unique_match_id(conn):
            _migrate_predictions_unique_match_id(conn)
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
