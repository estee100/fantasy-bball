"""Watchlist: follow players you passed on, against the player you'd have dropped for them.

The comparison is the part that makes it meaningful: picking someone up means dropping
someone. Each entry records who that would have been (your weakest player at the time,
unless you choose), plus the model's projection for both on the day you started watching.

Two readings per entry:
  results so far   - fantasy points each scored since then (hindsight; noisy for weeks)
  projection drift - how the model's view of the watched player changed since then
                     (the better signal for whether to act now)
"""
import json
from datetime import date

from . import db, model


def _fp(lines) -> list:
    pw = model.point_weights()
    if not pw:  # category leagues: value above replacement per game instead of points
        return [float(x) for x in model.line_values(lines)] if len(lines) else []
    return [float(sum(r[s] * w for s, w in pw.items() if s in r)) for _, r in lines.iterrows()]


def add(ctx, pid: int, compare_id: int | None = None, note: str = "") -> dict:
    mine = ctx.rosters.get(ctx.my_key, [])
    if pid in mine:
        raise ValueError(f"{ctx.name(pid)} is already on your team")
    if compare_id is None and mine:
        compare_id = min(mine, key=lambda p: ctx.values["value"].get(p, -99))
    snap = {"value": float(ctx.values["value"].get(pid, 0)),
            "compare_value": float(ctx.values["value"].get(compare_id, 0)) if compare_id else None}
    with db.cursor() as c:
        c.execute("""INSERT INTO watchlist(created, nba_id, compare_id, note, snapshot, active)
                     VALUES (?,?,?,?,?,1)""", (date.today().isoformat(), int(pid),
                                              int(compare_id) if compare_id else None, note, json.dumps(snap)))
    return {"ok": True}


def remove(wid: int):
    with db.cursor() as c:
        c.execute("UPDATE watchlist SET active=0 WHERE id=?", (wid,))


def set_compare(ctx, wid: int, compare_id: int):
    with db.cursor() as c:
        row = c.execute("SELECT snapshot FROM watchlist WHERE id=?", (wid,)).fetchone()
        snap = json.loads(row["snapshot"]) if row else {}
        snap["compare_value"] = float(ctx.values["value"].get(compare_id, 0))
        c.execute("UPDATE watchlist SET compare_id=?, snapshot=? WHERE id=?",
                  (int(compare_id), json.dumps(snap), wid))


def report(ctx) -> list:
    with db.cursor() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM watchlist WHERE active=1 ORDER BY created DESC, id DESC")]
    owner = {p: k for k, ids in ctx.rosters.items() for p in ids}
    out = []
    for w in rows:
        pid, cid = w["nba_id"], w["compare_id"]
        snap = json.loads(w["snapshot"] or "{}")
        since = w["created"]

        def games(p):
            if p is None:
                return []
            g = ctx.logs[(ctx.logs["nba_id"] == p) & (ctx.logs["game_date"] > since)].sort_values("game_date")
            return list(zip(g["game_date"].dt.strftime("%m-%d"), _fp(g)))

        gw, gc = games(pid), games(cid)
        tot_w, tot_c = sum(v for _, v in gw), sum(v for _, v in gc)
        now_val = float(ctx.values["value"].get(pid, 0))
        own = owner.get(pid)
        out.append({
            "id": w["id"], "since": since, "note": w["note"],
            "nba_id": pid, "name": ctx.name(pid), "team": ctx.team_of(pid),
            "status": "on your team" if own == ctx.my_key else
                      (f"taken by {ctx.teams.get(own, {}).get('name', own)}" if own else "free agent"),
            "injury": (ctx.injuries.get(pid) or {}).get("status"),
            "compare_id": cid, "compare_name": ctx.name(cid) if cid else None,
            "games": len(gw), "compare_games": len(gc),
            "points": tot_w, "compare_points": tot_c, "diff": tot_w - tot_c,
            "per_game": tot_w / len(gw) if gw else None,
            "compare_per_game": tot_c / len(gc) if gc else None,
            "proj_then": snap.get("value"), "proj_now": now_val,
            "compare_proj_now": float(ctx.values["value"].get(cid, 0)) if cid else None,
            "log": [{"d": d, "fp": round(v, 1)} for d, v in gw[-12:]],
            "compare_log": [{"d": d, "fp": round(v, 1)} for d, v in gc[-12:]],
        })
    return out
