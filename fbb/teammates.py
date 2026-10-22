"""Teammate absences: when rotation players are out, everyone else gets more; when the whole
rotation is healthy (or a starter returns), slightly less than their recent averages suggest.

  game factor = 1 + c + b * missing_share
  missing_share = projected minutes of the player's rotation teammates who are out / 240

Live, "out" comes from the injury report (chance he misses that day, from status + timeline),
so a starter cleared to return shrinks missing_share immediately and his teammates scale back.
Backtests use absences that were knowable at the time: players who missed their team's last
game before the simulated week.

Evidence (Oct 2026): c/b fitted independently on 2024-25 and 2025-26 came out nearly identical
(c -0.021 / -0.024, b 0.271 / 0.276); per-game error improved 0.45% on held-out 2025-26 and
0.43% on all of 2024-25. But the weekly matchup backtest got worse (simulator Brier 0.2363 ->
0.2395, formula 0.2289 -> 0.2348), most likely because its stand-in for the injury report
("missed the last game = out all week") is crude. So it is OFF (tm_adjust False). Re-test once
injury_history (archived daily from Oct 2026) covers enough of this season to replay the real
injury report.
"""
import pandas as pd

from . import db, model

ROTATION_MIN = 20.0        # projected minutes to count as a rotation player
DEFAULT_C, DEFAULT_B = -0.0207, 0.271   # fitted on 2024-25 only (keeps the 2025-26 test clean)


def factor(c: float, b: float, share: float) -> float:
    return max(0.7, min(1.4, 1 + c + b * share))


def out_probability(ctx, pid, d) -> float:
    """Chance a player misses day d *because of a listed injury* (the healthy baseline of
    surprise absences is already inside c)."""
    if pid not in ctx.injuries and pid not in ctx.events:
        return 0.0
    a = model.availability(pid, [d], ctx.injuries, ctx.events, {}, set())[0]
    if pid not in ctx.injuries:
        a = a / max(model.HEALTHY_GAME, 1e-6)
    return max(0.0, min(1.0, 1.0 - a))


def missing_share_live(ctx, team, d) -> float:
    cache = ctx._pools.setdefault("_tm_share", {})
    key = (team, d)
    if key not in cache:
        on_team = ctx.players.index[ctx.players["team"] == team]
        total = 0.0
        for p in on_team:
            if p in ctx.proj.index and ctx.proj.at[p, "min"] >= ROTATION_MIN:
                total += out_probability(ctx, p, d) * ctx.proj.at[p, "min"]
        cache[key] = total / 240.0
    return cache[key]


def known_out_share(logs: pd.DataFrame, proj, ws) -> tuple[dict, set]:
    """Backtest stand-in for the injury report: per team, projected minutes of rotation players
    who missed the team's most recent game before `ws` (an ongoing absence), / 240."""
    ws = pd.Timestamp(ws)
    hist = logs[(logs["game_date"] < ws) & (logs["game_date"] >= ws - pd.Timedelta(days=21))]
    out, out_set = {}, set()
    for team, g in hist.groupby("team"):
        last = g["game_date"].max()
        played_last = set(g.loc[g["game_date"] == last, "nba_id"])
        regulars = set(g.loc[g["game_date"] < last, "nba_id"])
        missing = [p for p in regulars - played_last if p in proj.index and proj.at[p, "min"] >= ROTATION_MIN]
        out[team] = sum(proj.at[p, "min"] for p in missing) / 240.0
        out_set.update(missing)
    return out, out_set


def game_factor(ctx, pid, d) -> float:
    """Multiplier for one player's game on day d from teammates' absences (1.0 when off)."""
    if not ctx.params.get("tm_adjust"):
        return 1.0
    team = ctx.team_of(pid)
    if not isinstance(team, str):
        return 1.0
    c = ctx.params.get("tm_c", DEFAULT_C)
    b = ctx.params.get("tm_b", DEFAULT_B)
    fixed = ctx._pools.get("_tm_fixed")       # backtests: one known-out share per team per week
    if fixed is not None:
        share = fixed.get(team, 0.0)
        # his own absence doesn't open minutes for himself
        if pid in ctx.proj.index and ctx._pools.get("_tm_out_set") and pid in ctx._pools["_tm_out_set"]:
            share = max(0.0, share - ctx.proj.at[pid, "min"] / 240.0)
    else:
        share = missing_share_live(ctx, team, d)
        own = out_probability(ctx, pid, d)
        if own > 0 and pid in ctx.proj.index:
            share = max(0.0, share - own * ctx.proj.at[pid, "min"] / 240.0)
    return factor(c, b, share)
