"""Multi-league storage: shared NBA data in fbb.db, one file per league, one league per request.

All synthetic: a temp folder with no real data, so these run on a fresh clone too.
"""
import json
import sqlite3

import pytest

from fbb import config, db


@pytest.fixture
def two_leagues(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "fbb.db")
    monkeypatch.setattr(config, "YAHOO_LEAGUE_IDS", ["111", "222"])
    monkeypatch.setattr(config, "yahoo_configured", lambda: True)
    db.close_all()
    monkeypatch.setattr(db, "_conns", {})
    yield tmp_path
    db.close_all()


def _legacy_db(path, league_key="466.l.111"):
    """The old single-file layout: league tables and kv sitting in fbb.db itself."""
    c = sqlite3.connect(path)
    c.executescript(db.SHARED_SCHEMA.replace("shared_kv", "unused_kv"))
    c.executescript(db.LEAGUE_SCHEMA.format(p=""))
    c.execute("INSERT INTO players(nba_id, name) VALUES (1, 'Shared Guy')")
    c.execute("INSERT INTO fantasy_teams(team_key, name, is_mine) VALUES ('t1', 'Mine', 1)")
    c.execute("INSERT INTO rosters VALUES ('t1', 1, 'PG')")
    c.execute("INSERT INTO moves(created, week, add_id, drop_id, status) VALUES ('x', 3, 1, 2, 'suggested')")
    for k, v in {"league_key": league_key, "league_source": "yahoo", "num_teams": 12,
                 "claude_error": {"status": 400}, "model_params": {"half_life_games": 9.0}}.items():
        c.execute("INSERT INTO kv VALUES (?, ?)", (k, json.dumps(v)))
    c.commit()
    c.close()


def test_legacy_database_is_split_into_a_league_file(two_leagues):
    _legacy_db(config.DB_PATH)
    with db.use_league("111"):
        with db.cursor() as c:
            main = {r[0] for r in c.execute("SELECT name FROM main.sqlite_master WHERE type='table'")}
            assert not main & set(db.LEAGUE_TABLES)          # nothing left to shadow the league file
            assert c.execute("SELECT name FROM players").fetchone()[0] == "Shared Guy"
            assert c.execute("SELECT nba_id FROM rosters WHERE team_key='t1'").fetchone()[0] == 1
            assert c.execute("SELECT status FROM moves").fetchone()[0] == "suggested"
            assert "urgency" in db._cols(c, "lg", "moves")    # migrated columns exist
        assert db.kv_get("num_teams") == 12                   # league setting -> league file
        assert db.kv_get("claude_error") == {"status": 400}   # shared setting -> fbb.db
    assert db.league_path("111").exists()
    assert list((two_leagues / "backups").glob("fbb-before-league-split-*.db"))


def test_leagues_are_isolated_but_share_nba_data(two_leagues):
    with db.use_league("111"):
        with db.cursor() as c:
            c.execute("INSERT INTO players(nba_id, name) VALUES (7, 'Everyone Sees Me')")
            c.execute("INSERT INTO rosters VALUES ('a', 7, 'C')")
        db.kv_set("num_teams", 10)
        db.kv_set("injuries_fetched", "2026-11-08T00:00:00+00:00")
    with db.use_league("222"):
        with db.cursor() as c:
            assert c.execute("SELECT count(*) FROM rosters").fetchone()[0] == 0
            assert c.execute("SELECT name FROM players WHERE nba_id=7").fetchone()[0] == "Everyone Sees Me"
        assert db.kv_get("num_teams") is None
        assert db.kv_get("injuries_fetched") == "2026-11-08T00:00:00+00:00"


def test_new_league_starts_from_another_leagues_tuning(two_leagues):
    with db.use_league("111"):
        db.kv_set("model_params", {"half_life_games": 9.0})
    with db.use_league("222"):
        assert db.get_params()["half_life_games"] == 9.0


def test_default_league_and_bad_ids(two_leagues):
    assert db.current_league() == "111"
    with pytest.raises(ValueError):
        with db.use_league("../evil"):
            pass


def test_thread_keeps_the_league_it_was_started_from(two_leagues):
    import threading
    seen = []
    with db.use_league("222"):
        t = threading.Thread(target=db.bound(lambda: seen.append(db.current_league())))
    t.start()
    t.join()
    assert seen == ["222"]


def test_each_request_runs_in_its_chosen_league(two_leagues):
    from fastapi.testclient import TestClient
    from fbb import server
    client = TestClient(server.app)   # no `with`: the scheduler (lifespan) doesn't start
    assert client.get("/api/leagues").json()["current"] == "111"
    client.cookies.set(server.LEAGUE_COOKIE, "222")
    assert client.get("/api/leagues").json()["current"] == "222"   # reaches the threadpool endpoint
    assert client.get("/api/leagues?league=111").json()["current"] == "111"
    client.cookies.set(server.LEAGUE_COOKIE, "999")                  # not configured: default
    assert client.get("/api/leagues").json()["current"] == "111"
    assert not db.league_path("999").exists()
