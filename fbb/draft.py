"""Draft helper: a big board ranked on rest-of-season value, a live pick tracker, and a pick
suggestion that looks ahead to your next turn.

Value. Mid-season, per-game value overrates anyone who'll miss games, so the board ranks on
rest-of-season value: per-game value (fantasy points, or summed category z-scores) x expected
games left (games remaining x the availability model's share for injuries and rest). Your takes
change the per-game number like they do everywhere else. VOR is that minus the last player who
would be drafted (teams x rounds): the number that decides a pick.

Suggestion. Taking the best VOR now isn't always right: if the next-best player will still be
there at your next pick but the one after won't, take the one who won't last. Other managers
are simulated picking off a noisy ranking (Yahoo's own rank by default: it's what their draft
room shows them), a few hundred times. For each candidate the score is
    VOR(candidate) + average VOR of the best player left at your next pick, given you took them.
Late in the draft, picks that can't fill an open starting slot are dropped while slots remain
that the remaining picks would otherwise leave empty.

Two modes: live (picks and draft order come from Yahoo) and mock (you enter picks, Yahoo's are
ignored), so the same board works for practice and for the real thing.
"""
import logging
import math
import time
from datetime import datetime, timezone

import numpy as np

from . import db, lineup, model, pipeline

log = logging.getLogger(__name__)
API = "https://fantasysports.yahooapis.com/fantasy/v2"
MARKET_HOURS = 6          # how often Yahoo's rankings/ADP are refetched before the draft
MARKET_DEPTH = 300        # players fetched from Yahoo's ranking
POOL = 320                # players on the board
SIMS = 300
CANDIDATES = 12
MIN_SYNC_SECONDS = 5      # live polling: don't hit Yahoo more often than this

DEFAULTS = {
    "mode": "live",              # live: Yahoo picks + order | mock: picks you enter
    "num_teams": None,           # None: the league's setting
    "my_slot": None,             # None: Yahoo's draft order once set
    "rounds": None,              # None: roster size
    "order": "snake",            # snake | linear
    "auction": None,             # None: Yahoo's setting
    "budget": 200,
    "opponents": "yahoo",        # how other managers pick: yahoo (rank) | adp | model
    "randomness": 0.3,           # spread of other managers' picks around that ranking
    "injuries": True,            # discount players for games they're expected to miss
    "avoid": [],                 # players never suggested
    "team_order": {},            # {team_key: slot}, until Yahoo publishes its own order
}


# ---------------------------------------------------------------- settings

def settings() -> dict:
    """Effective settings, with where each came from."""
    user = db.kv_get("draft_cfg") or {}
    y = db.kv_get("draft_yahoo") or {}
    live = (user.get("mode") or DEFAULTS["mode"]) == "live"
    eff, src = {}, {}
    for k, d in DEFAULTS.items():
        v, s = user.get(k), "you"
        if v is None and k == "num_teams":
            v, s = db.kv_get("num_teams"), "league"
        if v is None and k == "rounds":
            v, s = db.kv_get("roster_size"), "league"
        if k == "my_slot" and live and y.get("my_slot"):
            v, s = y["my_slot"], "yahoo"     # the real order beats a guess once Yahoo sets it
        if k == "my_slot" and v is None and y.get("my_key") in (user.get("team_order") or {}):
            v, s = user["team_order"][y["my_key"]], "your team order"
        if v is None and k == "auction" and "auction" in y:
            v, s = y["auction"], "yahoo"
        if v is None:
            v, s = d, "default"
        eff[k], src[k] = v, s
    eff["num_teams"] = int(eff["num_teams"] or 12)
    eff["rounds"] = int(eff["rounds"] or 13)
    eff["my_slot"] = int(eff["my_slot"]) if eff["my_slot"] else None
    eff["sources"] = src
    return eff


def save_settings(changes: dict) -> dict:
    cfg = db.kv_get("draft_cfg") or {}
    for k, v in changes.items():
        if k not in DEFAULTS:
            raise ValueError(f"unknown setting: {k}")
        if k == "mode" and v not in ("live", "mock"):
            raise ValueError("mode is live or mock")
        if k == "order" and v not in ("snake", "linear"):
            raise ValueError("order is snake or linear")
        if k == "opponents" and v not in ("yahoo", "adp", "model"):
            raise ValueError("opponents is yahoo, adp or model")
        if k == "num_teams" and v is not None and not 2 <= int(v) <= 20:
            raise ValueError("teams: 2 to 20")
        if k == "rounds" and v is not None and not 1 <= int(v) <= 30:
            raise ValueError("rounds: 1 to 30")
        if k == "randomness" and not 0 <= float(v) <= 1.5:
            raise ValueError("randomness: 0 to 1.5")
        if k == "team_order":
            v = {str(t): int(sl) for t, sl in (v or {}).items()}
            if len(set(v.values())) != len(v):
                raise ValueError("two teams can't share a draft slot")
        cfg[k] = v
    s = {**DEFAULTS, **cfg}
    if s["my_slot"] is not None and not 1 <= int(s["my_slot"]) <= int(s["num_teams"] or db.kv_get("num_teams") or 12):
        raise ValueError("your pick slot must be between 1 and the number of teams")
    db.kv_set("draft_cfg", cfg)
    return settings()


def slot_of(pick: int, n: int, order: str) -> int:
    """Draft slot (1..n) that makes overall pick `pick` (1-based)."""
    rnd, i = divmod(pick - 1, n)
    return n - i if order == "snake" and rnd % 2 else i + 1


# ---------------------------------------------------------------- picks

def picks() -> list:
    """Picks so far, sorted: [{pick, slot, nba_id, source, team_key?}]."""
    s = settings()
    manual = db.kv_get("draft_manual") or []
    if s["mode"] == "mock":
        out = list(manual)
    else:
        y = (db.kv_get("draft_yahoo") or {}).get("picks", [])
        taken = {p["pick"] for p in y}
        out = y + [p for p in manual if p["pick"] not in taken]   # yours fill gaps (Yahoo lag)
    seen, clean = set(), []
    for p in sorted(out, key=lambda p: p["pick"]):
        if p.get("nba_id") is None or p["nba_id"] not in seen:
            seen.add(p.get("nba_id"))
            clean.append(dict(p, slot=p.get("slot") or slot_of(p["pick"], s["num_teams"], s["order"])))
    return clean


def add_pick(nba_id: int, pick: int | None = None, source: str = "you") -> dict:
    ps = picks()
    if any(p["nba_id"] == nba_id for p in ps):
        raise ValueError("already drafted")
    s = settings()
    pick = pick or (max((p["pick"] for p in ps), default=0) + 1)
    if pick > s["num_teams"] * s["rounds"]:
        raise ValueError("the draft is over")
    manual = [p for p in (db.kv_get("draft_manual") or []) if p["pick"] != pick]
    manual.append({"pick": int(pick), "nba_id": int(nba_id), "source": source})
    db.kv_set("draft_manual", manual)
    return {"pick": pick}


def undo_pick(pick: int | None = None) -> dict:
    """Remove a pick. Without `pick`: your last pick, along with the simulated picks made after
    it, so a mock draft goes back to the moment you chose."""
    manual = sorted(db.kv_get("draft_manual") or [], key=lambda p: p["pick"])
    if not manual:
        raise ValueError("no picks of yours to undo (Yahoo's picks come from Yahoo)")
    if pick is None:
        while manual and manual[-1].get("source") == "sim":
            manual.pop()
        if not manual:
            raise ValueError("no pick of yours to undo yet")
        pick = manual[-1]["pick"]
        manual = [p for p in manual if p["pick"] < pick]
    else:
        manual = [p for p in manual if p["pick"] != pick]
    db.kv_set("draft_manual", manual)
    return {"removed": pick}


def reset() -> None:
    db.kv_set("draft_manual", [])


def _opp_rank(s, market, model_rank, p) -> float:
    """Where other managers rank a player: Yahoo's rank (their draft room's default sort), ADP,
    or this model. Players Yahoo doesn't rank fall in behind everyone it does."""
    m = market.get(str(p)) or {}
    if s["opponents"] == "adp" and m.get("adp"):
        return m["adp"]
    if s["opponents"] in ("yahoo", "adp") and m.get("rank"):
        return m["rank"]
    return model_rank.get(p, 999) if s["opponents"] == "model" else MARKET_DEPTH + model_rank.get(p, 999)


MAX_CENTERS = 3   # simulated managers stop taking center-only players after this many


def sim_to_my_turn(seed=None) -> dict:
    """Mock drafts: make the other teams' picks until you're on the clock (or the draft ends).
    Each simulated manager takes the best player on a noisy version of their ranking (the same
    model the 'Next pick' odds use) and, like a person would, skips a 4th pure center."""
    s = settings()
    if s["mode"] != "mock":
        raise ValueError("other teams only pick for themselves in mock mode")
    if not s["my_slot"]:
        raise ValueError("set your pick slot first")
    ctx = pipeline.context()
    n, total = s["num_teams"], s["num_teams"] * s["rounds"]
    vals = _value_table(ctx, s)
    ranked = sorted(vals, key=lambda p: -vals[p]["ros"])
    model_rank = {p: i + 1 for i, p in enumerate(ranked)}
    market = (db.kv_get("draft_market") or {}).get("players", {})
    rng = np.random.default_rng(seed)
    ps = picks()
    taken = {p["nba_id"] for p in ps}
    rosters = {}
    for p in ps:
        rosters.setdefault(p["slot"], []).append(p["nba_id"])
    is_center = {p: lineup.positions(ctx, p)[0] == {"C"} for p in ranked[:POOL]}
    made = 0
    pick = max((p["pick"] for p in ps), default=0) + 1
    while pick <= total:
        slot = slot_of(pick, n, s["order"])
        if slot == s["my_slot"]:
            break
        centers = sum(is_center.get(p, False) for p in rosters.get(slot, []))
        best, best_key = None, math.inf
        for p in ranked[:POOL]:
            if p in taken or (centers >= MAX_CENTERS and is_center.get(p)):
                continue
            key = _opp_rank(s, market, model_rank, p) * math.exp(rng.normal(0, s["randomness"]))
            if key < best_key:
                best, best_key = p, key
        if best is None:
            break
        add_pick(best, pick, source="sim")
        taken.add(best)
        rosters.setdefault(slot, []).append(best)
        made += 1
        pick += 1
    return {"simulated": made, "next_pick": pick}


def advance() -> None:
    """After any change in a mock draft, let the other teams pick up to your turn."""
    s = settings()
    if s["mode"] == "mock" and s["my_slot"]:
        sim_to_my_turn()


# ---------------------------------------------------------------- Yahoo

def _players_page(raw) -> list:
    """[{yid, name, team, pos, adp}] from a league players page (with draft_analysis)."""
    league = (raw.get("fantasy_content") or {}).get("league") or []
    players = league[1].get("players") if len(league) > 1 and isinstance(league[1], dict) else {}
    out = []
    for k, v in (players or {}).items():
        if k == "count":
            continue
        meta = {kk: vv for d in v["player"][0] if isinstance(d, dict) for kk, vv in d.items()}
        da = next((d["draft_analysis"] for d in v["player"][1:] if isinstance(d, dict) and "draft_analysis" in d), [])
        da = {kk: vv for d in (da if isinstance(da, list) else [da]) if isinstance(d, dict) for kk, vv in d.items()}
        try:
            adp = float(da.get("average_pick"))
        except (TypeError, ValueError):
            adp = None
        out.append({"yid": str(meta.get("player_id")), "name": meta["name"]["full"],
                    "team": meta.get("editorial_team_abbr", ""), "pos": meta.get("display_position", ""),
                    "adp": adp})
    return out


def parse_draft_results(raw) -> list:
    league = (raw.get("fantasy_content") or {}).get("league") or []
    res = league[1].get("draft_results") if len(league) > 1 and isinstance(league[1], dict) else []
    items = res.values() if isinstance(res, dict) else res or []
    out = []
    for v in items:
        if not isinstance(v, dict) or "draft_result" not in v:
            continue
        d = v["draft_result"]
        if d.get("player_key"):   # Yahoo lists empty future picks in some states
            out.append({"pick": int(d["pick"]), "team_key": d.get("team_key"), "player_key": d["player_key"],
                        "cost": int(d["cost"]) if d.get("cost") not in (None, "") else None})
    return out


def _resolve(q, lk, keys: list, ymap: dict) -> dict:
    """Yahoo player keys -> nba_id, fetching names for keys we haven't seen."""
    from . import yahoo
    missing = [k for k in keys if k.split(".p.")[-1] not in ymap]
    idx = yahoo._player_index()
    for i in range(0, len(missing), 25):
        raw = q.get_response(f"{API}/league/{lk}/players;player_keys={','.join(missing[i:i + 25])}").json()
        for p in _players_page(raw):
            pid = yahoo._match(idx, p["name"], p["team"])
            if pid:
                ymap[p["yid"]] = int(pid)
    return ymap


def sync_yahoo(q=None, force=False) -> dict:
    """Draft status, order and picks from Yahoo, plus its rankings/ADP (every few hours)."""
    from . import yahoo
    y = db.kv_get("draft_yahoo") or {}
    if not force and y.get("synced_ts") and time.time() - y["synced_ts"] < MIN_SYNC_SECONDS:
        return y
    q = q or yahoo.query()
    lk = db.kv_get("league_key")
    meta_settings = q.get_response(f"{API}/league/{lk}/settings").json()["fantasy_content"]["league"]
    st = meta_settings[1]["settings"][0]
    y.update(status=meta_settings[0].get("draft_status"), auction=st.get("is_auction_draft") == "1",
             time=int(st["draft_time"]) if st.get("draft_time") else None,
             pick_seconds=int(st["draft_pick_time"]) if st.get("draft_pick_time") else None)
    teams = q.get_response(f"{API}/league/{lk}/teams").json()["fantasy_content"]["league"][1]["teams"]
    order, my_key = {}, None
    for k, v in teams.items():
        if k == "count":
            continue
        m = {kk: vv for d in v["team"][0] if isinstance(d, dict) for kk, vv in d.items()}
        if m.get("draft_position"):
            order[m["team_key"]] = int(m["draft_position"])
        if m.get("is_owned_by_current_login"):
            my_key = m["team_key"]
    y.update(order=order, my_key=my_key, my_slot=order.get(my_key))

    market = db.kv_get("draft_market") or {}
    ymap = {k: int(v) for k, v in (market.get("ymap") or {}).items()}
    fresh = market.get("at") and time.time() - market["at"] < MARKET_HOURS * 3600
    if not fresh and y.get("status") != "postdraft":
        idx, rows = yahoo._player_index(), {}
        positions = []
        for start in range(0, MARKET_DEPTH, 25):
            raw = q.get_response(f"{API}/league/{lk}/players;sort=AR;count=25;start={start}/draft_analysis").json()
            page = _players_page(raw)
            for i, p in enumerate(page):
                pid = yahoo._match(idx, p["name"], p["team"])
                if pid:
                    pid = int(pid)
                    ymap[p["yid"]] = pid
                    rows[str(pid)] = {"rank": start + i + 1, "adp": p["adp"]}
                    if p["pos"]:
                        positions.append((p["pos"], pid))
            if len(page) < 25:
                break
        with db.cursor() as c:   # Yahoo eligibility: same in every league, and the lineup code reads it
            c.executemany("UPDATE players SET positions=? WHERE nba_id=?", positions)
        market = {"at": time.time(), "players": rows, "ymap": {k: v for k, v in ymap.items()}}
        db.kv_set("draft_market", market)

    res = q.get_response(f"{API}/league/{lk}/draftresults").json()
    results = parse_draft_results(res)
    if results:
        ymap = _resolve(q, lk, [r["player_key"] for r in results], ymap)
        market["ymap"] = ymap
        db.kv_set("draft_market", market)
    y["picks"] = [{"pick": r["pick"], "team_key": r["team_key"], "cost": r["cost"], "source": "yahoo",
                   "nba_id": ymap.get(r["player_key"].split(".p.")[-1]),
                   "slot": order.get(r["team_key"])} for r in results]
    y["unmatched"] = [r["player_key"] for r in results if r["player_key"].split(".p.")[-1] not in ymap]
    y["synced"] = db.now_iso()
    y["synced_ts"] = time.time()
    db.kv_set("draft_yahoo", y)
    return y


# ---------------------------------------------------------------- the board

def _value_table(ctx, s) -> dict:
    left = model.season_games_left()
    out = {}
    for pid in ctx.values.index:
        if not isinstance(ctx.team_of(pid), str):   # not on an NBA roster
            continue
        per_game = float(ctx.values.at[pid, "value"])
        share = model.ros_availability(pid, ctx.injuries, ctx.events) if s["injuries"] else model.HEALTHY_ROS
        out[int(pid)] = {"per_game": per_game, "games": left * share, "ros": per_game * left * share,
                         "sd": float(ctx.values.at[pid, "value_sd"]) if "value_sd" in ctx.values else None}
    return out


def _fill(ctx, pids) -> int:
    """Starting slots these players can fill at once."""
    slots = lineup.slot_list()
    cands = [(p, lineup.positions(ctx, p)[0], 1.0) for p in pids]
    _, total = lineup.solve(cands, slots)
    return int(round(total))


def board(sims=SIMS, seed=None) -> dict:
    ctx = pipeline.context()
    s = settings()
    n, rounds = s["num_teams"], s["rounds"]
    total_picks = n * rounds
    ps = picks()
    drafted = {p["nba_id"]: p for p in ps if p.get("nba_id") is not None}
    vals = _value_table(ctx, s)
    ranked = sorted(vals, key=lambda p: -vals[p]["ros"])
    repl = vals[ranked[min(total_picks, len(ranked) - 1)]]["ros"] if ranked else 0.0
    market = (db.kv_get("draft_market") or {}).get("players", {})
    y = db.kv_get("draft_yahoo") or {}
    order_map = (y.get("order") if s["mode"] == "live" else None) or s["team_order"] or {}
    teams = ctx.teams
    avoid = set(int(a) for a in s["avoid"] or [])

    next_pick = max((p["pick"] for p in ps), default=0) + 1
    done = next_pick > total_picks
    mine_slot = s["my_slot"]
    my_key = y.get("my_key") or ctx.my_key
    my_picks = [p for p in ps if (p.get("team_key") == my_key if p.get("team_key") and s["mode"] == "live"
                                  else mine_slot and p["slot"] == mine_slot)]
    my_ids = [p["nba_id"] for p in my_picks if p.get("nba_id") is not None]
    upcoming = [k for k in range(next_pick, total_picks + 1) if mine_slot and slot_of(k, n, s["order"]) == mine_slot]

    pool = [p for p in ranked if p not in drafted][:POOL]
    vor = {p: vals[p]["ros"] - repl for p in pool}

    model_rank = {p: i + 1 for i, p in enumerate(ranked)}
    rng = np.random.default_rng(seed)
    base = np.array([_opp_rank(s, market, model_rank, p) for p in pool], dtype=float)
    noisy = base[None, :] * np.exp(rng.normal(0, s["randomness"], (sims, len(pool))))
    orders = np.argsort(noisy, axis=1)          # each row: the order others take players in
    by_vor = sorted(range(len(pool)), key=lambda i: -vor[pool[i]])

    # roster needs: starting slots my picks fill, and whether a candidate adds one
    filled = _fill(ctx, my_ids) if my_ids else 0
    n_slots = len(lineup.slot_list())
    picks_left_mine = len(upcoming)
    must_fill = picks_left_mine and (n_slots - filled) >= picks_left_mine
    adds_slot = {}
    for i in by_vor[:60]:
        adds_slot[pool[i]] = _fill(ctx, my_ids + [pool[i]]) > filled

    def survive(start, stop, my_turns, take_first=None):
        """P(each pool player is still there at overall pick `stop`), simulating picks from
        `start` (others by noisy rank, mine by VOR). take_first: pool index I take at `start`."""
        alive = np.zeros(len(pool))
        for row in orders:
            taken = set()
            ptr = 0
            for k in range(start, stop):
                if k in my_turns:
                    if k == start and take_first is not None:
                        taken.add(take_first)
                        continue
                    best = next((i for i in by_vor if i not in taken), None)
                    if best is not None:
                        taken.add(best)
                    continue
                while ptr < len(row) and row[ptr] in taken:
                    ptr += 1
                if ptr < len(row):
                    taken.add(row[ptr])
            mask = np.ones(len(pool), bool)
            mask[list(taken)] = False
            alive += mask
        return alive / len(orders)

    on_clock = None if done else {"pick": next_pick, "round": (next_pick - 1) // n + 1,
                                  "slot": slot_of(next_pick, n, s["order"]),
                                  "mine": bool(upcoming and upcoming[0] == next_pick)}
    if on_clock:
        on_clock["picks_until_mine"] = (upcoming[0] - next_pick) if upcoming else None
        on_clock["team"] = next((t["name"] for k, t in teams.items() if order_map.get(k) == on_clock["slot"]), None)

    p_next = p_after = None
    horizon = [u for u in upcoming if u >= next_pick]
    if not done and horizon:
        first = horizon[0]
        mine_set = set(horizon)
        if first > next_pick:
            p_next = survive(next_pick, first, mine_set)
        if len(horizon) > 1:
            p_after = survive(next_pick, horizon[1], mine_set)

    suggestion = None
    if on_clock and on_clock["mine"] and not s["auction"]:
        cands = [i for i in by_vor if pool[i] not in avoid]
        if must_fill:
            fill = [i for i in cands if adds_slot.get(pool[i])]
            cands = fill or cands
        cands = cands[:CANDIDATES]
        nxt = horizon[1] if len(horizon) > 1 else None
        scored = []
        for i in cands:
            if nxt is None:
                scored.append((vor[pool[i]], i, None, None))
                continue
            k = nxt - next_pick - 1                     # others' picks before my next turn
            best_next = []
            for row in orders:
                gone = {i}
                for j in row:
                    if len(gone) > k:
                        break
                    gone.add(j)
                best = next((b for b in by_vor if b not in gone and pool[b] not in avoid), None)
                best_next.append(vor[pool[best]] if best is not None else 0.0)
            # back-to-back picks (k = 0): nobody picks in between, so he is certainly back
            back = float(np.mean([i not in set(row[:k]) for row in orders])) if k > 0 else 1.0
            scored.append((vor[pool[i]] + float(np.mean(best_next)), i, float(np.mean(best_next)), back))
        scored.sort(key=lambda x: -x[0])
        suggestion = [{"nba_id": int(pool[i]), "name": ctx.name(pool[i]), "score": round(sc, 1),
                       "vor": round(vor[pool[i]], 1), "next_best": round(nb, 1) if nb is not None else None,
                       "p_back": back} for sc, i, nb, back in scored[:5]]
        if must_fill:
            for x in suggestion:
                x["fills_slot"] = True

    units = "fantasy pts" if model.scoring_mode() == "points" else "value"
    dollars = {}
    if s["auction"]:
        pos_vor = [max(vor[p], 0.0) for p in pool]
        top = sorted(pos_vor, reverse=True)[:total_picks]
        spare = n * float(s["budget"]) - total_picks
        tot = sum(top) or 1.0
        dollars = {p: 1 + max(vor[p], 0.0) / tot * spare for p in pool}

    rows = []
    for i, p in enumerate(pool):
        v, m = vals[p], market.get(str(p)) or {}
        inj = ctx.injuries.get(p)
        rows.append({"nba_id": p, "name": ctx.name(p), "team": ctx.team_of(p),
                     "pos": "/".join(sorted(lineup.positions(ctx, p)[0] - {"?"})) or "?",
                     "per_game": round(v["per_game"], 1), "games": round(v["games"], 1),
                     "ros": round(v["ros"]), "vor": round(vor[p]), "model_rank": model_rank[p],
                     "yahoo_rank": m.get("rank"), "adp": m.get("adp"),
                     "p_next": round(float(p_next[i]), 2) if p_next is not None else None,
                     "p_after": round(float(p_after[i]), 2) if p_after is not None else None,
                     "injury": f"{inj['status']}: {inj['comment']}" if inj else None,
                     "take": bool(ctx.takes.get(p)), "avoid": p in avoid,
                     "adds_slot": adds_slot.get(p), "dollars": round(dollars[p]) if p in dollars else None})

    team_name = {}
    for k, t in teams.items():
        sl = order_map.get(k)
        if sl:
            team_name[sl] = t["name"]
    log_rows = [{"pick": p["pick"], "round": (p["pick"] - 1) // n + 1, "slot": p["slot"],
                 "team": team_name.get(p["slot"]) or f"Slot {p['slot']}",
                 "mine": p in my_picks, "nba_id": p.get("nba_id"),
                 "name": ctx.name(p["nba_id"]) if p.get("nba_id") is not None else "(unknown player)",
                 "source": p.get("source"), "cost": p.get("cost")} for p in ps]
    my_team = [{"nba_id": pid, "name": ctx.name(pid),
                "pos": "/".join(sorted(lineup.positions(ctx, pid)[0] - {"?"})) or "?",
                "per_game": round(vals[pid]["per_game"], 1) if pid in vals else None,
                "ros": round(vals[pid]["ros"]) if pid in vals else None} for pid in my_ids]
    return {"settings": s, "units": units, "replacement": round(repl), "games_left": round(model.season_games_left(), 1),
            "yahoo": {k: y.get(k) for k in ("status", "time", "pick_seconds", "synced", "unmatched", "auction")},
            "market_at": (db.kv_get("draft_market") or {}).get("at"),
            "on_clock": on_clock, "done": done, "my_upcoming": horizon[:4],
            "suggestion": suggestion, "must_fill": bool(must_fill),
            "my_team": my_team, "slots_filled": filled, "slots_total": n_slots,
            "picks": log_rows, "board": rows}


def draft_time_label(ts) -> str | None:
    if not ts:
        return None
    return datetime.fromtimestamp(ts, timezone.utc).astimezone().strftime("%a %b %d, %I:%M %p")
