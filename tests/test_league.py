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
