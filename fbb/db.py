"""SQLite storage, split in two:

  data/fbb.db            shared by every league: NBA data, news, injuries, scouting
  data/leagues/<id>.db   one per league: teams, rosters, moves, forecasts, settings, tuning

Every connection opens fbb.db as "main" and attaches one league file as "lg". Queries use
unqualified table names: SQLite looks in main first, then in attached databases, so a league
table resolves to the league file as long as main has no table of that name (connect()
guarantees it). Which league a connection serves comes from use_league(), a context variable,
so API requests for different leagues and the scheduler's per-league loop never mix.
"""
import contextvars
import json
import re
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

from . import config

SHARED_SCHEMA = """
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
CREATE TABLE IF NOT EXISTS shared_kv (key TEXT PRIMARY KEY, value TEXT);
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
"""

# Everything that depends on a league's teams or scoring. Tuned model parameters live here
# too: tuning optimises the league's own categories, and takes are stored in its value units.
LEAGUE_SCHEMA = """
CREATE TABLE IF NOT EXISTS {p}kv (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS {p}fantasy_teams (
    team_key TEXT PRIMARY KEY, name TEXT, manager TEXT, is_mine INTEGER,
    wins INTEGER, losses INTEGER, ties INTEGER, rank INTEGER
);
CREATE TABLE IF NOT EXISTS {p}rosters (
    team_key TEXT, nba_id INTEGER, slot TEXT, PRIMARY KEY (team_key, nba_id)
);
CREATE TABLE IF NOT EXISTS {p}alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, created TEXT, nba_id INTEGER, kind TEXT,
    severity REAL, title TEXT, detail TEXT, active INTEGER DEFAULT 1,
    UNIQUE (nba_id, kind)
);
CREATE TABLE IF NOT EXISTS {p}moves (
    id INTEGER PRIMARY KEY AUTOINCREMENT, created TEXT, week INTEGER,
    add_id INTEGER, drop_id INTEGER, pred_week_dwin REAL, pred_season_dwins REAL,
    confidence REAL, raw_confidence REAL, rationale TEXT,
    status TEXT,              -- suggested | executed | dismissed | manual
    executed_at TEXT, eval TEXT, evaluated_at TEXT,
    UNIQUE (add_id, drop_id, week)
);
CREATE TABLE IF NOT EXISTS {p}matchup_predictions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, created TEXT, week INTEGER, team_key TEXT,
    opp_key TEXT, win_prob REAL, cat_probs TEXT, kind TEXT,   -- kind: opening | latest
    result REAL, resolved_at TEXT,
    UNIQUE (week, team_key, kind)
);
CREATE TABLE IF NOT EXISTS {p}watchlist (
    id INTEGER PRIMARY KEY AUTOINCREMENT, created TEXT, nba_id INTEGER, compare_id INTEGER,
    note TEXT, snapshot TEXT, active INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS {p}lineup_days (     -- a team's lineup as set on a given day
    team_key TEXT, day TEXT, nba_id INTEGER, slot TEXT, PRIMARY KEY (team_key, day, nba_id)
);
CREATE TABLE IF NOT EXISTS {p}league_moves (    -- every add/drop with its time, any team
    team_key TEXT, ts TEXT, nba_id INTEGER, kind TEXT, source TEXT,
    PRIMARY KEY (team_key, ts, nba_id, kind)
);
CREATE TABLE IF NOT EXISTS {p}backtests (
    id INTEGER PRIMARY KEY AUTOINCREMENT, created TEXT, params TEXT, model_err REAL,
    baseline_err REAL, n INTEGER
);
CREATE TABLE IF NOT EXISTS {p}transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, created TEXT, effective TEXT, kind TEXT,
    team_key TEXT, other_key TEXT, players_in TEXT, players_out TEXT,
    source TEXT, note TEXT, move_id INTEGER, yahoo_key TEXT UNIQUE,
    prediction TEXT, eval TEXT, evaluated_at TEXT, deleted INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS {p}overrides (
    id INTEGER PRIMARY KEY AUTOINCREMENT, created TEXT, since TEXT, nba_id INTEGER, kind TEXT,
    value REAL, note TEXT, active INTEGER DEFAULT 1, cleared TEXT,
    model_min REAL, model_per_game REAL, your_per_game REAL
);
CREATE TABLE IF NOT EXISTS {p}shadow_days (
    day TEXT PRIMARY KEY, starters TEXT, opp_key TEXT, points REAL, scored INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS {p}shadow_moves (
    id INTEGER PRIMARY KEY AUTOINCREMENT, created TEXT, day TEXT, kind TEXT,
    add_id INTEGER, drop_id INTEGER, status TEXT, until TEXT, score REAL, reason TEXT
);
"""
LEAGUE_TABLES = re.findall(r"\{p\}(\w+) \(", LEAGUE_SCHEMA)

# Settings that aren't about any one league: one Claude key, one injury feed, one phone.
SHARED_KV = {"claude_error", "injuries_fetched", "shared_daily_done", "shared_status", "alerts_cfg"}

_lock = threading.RLock()
_conns: dict = {}
_league = contextvars.ContextVar("fbb_league", default=None)
_ID_OK = re.compile(r"^[A-Za-z0-9_-]{1,40}$")   # it becomes a file name


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------- which league

def league_ids() -> list:
    """Configured leagues in .env order. Without Yahoo credentials: the demo league."""
    return list(config.YAHOO_LEAGUE_IDS) if config.yahoo_configured() else ["demo"]


def current_league() -> str:
    return _league.get() or league_ids()[0]


@contextmanager
def use_league(lid):
    lid = str(lid)
    if not _ID_OK.match(lid):
        raise ValueError(f"bad league id: {lid!r}")
    token = _league.set(lid)
    try:
        yield lid
    finally:
        _league.reset(token)


def bound(fn):
    """fn pinned to the current league, for handing to a new thread (which starts without
    this context and would otherwise fall back to the first league)."""
    lid = current_league()

    def run(*a, **kw):
        with use_league(lid):
            return fn(*a, **kw)
    return run


def leagues_dir():
    return config.DB_PATH.parent / "leagues"


def league_path(lid):
    return leagues_dir() / f"{lid}.db"


# ---------------------------------------------------------------- connections

def connect(lid=None) -> sqlite3.Connection:
    lid = lid or current_league()
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SHARED_SCHEMA)
    _migrate_shared(conn)
    _split_legacy(conn)
    leagues_dir().mkdir(parents=True, exist_ok=True)
    path = league_path(lid)
    new = not path.exists()
    conn.execute("ATTACH DATABASE ? AS lg", (str(path),))
    conn.execute("PRAGMA lg.journal_mode=WAL")
    conn.executescript(LEAGUE_SCHEMA.format(p="lg."))
    _migrate_league(conn, "lg")
    if new:
        _seed_league(conn, lid)
    conn.commit()
    return conn


@contextmanager
def cursor():
    """One connection per league, all behind one lock: the scheduler and API threads both write."""
    lid = current_league()
    with _lock:
        conn = _conns.get(lid)
        if conn is None:
            conn = _conns[lid] = connect(lid)
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def close_all():
    with _lock:
        for c in _conns.values():
            c.close()
        _conns.clear()


def _cols(conn, schema, table) -> list:
    return [r[1] for r in conn.execute(f"PRAGMA {schema}.table_info({table})")]


def _migrate_shared(conn):
    if "tip_utc" not in _cols(conn, "main", "schedule"):
        conn.execute("ALTER TABLE schedule ADD COLUMN tip_utc TEXT")
    if "espn_pos" not in _cols(conn, "main", "players"):
        conn.execute("ALTER TABLE players ADD COLUMN espn_pos TEXT")


def _migrate_league(conn, schema):
    cols = _cols(conn, schema, "moves")
    for col in ("kind", "horizon_end", "urgency"):
        if col not in cols:
            conn.execute(f"ALTER TABLE {schema}.moves ADD COLUMN {col} TEXT")


def _seed_league(conn, lid):
    """A new league starts from another league's tuned projection settings rather than the
    defaults, until its own daily tune runs."""
    for other in sorted(leagues_dir().glob("*.db")):
        if other.stem == lid:
            continue
        src = sqlite3.connect(other)
        try:
            row = src.execute("SELECT value FROM kv WHERE key='model_params'").fetchone()
        except sqlite3.OperationalError:
            row = None
        finally:
            src.close()
        if row:
            conn.execute("INSERT OR IGNORE INTO lg.kv VALUES ('model_params', ?)", (row[0],))
            return


def _legacy_league_id(conn) -> str:
    kv = {r[0]: json.loads(r[1]) for r in conn.execute("SELECT key, value FROM main.kv")}
    if kv.get("league_source") == "demo":
        return "demo"
    key = kv.get("league_key") or ""
    return key.split(".l.")[-1] if ".l." in key else league_ids()[0]


def _split_legacy(conn):
    """One-time move from the single-league layout (everything in fbb.db) to a league file.
    fbb.db is backed up first."""
    main = {r[0] for r in conn.execute("SELECT name FROM main.sqlite_master WHERE type='table'")}
    present = [t for t in LEAGUE_TABLES if t in main]
    if not present:
        return
    rows = sum(conn.execute(f"SELECT count(*) FROM main.{t}").fetchone()[0] for t in present)
    if rows:
        lid = _legacy_league_id(conn) if "kv" in present else league_ids()[0]
        target = league_path(lid)
        if target.exists() and target.stat().st_size > 0:
            raise RuntimeError(f"fbb.db still holds league data but {target} already exists: refusing "
                               "to merge. Is an old copy of the server still running?")
        conn.commit()
        folder = config.DB_PATH.parent / "backups"
        folder.mkdir(parents=True, exist_ok=True)
        dst = sqlite3.connect(folder / f"fbb-before-league-split-{datetime.now():%Y%m%d-%H%M%S}.db")
        conn.backup(dst)
        dst.close()
        leagues_dir().mkdir(parents=True, exist_ok=True)
        conn.execute("ATTACH DATABASE ? AS mig", (str(target),))
        conn.executescript(LEAGUE_SCHEMA.format(p="mig."))
        _migrate_league(conn, "mig")
        for t in present:
            if t == "kv":
                continue
            mig_cols = set(_cols(conn, "mig", t))
            cl = ", ".join(c for c in _cols(conn, "main", t) if c in mig_cols)
            conn.execute(f"INSERT INTO mig.{t} ({cl}) SELECT {cl} FROM main.{t}")
        if "kv" in present:
            for k, v in conn.execute("SELECT key, value FROM main.kv").fetchall():
                table = "main.shared_kv" if k in SHARED_KV else "mig.kv"
                conn.execute(f"INSERT OR REPLACE INTO {table} VALUES (?, ?)", (k, v))
        conn.commit()
        conn.execute("DETACH DATABASE mig")
    for t in present:
        conn.execute(f"DROP TABLE main.{t}")
    conn.commit()


# ---------------------------------------------------------------- key/value settings

def _kv_table(key) -> str:
    return "shared_kv" if key in SHARED_KV else "kv"


def kv_get(key, default=None):
    with cursor() as c:
        row = c.execute(f"SELECT value FROM {_kv_table(key)} WHERE key=?", (key,)).fetchone()
    return json.loads(row["value"]) if row else default


def kv_set(key, value):
    with cursor() as c:
        c.execute(f"INSERT OR REPLACE INTO {_kv_table(key)}(key, value) VALUES (?, ?)",
                  (key, json.dumps(value, default=_jsonable)))


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


def league_name(lid) -> str:
    with use_league(lid):
        return kv_get("league_name") or ("Demo league" if lid == "demo" else f"League {lid}")


def backup(keep=14) -> str:
    """Dated copies of fbb.db and every league file (SQLite's online backup: safe while the
    app is running). Keeps the newest `keep` copies of each."""
    from datetime import date
    folder = config.DB_PATH.parent / "backups"
    folder.mkdir(parents=True, exist_ok=True)
    today = date.today().isoformat()
    sources = [("fbb", config.DB_PATH)] + [(f"league-{p.stem}", p) for p in sorted(leagues_dir().glob("*.db"))]
    with _lock:
        for prefix, path in sources:
            src, dst = sqlite3.connect(path), sqlite3.connect(folder / f"{prefix}-{today}.db")
            src.backup(dst)
            src.close()
            dst.close()
            for old in sorted(folder.glob(f"{prefix}-????-??-??.db"))[:-keep]:
                old.unlink()
    return str(folder)


def snapshot_lineup(team_key, day=None):
    """Record the team's current slots as its lineup for `day` (default today)."""
    from datetime import date
    day = day or date.today().isoformat()
    with cursor() as c:
        c.execute("DELETE FROM lineup_days WHERE team_key=? AND day=?", (team_key, day))
        c.execute("INSERT INTO lineup_days SELECT team_key, ?, nba_id, slot FROM rosters WHERE team_key=?", (day, team_key))


def add_move(team_key, ts: str, nba_id, kind: str, source: str):
    """kind: 'add' | 'drop'; ts: ISO datetime with offset."""
    with cursor() as c:
        c.execute("INSERT OR IGNORE INTO league_moves VALUES (?,?,?,?,?)", (team_key, ts, int(nba_id), kind, source))
