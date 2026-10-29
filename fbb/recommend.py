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


SCREEN_BY_ROS = 30    # free agents screened by rest-of-season value
SCREEN_BY_WEEK = 15   # ... and by value of this week's games
SCREEN_DROPS = 4      # your weakest players considered as drops in the automatic search


def baseline(ctx: sim.Context, n_sims=3000, seed=11) -> dict:
    """Everything a swap is compared against, computed once: your roster as it stands."""
    mine = ctx.rosters.get(ctx.my_key, [])
    dates = sim.remaining_dates(ctx)
    opp = ctx.rosters.get(ctx.opp_key, [])
    wtd_me, wtd_opp = sim.week_to_date(ctx, ctx.my_key), sim.week_to_date(ctx, ctx.opp_key)
    b = sim.simulate_team(ctx, opp, dates, n_sims, seed + 1, wtd_opp)
    base_a = sim.simulate_team(ctx, mine, dates, n_sims, seed, wtd_me)
    base_win, base_cats, _ = sim.score_totals(ctx, base_a, b)
    return {"mine": mine, "dates": dates, "opp": opp, "wtd_me": wtd_me, "b": b,
            "base_win": sim.calibrated(base_win, ctx.params), "base_cats": base_cats,
            "base_season": sim.season_strength(ctx, mine), "eff": effective_weeks(ctx),
            "alerts": active_alerts_by_player(), "n_sims": n_sims, "seed": seed}


def score_swap(ctx, B, a, d) -> dict:
    """Add `a`, drop `d`: this week's win-probability change (same simulated games for everyone
    else) + rest-of-season expected wins. The one scoring path every suggestion goes through."""
    new = [p for p in B["mine"] if p != d] + [a]
    A = sim.simulate_team(ctx, new, B["dates"], B["n_sims"], B["seed"], B["wtd_me"])
    win, cats, _ = sim.score_totals(ctx, A, B["b"])
    dw = sim.calibrated(win, ctx.params) - B["base_win"]
    ds = (sim.season_strength(ctx, new) - B["base_season"]) * B["eff"]
    kind = "stream" if ds < 0 < dw else "upgrade"
    edge, raw_conf = horizon_edge(ctx, a, d, B["dates"], kind)
    return {"add": a, "drop": d, "kind": kind, "week_dwin": dw, "season_dwins": ds,
            "score": dw + ds, "edge": edge, "raw_confidence": raw_conf}


def describe(ctx, B, m, with_urgency=True) -> dict:
    """Confidence, plain-language reasons, names, games left, urgency."""
    dates = B["dates"]
    m["confidence"] = learning.calibrate(m["raw_confidence"])
    m["rationale"] = rationale(ctx, m, B["base_cats"], dates, B["alerts"])
    m["add_name"], m["drop_name"] = ctx.name(m["add"]), ctx.name(m["drop"])
    m["add_team"], m["drop_team"] = ctx.team_of(m["add"]), ctx.team_of(m["drop"])
    m["add_games"] = len(sim.player_games(ctx, m["add"], dates))
    m["drop_games"] = len(sim.player_games(ctx, m["drop"], dates))
    plan = injury_plan(ctx, m["drop"])
    g = model.expected_games_out(m["drop"], ctx.injuries, ctx.events)
    if plan == "il":
        m["rationale"].insert(0, f"{m['drop_name']} is out ~{g:.0f} games: if you have a free IL slot, move him there "
                                 f"and add {m['add_name']} without dropping anyone")
    elif plan == "bench":
        m["rationale"].insert(0, f"{m['drop_name']} is listed Out but only short-term"
                                 + (f" (~{g:.0f} game{'s' if g >= 1.5 else ''})" if g else "")
                                 + ": benching him for that is usually better than cutting him")
    elif plan == "drop_ok":
        m["rationale"].insert(0, f"{m['drop_name']} looks out for the season: cutting him keeps your IL slot free")
    from . import free_agents
    av = free_agents.availability(m["add"])
    m["availability"] = av
    if av["status"] == "W":
        m["rationale"].insert(0, f"On waivers until {av['until']}: this is a waiver claim, not an instant add")
    if with_urgency:
        m.update(urgency(ctx, m, B["mine"], B["opp"], dates, B["b"], B["wtd_me"], B["n_sims"], B["seed"]))
    return m


IL_SLOTS = 2   # without a Yahoo sync (demo league)


def il_slots() -> int:
    """The league's IL slots from the Yahoo sync (Gordon's League has 3)."""
    return db.kv_get("il_slots") or IL_SLOTS

# IL is for absences long enough to be worth an add now and a drop when he's back (~a week).
# A game or two: bench him. Out for the season: cutting him keeps the IL slot for later.
IL_MIN_GAMES = 3
SEASON_OUT_SHARE = 0.8


def injury_plan(ctx, pid):
    """'il', 'bench' (short absence), 'drop_ok' (out for the season) or None (not listed Out)."""
    if ((ctx.injuries.get(pid) or {}).get("status") or "").lower() != "out":
        return None
    g = model.expected_games_out(pid, ctx.injuries, ctx.events)
    if g is not None and g >= SEASON_OUT_SHARE * max(model.season_games_left(), 1.0):
        return "drop_ok"
    return "il" if g is not None and g >= IL_MIN_GAMES else "bench"


RECENT_DROP_DAYS = 7


def _recent_drops(ctx) -> set:
    from datetime import date, timedelta
    since = ((date.fromisoformat(ctx.today) if ctx.today else date.today())
             - timedelta(days=RECENT_DROP_DAYS)).isoformat()
    with db.cursor() as c:
        return {r["nba_id"] for r in c.execute(
            "SELECT nba_id FROM league_moves WHERE team_key=? AND kind='drop' AND ts>=?", (ctx.my_key, since))}


def recommend(ctx: sim.Context, n_sims=3000, top=8, seed=11, log=True, budget=None) -> dict:
    """log=False / budget: used by the model's own team (fbb/shadow.py), which must not write
    to your suggestion ledger or count your adds."""
    mine = ctx.rosters.get(ctx.my_key, [])
    if not mine:
        return {"moves": [], "base": None}
    B = baseline(ctx, n_sims, seed)
    dates = B["dates"]
    rostered = ctx.rostered()
    with_team = set(ctx.players.index[ctx.players["team"].notna()])
    free = [p for p in ctx.proj.index if p not in rostered and p in with_team and in_rotation(ctx, p)]
    week_games = {p: sum(av for _, av in sim.player_games(ctx, p, dates)) for p in free}
    by_ros = sorted(free, key=lambda p: -ros_value(ctx, p))[:SCREEN_BY_ROS]
    by_week = sorted(free, key=lambda p: -week_games[p] * max(ctx.values["value"].get(p, 0), 0))[:SCREEN_BY_WEEK]
    # players on Yahoo's waiver list are always considered: a claim may be the only way to get them
    from . import free_agents
    listed = [p for p in free_agents.listed_ids() if p in ctx.proj.index and p not in rostered
              and isinstance(ctx.team_of(p), str) and in_rotation(ctx, p)]
    # Not players you cut in the last week: flipping back on a small projected edge just burns
    # adds (Oct 2026: Fears suggested back the day he was dropped, at +0.03 wins).
    recent = _recent_drops(ctx)
    adds = [p for p in dict.fromkeys(by_ros + by_week + listed) if p not in recent]
    # Players in IL slots don't count toward the roster limit: dropping one opens nothing,
    # and having one there may leave an open spot (an add with no drop at all).
    from . import lineup
    slots = sim.team_slots(ctx).get(ctx.my_key, {})
    il = {p for p in mine if slots.get(p) in lineup.NOT_STARTABLE}
    # A benched player listed Out with an IL slot free should go to IL, not be cut: that's a
    # free roster spot. Without this, Duren (out ~2 weeks, Oct 2026) kept showing as the drop.
    to_il = [p for p in sorted(mine, key=lambda p: -ros_value(ctx, p))
             if p not in il and injury_plan(ctx, p) == "il"][:max(0, il_slots() - max(len(il), getattr(ctx, "il_used", 0)))]
    il |= set(to_il)
    drops = sorted((p for p in mine if p not in il), key=lambda p: ros_value(ctx, p))[:SCREEN_DROPS]
    open_spots = max(0, ctx.roster_size - (len(mine) - len(il)))
    open_spot = None
    if open_spots:
        best = sorted((score_swap(ctx, B, a, None) for a in adds), key=lambda m: -m["score"])[:3]
        from . import free_agents
        open_spot = {"spots": open_spots,
                     "move_to_il": [{"nba_id": int(p), "name": ctx.name(p)} for p in to_il], "adds": [
            {"add": int(m["add"]), "add_name": ctx.name(m["add"]), "add_team": ctx.team_of(m["add"]),
             "score": m["score"], "availability": free_agents.availability(m["add"])} for m in best]}

    cands = sorted((score_swap(ctx, B, a, d) for a in adds for d in drops), key=lambda m: -m["score"])
    picked, used_adds, drop_count = [], set(), {}
    for m in cands:
        if m["score"] <= 0.01 or m["add"] in used_adds or drop_count.get(m["drop"], 0) >= 3:
            continue
        used_adds.add(m["add"])
        drop_count[m["drop"]] = drop_count.get(m["drop"], 0) + 1
        picked.append(describe(ctx, B, m))
        if len(picked) >= top:
            break
    # weekly add limit: at the limit nothing is actionable; with one left, only the best move
    from . import history
    budget = budget if budget is not None else history.add_budget(ctx)
    if budget["left"] is not None:
        for i, m in enumerate(picked):
            if budget["left"] == 0:
                m["urgency"], m["urgency_reason"] = "No adds left", f"you've used all {budget['limit']} adds this week"
            elif budget["left"] == 1 and i > 0 and m.get("urgency") in ("Do today", "Worth doing"):
                m["urgency"], m["urgency_reason"] = "Marginal", "only one add left this week: save it for the top move"
    if log:
        learning.log_suggestions(ctx, picked[:3])
    return {"moves": picked, "add_budget": budget, "open_spot": open_spot,
            "screened": {"free_agents": len(adds), "drops": len(drops), "swaps": len(cands)},
            "base": {"week_win": B["base_win"], "season_strength": B["base_season"],
                     "effective_weeks": B["eff"], "cat_probs": B["base_cats"]}}


def evaluate_add(ctx, pid, n_sims=3000, seed=11) -> dict:
    """Should I pick up this specific player, and for whom? Scores him against every player
    you could drop (not just your weakest four), with the same engine as the suggestions."""
    owner = next((k for k, ids in ctx.rosters.items() if pid in ids), None)
    out = {"nba_id": int(pid), "name": ctx.name(pid), "team": ctx.team_of(pid),
           "owner": ctx.teams.get(owner, {}).get("name") if owner else None,
           "injury": ctx.injuries.get(pid), "in_rotation": in_rotation(ctx, pid),
           "per_game": float(ctx.values["value"].get(pid, 0)), "warnings": []}
    if owner == ctx.my_key:
        out["warnings"].append("He's already on your team.")
        return dict(out, options=[], verdict="already yours")
    if owner:
        out["warnings"].append(f"He's on {out['owner']}: you'd need a trade (use the Trade lab).")
    if not isinstance(ctx.team_of(pid), str):  # missing team comes back as None or NaN
        out["warnings"].append("He isn't on an NBA roster right now (waived, unsigned or retired).")
    elif not out["in_rotation"]:
        out["warnings"].append("His team has played without him and there's no injury listed: he may be out of the rotation.")
    from . import free_agents
    av = free_agents.availability(pid)
    out["availability"] = av
    if not owner and av["status"] == "W":
        out["warnings"].append(f"On waivers until {av['until']}: a waiver claim, not an instant add.")
    if out["injury"]:
        g = model._games_out(pid, ctx.injuries, ctx.events)
        out["warnings"].append(f"Injury: {out['injury']['status']}" + (f", ~{g:.0f} games out" if g else "")
                               + f". {out['injury']['comment'] or ''}")
    B = baseline(ctx, n_sims, seed)
    slots = sim.team_slots(ctx).get(ctx.my_key, {})
    droppable = [d for d in B["mine"] if slots.get(d) not in ("IL", "IL+")]
    opts = sorted((score_swap(ctx, B, pid, d) for d in droppable), key=lambda m: -m["score"])
    for i, m in enumerate(opts):
        describe(ctx, B, m, with_urgency=(i == 0))  # urgency is a few extra sims: best option only
    best = opts[0] if opts else None
    if owner:
        verdict = "owned"
    elif best is None or best["score"] <= 0.01:
        verdict = "not worth it"
    else:
        verdict = best.get("urgency", "Marginal")
    return dict(out, options=opts, verdict=verdict)


URGENT_WAIT_COST = 0.02   # wins lost by waiting a day; ~2pp is just above simulation noise


def urgency(ctx, m, mine, opp, dates, b, wtd_me, n_sims, seed) -> dict:
    """Is there a reason to do this today rather than whenever?

    Two measurable reasons:
      waiting costs: simulate making the move tomorrow instead (today's games come from your
        current roster) and compare this week's win chance; plus the season gain lost per day
      it may not last: fresh positive news or a role-growth alert on the player you'd add
    Labels: 'Do today', 'Worth doing' (clear gain, no rush), 'Marginal' (small or uncertain)."""
    from datetime import date
    today = (ctx.today or date.today().isoformat())
    a, d = m["add"], m["drop"]
    new = [p for p in mine if p != d] + [a]
    wait_cost = 0.0
    if dates and dates[0] == today and len(dates) > 1:
        if any(dd == today for dd, _ in sim.player_games(ctx, a, [today])):
            first = sim.simulate_team(ctx, mine, dates[:1], n_sims, seed, wtd_me)
            rest = sim.simulate_team(ctx, new, dates[1:], n_sims, seed + 7)
            win_tmrw, _, _ = sim.score_totals(ctx, first + rest, b)
            win_tmrw = sim.calibrated(win_tmrw, ctx.params)
            # same split (today | rest) and seeds as the "tomorrow" run, so only the swap differs
            full = sim.simulate_team(ctx, new, dates[:1], n_sims, seed, wtd_me) + rest
            win_now, _, _ = sim.score_totals(ctx, full, b)
            win_now = sim.calibrated(win_now, ctx.params)
            wait_cost += max(0.0, win_now - win_tmrw)
    days_left = max(1.0, effective_weeks(ctx) * 7)
    wait_cost += max(0.0, m["season_dwins"]) / days_left
    hot = []
    ev = ctx.events.get(a)
    if ev and ev.get("direction", 0) > 0:
        hot.append(f"news: {ev.get('takeaway') or ev['event_type']}")
    with db.cursor() as c:
        al = c.execute("SELECT title FROM alerts WHERE nba_id=? AND kind='role_expansion' AND active=1", (a,)).fetchone()
    if al:
        hot.append(al["title"])
    score, conf = m["score"], m["confidence"]
    from . import free_agents
    av = free_agents.availability(a)
    if av["status"] == "W" and score > 0.1:
        return {"urgency": "Waiver claim", "wait_cost": wait_cost,
                "urgency_reason": f"he's on waivers until {av['until']}: put in a claim now, he can't be added before then"}
    if (wait_cost >= URGENT_WAIT_COST and score > 0.1) or (hot and score >= 0.2 and conf >= 0.6):
        label = "Do today"
        why = []
        if wait_cost >= URGENT_WAIT_COST:
            why.append(f"waiting a day costs ~{wait_cost * 100:.1f}% of a win (he plays today)")
        if hot:
            why.append("he may not last on waivers (" + hot[0][:80] + ")")
        reason = "; ".join(why)
    elif score >= 0.25 and conf >= 0.65:
        label, reason = "Worth doing", "clear gain, but it's spread over the season: no rush"
    else:
        label, reason = "Marginal", "small or uncertain gain: fine to skip or wait"
    return {"urgency": label, "urgency_reason": reason, "wait_cost": wait_cost}


def best_free_agents(ctx, n=20) -> list:
    """Free agents who can actually play for you (on an NBA roster, in the rotation), best first.
    The one list every roster-filling step uses: an unfiltered list once filled a trade's open
    spot with a waived player (Kevin Porter Jr.) and flipped a +0.61 trade to -0.15."""
    if "_best_fa" not in ctx._pools:
        rostered = ctx.rostered()
        ctx._pools["_best_fa"] = sorted(
            [p for p in ctx.proj.index if p not in rostered and isinstance(ctx.team_of(p), str) and in_rotation(ctx, p)],
            key=lambda p: -ros_value(ctx, p))[:max(n, 20)]
    return ctx._pools["_best_fa"][:n]


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
    fa = best_free_agents(ctx)

    from . import lineup
    all_slots = sim.team_slots(ctx)

    def fix_size(r, key):
        """Cut the weakest / fill from free agents to the roster limit. IL players don't count
        toward it. Both sides of the comparison go through this, before and after: comparing a
        resized roster with the raw one (IL players counted, so 1-2 cut) made a trade that
        changes nothing score -0.85 wins, and every real trade looked bad (Oct 2026)."""
        il = [p for p in r if all_slots.get(key, {}).get(p) in lineup.NOT_STARTABLE]
        r = [p for p in r if p not in il]
        while len(r) > ctx.roster_size:
            r.remove(min(r, key=lambda p: ros_value(ctx, p)))
        for p in fa:
            if len(r) >= ctx.roster_size:
                break
            if p not in r:
                r.append(p)
        return r + il

    new_mine = fix_size([p for p in mine if p not in give] + list(get), ctx.my_key)
    new_theirs = fix_size([p for p in theirs if p not in get] + list(give), other_key)
    eff = effective_weeks(ctx)
    base_mine = fix_size(mine, ctx.my_key)
    me = (sim.season_strength(ctx, new_mine) - sim.season_strength(ctx, base_mine)) * eff
    them = (sim.season_strength(ctx, new_theirs, exclude_key=other_key)
            - sim.season_strength(ctx, fix_size(theirs, other_key), exclude_key=other_key)) * eff if theirs else None
    cat_shift = {}
    if model.scoring_mode() == "categories":
        for k in ctx.cats:
            z = ctx.values[f"z_{k}"]
            cat_shift[k] = float(sum(z.get(p, 0) for p in get) - sum(z.get(p, 0) for p in give))
    return {"my_dwins": me, "their_dwins": them, "other_team": other_key,
            "cat_shift": cat_shift,
            "verdict": "accept" if me > 0.05 else ("decline" if me < -0.05 else "roughly even"),
            "auto_filled": [ctx.name(p) for p in new_mine if p not in base_mine and p not in get],
            "auto_cut": [ctx.name(p) for p in base_mine + list(get) if p not in new_mine and p not in give]}
