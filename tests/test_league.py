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


# ---------------------------------------------------------------- paste import

def test_paste_parser_reads_all_players_slots_and_positions(league_db, paste_text):
    from fbb import manual
    players, _ = manual.parse_roster(paste_text)
    assert len(players) == 13
    slots = [s for _, s in players]
    assert slots.count("BN") == 3 and slots.count("C") == 2 and slots.count("Util") == 2
    assert len(manual.parse_roster.last_positions) == 13
    assert manual.unmatched_candidates(paste_text, [p for p, _ in players]) == []


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


def test_backdated_move_is_recorded_scored_and_editable(ctx, pid):
    from fbb import history, pipeline
    mine = ctx.rosters[ctx.my_key]
    out = min(mine, key=lambda p: ctx.values["value"].get(p, 0))      # your weakest
    free = [p for p in ctx.values.sort_values("value", ascending=False).index   # best *healthy* free agent:
            if p not in ctx.rostered() and isinstance(ctx.team_of(p), str) and p not in ctx.injuries]  # an injured one
                                                                                # (e.g. Butler, out to Feb) really is a loss now
    tid = history.record(ctx, "add/drop", [free[0]], [out], effective="2026-10-21", apply=True)
    ctx = pipeline.context(reload=True)
    assert free[0] in ctx.rosters[ctx.my_key] and out not in ctx.rosters[ctx.my_key]
    item = next(i for i in history.report(ctx)["items"] if i["id"] == tid)
    # regression: the prediction must use the roster BEFORE the move (a clear upgrade can't be negative)
    assert item["prediction"]["per_game_diff"] > 0
    assert item["prediction"]["season_dwins"] > 0
    history.evaluate(ctx)
    history.update(tid, effective="2026-10-22")
    history.evaluate(ctx)
    item = next(i for i in history.report(ctx)["items"] if i["id"] == tid)
    assert item["effective"] == "2026-10-22"
    history.delete(tid)
    assert all(i["id"] != tid for i in history.report(ctx)["items"])


def test_trade_moves_players_both_ways(ctx):
    from fbb import history, pipeline
    other = next(k for k in ctx.rosters if k != ctx.my_key)
    give, get = ctx.rosters[ctx.my_key][0], ctx.rosters[other][0]
    history.record(ctx, "trade", [get], [give], other_key=other, apply=True)
    ctx = pipeline.context(reload=True)
    assert get in ctx.rosters[ctx.my_key] and give in ctx.rosters[other]


def test_trade_validation_blocks_wrong_team(ctx, pid):
    from fbb import history
    other = next(k for k in ctx.rosters if k != ctx.my_key)
    elsewhere = next(p for k, ids in ctx.rosters.items() if k not in (ctx.my_key, other) for p in ids)
    with pytest.raises(ValueError):
        history.record(ctx, "trade", [elsewhere], [ctx.rosters[ctx.my_key][0]], other_key=other, apply=True)


def test_editor_add_refuses_player_on_another_team(ctx):
    from fbb import manual
    owned = next(p for k, ids in ctx.rosters.items() if k != ctx.my_key for p in ids)
    with pytest.raises(ValueError):
        manual.edit_roster(ctx.my_key, add=owned, ctx=ctx)


# ---------------------------------------------------------------- weeks

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
    res = overrides.set_take(ctx, mc, "minutes", 28, "more minutes")
    assert res["your_per_game"] == pytest.approx(res["model_per_game"] * 28 / model_min, rel=1e-6)
    ctx = pipeline.context(reload=True)
    assert ctx.values["value"][mc] > before * 1.2           # rankings/recommendations see it
    assert ctx.raw_proj.at[mc, "min"] == pytest.approx(model_min)  # the model's own view is kept
    assert ctx.takes[mc] == pytest.approx(28 / model_min)
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


# ---------------------------------------------------------------- Yahoo free-agent list

def test_free_agent_paste_reads_status_waivers_and_positions(ctx, pid):
    from datetime import date
    from fbb import free_agents
    from pathlib import Path
    text = (Path(__file__).parent / "fixtures" / "yahoo_fa_paste.txt").read_text(encoding="utf-8")
    players, unmatched = free_agents.parse(text, today=date(2026, 10, 22))
    by = {p["nba_id"]: p for p in players}
    assert by[pid("Bilal Coulibaly")]["status"] == "FA" and by[pid("Bilal Coulibaly")]["positions"] == "SG,SF"
    assert by[pid("Santi Aldama")]["status"] == "W" and by[pid("Santi Aldama")]["until"] == "2026-10-24"
    assert by[pid("Precious Achiuwa")]["status"] == "FA"
    assert unmatched == ["Notarealplayer Fakename"]


def test_free_agent_list_cleans_outdated_rosters_and_flags_waivers(ctx, pid):
    from datetime import date
    from fbb import free_agents, db
    other = next(k for k in ctx.rosters if k != ctx.my_key)
    victim = ctx.rosters[other][0]
    text = f"{ctx.name(victim)}\n{ctx.name(victim)}Player Note XXX - PG\nFA\nSanti Aldama\nSanti AldamaPlayer Note DAL - PF,C\nW (Dec 31)\n"
    res = free_agents.import_list(text)
    assert any(ctx.name(victim) in x for x in res["removed_from_outdated_rosters"])
    with db.cursor() as c:
        assert c.execute("SELECT 1 FROM rosters WHERE team_key=? AND nba_id=?", (other, victim)).fetchone() is None
    assert free_agents.availability(pid("Santi Aldama"))["status"] == "W"
    assert free_agents.availability(pid("Nikola Jokić"))["listed"] is False


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
