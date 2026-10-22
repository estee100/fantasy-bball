"""Daily lineup optimizer with real position eligibility.

For each day: every player with a game gets an expected value (projection x chance he plays
that day). The solver picks the legal lineup with the highest total, exactly: dynamic
programming over players with a bitmask of filled slots (10 slots -> 1,024 states), so
there's no greedy shortcut that strands a center in a Util spot while a C slot sits empty.

Maximizing expected points is the right objective in a points league: a week's total is a
sum, so each day's expected points add up.
"""
from datetime import date

from . import db, model, sim

# slot -> base positions that can fill it
SLOT_ELIG = {
    "PG": {"PG"}, "SG": {"SG"}, "G": {"PG", "SG"}, "SF": {"SF"}, "PF": {"PF"}, "F": {"SF", "PF"},
    "C": {"C"}, "Util": {"PG", "SG", "SF", "PF", "C", "?"},
}
DEFAULT_SLOTS = ["PG", "SG", "G", "SF", "PF", "F", "C", "C", "Util", "Util"]
NOT_STARTABLE = {"IL", "IL+", "NA"}
NOT_STARTING = {"BN", "?", None} | NOT_STARTABLE   # slots that don't score today
_EXPAND = {"G": {"PG", "SG"}, "F": {"SF", "PF"}, "GF": {"SG", "SF"}, "FC": {"PF", "C"}}


def slot_list() -> list:
    return db.kv_get("slot_list") or DEFAULT_SLOTS


def parse_positions(text: str | None) -> set:
    out = set()
    if not isinstance(text, str):  # blank in the database comes back as None or NaN
        return out
    for tok in text.replace("/", ",").replace("-", ",").split(","):
        t = tok.strip().upper()
        if t in ("PG", "SG", "SF", "PF", "C"):
            out.add(t)
        elif t in _EXPAND:
            out |= _EXPAND[t]
    return out


# ESPN lists one position; Yahoo usually grants neighbours too (a "C" is often PF,C).
# Widening ESPN's label keeps players without Yahoo data from being artificially rigid.
ESPN_WIDEN = {"PG": {"PG", "SG"}, "SG": {"PG", "SG", "SF"}, "SF": {"SG", "SF", "PF"},
              "PF": {"SF", "PF", "C"}, "C": {"PF", "C"}}


def positions(ctx, pid, mode="widened") -> tuple[set, str]:
    """(eligible base positions, source). Yahoo/paste eligibility first, ESPN's coarse
    position as a fallback (widened unless mode='strict'); unknown players fill Util only."""
    row = ctx.players.loc[pid] if pid in ctx.players.index else None
    if row is not None:
        p = parse_positions(row.get("positions"))
        if p:
            return p, "yahoo"
        p = parse_positions(row.get("espn_pos"))
        if p:
            if mode != "strict":
                p = set().union(*(ESPN_WIDEN.get(x, {x}) for x in p))
            return p, "espn"
    return {"?"}, "unknown"


def solve(cands: list, slots: list) -> tuple[dict, float]:
    """cands: [(pid, pos_set, ev)]. Returns ({slot_index: pid}, total ev). Exact.

    dp[k][mask] = best total using the first k players with exactly the slots in `mask` filled.
    Each player is either benched (mask unchanged) or put in one empty slot he's eligible for."""
    n = len(slots)
    full = 1 << n
    NEG = float("-inf")
    tables = [[NEG] * full]
    tables[0][0] = 0.0
    for pid, pos, ev in cands:
        dp = tables[-1]
        nxt = dp[:]  # bench him
        for mask in range(full):
            if dp[mask] == NEG:
                continue
            for i, slot in enumerate(slots):
                if not mask & (1 << i) and pos & SLOT_ELIG[slot]:
                    m2 = mask | (1 << i)
                    if dp[mask] + ev > nxt[m2]:
                        nxt[m2] = dp[mask] + ev
        tables.append(nxt)
    mask = max(range(full), key=lambda m: tables[-1][m])
    best = tables[-1][mask]
    assign = {}
    for k in range(len(cands), 0, -1):  # walk back: benched, or which slot did he take?
        pid, pos, ev = cands[k - 1]
        cur = tables[k][mask]
        if tables[k - 1][mask] == cur:
            continue
        for i, slot in enumerate(slots):
            if mask & (1 << i) and pos & SLOT_ELIG[slot] and abs(tables[k - 1][mask ^ (1 << i)] + ev - cur) < 1e-9:
                assign[i] = pid
                mask ^= 1 << i
                break
    return assign, best


def _opponents(dates) -> dict:
    with db.cursor() as c:
        rows = c.execute(f"SELECT game_date, home, away FROM schedule WHERE game_date IN ({','.join('?' * len(dates))})",
                         dates).fetchall()
    out = {}
    for r in rows:
        out[(r["home"], r["game_date"])] = f"vs {r['away']}"
        out[(r["away"], r["game_date"])] = f"@ {r['home']}"
    return out


def day_plan(ctx, team_key, d, opp_map=None) -> dict:
    roster = ctx.rosters.get(team_key, [])
    cur_slots = sim.team_slots(ctx).get(team_key, {})
    slots = slot_list()
    opp_map = opp_map or _opponents([d])
    pw = model.scoring_mode() == "points"
    cands, info = [], {}
    for p in roster:
        pos, src = positions(ctx, p)
        games = dict(sim.player_games(ctx, p, [d]))
        a = float(games.get(d, 0.0))
        per_game = float(ctx.values["value" if pw else "var"].get(p, 0.0))
        per_game *= model.production_multiplier(p, ctx.events, ctx.params)
        info[p] = {"nba_id": int(p), "name": ctx.name(p), "team": ctx.team_of(p), "positions": sorted(pos),
                   "pos_source": src, "game": opp_map.get((ctx.team_of(p), d)), "avail": a,
                   "ev": a * per_game * sim.game_factor(ctx, p, d), "current_slot": cur_slots.get(p),
                   "game_factor": sim.game_factor(ctx, p, d)}
        if cur_slots.get(p) in NOT_STARTABLE or d not in dict(sim.player_games(ctx, p, [d])):
            continue
        cands.append((p, pos, info[p]["ev"]))
    assign, total = solve(sorted(cands, key=lambda c: -c[2]), slots)
    lineup = [dict(info[assign[i]], slot=s) if i in assign else {"slot": s, "empty": True} for i, s in enumerate(slots)]
    started = set(assign.values())
    bench = []
    for p in roster:
        if p in started:
            continue
        x = info[p]
        if x["current_slot"] in NOT_STARTABLE:
            why = f"on {x['current_slot']}"
        elif not x["game"]:
            why = "no game"
        elif x["avail"] < 0.2:
            why = "not expected to play"
        else:
            why = "lower expected points than the starters" if pw else "lower value than the starters"
        bench.append(dict(x, reason=why))
    # your current lineup's expected total (pasted slots; players without a game score 0)
    cur = sum(info[p]["ev"] for p in roster if cur_slots.get(p) not in NOT_STARTING)
    to_start = [info[p]["name"] for p in started if cur_slots.get(p) in ("BN", None, "?")]
    # Who has to move out in Yahoo: starters with a game who lost their spot, plus only as many
    # idle (no-game) starters as the incoming players need slots, preferring ones whose slot
    # an incoming player is eligible for. Idle starters nobody needs to replace can stay put.
    out_of_lineup = [p for p in roster if cur_slots.get(p) not in NOT_STARTING and p not in started]
    lost = [p for p in out_of_lineup if info[p]["game"]]
    idle = [p for p in out_of_lineup if not info[p]["game"]]
    incoming = [p for p in started if cur_slots.get(p) in NOT_STARTING]
    open_slots = len(slots) - sum(1 for p in roster if cur_slots.get(p) not in NOT_STARTING)
    needed = max(0, len(incoming) - open_slots - len(lost))
    fits = lambda p: any(set(info[q]["positions"]) & SLOT_ELIG.get(cur_slots.get(p), set()) for q in incoming)
    idle.sort(key=lambda p: not fits(p))
    to_bench = [f"{info[p]['name']} (lower projection)" for p in lost] +                [f"{info[p]['name']} (no game)" for p in idle[:needed]]
    return {"date": d, "lineup": lineup, "bench": bench, "ev_optimal": total, "ev_current": cur,
            "gain": total - cur, "start": to_start, "sit": to_bench,
            "unknown_positions": [info[p]["name"] for p in roster if info[p]["pos_source"] == "unknown"]}


def week_plan(ctx, team_key=None) -> dict:
    team_key = team_key or ctx.my_key
    dates = sim.remaining_dates(ctx)
    opp_map = _opponents(dates) if dates else {}
    days = [day_plan(ctx, team_key, d, opp_map) for d in dates]
    return {"days": days, "today": (ctx.today or date.today().isoformat()),
            "week_gain": sum(x["gain"] for x in days), "slots": slot_list(),
            "unit": "fantasy pts" if model.scoring_mode() == "points" else "value"}


def apply_today(ctx, team_key=None) -> dict:
    """Write today's optimal lineup into your pasted slots (so week-to-date scoring follows
    it). Yahoo itself still has to be set by you."""
    team_key = team_key or ctx.my_key
    plan = day_plan(ctx, team_key, (ctx.today or date.today().isoformat()))
    with db.cursor() as c:
        cur = {r["nba_id"]: r["slot"] for r in c.execute("SELECT nba_id, slot FROM rosters WHERE team_key=?", (team_key,))}
        started = {x["nba_id"]: x["slot"] for x in plan["lineup"] if not x.get("empty")}
        for pid, slot in cur.items():
            if slot in NOT_STARTABLE:
                continue
            c.execute("UPDATE rosters SET slot=? WHERE team_key=? AND nba_id=?",
                      (started.get(pid, "BN"), team_key, pid))
    return {"ok": True, "started": len(started)}


# ---------------------------------------------------------------- fast fill for the simulator
ANY = {"PG", "SG", "SF", "PF", "C"}


def slot_masks(pos: set, slots: list) -> int:
    """Bitmask of the slots a player can fill. Unknown positions count as anything here:
    the simulator shouldn't punish a team for missing data (the lineup card flags it)."""
    pos = ANY if (not pos or pos == {"?"}) else pos
    m = 0
    for i, s in enumerate(slots):
        if pos & SLOT_ELIG[s]:
            m |= 1 << i
    return m


def greedy_fill(masks: list, n_slots: int) -> list:
    """Players arrive best-first (masks in value order). Each takes, among open slots he's
    eligible for, the one the players still to come are least able to use. Returns started
    flags. Used inside the simulator (thousands of calls); see check_greedy() for how often
    it matches the exact solver."""
    free = (1 << n_slots) - 1
    started = [False] * len(masks)
    for j, m in enumerate(masks):
        cand = m & free
        if not cand:
            continue
        best, best_d = None, None
        i = 0
        while cand >> i:
            if (cand >> i) & 1:
                d = 0
                for mm in masks[j + 1:]:
                    d += (mm >> i) & 1
                if best_d is None or d < best_d:
                    best, best_d = i, d
            i += 1
        free &= ~(1 << best)
        started[j] = True
    return started


def check_greedy(trials=2000, seed=0) -> dict:
    """How often the fast fill equals the exact optimum, on random rosters drawn like real ones."""
    import random
    rng = random.Random(seed)
    slots = DEFAULT_SLOTS
    shapes = [{"PG"}, {"PG", "SG"}, {"SG"}, {"SG", "SF"}, {"SF"}, {"SF", "PF"}, {"PF"}, {"PF", "C"}, {"C"}]
    same, losses = 0, []
    for _ in range(trials):
        k = rng.randint(6, 13)
        cands = sorted([(j, rng.choice(shapes), rng.uniform(15, 55)) for j in range(k)], key=lambda c: -c[2])
        _, exact = solve(cands, slots)
        flags = greedy_fill([slot_masks(c[1], slots) for c in cands], len(slots))
        fast = sum(c[2] for c, f in zip(cands, flags) if f)
        if abs(fast - exact) < 1e-9:
            same += 1
        else:
            losses.append(exact - fast)
    return {"trials": trials, "optimal_share": same / trials,
            "avg_loss_when_not": (sum(losses) / len(losses)) if losses else 0.0,
            "max_loss": max(losses) if losses else 0.0}
