"""Roster move recommendations, scored in expected matchup wins.

Every move is valued in one unit — expected extra matchup wins — so a streamer who wins you
this week and a stash who helps for months are directly comparable:

    score = delta P(win this week)                       (Monte Carlo, same random draws)
          + delta P(win a typical week) x effective weeks (analytic, ROS projections)

effective weeks = sum of discount^w over remaining weeks: future edges are worth less because
projections drift and you'll make more moves.
"""
import math

import numpy as np

from . import db, learning, model, sim


def effective_weeks(ctx) -> float:
    d = ctx.params["future_week_discount"]
    return sum(d ** w for w in range(1, ctx.weeks_remaining + 1))


def ros_value(ctx, pid) -> float:
    return float(ctx.values["var"].get(pid, 0.0)) * model.ros_availability(pid, ctx.injuries, ctx.events)


def _phi(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def horizon_edge(ctx, add, drop, dates, kind="upgrade"):
    """The claim behind a move, in value-above-replacement x games:
      stream  -> `add` outproduces `drop` over the rest of this week
      upgrade -> `add` outproduces `drop` over the next EVAL_HORIZON_DAYS
    Returns (expected edge, confidence edge > 0). The learning module checks exactly this
    claim against real box scores, which is what makes the confidence calibratable."""
    v, sd = ctx.values["var"], ctx.values["value_sd"]
    extra_weeks = 0 if kind == "stream" else learning.EVAL_HORIZON_DAYS / 7 - 1
    out = []
    for p in (add, drop):
        g = sum(av for _, av in sim.player_games(ctx, p, dates))
        g += extra_weeks * 3.5 * model.ros_availability(p, ctx.injuries, ctx.events)
        # edge sd = estimate uncertainty (value_sd, shrinks with sample size) combined
        # with game-to-game noise, which dominates over short horizons like a stream
        game_sd = model.game_value_sd(ctx.logs)
        out.append((g * v.get(p, 0.0), math.hypot(g * sd.get(p, 1.0), math.sqrt(max(g, 0)) * game_sd)))
    (ea, sa), (ed, sdd) = out
    edge = ea - ed
    return edge, _phi(edge / max(math.hypot(sa, sdd), 1e-6))


def recommend(ctx: sim.Context, n_sims=3000, top=8, seed=11) -> dict:
    mine = ctx.rosters.get(ctx.my_key, [])
    if not mine:
        return {"moves": [], "base": None}
    dates = sim.remaining_dates(ctx)
    opp = ctx.rosters.get(ctx.opp_key, [])
    wtd_me, wtd_opp = sim.week_to_date(ctx, ctx.my_key), sim.week_to_date(ctx, ctx.opp_key)
    b = sim.simulate_team(ctx, opp, dates, n_sims, seed + 1, wtd_opp)
    base_a = sim.simulate_team(ctx, mine, dates, n_sims, seed, wtd_me)
    base_win, base_cats, _ = sim.score_totals(ctx, base_a, b)
    base_win = sim.calibrated(base_win, ctx.params)
    avg = sim.league_average_dist(ctx)
    base_season = sim.season_strength(ctx, mine, avg)
    eff = effective_weeks(ctx)

    rostered = ctx.rostered()
    with_team = set(ctx.players.index[ctx.players["team"].notna()])
    free = [p for p in ctx.proj.index if p not in rostered and p in with_team and in_rotation(ctx, p)]
    week_games = {p: sum(av for _, av in sim.player_games(ctx, p, dates)) for p in free}
    by_ros = sorted(free, key=lambda p: -ros_value(ctx, p))[:15]
    by_week = sorted(free, key=lambda p: -week_games[p] * max(ctx.values["value"].get(p, 0), 0))[:10]
    adds = list(dict.fromkeys(by_ros + by_week))
    drops = sorted(mine, key=lambda p: ros_value(ctx, p))[:4]
    alerts = active_alerts_by_player()

    cands = []
    for a in adds:
        for d in drops:
            new = [p for p in mine if p != d] + [a]
            A = sim.simulate_team(ctx, new, dates, n_sims, seed, wtd_me)
            win, cats, _ = sim.score_totals(ctx, A, b)
            win = sim.calibrated(win, ctx.params)
            dw = win - base_win
            ds = (sim.season_strength(ctx, new, avg) - base_season) * eff
            kind = "stream" if ds < 0 < dw else "upgrade"
            edge, raw_conf = horizon_edge(ctx, a, d, dates, kind)
            cands.append({"add": a, "drop": d, "kind": kind, "week_dwin": dw, "season_dwins": ds,
                          "score": dw + ds, "edge": edge, "raw_confidence": raw_conf,
                          "cats_after": cats})
    cands.sort(key=lambda m: -m["score"])
    picked, used_adds, drop_count = [], set(), {}
    for m in cands:
        if m["score"] <= 0.01 or m["add"] in used_adds or drop_count.get(m["drop"], 0) >= 3:
            continue
        used_adds.add(m["add"])
        drop_count[m["drop"]] = drop_count.get(m["drop"], 0) + 1
        m["confidence"] = learning.calibrate(m["raw_confidence"])
        m["rationale"] = rationale(ctx, m, base_cats, dates, alerts)
        m["add_name"], m["drop_name"] = ctx.name(m["add"]), ctx.name(m["drop"])
        m["add_team"], m["drop_team"] = ctx.team_of(m["add"]), ctx.team_of(m["drop"])
        m["add_games"] = len(sim.player_games(ctx, m["add"], dates))
        m["drop_games"] = len(sim.player_games(ctx, m["drop"], dates))
        m.pop("cats_after")
        picked.append(m)
        if len(picked) >= top:
            break
    learning.log_suggestions(ctx, picked[:3])
    return {"moves": picked, "base": {"week_win": base_win, "season_strength": base_season,
                                      "effective_weeks": eff, "cat_probs": base_cats}}


def in_rotation(ctx, pid) -> bool:
    """False for a player whose NBA team has played this season but who hasn't appeared,
    with no injury to explain it: waived, out of the rotation, or away from the team.
    (Real case: Kevin Porter Jr. was waived 2026-10-19 but still listed on MIL by the NBA's
    player directory; the model projected him from last season and recommended him.)"""
    if pid in ctx.injuries:
        return True  # absence explained; injury availability handles it
    season = ctx.logs["season"].max()
    cur = ctx.logs[ctx.logs["season"] == season]
    if "_team_games" not in ctx._pools:
        ctx._pools["_team_games"] = cur.groupby("team")["game_date"].nunique().to_dict()
        ctx._pools["_player_games"] = cur.groupby("nba_id").size().to_dict()
    team_games = ctx._pools["_team_games"].get(ctx.team_of(pid), 0)
    return team_games == 0 or ctx._pools["_player_games"].get(pid, 0) > 0


def rationale(ctx, m, base_cats, dates, alerts) -> list:
    a, d = m["add"], m["drop"]
    out = []
    ga, gd = len(sim.player_games(ctx, a, dates)), len(sim.player_games(ctx, d, dates))
    if ga != gd:
        out.append(f"{ga} games left this week vs {gd}")
    if model.scoring_mode() == "points":
        dv = ctx.values["value"].get(a, 0) - ctx.values["value"].get(d, 0)
        out.append(f"{dv:+.1f} fantasy pts per game ({ctx.values['value'].get(a, 0):.1f} vs "
                   f"{ctx.values['value'].get(d, 0):.1f})")
    if model.scoring_mode() == "categories":
        close = [k for k, p in base_cats.items() if 0.2 <= p <= 0.8]
        diffs = {k: ctx.values[f"z_{k}"].get(a, 0) - ctx.values[f"z_{k}"].get(d, 0) for k in ctx.cats}
        helps = [k for k in sorted(close, key=lambda k: -diffs[k]) if diffs[k] > 0.4][:3]
        hurts = [k for k in sorted(close, key=lambda k: diffs[k]) if diffs[k] < -0.4][:2]
        label = lambda k: model.config.CATEGORY_DEFS[k][0]
        if helps:
            out.append("Boosts swing categories: " + ", ".join(map(label, helps)))
        if hurts:
            out.append("Costs some " + ", ".join(map(label, hurts)))
    inj = ctx.injuries.get(d)
    if inj:
        out.append(f"{ctx.name(d)} is {inj['status']}")
    inj_a = ctx.injuries.get(a)
    if inj_a:  # a stash: worth knowing before you spend a roster spot on him
        g = model._games_out(a, ctx.injuries, ctx.events)
        out.append(f"{ctx.name(a)} is {inj_a['status']}" + (f", ~{g:.0f} games out" if g else "")
                   + f": {(inj_a['comment'] or '')[:90]}")
    for pid in (a, d):
        for al in alerts.get(pid, [])[:1]:
            out.append(al)
        ev = ctx.events.get(pid)
        if ev and ev.get("takeaway"):
            out.append(f"News ({ctx.name(pid)}): {ev['takeaway']}")
    return out


def active_alerts_by_player() -> dict:
    with db.cursor() as c:
        rows = c.execute("SELECT nba_id, title FROM alerts WHERE active=1 ORDER BY severity DESC").fetchall()
    out = {}
    for r in rows:
        out.setdefault(r["nba_id"], []).append(r["title"])
    return out


def start_sit_today(ctx) -> list:
    """If more of your players have a game today than you have active slots, who should sit."""
    from datetime import date
    today = date.today().isoformat()
    mine = ctx.rosters.get(ctx.my_key, [])
    playing = [p for p in mine if any(d == today for d, _ in sim.player_games(ctx, p, [today]))]
    if len(playing) <= ctx.active_slots:
        return []
    ranked = sorted(playing, key=lambda p: -ctx.values["value"].get(p, -99) *
                    (dict(sim.player_games(ctx, p, [today])).get(today, 0)))
    return [{"nba_id": p, "name": ctx.name(p), "value": float(ctx.values["value"].get(p, 0))}
            for p in ranked[ctx.active_slots:]]


def evaluate_trade(ctx, give: list, get: list) -> dict:
    """Effect of a trade on both sides, in expected matchup wins over the rest of the season."""
    mine = ctx.rosters.get(ctx.my_key, [])
    not_mine = [p for p in give if p not in mine]
    if not_mine:
        raise ValueError("You don't own: " + ", ".join(ctx.name(p) for p in not_mine))
    owners = {k for k, ids in ctx.rosters.items() for p in get if p in ids}
    if ctx.my_key in owners:
        raise ValueError("You already own someone on the 'get' side")
    if len(owners) > 1:
        raise ValueError("Players on the 'get' side belong to different teams")
    other_key = next(iter(owners), None)   # None = they're free agents
    theirs = ctx.rosters.get(other_key, [])
    rostered = ctx.rostered()
    fa = sorted([p for p in ctx.proj.index if p not in rostered and p in ctx.players.index],
                key=lambda p: -ros_value(ctx, p))

    def fix_size(r):
        r = list(r)
        while len(r) > ctx.roster_size:
            r.remove(min(r, key=lambda p: ros_value(ctx, p)))
        for p in fa:
            if len(r) >= ctx.roster_size:
                break
            if p not in r:
                r.append(p)
        return r

    new_mine = fix_size([p for p in mine if p not in give] + list(get))
    new_theirs = fix_size([p for p in theirs if p not in get] + list(give))
    eff = effective_weeks(ctx)
    avg = sim.league_average_dist(ctx)
    me = (sim.season_strength(ctx, new_mine, avg) - sim.season_strength(ctx, mine, avg)) * eff
    them = (sim.season_strength(ctx, new_theirs, avg) - sim.season_strength(ctx, theirs, avg)) * eff \
        if theirs else None
    cat_shift = {}
    if model.scoring_mode() == "categories":
        for k in ctx.cats:
            z = ctx.values[f"z_{k}"]
            cat_shift[k] = float(sum(z.get(p, 0) for p in get) - sum(z.get(p, 0) for p in give))
    return {"my_dwins": me, "their_dwins": them, "other_team": other_key,
            "cat_shift": cat_shift,
            "verdict": "accept" if me > 0.05 else ("decline" if me < -0.05 else "roughly even"),
            "auto_filled": [ctx.name(p) for p in new_mine if p not in mine and p not in get]}
