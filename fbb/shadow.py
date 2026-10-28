"""The model's own team: a paper copy of your roster that the model manages by itself.

It starts as a copy of your team and from then on follows only its own advice:
  - every morning: IL moves, then at most one add (an open spot first, else its top swap
    labelled "Do today", "Worth doing" or "Waiver claim"), within the league's 4 adds/week
  - waiver players arrive when their waiver period ends (a pending claim, like Yahoo)
  - every refresh until the day's first tip-off: best lineup by projection (frozen after)
  - each finished day is scored from box scores; each week against your real opponent

What it is for: judging the model as a manager (moves compound, which per-move scoring
misses) and showing the moves you passed on. What it is NOT for: tuning. It is a scoreboard;
one team over one season is far too small and noisy to learn from.

Known biases: it never competes for players (a paper claim always succeeds) and makes no
trades; your own week-to-date uses your recorded lineups or, on days without one, the best
lineup by projection, which can flatter you.
"""
import copy
import json
from datetime import date, datetime, timedelta

from . import db, lineup, model, recommend, sim

MAX_ADDS_WEEK = 4
MOVE_LABELS = {"Do today", "Worth doing", "Waiver claim"}
MIN_OPEN_SPOT_GAIN = 0.02

SCHEMA = """
CREATE TABLE IF NOT EXISTS shadow_days (
    day TEXT PRIMARY KEY, starters TEXT, opp_key TEXT, points REAL, scored INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS shadow_moves (
    id INTEGER PRIMARY KEY AUTOINCREMENT, created TEXT, day TEXT, kind TEXT,
    add_id INTEGER, drop_id INTEGER, status TEXT, until TEXT, score REAL, reason TEXT
);
"""


def _ensure():
    with db.cursor() as c:
        c.executescript(SCHEMA)


def state() -> dict | None:
    return db.kv_get("shadow_state")


def _save(s):
    db.kv_set("shadow_state", s)


def start(roster: list, il: list, day: str) -> dict:
    """Fork the model team from a roster (active players + IL), effective `day`."""
    _ensure()
    il = list(dict.fromkeys(int(p) for p in il))
    roster = [p for p in dict.fromkeys(int(p) for p in roster) if p not in il]
    s = {"started": day, "roster": roster, "il": il, "pending": [], "last_daily": None}
    _save(s)
    return s


def _fp(rows) -> float:
    w = model.point_weights()
    return float(sum(rows[k].sum() * v for k, v in w.items() if k in rows))


def _week_bounds(d: str):
    x = date.fromisoformat(d)
    ws = x - timedelta(days=x.weekday())
    return ws.isoformat(), (ws + timedelta(days=6)).isoformat()


def _shadow_ctx(ctx, s):
    """The real context with your team replaced by the model team (same league, same
    opponent). Players you really own stay unavailable to it."""
    sc = copy.copy(ctx)
    sc.rosters = dict(ctx.rosters)
    real_mine = set(ctx.rosters.get(ctx.my_key, []))
    sc.rosters[ctx.my_key] = list(s["roster"])
    sc._pools = {"_slots": {ctx.my_key: {}}}           # no pasted slots: it manages its own
    owned = ({p for k, ids in ctx.rosters.items() if k != ctx.my_key for p in ids}
             | real_mine | set(s["roster"]) | set(s["il"]) | {c["add"] for c in s["pending"]})
    sc.rostered = lambda: owned
    sc.il_used = len(s["il"])  # its IL players are off sc.rosters: tell the recommender they're there
    sc.wtd = dict(ctx.wtd)
    sc.wtd[ctx.my_key] = _week_totals(ctx, s, ctx.week_start)
    return sc


def _week_totals(ctx, s, week_start) -> dict:
    """Stat totals of the model team's started games this week, from recorded lineups."""
    tot = {k: 0.0 for k in sim.SIM_STATS}
    with db.cursor() as c:
        rows = c.execute("SELECT day, starters FROM shadow_days WHERE day >= ? AND day < ?",
                         (week_start, date.today().isoformat())).fetchall()
    for r in rows:
        g = ctx.logs[(ctx.logs["game_date"].astype(str).str[:10] == r["day"]) & ctx.logs["nba_id"].isin(json.loads(r["starters"]))]
        for k in tot:
            tot[k] += float(g[k].sum())
    return tot


def _adds_used(s, today) -> int:
    ws, we = _week_bounds(today)
    with db.cursor() as c:
        done = c.execute("SELECT count(*) FROM shadow_moves WHERE status='done' AND add_id IS NOT NULL AND day BETWEEN ? AND ?",
                         (ws, we)).fetchone()[0]
    return done + len(s["pending"])


def _log(day, kind, add=None, drop=None, status="done", until=None, score=None, reason=""):
    with db.cursor() as c:
        c.execute("INSERT INTO shadow_moves(created, day, kind, add_id, drop_id, status, until, score, reason) VALUES (?,?,?,?,?,?,?,?,?)",
                  (db.now_iso(), day, kind, add, drop, status, until, score, reason))


def _out(ctx, p) -> bool:
    return ((ctx.injuries.get(p) or {}).get("status") or "").lower() in ("out", "injured reserve")


def daily(ctx, today=None) -> dict:
    """Once a day, before games: claims that cleared, IL, then at most one move."""
    _ensure()
    s = state()
    today = today or date.today().isoformat()
    if not s or today < s["started"] or s.get("last_daily") == today:
        return {"skipped": True}
    if not _daily_due(ctx, s, today):
        return {"skipped": "waiting for the morning news"}
    done = []
    limit = db.kv_get("roster_size", 13)

    # 0. a player another (real) team actually has can't be the model's: its claim on him
    #    lost (Oct 2026: it started with Bane, assumed claimed; Metapod got him)
    real = {p: k for k, ids in ctx.rosters.items() if k != ctx.my_key for p in ids}
    for p in list(s["roster"]) + list(s["il"]):
        if p in real:
            (s["roster"] if p in s["roster"] else s["il"]).remove(p)
            _log(today, "lost", None, p, reason=f"{ctx.name(p)} is really on {ctx.teams.get(real[p], {}).get('name', 'another team')}")
            done.append(f"lost {ctx.name(p)} (another team has him)")

    # 1. waiver claims whose period has ended
    for cl in list(s["pending"]):
        if cl["until"] <= today:
            s["pending"].remove(cl)
            if cl.get("drop") in s["roster"]:
                s["roster"].remove(cl["drop"])
            elif cl.get("drop") is not None or len(s["roster"]) >= limit:
                _log(today, "claim failed", cl["add"], cl.get("drop"), status="failed", reason="no room when it cleared")
                continue
            s["roster"].append(cl["add"])
            with db.cursor() as c:
                c.execute("UPDATE shadow_moves SET status='done', day=? WHERE status='pending' AND add_id=?", (today, cl["add"]))
            done.append(f"claim cleared: +{ctx.name(cl['add'])}")

    # 2. IL: healthy again -> back (dropping the weakest if full); out -> onto a free IL slot
    for p in list(s["il"]):
        if not _out(ctx, p):
            s["il"].remove(p)
            if len(s["roster"]) >= limit:
                weakest = min(s["roster"] + [p], key=lambda q: recommend.ros_value(ctx, q))
                if weakest != p:
                    s["roster"].remove(weakest)
                    s["roster"].append(p)
                _log(today, "activate", p if weakest != p else None, weakest, reason="healthy again; roster full")
                done.append(f"activated {ctx.name(p)}, dropped {ctx.name(weakest)}")
            else:
                s["roster"].append(p)
                _log(today, "activate", p, None, reason="healthy again")
                done.append(f"activated {ctx.name(p)}")
    for p in sorted(s["roster"], key=lambda q: -recommend.ros_value(ctx, q)):
        if len(s["il"]) < recommend.il_slots() and recommend.injury_plan(ctx, p) == "il":  # not for a game or two
            s["roster"].remove(p)
            s["il"].append(p)
            _log(today, "to IL", None, None, reason=f"{ctx.name(p)} out")
            done.append(f"{ctx.name(p)} to IL")

    # 3. at most one add
    if _adds_used(s, today) < MAX_ADDS_WEEK:
        sc = _shadow_ctx(ctx, s)
        open_spots = limit - len(s["roster"]) - sum(1 for c in s["pending"] if c.get("drop") is None)
        if open_spots > 0:
            B = recommend.baseline(sc)
            best = None
            for a in recommend.best_free_agents(sc, 25):
                if _out(sc, a):
                    continue
                m = recommend.score_swap(sc, B, a, None)
                if best is None or m["score"] > best["score"]:
                    best = m
            if best and best["score"] >= MIN_OPEN_SPOT_GAIN:
                done.append(_execute(sc, s, today, best["add"], None, best["score"], "fills an open roster spot"))
        else:
            r = recommend.recommend(sc, log=False, budget={"limit": MAX_ADDS_WEEK, "used": _adds_used(s, today),
                                                           "left": MAX_ADDS_WEEK - _adds_used(s, today)})
            m = next((m for m in r["moves"] if m.get("urgency") in MOVE_LABELS), None)
            if m:
                done.append(_execute(sc, s, today, m["add"], m["drop"], m["score"], m.get("urgency_reason") or m.get("urgency")))
    s["last_daily"] = today
    _save(s)
    return {"moves": done}


DAILY_HOUR = 10   # local: overnight injury news and morning updates are in by then


def _daily_due(ctx, s, today) -> bool:
    """After DAILY_HOUR, or earlier if one of its players tips off within 2 hours.
    (It used to run on the first refresh after midnight, before the day's news.)"""
    now = datetime.now().astimezone()
    if now.hour >= DAILY_HOUR:
        return True
    first = _first_tip(ctx, s["roster"], today)
    return bool(first and (first - now).total_seconds() < 2 * 3600)


def _first_tip(ctx, pids, day):
    tips = lineup.tip_times(ctx)
    return min((t for p in pids if (t := tips.get((ctx.team_of(p), day)))), default=None)


def _execute(ctx, s, today, add, drop, score, reason) -> str:
    from . import free_agents
    av = free_agents.availability(add)
    if av["status"] == "W" and av["until"] and av["until"] > today:
        s["pending"].append({"add": int(add), "drop": int(drop) if drop is not None else None, "until": av["until"]})
        _log(today, "waiver claim", int(add), drop, status="pending", until=av["until"], score=score, reason=reason)
        return f"claim: +{ctx.name(add)}" + (f" / -{ctx.name(drop)}" if drop is not None else "") + f" (clears {av['until']})"
    if drop is not None:
        s["roster"].remove(drop)
    s["roster"].append(int(add))
    _log(today, "add/drop" if drop is not None else "add", int(add), drop, score=score, reason=reason)
    return f"+{ctx.name(add)}" + (f" / -{ctx.name(drop)}" if drop is not None else "")


def plan_lineup(ctx, now=None) -> list | None:
    """Today's lineup, re-planned each refresh until the day's first tip-off, then frozen."""
    _ensure()
    s = state()
    today = date.today().isoformat()
    if not s or today < s["started"]:
        return None
    now = now or datetime.now().astimezone()
    sc = _shadow_ctx(ctx, s)
    # Frozen from the day's FIRST tip among its players. day_plan's first_tip is the next
    # *unstarted* game, so checking that let it re-plan all evening, and each re-plan (it has
    # no saved slots) dropped every player already locked: Oct 26 it "started" 2 of 8.
    first = _first_tip(ctx, s["roster"], today)
    with db.cursor() as c:
        have = c.execute("SELECT 1 FROM shadow_days WHERE day=?", (today,)).fetchone()
    if have and first and first <= now:
        return None
    if not have and first and first <= now:
        now = first - timedelta(minutes=1)  # first plan of the day came late: plan as of tip-off
    plan = lineup.day_plan(sc, ctx.my_key, today, now=now)
    with db.cursor() as c:
        starters = [x["nba_id"] for x in plan["lineup"] if not x.get("empty")]
        c.execute("INSERT OR REPLACE INTO shadow_days(day, starters, opp_key, points, scored) VALUES (?,?,?,NULL,0)",
                  (today, json.dumps([int(p) for p in starters]), ctx.opp_key))
    return starters


def score_days(ctx) -> int:
    """Points for every finished day (box scores in)."""
    _ensure()
    today = date.today().isoformat()
    n = 0
    with db.cursor() as c:
        rows = c.execute("SELECT day, starters FROM shadow_days WHERE scored=0 AND day < ?", (today,)).fetchall()
    for r in rows:
        g = ctx.logs[(ctx.logs["game_date"].astype(str).str[:10] == r["day"]) & ctx.logs["nba_id"].isin(json.loads(r["starters"]))]
        with db.cursor() as c:
            c.execute("UPDATE shadow_days SET points=?, scored=1 WHERE day=?", (_fp(g), r["day"]))
        n += 1
    return n


def tick(ctx) -> dict:
    """Called on every refresh."""
    if not state():
        return {"skipped": "not started"}
    out = {"scored_days": score_days(ctx)}
    if state().get("last_daily") != date.today().isoformat():
        out.update(daily(ctx))
    out["lineup"] = bool(plan_lineup(ctx))
    return out


def report(ctx) -> dict:
    """Weekly record of the model team vs yours, against your real opponents, plus its moves."""
    _ensure()
    s = state()
    if not s:
        return {"started": False}
    with db.cursor() as c:
        days = [dict(r) for r in c.execute("SELECT * FROM shadow_days WHERE scored=1 ORDER BY day")]
        moves = [dict(r) for r in c.execute("SELECT * FROM shadow_moves ORDER BY id")]
        hist = [dict(r) for r in c.execute("SELECT effective, players_in FROM transactions WHERE deleted=0")]
    my_ins = {p for h in hist for p in json.loads(h["players_in"])}
    weeks = {}
    for d in days:
        ws, _ = _week_bounds(d["day"])
        w = weeks.setdefault(ws, {"week_start": ws, "model": 0.0, "days": 0, "opp_key": d["opp_key"]})
        w["model"] += d["points"] or 0.0
        w["days"] += 1
        w["opp_key"] = d["opp_key"] or w["opp_key"]
    today = date.today().isoformat()
    rec = {"model": [0, 0], "you": [0, 0]}
    for ws, w in weeks.items():
        we = (date.fromisoformat(ws) + timedelta(days=6)).isoformat()
        wc = copy.copy(ctx)
        wc._pools = {}
        wc.week_start, wc.today = ws, min(today, (date.fromisoformat(we) + timedelta(days=1)).isoformat())
        wc.logs = ctx.logs[ctx.logs["game_date"].astype(str).str[:10] <= we]
        w["you"] = _fp_dict(sim.estimate_wtd(wc, ctx.rosters.get(ctx.my_key, []), ctx.my_key))
        w["opp"] = _fp_dict(sim.estimate_wtd(wc, ctx.rosters.get(w["opp_key"], []), w["opp_key"])) if w["opp_key"] else None
        w["opp_name"] = (ctx.teams.get(w["opp_key"]) or {}).get("name") if w["opp_key"] else None
        w["final"] = we < today
        if w["final"] and w["opp"] is not None:
            rec["model"][0 if w["model"] > w["opp"] else 1] += 1
            rec["you"][0 if w["you"] > w["opp"] else 1] += 1
    since = s["started"]
    out_moves = []
    for m in moves:
        x = dict(m)
        x["add_name"] = ctx.name(m["add_id"]) if m["add_id"] else None
        x["drop_name"] = ctx.name(m["drop_id"]) if m["drop_id"] else None
        x["you_too"] = bool(m["add_id"] and m["add_id"] in my_ins)
        if m["add_id"] and m["status"] == "done":
            g = ctx.logs[(ctx.logs["nba_id"] == m["add_id"]) & (ctx.logs["game_date"].astype(str).str[:10] >= m["day"])]
            x["add_pts_since"] = round(_fp(g), 1)
            if m["drop_id"]:
                g2 = ctx.logs[(ctx.logs["nba_id"] == m["drop_id"]) & (ctx.logs["game_date"].astype(str).str[:10] >= m["day"])]
                x["drop_pts_since"] = round(_fp(g2), 1)
        out_moves.append(x)
    return {"started": since, "roster": [ctx.name(p) for p in s["roster"]], "il": [ctx.name(p) for p in s["il"]],
            "pending": [{"add": ctx.name(c["add"]), "drop": ctx.name(c["drop"]) if c.get("drop") else None, "until": c["until"]}
                        for c in s["pending"]],
            "weeks": sorted(weeks.values(), key=lambda w: w["week_start"]), "record": rec,
            "moves": out_moves[::-1],
            "missed": [m for m in out_moves if m["add_id"] and m["status"] == "done" and not m["you_too"]]}


def _fp_dict(tot) -> float:
    return float(sum(tot.get(k, 0.0) * v for k, v in model.point_weights().items()))
