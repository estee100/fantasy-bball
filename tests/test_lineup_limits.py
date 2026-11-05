"""Who can actually score: IL players need a roster spot, and today's lineup locks at tip-off."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from fbb import sim

DAY = "2026-11-04"


def _ctx(slots, roster_size=3, active_slots=2, tips=None, played=()):
    return SimpleNamespace(
        today=None, wtd={"t": {}}, roster_size=roster_size, active_slots=active_slots,
        values={"value": {p: 40 - i for i, p in enumerate(slots)}},
        team_of=lambda p: f"T{p}",
        _team_dates={"_played": set(played), "_loaded": True, **{f"T{p}": {DAY} for p in slots}},
        _pools={"_slots": {"me": slots}, "_tips": tips or {}})


def test_il_player_needs_a_roster_spot(monkeypatch):
    monkeypatch.setattr(sim, "player_games", lambda c, p, d: [(DAY, 0.9)])
    full = _ctx({1: "PG", 2: "SG", 3: "BN", 4: "IL"})
    assert sim.can_score(full, [1, 2, 3, 4], [DAY]) == [1, 2, 3]
    room = _ctx({1: "PG", 2: "SG", 4: "IL", 5: "IL"})
    assert sim.can_score(room, [1, 2, 4, 5], [DAY]) == [1, 2, 4]  # one spot, the better player


def test_backtests_ignore_todays_slots(monkeypatch):
    monkeypatch.setattr(sim, "player_games", lambda c, p, d: [(DAY, 0.9)])
    ctx = _ctx({1: "PG", 2: "SG", 3: "BN", 4: "IL"})
    ctx.today = DAY
    assert sim.can_score(ctx, [1, 2, 3, 4], [DAY]) == [1, 2, 3, 4]


def test_bench_is_locked_once_the_slots_are_used():
    now = datetime.now(timezone.utc)
    started, later = now - timedelta(hours=1), now + timedelta(hours=1)
    tips = {("T1", DAY): started, ("T2", DAY): started, ("T3", DAY): started, ("T4", DAY): later}
    # both starters under way, nothing open: neither bench player can get in
    ctx = _ctx({1: "PG", 2: "SG", 3: "BN", 4: "BN"}, roster_size=4, tips=tips)
    assert sim._locked_out_today(ctx, [1, 2, 3, 4], DAY) == {3, 4}
    # one starter hasn't tipped, so his slot can still go to the waiting bench player
    tips = {("T1", DAY): started, ("T2", DAY): later, ("T4", DAY): later}
    ctx = _ctx({1: "PG", 2: "SG", 4: "BN"}, roster_size=4, tips=tips)
    assert sim._locked_out_today(ctx, [1, 2, 4], DAY) == set()
