"""NBA stats ingest: game logs, schedule, player directory (via nba_api / stats.nba.com)."""
import logging
import re
import unicodedata

import pandas as pd
from nba_api.stats.endpoints import commonallplayers, leaguegamelog, scheduleleaguev2

from . import config, db

log = logging.getLogger(__name__)

_SUFFIX = re.compile(r"\b(jr|sr|ii|iii|iv|v)\b")


def norm_name(name: str) -> str:
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[^a-z ]", "", s.replace("-", " "))
    return " ".join(_SUFFIX.sub("", s).split())


def _logs_frame(season: str) -> pd.DataFrame:
    df = leaguegamelog.LeagueGameLog(season=season, player_or_team_abbreviation="P",
                                     timeout=60).get_data_frames()[0]
    if df.empty:
        return df
    df = df.rename(columns=str.lower).rename(columns={
        "player_id": "nba_id", "team_abbreviation": "team", "tov": "tov"})
    cats = df[["pts", "reb", "ast", "stl", "blk"]].ge(10).sum(axis=1)
    df["dd"] = (cats >= 2).astype(float)
    df["td"] = (cats >= 3).astype(float)
    df["season"] = season
    df["min"] = pd.to_numeric(df["min"], errors="coerce").fillna(0.0)
    return df


def ingest_logs(seasons=None) -> int:
    seasons = seasons or [config.SEASON]
    cols = ["nba_id", "game_id", "game_date", "season", "team", "matchup"] + \
        [c for c in config.COUNT_STATS]
    total = 0
    names = {}
    for season in seasons:
        df = _logs_frame(season)
        if df.empty:
            continue
        rows = df[cols].itertuples(index=False, name=None)
        with db.cursor() as c:
            c.executemany(f"INSERT OR REPLACE INTO game_logs({','.join(cols)}) "
                          f"VALUES ({','.join('?' * len(cols))})", rows)
        total += len(df)
        latest = df.sort_values("game_date").groupby("nba_id").tail(1)
        for r in latest.itertuples():
            names[r.nba_id] = (r.player_name, r.team, season == config.SEASON)
    with db.cursor() as c:
        for pid, (name, team, current) in names.items():
            # only this season's box scores may move a player to a new team; older seasons
            # just make sure the player exists
            on_conflict = "DO UPDATE SET team=excluded.team, updated=excluded.updated" if current else "DO NOTHING"
            c.execute(f"""INSERT INTO players(nba_id, name, norm_name, team, updated) VALUES (?,?,?,?,?)
                          ON CONFLICT(nba_id) {on_conflict}""",
                      (pid, name, norm_name(name), team, db.now_iso()))
    log.info("ingested %d game log rows", total)
    return total


def ingest_player_directory() -> int:
    """Current rosters, so offseason moves (and rookies) show the right team before they play."""
    df = commonallplayers.CommonAllPlayers(is_only_current_season=1, season=config.SEASON,
                                           timeout=60).get_data_frames()[0]
    df = df[df["TEAM_ABBREVIATION"].fillna("") != ""]
    if len(df) < 300:  # a truncated response must not wipe everyone's team
        raise RuntimeError(f"player directory looks incomplete ({len(df)} players); keeping old teams")
    with db.cursor() as c:
        # Anyone missing from today's directory is not on an NBA roster (waived, retired,
        # unsigned). Without this, they keep their last team forever and get recommended.
        c.execute("UPDATE players SET team=NULL")
        for r in df.itertuples():
            c.execute("""INSERT INTO players(nba_id, name, norm_name, team, updated) VALUES (?,?,?,?,?)
                         ON CONFLICT(nba_id) DO UPDATE SET team=excluded.team, name=excluded.name,
                         norm_name=excluded.norm_name, updated=excluded.updated""",
                      (int(r.PERSON_ID), r.DISPLAY_FIRST_LAST, norm_name(r.DISPLAY_FIRST_LAST),
                       r.TEAM_ABBREVIATION, db.now_iso()))
    return len(df)


def ingest_schedule() -> int:
    df = scheduleleaguev2.ScheduleLeagueV2(season=config.SEASON, timeout=60).get_data_frames()[0]
    # 002 = regular season (includes NBA Cup group games); 006 = Cup final. Skip preseason/playoffs.
    df = df[df["gameId"].astype(str).str[:3].isin(["002", "006"])]
    rows = [(str(r.gameId), str(r.gameDateEst)[:10], r.homeTeam_teamTricode, r.awayTeam_teamTricode)
            for r in df.itertuples()]
    with db.cursor() as c:
        c.executemany("INSERT OR REPLACE INTO schedule VALUES (?,?,?,?)", rows)
    return len(rows)


def load_logs() -> pd.DataFrame:
    with db.cursor() as c:
        df = pd.read_sql_query("SELECT * FROM game_logs WHERE min > 0", c)
    df["game_date"] = pd.to_datetime(df["game_date"])
    return df


def team_games(start: str, end: str) -> dict:
    """team abbr -> list of ISO dates with a game in [start, end]."""
    with db.cursor() as c:
        rows = c.execute("SELECT game_date, home, away FROM schedule WHERE game_date BETWEEN ? AND ?",
                         (start, end)).fetchall()
    out = {}
    for r in rows:
        for t in (r["home"], r["away"]):
            out.setdefault(t, []).append(r["game_date"])
    return out
