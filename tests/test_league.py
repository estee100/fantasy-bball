"""Tests against a copy of the real league database (skipped without one)."""
import json
import math
from datetime import date

import numpy as np
import pytest


# ---------------------------------------------------------------- projections

def test_projections_have_no_nans(ctx):
    cols = ["min", "pts", "reb", "ast", "stl", "blk", "tov"]
    assert not ctx.proj[cols].isna().any().any()
    assert not ctx.values["value"].isna().any()


def test_injury_shortened_season_uses_season_before(ctx, pid):
    # Haliburton missed 2025-26 (Achilles); without the fallback he projected ~27 FP/G
    assert ctx.values["value"][pid("Tyrese Haliburton")] > 35


def test_waived_or_retired_players_have_no_team(ctx, pid):
    from fbb import recommend
    for name in ("Kevin Porter Jr.", "Russell Westbrook"):
        assert not isinstance(ctx.team_of(pid(name)), str)
    e = recommend.evaluate_add(ctx, pid("Kevin Porter Jr."))
    assert any("isn't on an NBA roster" in w for w in e["warnings"])


# ---------------------------------------------------------------- simulator

def test_simulation_is_reproducible(ctx):
    from fbb import sim
    mine = ctx.rosters[ctx.my_key]
    dates = sim.remaining_dates(ctx)
    a = sim.simulate_team(ctx, mine, dates, 500, seed=5)
    b = sim.simulate_team(ctx, mine, dates, 500, seed=5)
    assert np.array_equal(a, b)


def test_lineups_never_exceed_slots(ctx):
    from fbb import sim, lineup
    mine = ctx.rosters[ctx.my_key]
    for d in sim.lookahead_dates(ctx)[:7]:
        playing = [p for p in mine if d in sim.team_dates(ctx, ctx.team_of(p))]
        if not playing:
            continue
        masks = np.ones((50, len(playing)), dtype=bool)
        taken = sim.fill_patterns(ctx, playing, masks)
        assert taken.sum(1).max() <= len(lineup.slot_list())


def test_season_strength_is_deterministic(ctx):
    from fbb import sim
    mine = ctx.rosters[ctx.my_key]
    assert sim.season_strength(ctx, mine) == sim.season_strength(ctx, mine)


def test_day_plan_is_legal(ctx):
    from fbb import lineup, sim
    for d in sim.remaining_dates(ctx):
        plan = lineup.day_plan(ctx, ctx.my_key, d)
        started = [x for x in plan["lineup"] if not x.get("empty")]
        assert len({x["nba_id"] for x in started}) == len(started)
        for x in started:
            assert set(x["positions"]) & lineup.SLOT_ELIG[x["slot"]] or "?" in x["positions"]


# ---------------------------------------------------------------- trades & history

def test_trade_rejects_players_you_dont_own(ctx, pid):
    from fbb import recommend
    not_mine = next(p for k, ids in ctx.rosters.items() if k != ctx.my_key for p in ids)
    with pytest.raises(ValueError):
        recommend.evaluate_trade(ctx, [not_mine], [pid("Nikola Jokić")])


def test_move_found_by_sync_is_recorded_and_scored(ctx, pid):
    from fbb import db, history, pipeline
    mine = ctx.rosters[ctx.my_key]
    out = min(mine, key=lambda p: ctx.values["value"].get(p, 0))      # your weakest
    free = [p for p in ctx.values.sort_values("value", ascending=False).index   # best *healthy* free agent:
            if p not in ctx.rostered() and isinstance(ctx.team_of(p), str) and p not in ctx.injuries]  # an injured one
                                                                                # (e.g. Butler, out to Feb) really is a loss now
    with db.cursor() as c:  # the sync has already put the move on the roster
        c.execute("DELETE FROM rosters WHERE team_key=? AND nba_id=?", (ctx.my_key, out))
        c.execute("INSERT INTO rosters VALUES (?,?,?)", (ctx.my_key, free[0], "BN"))
    ctx = pipeline.context(reload=True)
    tid = history.record(ctx, "add/drop", [free[0]], [out], effective="2026-10-21")
    item = next(i for i in history.report(ctx)["items"] if i["id"] == tid)
    # regression: the prediction must use the roster BEFORE the move (a clear upgrade can't be negative)
    assert item["prediction"]["per_game_diff"] > 0
    assert item["prediction"]["season_dwins"] > 0
    history.evaluate(ctx)
    history.delete(tid)
    assert all(i["id"] != tid for i in history.report(ctx)["items"])


def test_season_week_counts_from_first_game_week(league_db):
    from fbb import sim
    assert sim.season_week(date(2026, 10, 22)) == 1
    assert sim.season_week(date(2026, 10, 26)) == 2
    assert sim.week_range_for(2) == ("2026-10-26", "2026-11-01")


# ---------------------------------------------------------------- your take

def test_take_changes_projection_everywhere_and_clears(ctx, pid):
    from fbb import overrides, pipeline, sim
    mc = pid("Jared McCain")
    before = ctx.values["value"][mc]
    model_min = ctx.raw_proj.at[mc, "min"]
    target = min(round(model_min * 1.4), 44)   # relative: his real minutes move as the season goes
    res = overrides.set_take(ctx, mc, "minutes", target, "more minutes")
    assert res["your_per_game"] == pytest.approx(res["model_per_game"] * target / model_min, rel=1e-6)
    ctx = pipeline.context(reload=True)
    assert ctx.values["value"][mc] > before * 1.15          # rankings/recommendations see it
    assert ctx.raw_proj.at[mc, "min"] == pytest.approx(model_min)  # the model's own view is kept
    assert ctx.takes[mc] == pytest.approx(target / model_min)
    rep = overrides.report(ctx)
    assert rep["items"][0]["name"] == "Jared McCain" and rep["items"][0]["active"] == 1
    overrides.clear(mc)
    ctx = pipeline.context(reload=True)
    assert ctx.values["value"][mc] == pytest.approx(before, rel=1e-6)


def test_take_validation(ctx, pid):
    from fbb import overrides
    with pytest.raises(ValueError):
        overrides.set_take(ctx, pid("Jared McCain"), "minutes", 60)
    with pytest.raises(ValueError):
        overrides.set_take(ctx, pid("Jared McCain"), "vibes", 10)


def test_tuning_ignores_takes(ctx, pid):
    from fbb import model, overrides
    overrides.set_take(ctx, pid("Jared McCain"), "pct", 50)
    fresh = model.project(ctx.logs, ctx.params)       # what tuning/backtests use
    assert fresh.at[pid("Jared McCain"), "min"] == pytest.approx(ctx.raw_proj.at[pid("Jared McCain"), "min"])


# ---------------------------------------------------------------- phone alerts

@pytest.fixture
def outbox(monkeypatch):
    """Capture alerts instead of sending them."""
    from fbb import alerts
    box = []

    class Resp:
        def raise_for_status(self):
            pass

    def fake_post(url, data=None, headers=None, timeout=None):
        box.append({"url": url, "title": headers["Title"], "body": data.decode()})
        return Resp()
    monkeypatch.delenv("FBB_NO_ALERTS", raising=False)
    monkeypatch.setattr(alerts.requests, "post", fake_post)
    return box


def test_injury_change_alerts_once_and_respects_quiet_hours(ctx, outbox):
    from datetime import datetime
    from fbb import alerts
    alerts.save(enabled=True, types={"lineup": False, "moves": False, "news": False, "injury": True})
    noon, night = datetime(2026, 10, 22, 12), datetime(2026, 10, 22, 2)
    alerts.run(ctx, now=noon)                                  # first run: takes a snapshot only
    assert outbox == []
    p = ctx.rosters[ctx.my_key][0]
    ctx.injuries[p] = {"nba_id": p, "status": "Out", "comment": "ankle sprain", "date": "2026-10-22"}
    assert alerts.run(ctx, now=night)["reason"] == "quiet hours"   # waits overnight
    assert outbox == []
    alerts.run(ctx, now=noon)
    assert len(outbox) == 1 and "Out" in outbox[0]["title"] and "/fbb-" in outbox[0]["url"]
    alerts.run(ctx, now=noon)                                  # no duplicates
    assert len(outbox) == 1


def test_alerts_off_by_default_and_titles_ascii(ctx, outbox):
    from fbb import alerts
    assert alerts.settings()["enabled"] is False
    assert alerts.run(ctx)["reason"] == "disabled"
    assert alerts._ascii("Luka Dončić: Out") == "Luka Doncic: Out"


# ---------------------------------------------------------------- trade finder

def test_trade_finder_results_are_consistent(ctx):
    from fbb import trades
    R = trades.find(ctx, limit=6)
    assert R["evaluated"] > 0
    mine = set(ctx.rosters[ctx.my_key])
    per_team = {}
    for t in R["trades"]:
        assert t["my_dwins"] >= trades.MIN_GAIN and t["their_dwins"] >= -trades.MAX_THEIR_LOSS
        assert all(x["nba_id"] in mine for x in t["give"])
        assert all(x["nba_id"] in ctx.rosters[t["other_key"]] for x in t["get"])
        per_team[t["other_key"]] = per_team.get(t["other_key"], 0) + 1
    assert all(n <= 2 for n in per_team.values())


def test_trade_finder_and_evaluator_agree(ctx):
    """Regression: the evaluator once filled open roster spots from an unfiltered free-agent list
    (a waived player), so the same trade scored +0.61 in the finder and -0.15 in the evaluator."""
    from fbb import recommend, trades
    R = trades.find(ctx, limit=3)
    for t in R["trades"]:
        e = recommend.evaluate_trade(ctx, [x["nba_id"] for x in t["give"]], [x["nba_id"] for x in t["get"]])
        assert e["my_dwins"] == pytest.approx(t["my_dwins"], abs=1e-6)
    for p in recommend.best_free_agents(ctx):
        assert isinstance(ctx.team_of(p), str)


# ---------------------------------------------------------------- Yahoo waivers and moves

def test_move_made_in_yahoo_is_matched_to_its_suggestion(ctx, pid):
    from fbb import db, yahoo
    a, d = pid("Santi Aldama"), pid("Nikola Jokić")
    with db.cursor() as c:
        mid = c.execute("INSERT INTO moves(created, week, add_id, drop_id, status, kind) VALUES (?,?,?,?,?,?)",
                        (db.now_iso(), 1, a, d, "suggested", "upgrade")).lastrowid
    assert yahoo._followed_suggestion([a], [d]) == mid
    assert yahoo._followed_suggestion([a], []) is None          # different move: no drop
    assert yahoo._followed_suggestion([a, d], [d]) is None      # not one-for-one


def test_yahoo_waiver_list_confirms_everyone_else_is_a_free_agent(ctx, pid):
    from fbb import free_agents, db
    db.kv_set("league_source", "yahoo")
    free_agents.set_yahoo_waivers([{"nba_id": pid("Santi Aldama"), "status": "W", "until": "2099-01-01",
                                    "positions": "PF,C"}])
    assert free_agents.availability(pid("Santi Aldama")) == {"known": True, "status": "W", "until": "2099-01-01"}
    # not on the waiver list: a confirmed free agent
    assert free_agents.availability(pid("Nikola Jokić")) == {"known": True, "status": "FA", "until": None}


# ---------------------------------------------------------------- weekly add limit & locks

def test_add_limit_blocks_moves_when_used_up(ctx, pid, monkeypatch):
    from fbb import db, history, recommend, learning
    monkeypatch.setattr(learning, "log_suggestions", lambda *a, **k: None)
    db.kv_set("max_adds_week", 1)
    mine = ctx.rosters[ctx.my_key]
    free = next(p for p in ctx.values.sort_values("value", ascending=False).index
                if p not in ctx.rostered() and isinstance(ctx.team_of(p), str) and p not in ctx.injuries)
    history.record(ctx, "add/drop", [free], [mine[-1]], effective=ctx.week_start if ctx.week_start <= "2026-10-22" else None)
    assert history.add_budget(ctx)["left"] == 0
    r = recommend.recommend(ctx, n_sims=300)
    assert all(m["urgency"] == "No adds left" for m in r["moves"])


def test_locked_players_keep_their_slot(ctx):
    from datetime import timedelta
    from fbb import lineup, sim
    d = sim.remaining_dates(ctx)[0]
    tips = lineup.tip_times(ctx)
    mine_teams = {ctx.team_of(p) for p in ctx.rosters[ctx.my_key]}
    today_tips = [t for (team, day), t in tips.items() if day == d and team in mine_teams]
    if not today_tips:
        pytest.skip("none of your players play on the first remaining day")
    plan = lineup.day_plan(ctx, ctx.my_key, d, now=max(today_tips) + timedelta(minutes=1))
    slots = sim.team_slots(ctx)[ctx.my_key]
    for x in plan["lineup"]:
        if not x.get("empty") and x["locked"]:
            assert slots.get(x["nba_id"]) not in ("BN", None, "?")   # locked starters stay put
    started = {x["nba_id"] for x in plan["lineup"] if not x.get("empty")}
    for b in plan["bench"]:
        if b["locked"]:
            assert b["nba_id"] not in started                        # locked bench can't come in


# ---------------------------------------------------------------- week-to-date scoring

def test_was_on_roster_at_tipoff():
    from datetime import datetime, timezone
    from fbb.sim import _was_on
    t = lambda h: datetime(2026, 10, 23, h, tzinfo=timezone.utc)
    assert _was_on([(t(10), "add")], t(23), False) is True          # added that morning
    assert _was_on([(t(23), "add")], t(20), True) is False          # added after the game
    assert _was_on([(t(23), "drop")], t(20), False) is True         # dropped after the game
    assert _was_on([(t(10), "drop")], t(20), False) is False        # dropped before it
    assert _was_on([], t(20), True) is True


def test_week_to_date_uses_that_days_lineup_not_todays(ctx):
    """Regression: pressing 'I set this in Yahoo' on a light day used to bench every earlier game."""
    from fbb import db, sim
    me = ctx.my_key
    with db.cursor() as c:
        c.execute("UPDATE rosters SET slot='BN' WHERE team_key=?", (me,))   # today's lineup: all bench
    ctx._pools.clear()
    wtd = sim.estimate_wtd(ctx, ctx.rosters[me], me)
    played = ctx.logs[(ctx.logs["game_date"] >= ctx.week_start) & ctx.logs["nba_id"].isin(ctx.rosters[me])]
    if played.empty:
        import pytest
        pytest.skip("no games played yet this week")
    assert wtd["pts"] > 0
    # a lineup recorded for a past day is honoured: all bench every day -> nothing counts.
    # (Subtracting a day's points instead overcounts: only that day's starters were ever in wtd.)
    for d in played["game_date"].astype(str).str[:10].unique():
        db.snapshot_lineup(me, d)
    ctx._pools.clear()
    assert sim.estimate_wtd(ctx, ctx.rosters[me], me)["pts"] == 0


# ---------------------------------------------------------------- unfinished games / model team

def test_games_in_progress_are_not_stored(league_db, monkeypatch):
    from datetime import datetime, timedelta, timezone
    from fbb import nba, db
    with db.cursor() as c:
        gid = c.execute("SELECT game_id FROM game_logs ORDER BY game_date DESC LIMIT 1").fetchone()[0]
        c.execute("UPDATE schedule SET tip_utc=? WHERE game_id=?",
                  ((datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(), gid))
    assert str(gid) in nba._unfinished_games()
    monkeypatch.setattr(nba, "_logs_frame", lambda season: __import__("pandas").DataFrame())
    nba.ingest_logs()   # nothing new to store, but nothing breaks


def test_model_team_respects_add_limit_and_ownership(ctx, monkeypatch):
    from fbb import shadow, db
    monkeypatch.setattr(shadow, "_daily_due", lambda *a: True)   # not dependent on the clock
    me = ctx.rosters[ctx.my_key]
    shadow.start(list(me), [], "2026-10-26")
    with db.cursor() as c:   # 4 adds already used this week
        for i in range(4):
            c.execute("INSERT INTO shadow_moves(created, day, kind, add_id, status) VALUES (?,?,?,?,?)",
                      ("x", "2026-10-26", "add", 1000 + i, "done"))
    shadow.daily(ctx, today="2026-10-27")
    s = shadow.state()
    assert sorted(s["roster"] + s["il"]) == sorted(me)          # no add beyond the limit
    sc = shadow._shadow_ctx(ctx, s)
    others = {p for k, ids in ctx.rosters.items() if k != ctx.my_key for p in ids}
    assert others <= sc.rostered()                               # can't add owned players
    assert shadow.daily(ctx, today="2026-10-27") == {"skipped": True}   # once a day


# ---------------------------------------------------------------- injured: absence so far; delete reverses

def test_absence_so_far_sets_a_floor_on_remaining_absence():
    from fbb import model
    inj = {1: {"nba_id": 1, "status": "Out", "comment": "Smith (knee) is out for Saturday's game.", "date": None}}
    saved = model.ABSENCE_STREAK
    try:
        model.ABSENCE_STREAK = {1: 0}
        one = model.availability(1, ["2026-11-01", "2026-11-03", "2026-11-05"], inj, {}, {}, set())
        model.ABSENCE_STREAK = {1: 3}
        three = model.availability(1, ["2026-11-01", "2026-11-03", "2026-11-05"], inj, {}, {}, set())
    finally:
        model.ABSENCE_STREAK = saved
    assert one[0] == 0.0 and one[1] > 0.5          # the note alone: one game
    assert three == [0.0, 0.0, 0.0]                # missed 3 already: expect 3 more


def test_deleting_a_history_entry_reopens_its_suggestion(ctx):
    from fbb import history, db, recommend
    drop = ctx.rosters[ctx.my_key][-1]
    add = next(p for p in recommend.best_free_agents(ctx, 10))
    with db.cursor() as c:
        mid = c.execute("INSERT INTO moves(created, week, add_id, drop_id, status, kind) VALUES (?,?,?,?,?,?)",
                        (db.now_iso(), 1, add, drop, "executed", "upgrade")).lastrowid
    tid = history.record(ctx, "add/drop", [add], [drop], source="suggestion", move_id=mid)
    history.delete(tid)
    with db.cursor() as c:
        assert c.execute("SELECT status FROM moves WHERE id=?", (mid,)).fetchone()[0] == "suggested"
        assert c.execute("SELECT deleted FROM transactions WHERE id=?", (tid,)).fetchone()[0] == 1
        assert c.execute("SELECT count(*) FROM league_moves WHERE nba_id IN (?,?) AND source='suggestion'",
                         (add, drop)).fetchone()[0] == 0


def test_il_players_are_never_suggested_drops_and_open_spots_are_found(ctx, monkeypatch):
    from fbb import db, recommend, learning
    monkeypatch.setattr(learning, "log_suggestions", lambda *a, **k: None)
    me = ctx.rosters[ctx.my_key]
    il_player = me[0]
    with db.cursor() as c:
        c.execute("UPDATE rosters SET slot='IL' WHERE team_key=? AND nba_id=?", (ctx.my_key, il_player))
    ctx._pools.clear()
    r = recommend.recommend(ctx, n_sims=300, log=False)
    assert all(m["drop"] != il_player for m in r["moves"])
    if len(me) - 1 < ctx.roster_size:
        assert r["open_spot"] and r["open_spot"]["spots"] >= 1


# ---------------------------------------------------------------- trades: acceptance, resizing

def test_acceptance_reads_names_and_points(ctx):
    from fbb import recommend
    others = [p for k, ids in ctx.rosters.items() if k != ctx.my_key for p in ids]
    ranked = sorted(others, key=lambda p: -recommend.seen_value(ctx, p))
    star, scrub = ranked[0], ranked[-1]
    mine = sorted(ctx.rosters[ctx.my_key], key=lambda p: -recommend.seen_value(ctx, p))
    # they give their best player for my worst: no
    assert recommend.acceptance(ctx, [mine[-1]], [star])["label"] == "unlikely"
    # my best for their worst: they'd take that
    assert recommend.acceptance(ctx, [mine[0]], [scrub])["label"] == "likely"


def test_trade_baseline_is_resized_like_the_trade(ctx):
    """IL players don't count toward the roster limit, and before/after get the same resizing:
    otherwise every trade starts in the hole (a no-change trade scored -0.85 wins)."""
    from fbb import recommend, sim, lineup
    me = ctx.my_key
    roster = ctx.rosters[me]
    fa = recommend.best_free_agents(ctx)
    r = recommend.resized_roster(ctx, roster, me, fa)
    slots = sim.team_slots(ctx).get(me, {})
    il = [p for p in roster if slots.get(p) in lineup.NOT_STARTABLE]
    assert set(il) <= set(r)
    assert len([p for p in r if p not in il]) == ctx.roster_size
    assert recommend.resized_roster(ctx, r, me, fa) == r      # resizing twice changes nothing


def test_buy_low_gaps_are_consistent(ctx):
    # the "seen" number is the one the trade acceptance check uses, the two gap parts add up
    # to projection - seen, and each list holds who it says it does
    from fbb import buylow, recommend
    R = buylow.scan(ctx)
    if not R["available"]:
        pytest.skip(R["reason"])
    owner = {p: k for k, r in ctx.rosters.items() for p in r}
    for k in ("buy_low", "sell_high", "hold", "pickups"):
        for x in R[k]:
            p = x["nba_id"]
            assert x["seen"] == pytest.approx(recommend.seen_value(ctx, p))
            assert x["rate_gap"] + x["min_gap"] == pytest.approx(x["proj"] - x["seen"])
            assert abs(x["this_season"]["min"] - x["last_season"]["min"]) <= buylow.MAX_MIN_CHANGE
            assert abs(x["rate_gap"]) >= buylow.MIN_GAP
            mine = owner.get(p) == ctx.my_key
            assert mine == (k in ("sell_high", "hold"))
            assert (p in owner) == (k != "pickups")


def test_streams_rank_this_week_and_never_drop_il_players(ctx):
    from fbb import lineup, recommend, sim
    R = recommend.streams(ctx)
    assert R["available"]
    mine = ctx.rosters[ctx.my_key]
    slots = sim.team_slots(ctx)[ctx.my_key]
    il = {p for p in mine if slots.get(p) in lineup.NOT_STARTABLE}
    rostered, recent = ctx.rostered(), recommend._recent_drops(ctx)
    gains = [x["week_dwin"] for x in R["streams"]]
    assert gains == sorted(gains, reverse=True)
    assert all(g >= recommend.STREAM_MIN_GAIN for g in gains)
    for x in R["streams"]:
        assert x["nba_id"] not in rostered and x["nba_id"] not in recent
        assert x["minutes"] >= recommend.STREAM_MIN_MINUTES
        assert x["drop"] is None or x["drop"]["nba_id"] not in il
    # every player you could cut is judged, IL players never are
    listed = {x["nba_id"] for x in R["drops"]}
    assert not listed & il
    assert listed | il | {p for p in mine if recommend.injury_plan(ctx, p) == "il"} >= set(mine) or not R["streams"]
    assert recommend.streams(ctx) is R  # cached until the context reloads
