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
])
def test_injury_timelines(note, lo, hi):
    g = model.injury_games_out(note, "Out", TODAY)
    assert g is not None and lo <= g <= hi


def test_start_of_season_is_not_season_ending():
    note = "Ingram won't be ready for the start of the season due to a partially torn plantar fascia"
    assert not model.SEASON_ENDING.search(note.lower())
    assert model.injury_games_out(note, "Out", TODAY) is None


# ---------------------------------------------------------------- Yahoo parsing

def test_parse_team_stats_reads_fraction_stats():
    raw = {"fantasy_content": {"team": [[{"team_key": "x"}], {"team_stats": {"stats": [
        {"stat": {"stat_id": "9004003", "value": "120/250"}}, {"stat": {"stat_id": "5", "value": ".480"}},
        {"stat": {"stat_id": "12", "value": "330"}}, {"stat": {"stat_id": "17", "value": ""}}]}}]}}
    out = yahoo.parse_team_stats(raw)
    assert out == {"fgm": 120.0, "fga": 250.0, "pts": 330.0}


# ---------------------------------------------------------------- calibration math

def test_temperature_pulls_toward_half():
    assert backtest.apply_temperature(0.8, 1.0) == pytest.approx(0.8)
    assert 0.5 < backtest.apply_temperature(0.8, 2.0) < 0.8
    assert backtest.apply_temperature(0.5, 3.0) == pytest.approx(0.5)


def test_poisson_binomial_win():
    assert model.poisson_binomial_win([0.5, 0.5]) == pytest.approx(0.5)
    assert model.poisson_binomial_win([1.0, 1.0, 0.0]) == pytest.approx(1.0)
