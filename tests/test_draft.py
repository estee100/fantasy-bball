"""Draft helper: pick order, Yahoo parsing, and a mock draft against a copy of the real league."""
import pytest

from fbb import draft


def test_snake_and_linear_order():
    assert [draft.slot_of(p, 4, "snake") for p in range(1, 10)] == [1, 2, 3, 4, 4, 3, 2, 1, 1]
    assert [draft.slot_of(p, 4, "linear") for p in range(1, 10)] == [1, 2, 3, 4, 1, 2, 3, 4, 1]


def test_parse_draft_results_skips_empty_future_picks():
    raw = {"fantasy_content": {"league": [{"league_key": "478.l.1"}, {"draft_results": {
        "0": {"draft_result": {"pick": 1, "round": 1, "team_key": "478.l.1.t.3", "player_key": "478.p.10094"}},
        "1": {"draft_result": {"pick": 2, "round": 1, "team_key": "478.l.1.t.5"}},
        "count": 2}}]}}
    assert draft.parse_draft_results(raw) == [
        {"pick": 1, "team_key": "478.l.1.t.3", "player_key": "478.p.10094", "cost": None}]
    pre = {"fantasy_content": {"league": [{}, {"draft_results": []}]}}   # what Yahoo sends before the draft
    assert draft.parse_draft_results(pre) == []


def test_mock_draft_flow(ctx):
    draft.save_settings({"mode": "mock", "num_teams": 4, "rounds": 3, "my_slot": 2})
    b = draft.board(sims=60, seed=1)
    assert b["on_clock"]["pick"] == 1 and not b["on_clock"]["mine"]
    assert b["on_clock"]["picks_until_mine"] == 1 and b["my_upcoming"] == [2, 7, 10]
    assert b["suggestion"] is None and b["board"][0]["p_next"] is not None
    first = b["board"][0]["nba_id"]
    draft.add_pick(first)                        # someone else takes the top player
    b = draft.board(sims=60, seed=1)
    assert b["on_clock"]["mine"] and first not in {r["nba_id"] for r in b["board"]}
    sug = b["suggestion"]
    assert len(sug) == 5 and all(x["p_back"] is not None for x in sug)
    # the lookahead score is VOR now plus the expected best VOR at my next pick
    assert all(abs(x["score"] - x["vor"] - x["next_best"]) < 1 for x in sug)
    draft.add_pick(sug[0]["nba_id"])
    b = draft.board(sims=60, seed=1)
    assert [p["nba_id"] for p in b["my_team"]] == [sug[0]["nba_id"]]
    assert b["picks"][1]["mine"] and not b["picks"][0]["mine"]
    with pytest.raises(ValueError):
        draft.add_pick(first)                    # can't draft someone twice
    draft.undo_pick()
    assert len(draft.picks()) == 1
    draft.reset()
    assert draft.picks() == []


def test_avoided_players_are_never_suggested(ctx):
    draft.save_settings({"mode": "mock", "num_teams": 4, "rounds": 3, "my_slot": 1})
    top = draft.board(sims=40, seed=2)["suggestion"][0]["nba_id"]
    draft.save_settings({"avoid": [top]})
    sug = draft.board(sims=40, seed=2)["suggestion"]
    assert top not in {x["nba_id"] for x in sug}


def test_late_picks_must_fill_open_starting_slots(ctx):
    # 2 rounds for a 10-slot lineup: every pick is needed to fill a starting slot
    draft.save_settings({"mode": "mock", "num_teams": 2, "rounds": 2, "my_slot": 1})
    b = draft.board(sims=20, seed=3)
    assert b["must_fill"]


def test_settings_validation(ctx):
    for bad in ({"mode": "x"}, {"order": "random"}, {"num_teams": 40}, {"nope": 1},
                {"num_teams": 4, "my_slot": 9}, {"randomness": 3}):
        with pytest.raises(ValueError):
            draft.save_settings(bad)
    s = draft.save_settings({"num_teams": None, "rounds": None})
    assert s["sources"]["num_teams"] == "league"


def test_team_order_names_the_picks(ctx):
    keys = list(ctx.teams)[:4]
    draft.save_settings({"mode": "mock", "num_teams": 4, "rounds": 2,
                         "team_order": {k: i + 1 for i, k in enumerate(keys)}})
    b = draft.board(sims=10, seed=1)
    assert b["on_clock"]["team"] == ctx.teams[keys[0]]["name"]
    with pytest.raises(ValueError):
        draft.save_settings({"team_order": {keys[0]: 1, keys[1]: 1}})


def test_other_teams_pick_until_my_turn(ctx):
    draft.save_settings({"mode": "mock", "num_teams": 12, "rounds": 13, "my_slot": 5})
    out = draft.sim_to_my_turn(seed=1)
    assert out == {"simulated": 4, "next_pick": 5}
    b = draft.board(sims=30, seed=1)
    assert b["on_clock"]["mine"] and [p["source"] for p in b["picks"]] == ["sim"] * 4
    draft.add_pick(b["suggestion"][0]["nba_id"])
    out = draft.sim_to_my_turn(seed=1)                 # snake: slot 5 picks again at 20
    assert out == {"simulated": 14, "next_pick": 20}
    assert len({p["nba_id"] for p in draft.picks()}) == 19   # nobody drafted twice
    draft.undo_pick()                                  # back to the moment I chose at pick 5
    ps = draft.picks()
    assert len(ps) == 4 and all(p["source"] == "sim" for p in ps)


def test_simulated_managers_follow_their_ranking(ctx):
    # with no randomness, others take the top of Yahoo's ranking in order, even where it
    # disagrees with the model: that's the behaviour the 'Next pick' odds rest on
    top = list(ctx.values.sort_values("value", ascending=False).index[:40])
    yahoo_order = top[::-1]                              # Yahoo ranks them the opposite way
    draft.db.kv_set("draft_market", {"players": {str(p): {"rank": i + 1} for i, p in enumerate(yahoo_order)}})
    draft.save_settings({"mode": "mock", "num_teams": 12, "rounds": 13, "my_slot": 12, "randomness": 0})
    draft.sim_to_my_turn(seed=1)
    taken = [p["nba_id"] for p in draft.picks()]
    expected = [p for p in yahoo_order if isinstance(ctx.team_of(p), str)][:11]   # free agents can't be drafted
    assert taken[:5] == expected[:5]


def test_live_mode_never_simulates(ctx):
    draft.save_settings({"mode": "live", "my_slot": 3})
    with pytest.raises(ValueError):
        draft.sim_to_my_turn()
    draft.advance()                                    # a no-op outside mock
    assert all(p.get("source") != "sim" for p in draft.picks())


def test_back_to_back_picks_are_certain_to_come_back(ctx):
    # slot 4 of 4 picks 4 and 5 in a row: whoever you pass on at 4 is still there at 5
    draft.save_settings({"mode": "mock", "num_teams": 4, "rounds": 3, "my_slot": 4})
    draft.sim_to_my_turn(seed=1)
    sug = draft.board(sims=20, seed=1)["suggestion"]
    assert all(x["p_back"] == 1.0 for x in sug)
