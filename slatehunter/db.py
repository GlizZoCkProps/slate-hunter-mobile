"""SQLite schema and connection helpers.

Design rules:
- prop_snapshots, injury_reports, market_signals and projections are append-only.
  Nothing in the collector UPDATEs or DELETEs them.
- Every collector job writes a collection_runs row; every failure or missing
  piece of data writes a collection_errors row (kind = 'error' | 'missing').
- Timestamps are stored as ISO-8601 UTC ('...Z') plus a Central Time copy for
  human reading. Compare/sort on the UTC column.
"""
import os
import sqlite3

SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS collection_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job TEXT NOT NULL,                    -- collect_games | collect_props | grade | ...
    started_at_utc TEXT NOT NULL,
    started_at_ct TEXT NOT NULL,
    finished_at_utc TEXT,
    status TEXT NOT NULL DEFAULT 'running', -- running | ok | partial | failed | skipped
    items_written INTEGER NOT NULL DEFAULT 0,
    error_count INTEGER NOT NULL DEFAULT 0,
    message TEXT,
    quota_remaining INTEGER,              -- The Odds API x-requests-remaining after run
    quota_used INTEGER,                   -- The Odds API x-requests-used after run
    code_version TEXT
);
CREATE INDEX IF NOT EXISTS ix_runs_job_time ON collection_runs(job, started_at_utc);

CREATE TABLE IF NOT EXISTS collection_errors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER REFERENCES collection_runs(id),
    at_utc TEXT NOT NULL,
    kind TEXT NOT NULL,                   -- error | missing
    source TEXT NOT NULL,
    scope TEXT,                           -- e.g. 'NBA 2026-10-24' or odds event id
    error_type TEXT,
    message TEXT
);
CREATE INDEX IF NOT EXISTS ix_errors_run ON collection_errors(run_id);

CREATE TABLE IF NOT EXISTS games (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sport TEXT NOT NULL,
    espn_event_id TEXT NOT NULL,
    name TEXT,
    home_team TEXT,
    away_team TEXT,
    start_time_utc TEXT,
    start_time_ct TEXT,
    state TEXT,                           -- pre | in | post
    status_detail TEXT,
    completed INTEGER NOT NULL DEFAULT 0,
    home_score TEXT,
    away_score TEXT,
    odds_event_id TEXT,                   -- The Odds API event id once matched
    source TEXT NOT NULL,
    source_url TEXT,
    first_seen_utc TEXT NOT NULL,
    last_updated_utc TEXT NOT NULL,
    UNIQUE (sport, espn_event_id)
);
CREATE INDEX IF NOT EXISTS ix_games_start ON games(start_time_utc);

-- One row per side per book per line per collection run. Never updated.
CREATE TABLE IF NOT EXISTS prop_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES collection_runs(id),
    collected_at_utc TEXT NOT NULL,
    collected_at_ct TEXT NOT NULL,
    source TEXT NOT NULL,                 -- 'the_odds_api' | 'manual' | ...
    book_key TEXT NOT NULL,
    book_title TEXT,
    region TEXT,
    sport TEXT NOT NULL,
    game_id INTEGER REFERENCES games(id), -- NULL when no ESPN game matched (logged)
    odds_event_id TEXT,
    commence_time_utc TEXT,
    market TEXT NOT NULL,                 -- normalized: points, rebounds, passing_yards ...
    source_market_key TEXT,               -- provider key, e.g. player_points
    player_name TEXT NOT NULL,
    player_key TEXT NOT NULL,             -- normalized name for matching
    side TEXT NOT NULL,                   -- over | under | yes | no
    line REAL,
    price_american INTEGER,               -- NULL for pick'em sources with no price
    book_last_update_utc TEXT,
    UNIQUE (run_id, book_key, market, player_key, side, line)
);
CREATE INDEX IF NOT EXISTS ix_snap_lookup ON prop_snapshots(game_id, market, player_key, book_key, side, collected_at_utc);

CREATE TABLE IF NOT EXISTS injury_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER REFERENCES collection_runs(id),
    collected_at_utc TEXT NOT NULL,
    sport TEXT NOT NULL,
    game_id INTEGER REFERENCES games(id),
    team TEXT,
    player_name TEXT NOT NULL,
    player_key TEXT NOT NULL,
    status TEXT,
    detail TEXT,
    source TEXT NOT NULL,
    source_url TEXT
);
CREATE INDEX IF NOT EXISTS ix_inj_game ON injury_reports(game_id, player_key, collected_at_utc);

CREATE TABLE IF NOT EXISTS projections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at_utc TEXT NOT NULL,
    created_at_ct TEXT NOT NULL,
    model_version TEXT NOT NULL,
    sport TEXT NOT NULL,
    game_id INTEGER REFERENCES games(id),
    espn_athlete_id TEXT,
    player_name TEXT NOT NULL,
    player_key TEXT NOT NULL,
    market TEXT NOT NULL,
    projection REAL,
    est_prob_over REAL,                   -- NULL unless a calibrated model produced it
    reference_line REAL,
    reference_snapshot_id INTEGER REFERENCES prop_snapshots(id),
    sample_size INTEGER,
    inputs_json TEXT,                     -- inputs used, for reproducibility
    source TEXT
);
CREATE INDEX IF NOT EXISTS ix_proj_lookup ON projections(game_id, player_key, market, created_at_utc);

-- Line/price moves derived from snapshots, and externally reported betting splits.
-- evidence_level makes the distinction explicit:
--   observed_line_change : two of our own snapshots differ (fact about the line only)
--   reported_split        : ticket/handle % from a cited source (source_url required)
--   unsupported_claim     : someone said "sharp money" without data; kept for audit only
CREATE TABLE IF NOT EXISTS market_signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    observed_at_utc TEXT NOT NULL,
    sport TEXT NOT NULL,
    game_id INTEGER REFERENCES games(id),
    player_key TEXT,
    player_name TEXT,
    market TEXT,
    book_key TEXT,
    side TEXT,
    signal_type TEXT NOT NULL,            -- line_move | price_move | ticket_pct | handle_pct | role_change | note
    value_before REAL,
    value_after REAL,
    from_snapshot_id INTEGER REFERENCES prop_snapshots(id),
    to_snapshot_id INTEGER REFERENCES prop_snapshots(id),
    evidence_level TEXT NOT NULL,
    source TEXT NOT NULL,
    source_url TEXT,
    notes TEXT,
    UNIQUE (to_snapshot_id, signal_type)
);

-- Actual stat outcomes from official/public box scores.
CREATE TABLE IF NOT EXISTS results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id INTEGER NOT NULL REFERENCES games(id),
    sport TEXT NOT NULL,
    espn_athlete_id TEXT,
    player_name TEXT NOT NULL,
    player_key TEXT NOT NULL,
    team TEXT,
    market TEXT NOT NULL,
    actual_value REAL,
    did_not_play INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL,
    collected_at_utc TEXT NOT NULL,
    UNIQUE (game_id, player_key, market)
);

-- One grade per tracked prop (game, book, market, player, side), bet or not.
CREATE TABLE IF NOT EXISTS prop_grades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id INTEGER NOT NULL REFERENCES games(id),
    book_key TEXT NOT NULL,
    market TEXT NOT NULL,
    player_key TEXT NOT NULL,
    player_name TEXT,
    side TEXT NOT NULL,
    open_snapshot_id INTEGER REFERENCES prop_snapshots(id),
    close_snapshot_id INTEGER REFERENCES prop_snapshots(id),
    open_line REAL,
    close_line REAL,
    open_price INTEGER,
    close_price INTEGER,
    actual_value REAL,
    outcome TEXT NOT NULL,                -- win | loss | push | void | ungraded
    outcome_reason TEXT,
    graded_at_utc TEXT NOT NULL,
    UNIQUE (game_id, book_key, market, player_key, side)
);

-- One row per paid Odds API event-odds call (credit accounting; added in schema v2).
CREATE TABLE IF NOT EXISTS odds_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER REFERENCES collection_runs(id),
    called_at_utc TEXT NOT NULL,
    called_date_ct TEXT NOT NULL,         -- YYYY-MM-DD in Central Time, for daily budgets
    sport TEXT NOT NULL,
    odds_event_id TEXT NOT NULL,
    commence_time_utc TEXT,
    estimated_cost INTEGER,
    reported_cost INTEGER,                -- x-requests-last header when provided
    rows_returned INTEGER,
    ok INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_odds_calls_event ON odds_calls(odds_event_id);
CREATE INDEX IF NOT EXISTS ix_odds_calls_day ON odds_calls(called_date_ct);

-- Cache of provider player name -> ESPN athlete id resolutions.
CREATE TABLE IF NOT EXISTS player_ids (
    sport TEXT NOT NULL,
    player_key TEXT NOT NULL,
    espn_athlete_id TEXT,                 -- NULL = could not resolve (see note)
    name TEXT,
    team TEXT,
    resolved_at_utc TEXT NOT NULL,
    source TEXT NOT NULL,
    note TEXT,
    PRIMARY KEY (sport, player_key)
);

CREATE TABLE IF NOT EXISTS paper_picks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at_utc TEXT NOT NULL,
    created_at_ct TEXT NOT NULL,
    snapshot_id INTEGER NOT NULL REFERENCES prop_snapshots(id), -- exact line/price available
    side TEXT NOT NULL,
    line REAL,
    price_american INTEGER,
    stake_units REAL NOT NULL DEFAULT 1.0,
    projection_id INTEGER REFERENCES projections(id),
    model_version TEXT,
    rationale TEXT,
    status TEXT NOT NULL DEFAULT 'open',  -- open | graded
    outcome TEXT,                         -- win | loss | push | void
    profit_units REAL,
    close_snapshot_id INTEGER REFERENCES prop_snapshots(id),
    clv_line REAL,                        -- + means our line was better than close
    clv_prob REAL,                        -- close no-vig prob minus entry no-vig prob (same side)
    graded_at_utc TEXT
);
"""


def connect(path):
    if path != ":memory:":
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if path != ":memory:":
        # DELETE journal (not WAL) so the single .db file is always self-contained
        # when it is copied/committed by the scheduled host.
        conn.execute("PRAGMA journal_mode = DELETE")
    return conn


def init_db(conn):
    conn.executescript(SCHEMA)
    conn.execute("INSERT OR REPLACE INTO schema_meta(key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
    conn.commit()
    return conn


def open_db(path):
    return init_db(connect(path))


def integrity_ok(conn):
    row = conn.execute("PRAGMA integrity_check").fetchone()
    return row is not None and row[0] == "ok"
