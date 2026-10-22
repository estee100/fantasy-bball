import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS players (
    nba_id INTEGER PRIMARY KEY, name TEXT, norm_name TEXT, team TEXT,
    yahoo_key TEXT, positions TEXT, yahoo_status TEXT, updated TEXT
);
CREATE TABLE IF NOT EXISTS game_logs (
    nba_id INTEGER, game_id TEXT, game_date TEXT, season TEXT, team TEXT, matchup TEXT,
    min REAL, fgm REAL, fga REAL, fg3m REAL, fg3a REAL, ftm REAL, fta REAL,
    oreb REAL, dreb REAL, reb REAL, ast REAL, stl REAL, blk REAL, tov REAL, pts REAL,
    dd REAL, td REAL,
    PRIMARY KEY (nba_id, game_id)
);
CREATE INDEX IF NOT EXISTS ix_logs_date ON game_logs(game_date);
CREATE TABLE IF NOT EXISTS schedule (
    game_id TEXT PRIMARY KEY, game_date TEXT, home TEXT, away TEXT
);
CREATE TABLE IF NOT EXISTS fantasy_teams (
    team_key TEXT PRIMARY KEY, name TEXT, manager TEXT, is_mine INTEGER,
    wins INTEGER, losses INTEGER, ties INTEGER, rank INTEGER
);
CREATE TABLE IF NOT EXISTS rosters (
    team_key TEXT, nba_id INTEGER, slot TEXT, PRIMARY KEY (team_key, nba_id)
);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS injuries (
    nba_id INTEGER PRIMARY KEY, status TEXT, comment TEXT, date TEXT
);
CREATE TABLE IF NOT EXISTS news (
    id TEXT PRIMARY KEY, source TEXT, title TEXT, summary TEXT, url TEXT,
    published TEXT, fetched TEXT, player_ids TEXT, extracted TEXT, processed INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_news_pub ON news(published);
CREATE TABLE IF NOT EXISTS news_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, news_id TEXT, nba_id INTEGER, date TEXT,
    event_type TEXT, direction INTEGER, minutes_multiplier REAL, games_out INTEGER,
    confidence REAL, takeaway TEXT
);
CREATE TABLE IF NOT EXISTS scouting (
    id INTEGER PRIMARY KEY AUTOINCREMENT, nba_id INTEGER, source TEXT, created TEXT,
    strengths TEXT, weaknesses TEXT, role TEXT, fantasy_notes TEXT, raw_hash TEXT UNIQUE
);
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, created TEXT, nba_id INTEGER, kind TEXT,
    severity REAL, title TEXT, detail TEXT, active INTEGER DEFAULT 1,
    UNIQUE (nba_id, kind)
);
CREATE TABLE IF NOT EXISTS moves (
    id INTEGER PRIMARY KEY AUTOINCREMENT, created TEXT, week INTEGER,
    add_id INTEGER, drop_id INTEGER, pred_week_dwin REAL, pred_season_dwins REAL,
    confidence REAL, raw_confidence REAL, rationale TEXT,
    status TEXT,              -- suggested | executed | dismissed | manual
    kind TEXT,                -- stream (judged on rest of week) | upgrade (judged over 21 days)
    horizon_end TEXT,
    executed_at TEXT, eval TEXT, evaluated_at TEXT,
    UNIQUE (add_id, drop_id, week)
);
CREATE TABLE IF NOT EXISTS matchup_predictions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, created TEXT, week INTEGER, team_key TEXT,
    opp_key TEXT, win_prob REAL, cat_probs TEXT, kind TEXT,   -- kind: opening | latest
    result REAL, resolved_at TEXT,
    UNIQUE (week, team_key, kind)
);
CREATE TABLE IF NOT EXISTS watchlist (
    id INTEGER PRIMARY KEY AUTOINCREMENT, created TEXT, nba_id INTEGER, compare_id INTEGER,
    note TEXT, snapshot TEXT, active INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS backtests (
    id INTEGER PRIMARY KEY AUTOINCREMENT, created TEXT, params TEXT, model_err REAL,
    baseline_err REAL, n INTEGER
);
"""

_lock = threading.RLock()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


_conn = None


@contextmanager
def cursor():
    """Single shared connection guarded by a lock; the scheduler and API threads both write."""
    global _conn
    with _lock:
        if _conn is None:
            _conn = connect()
            _conn.executescript(SCHEMA)
            _migrate(_conn)
        try:
            yield _conn
            _conn.commit()
        except Exception:
            _conn.rollback()
            raise


def _migrate(conn):
    pcols = {r[1] for r in conn.execute("PRAGMA table_info(players)")}
    if "espn_pos" not in pcols:
        conn.execute("ALTER TABLE players ADD COLUMN espn_pos TEXT")
    cols = {r[1] for r in conn.execute("PRAGMA table_info(moves)")}
    for col in ("kind", "horizon_end", "urgency"):
        if col not in cols:
            conn.execute(f"ALTER TABLE moves ADD COLUMN {col} TEXT")


def kv_get(key, default=None):
    with cursor() as c:
        row = c.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return json.loads(row["value"]) if row else default


def kv_set(key, value):
    with cursor() as c:
        c.execute("INSERT OR REPLACE INTO kv(key, value) VALUES (?, ?)", (key, json.dumps(value, default=_jsonable)))


def _jsonable(o):
    # numpy scalars (player ids from pandas indexes, float64s) aren't JSON-serializable
    if hasattr(o, "item"):
        return o.item()
    if hasattr(o, "tolist"):
        return o.tolist()
    raise TypeError(f"not JSON serializable: {type(o).__name__}")


def get_params() -> dict:
    params = dict(config.DEFAULT_PARAMS)
    params.update(kv_get("model_params", {}))
    return params
