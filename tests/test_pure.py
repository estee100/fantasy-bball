"""Pure-logic tests: no database needed."""
import itertools
import math
import random
from datetime import date

import pytest

from fbb import backtest, lineup, model, yahoo
from fbb.lineup import SLOT_ELIG, solve, greedy_fill, slot_masks, parse_positions, DEFAULT_SLOTS


# ---------------------------------------------------------------- lineup solver

def brute(cands, slots):
    best = 0.0
    for perm in itertools.product([None] + list(range(len(slots))), repeat=len(cands)):
        used = [x for x in perm if x is not None]
        if len(used) != len(set(used)):
            continue
        if any(x is not None and not (cands[k][1] & SLOT_ELIG[slots[x]]) for k, x in enumerate(perm)):
            continue
        best = max(best, sum(cands[k][2] for k, x in enumerate(perm) if x is not None))
    return best


def test_solver_matches_brute_force():
    rng = random.Random(1)
    positions = ["PG", "SG", "SF", "PF", "C"]
    for _ in range(150):
        slots = rng.sample(["PG", "SG", "G", "SF", "PF", "F", "C", "Util"], 4)
        cands = [(k, set(rng.sample(positions, rng.randint(1, 2))), rng.uniform(5, 50)) for k in range(5)]
        assign, total = solve(cands, slots)
        assert total == pytest.approx(brute(cands, slots))
        # the returned lineup is legal and adds up to the reported total
        by_id = {c[0]: c for c in cands}
        assert all(by_id[p][1] & SLOT_ELIG[slots[i]] for i, p in assign.items())
        assert sum(by_id[p][2] for p in assign.values()) == pytest.approx(total)
        assert len(set(assign.values())) == len(assign)


def test_solver_prefers_legal_lineup():
    # a guard must take G; two centers share C and Util
    assign, total = solve([(1, {"C"}, 30), (2, {"C"}, 20), (3, {"PG"}, 25), (4, {"SF"}, 10)], ["G", "C", "Util"])
    assert total == 75
    assert {i: p for i, p in assign.items()} == {0: 3, 1: 2, 2: 1} or set(assign.values()) == {1, 2, 3}


def test_greedy_fill_is_nearly_optimal():
    stats = lineup.check_greedy(trials=400, seed=3)
    assert stats["optimal_share"] >= 0.95


def test_greedy_never_exceeds_slots():
    masks = [slot_masks({"C"}, DEFAULT_SLOTS)] * 13
    started = greedy_fill(masks, len(DEFAULT_SLOTS))
    assert sum(started) == 4  # C, C, Util, Util


@pytest.mark.parametrize("raw,expected", [
    ("PG,SG", {"PG", "SG"}), ("G", {"PG", "SG"}), ("F", {"SF", "PF"}), ("SF/PF", {"SF", "PF"}),
    (None, set()), (float("nan"), set()), ("Util", set()),
])
def test_parse_positions(raw, expected):
    assert parse_positions(raw) == expected


def test_unknown_positions_fit_anything_in_simulator():
    assert slot_masks({"?"}, DEFAULT_SLOTS) == (1 << len(DEFAULT_SLOTS)) - 1


# ---------------------------------------------------------------- injury notes

TODAY = date(2026, 10, 22)


@pytest.mark.parametrize("note,lo,hi", [
    ("Nurkic will miss at least the next four weeks due to a partial plantar plate tear", 12, 15),
    ("He is expected to miss 2-3 weeks", 7, 10),
    ("Butler (knee) could return sometime between January and February", 35, 60),
    ("out for the season after surgery", 75, 90),
    ("sidelined until December", 20, 30),
    ("White (calf) is out for Wednesday's season opener against the Nets.", 1, 1),
    ("Siakam will be rested against the Hawks on Monday.", 1, 1),
    ("Brown (ankle) won't play Friday against Boston and will sit out tonight.", 1, 1),
    ("Brown sprained his ankle on Monday and is out two weeks.", 6, 8),
])
def test_injury_timelines(note, lo, hi):
    g = model.injury_games_out(note, "Out", TODAY)
    assert g is not None and lo <= g <= hi


def test_start_of_season_is_not_season_ending():
    note = "Ingram won't be ready for the start of the season due to a partially torn plantar fascia"
    assert not model.SEASON_ENDING.search(note.lower())
    g = model.injury_games_out(note, "Out", TODAY)
    assert g is not None and 5 <= g <= 30  # a tear with no timeline: weeks, not the season


def test_routine_news_doesnt_shorten_a_long_injury():
    inj = {1: {"status": "Out", "comment": "torn meniscus, expected to miss six months", "date": "2026-10-01"}}
    bad = {1: {"games_out": 1, "direction": -1}}     # "out Saturday"
    good = {1: {"games_out": 2, "direction": 1}}     # "targeting a return next week"
    assert model._games_out(1, inj, bad) > 60
    assert model._games_out(1, inj, good) == 2


def test_one_game_timelines_count_down_from_the_us_day_written():
    from datetime import date, timedelta
    today = date.today()
    mon, wed = (today - timedelta(days=1)).isoformat(), (today + timedelta(days=1)).isoformat()
    team = {mon, wed}
    # note refreshed after midnight UTC on Monday night US time: Monday's game is the one
    late_utc = f"{today.isoformat()}T05:50Z"
    inj = {1: {"status": "Day-To-Day", "comment": "out for Monday's game", "date": late_utc}}
    assert model.availability(1, [wed], inj, {}, {}, team)[0] >= 0.8
    # news "out 1 game" counts down too (it used to take his next game for two weeks)
    ev = {2: {"games_out": 1, "direction": -1, "date": mon, "event_type": "injury", "minutes_multiplier": None}}
    assert model.availability(2, [wed], {}, ev, {}, team)[0] > 0.8


def test_reddit_and_unknown_publishers_dont_move_the_model():
    from fbb import news
    assert not news.is_credible("Reddit", "Paul Reed versus Aday Mara")
    assert not news.is_credible("Google News", "Shocking Luka update - Fadeaway World")
    assert news.is_credible("Google News", "Paul Reed News: Enters starting lineup - RotoWire")
    assert news.is_credible("Google News", "Pistons notes - The Detroit News")
    assert news.is_credible("ESPN", "anything")


def test_news_minutes_boost_is_discounted_and_cuts_are_not():
    up = {1: {"minutes_multiplier": 1.4, "event_type": "lineup_start"}}
    down = {1: {"minutes_multiplier": 0.6, "event_type": "lineup_bench"}}
    assert model.production_multiplier(1, up, {}) == pytest.approx(1.2)
    assert model.production_multiplier(1, down, {}) == pytest.approx(0.6)
    assert model.production_multiplier(2, up, {}) == 1.0


def test_game_in_progress_counts_only_what_is_left():
    """Yahoo's week-to-date points are live: a game under way is partly in them already."""
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace
    from fbb import sim
    now = datetime.now(timezone.utc)
    tips = {("AAA", "d"): now - timedelta(minutes=70), ("BBB", "d"): now - timedelta(hours=4),
            ("CCC", "d"): now + timedelta(hours=1)}
    live = SimpleNamespace(today=None, wtd={"t": {}}, _pools={"_tips": tips})
    assert abs(sim._share_left(live, "AAA", "d") - 0.5) < 0.01
    assert sim._share_left(live, "BBB", "d") == 0.0   # over, box score not in yet
    assert sim._share_left(live, "CCC", "d") == 1.0
    boxes = SimpleNamespace(today=None, wtd={}, _pools={"_tips": tips})
    assert sim._share_left(boxes, "AAA", "d") == 1.0  # box-score week-to-date has no live games


def test_week_dates_come_from_yahoo_when_synced(monkeypatch):
    """Two Yahoo weeks run double (NBA Cup, All-Star): week 8 isn't 7 days after week 7."""
    from fbb import db, sim
    weeks = {"7": ["2026-11-30", "2026-12-13"], "8": ["2026-12-14", "2026-12-20"]}
    monkeypatch.setattr(db, "kv_get", lambda k, d=None: weeks if k == "game_weeks" else d)
    assert sim.week_range_for(8) == ("2026-12-14", "2026-12-20")
    assert sim.week_range_for(7) == ("2026-11-30", "2026-12-13")


# ---------------------------------------------------------------- Yahoo parsing

def test_parse_team_stats_reads_fraction_stats():
    raw = {"fantasy_content": {"team": [[{"team_key": "x"}], {"team_stats": {"stats": [
        {"stat": {"stat_id": "9004003", "value": "120/250"}}, {"stat": {"stat_id": "5", "value": ".480"}},
        {"stat": {"stat_id": "12", "value": "330"}}, {"stat": {"stat_id": "17", "value": ""}}]}}]}}
    out = yahoo.parse_team_stats(raw)
    assert out == {"fgm": 120.0, "fga": 250.0, "pts": 330.0}


def test_parse_roster_adds_reads_weekly_count():
    # shape of a real team/metadata response
    raw = {"fantasy_content": {"team": [[{"team_key": "x"}, {"number_of_trades": 0},
        {"roster_adds": {"coverage_type": "week", "coverage_value": 2, "value": "4"}}, []]]}}
    assert yahoo.parse_roster_adds(raw) == {"week": 2, "used": 4}
    assert yahoo.parse_roster_adds({"fantasy_content": {"team": [[{"team_key": "x"}]]}}) is None


def test_parse_transactions_reads_who_moved_where_and_when():
    # shape of a real league transactions response: an add's transaction_data is a list, a drop's a dict
    def player(name, team, td):
        return {"player": [[{"player_key": "p"}, {"name": {"full": name}}, {"editorial_team_abbr": team}], {"transaction_data": td}]}
    raw = {"fantasy_content": {"league": [{"league_key": "x"}, {"transactions": {
        "0": {"transaction": [{"transaction_key": "478.l.1.tr.59", "type": "add/drop", "status": "successful",
                               "timestamp": "1793145038"},
                              {"players": {"0": player("Julian Champagnie", "SAS", [{"type": "add", "source_type": "freeagents",
                                                                                      "destination_team_key": "478.l.1.t.3"}]),
                                           "1": player("Dillon Brooks", "PHX", {"type": "drop", "source_team_key": "478.l.1.t.3",
                                                                                "destination_type": "waivers"}),
                                           "count": 2}}]},
        "count": 1}}]}}
    (t,) = yahoo.parse_transactions(raw)
    assert t["key"] == "478.l.1.tr.59" and t["status"] == "successful"
    assert t["when"].isoformat() == "2026-10-27T23:50:38+00:00"
    assert t["moves"] == [("Julian Champagnie", "SAS", "add", None, "478.l.1.t.3"),
                          ("Dillon Brooks", "PHX", "drop", "478.l.1.t.3", None)]


def test_parse_waivers_reads_name_team_positions_and_date():
    # shape of a real league players;status=W;out=ownership response
    raw = {"fantasy_content": {"league": [{"league_key": "x"}, {"players": {
        "0": {"player": [[{"player_key": "478.p.4614"}, {"name": {"full": "DeMar DeRozan"}}, [],
                          {"editorial_team_abbr": "DEN"}, {"display_position": "SF,PF"}],
                         {"ownership": {"waiver_date": "2026-10-28", "ownership_type": "waivers"}}]},
        "count": 1}}]}}
    assert yahoo.parse_waivers(raw) == [("DeMar DeRozan", "DEN", "SF,PF", "2026-10-28")]
    assert yahoo.parse_waivers({"fantasy_content": {"league": [{"league_key": "x"}, {"players": []}]}}) == []


# ---------------------------------------------------------------- calibration math

def test_temperature_pulls_toward_half():
    assert backtest.apply_temperature(0.8, 1.0) == pytest.approx(0.8)
    assert 0.5 < backtest.apply_temperature(0.8, 2.0) < 0.8
    assert backtest.apply_temperature(0.5, 3.0) == pytest.approx(0.5)


def test_poisson_binomial_win():
    assert model.poisson_binomial_win([0.5, 0.5]) == pytest.approx(0.5)
    assert model.poisson_binomial_win([1.0, 1.0, 0.0]) == pytest.approx(1.0)


# ---------------------------------------------------------------- claude error retry

def test_claude_credit_error_retries_with_unchanged_key(monkeypatch):
    from fbb import config, news
    monkeypatch.setattr(config, "ANTHROPIC_KEY", "test-key-abc123")
    monkeypatch.setattr(config, "ANTHROPIC_WORKSPACE_ID", "")
    same = {"key_tail": "abc123", "workspace": ""}
    credit = "Your credit balance is too low to access the Anthropic API."
    assert news.claude_error_is_stale({**same, "message": credit})  # a top-up isn't visible locally
    assert not news.claude_error_is_stale({**same, "message": "invalid x-api-key"})  # waits for a key change
    assert news.claude_error_is_stale({"key_tail": "old000", "workspace": "", "message": "invalid x-api-key"})
