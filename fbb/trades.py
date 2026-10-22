"""Trade finder: search the league for trades that help you without hurting the other side.

Search: every 1-for-1, 2-for-1 (consolidate two of yours into one better player) and 1-for-2
with each other team. A cheap screen on per-game value throws out trades either side would
obviously refuse; survivors get the full rest-of-season evaluation for BOTH teams (positions,
schedule, depth, roster limits: an extra player means dropping your weakest, a missing one
means adding the best free agent).

Shown: trades where you gain and, by the model, they don't lose ("could plausibly accept").
The model judging the other side isn't the other manager judging it: these are leads to
explore, not guaranteed accepts.
"""
from itertools import combinations

from . import model, recommend, sim

MIN_GAIN = 0.05        # expected wins for you over the rest of the season
MAX_THEIR_LOSS = 0.02  # they shouldn't lose more than this (the model's view of "acceptable")
SCREEN_LIMIT = 600     # full evaluations per search


def _fix_size(ctx, roster, fa_sorted):
    roster = list(roster)
    while len(roster) > ctx.roster_size:
        roster.remove(min(roster, key=lambda p: recommend.ros_value(ctx, p)))
    for p in fa_sorted:
        if len(roster) >= ctx.roster_size:
            break
        if p not in roster:
            roster.append(p)
    return roster


def find(ctx, limit=12, progress=None) -> dict:
    me = ctx.my_key
    v = ctx.values["value"]
    slots = sim.team_slots(ctx)
    tradable = lambda key: [p for p in ctx.rosters.get(key, []) if slots.get(key, {}).get(p) not in ("IL", "IL+")]
    mine = tradable(me)
    rostered = ctx.rostered()
    fa_sorted = recommend.best_free_agents(ctx)
    eff = recommend.effective_weeks(ctx)
    base_me = sim.season_strength(ctx, ctx.rosters[me])

    cands = []
    for other, ids in ctx.rosters.items():
        if other == me or not ids:
            continue
        theirs = tradable(other)
        shapes = [((p,), (q,)) for p in mine for q in theirs]
        shapes += [((p1, p2), (q,)) for p1, p2 in combinations(mine, 2) for q in theirs]
        shapes += [((p,), (q1, q2)) for p in mine for q1, q2 in combinations(theirs, 2)]
        for give, get in shapes:
            g_val, r_val = sum(v.get(p, 0) for p in give), sum(v.get(q, 0) for q in get)
            # screen: per-game value moving each way, with the best single player weighted up
            # (consolidation: one 40 FP/G player is worth more than two 22s in a 10-slot lineup)
            top_in, top_out = max(v.get(q, 0) for q in get), max(v.get(p, 0) for p in give)
            me_cheap = (top_in - top_out) + 0.35 * ((r_val - top_in) - (g_val - top_out))
            them_cheap = -me_cheap
            if me_cheap < -3 or them_cheap < -6:
                continue
            cands.append((abs(me_cheap), other, give, get))
    cands.sort(key=lambda c: c[0])   # closest to even first: those are the ones both sides might take
    cands = cands[:SCREEN_LIMIT]

    base_them = {}
    results = []
    for i, (_, other, give, get) in enumerate(cands):
        if progress and i % 25 == 0:
            progress(i, len(cands))
        if other not in base_them:
            base_them[other] = sim.season_strength(ctx, ctx.rosters[other], exclude_key=other)
        new_me = _fix_size(ctx, [p for p in ctx.rosters[me] if p not in give] + list(get), fa_sorted)
        new_them = _fix_size(ctx, [p for p in ctx.rosters[other] if p not in get] + list(give), fa_sorted)
        d_me = (sim.season_strength(ctx, new_me) - base_me) * eff
        if d_me < MIN_GAIN:
            continue
        d_them = (sim.season_strength(ctx, new_them, exclude_key=other) - base_them[other]) * eff
        if d_them < -MAX_THEIR_LOSS:
            continue
        results.append({"other_key": other, "other_team": ctx.teams.get(other, {}).get("name", other),
                        "give": [_card(ctx, p) for p in give],
                        "get": [_card(ctx, q) for q in get],
                        "shape": f"{len(give)}-for-{len(get)}",
                        "my_dwins": d_me, "their_dwins": d_them,
                        "auto_filled": [ctx.name(p) for p in new_me if p not in ctx.rosters[me] and p not in get],
                        "auto_dropped": [ctx.name(p) for p in ctx.rosters[me] if p not in new_me and p not in give],
                        "stat_shift": _stat_shift(ctx, give, get)})
    if progress:
        progress(len(cands), len(cands))
    # best first; at most 2 per partner so one team doesn't fill the list
    results.sort(key=lambda r: -(r["my_dwins"] + 0.5 * max(r["their_dwins"], 0)))
    shown, per_team = [], {}
    for r in results:
        if per_team.get(r["other_key"], 0) >= 2:
            continue
        per_team[r["other_key"]] = per_team.get(r["other_key"], 0) + 1
        shown.append(r)
        if len(shown) >= limit:
            break
    return {"trades": shown, "evaluated": len(cands), "qualifying": len(results)}


def _card(ctx, p) -> dict:
    inj = ctx.injuries.get(p)
    return {"nba_id": int(p), "name": ctx.name(p), "team": ctx.team_of(p),
            "per_game": float(ctx.values["value"].get(p, 0)),
            "status": inj["status"] if inj else None, "note": (inj or {}).get("comment")}


def _stat_shift(ctx, give, get) -> dict:
    """Per-game fantasy points by stat you gain (+) or lose (-), for the 'why' line."""
    pw = model.point_weights()
    if not pw:
        return {}
    p = ctx.proj
    return {s: float(sum(p.at[q, s] * w for q in get if q in p.index) - sum(p.at[x, s] * w for x in give if x in p.index))
            for s, w in pw.items()}
