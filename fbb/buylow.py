"""Buy low / sell high: players whose fantasy points per game, as managers see them, are far
from the model's projection.

"As managers see them" is recommend.seen_value: this season's average steadied by last
season's (8 games' worth). The projection counts last season's per-minute production as ~60
games (tuned by backtest) but last season's minutes as almost nothing. So the gap has two
parts, and only one is a trade signal:

    rate gap    = projected minutes x (projected pts/min - seen pts/min)
                  a cold (or hot) start per minute played: the model expects it to even out
    minutes gap = (projected minutes - seen minutes) x seen pts/min
                  a role change: the model already believes it, and so should you

Lists are ranked on the rate gap only. A rate gap the model expects to close is still a bet on
last season repeating: age, a new team or an injury he's playing through can make the slow
start real, so each player carries his active role alerts and injury status.

Players whose minutes moved a lot since last season are left out: last season's per-minute
production came from a different role (8 bench minutes don't scale to 20), so their "rate gap"
is the projection's blind spot, not a bounce back. Role alerts cover them.
"""
import pandas as pd

from . import config, model, recommend

MIN_CUR_GAMES = 4     # fewer games this season and nobody has formed a view yet
MIN_PREV_GAMES = 20   # a prior season this short is too thin to bet on a bounce back
MAX_MIN_CHANGE = 6.0  # minutes/game, this season vs last: beyond this it's a new role
MIN_GAP = 2.0         # fantasy pts/game from the rate gap alone, to be listed
LIMIT = 10


def _seen(g: pd.DataFrame, cols) -> pd.DataFrame:
    """Per-game `cols` as managers see them (same blend as recommend.seen_value), plus raw
    this-season / last-season numbers for display."""
    cur = g[g["season"] == config.SEASON]
    prev = g[g["season"] < config.SEASON]
    # "last season" per player = his most recent earlier season (the one seen_value uses)
    prev = prev[prev["season"] == prev.groupby("nba_id")["season"].transform("max")]
    c_sum, c_n = cur.groupby("nba_id")[cols].sum(), cur.groupby("nba_id").size()
    p_mean, p_n = prev.groupby("nba_id")[cols].mean(), prev.groupby("nba_id").size()
    ids = c_n.index.intersection(p_n.index)
    k = recommend.SEEN_PRIOR_GAMES
    n = c_n[ids]
    seen = c_sum.loc[ids].add(p_mean.loc[ids] * k).div(n + k, axis=0)
    out = seen.add_prefix("seen_")
    out["cur_games"], out["prev_games"] = n, p_n[ids]
    for s in ("fp", "min", "fgm", "fga"):
        out[f"cur_{s}"] = c_sum.loc[ids, s] / n
        out[f"prev_{s}"] = p_mean.loc[ids, s]
    return out


def scan(ctx) -> dict:
    if model.scoring_mode() != "points":
        return {"available": False, "reason": "Only for points leagues so far: in a category league "
                                              "there's no single \"visible\" number to compare against."}
    w = model.point_weights()
    stats = [s for s in w if s in ctx.logs.columns]
    logs = ctx.logs.assign(fp=sum(ctx.logs[s] * w[s] for s in stats))
    seen = _seen(logs, ["fp", "min", "fgm", "fga"] + stats)
    seen = seen[(seen["cur_games"] >= MIN_CUR_GAMES) & (seen["prev_games"] >= MIN_PREV_GAMES)
                & (seen["seen_min"] > 0)
                & ((seen["cur_min"] - seen["prev_min"]).abs() <= MAX_MIN_CHANGE)]
    ids = seen.index.intersection(ctx.proj.index).intersection(ctx.values.index)
    seen, proj = seen.loc[ids], ctx.proj.loc[ids]
    proj_fp, proj_min = ctx.values.loc[ids, "value"], proj["min"]
    seen_rate = seen["seen_fp"] / seen["seen_min"]
    rate_gap = proj_fp - proj_min * seen_rate
    min_gap = (proj_min - seen["seen_min"]) * seen_rate
    # the rate gap split by stat (sums to rate_gap): what the model expects to come back
    drivers = pd.DataFrame({s: w[s] * (proj[s] - proj_min * seen[f"seen_{s}"] / seen["seen_min"])
                            for s in stats})

    owner = {p: k for k, r in ctx.rosters.items() for p in r}
    alerts = _active_alerts()
    from . import sim
    my_slots = sim.team_slots(ctx).get(ctx.my_key, {})
    mine_active = [p for p in ctx.rosters.get(ctx.my_key, []) if my_slots.get(p) not in ("IL", "IL+")]
    # a free agent only matters if he'd beat someone you roster
    fa_floor = min((float(ctx.values["value"].get(p, 0)) for p in mine_active), default=0.0)

    def row(p):
        s, inj = seen.loc[p], ctx.injuries.get(p)
        d = drivers.loc[p]
        top = d.reindex(d.abs().sort_values(ascending=False).index)[:2]
        return {"nba_id": int(p), "name": ctx.name(p), "team": ctx.team_of(p),
                "owner": "mine" if owner.get(p) == ctx.my_key else
                         (ctx.teams.get(owner[p], {}).get("name") if p in owner else "free agent"),
                "proj": float(proj_fp[p]), "seen": float(s["seen_fp"]),
                "rate_gap": float(rate_gap[p]), "min_gap": float(min_gap[p]),
                "drivers": [{"stat": k, "fp": float(v)} for k, v in top.items() if abs(v) >= 0.5],
                "this_season": {"games": int(s["cur_games"]), "fp": float(s["cur_fp"]), "min": float(s["cur_min"]),
                                "fg_pct": float(s["cur_fgm"] / s["cur_fga"]) if s["cur_fga"] else None},
                "last_season": {"games": int(s["prev_games"]), "fp": float(s["prev_fp"]), "min": float(s["prev_min"]),
                                "fg_pct": float(s["prev_fgm"] / s["prev_fga"]) if s["prev_fga"] else None},
                "injury": {"status": inj["status"], "comment": inj["comment"]} if inj else None,
                "alerts": alerts.get(p, [])}

    def pick(cond, desc=True):
        sel = [p for p in ids if cond(p)]
        return [row(p) for p in sorted(sel, key=lambda p: -rate_gap[p] if desc else rate_gap[p])[:LIMIT]]

    return {
        "available": True,
        "min_gap": MIN_GAP,
        # on other teams, playing below the projection: trade for them
        "buy_low": pick(lambda p: p in owner and owner[p] != ctx.my_key and rate_gap[p] >= MIN_GAP),
        # yours, playing above it: their manager sees more than the model expects
        "sell_high": pick(lambda p: owner.get(p) == ctx.my_key and rate_gap[p] <= -MIN_GAP, desc=False),
        # yours, playing below it: selling now sells at the bottom
        "hold": pick(lambda p: owner.get(p) == ctx.my_key and rate_gap[p] >= MIN_GAP),
        "pickups": pick(lambda p: p not in owner and rate_gap[p] >= MIN_GAP and proj_fp[p] > fa_floor),
    }


def _active_alerts() -> dict:
    from . import db
    out = {}
    with db.cursor() as c:
        for r in c.execute("SELECT nba_id, kind, title FROM alerts WHERE active=1 ORDER BY severity DESC"):
            out.setdefault(r["nba_id"], []).append({"kind": r["kind"], "title": r["title"]})
    return out
