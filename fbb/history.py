"""Transaction history: every roster change you make, scored against what actually happened.

A transaction is "players in" vs "players out" on an effective date. It doesn't matter where
it came from (a suggestion you followed, your own call, a trade, a paste you updated days
later, or later the Yahoo feed): each one stores

  what the model thought at the time   (per-game projections then; expected wins for 1-for-1
                                        moves and trades)
  what happened since                  (fantasy points / value of the players in vs out,
                                        from the effective date on)

and your own calls are scored exactly like the model's, so the history can show who's been
right more often. Effective dates are editable: a move you made Tuesday and entered Thursday
is scored from Tuesday.
"""
import json
from datetime import date, timedelta

import pandas as pd

from . import config, db, learning, model

HORIZON_DAYS = {"trade": 28}
DEFAULT_HORIZON = 21

SCHEMA = """
CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, created TEXT, effective TEXT, kind TEXT,
    team_key TEXT, other_key TEXT, players_in TEXT, players_out TEXT,
    source TEXT, note TEXT, move_id INTEGER, yahoo_key TEXT UNIQUE,
    prediction TEXT, eval TEXT, evaluated_at TEXT, deleted INTEGER DEFAULT 0
);
"""


def _ensure():
    with db.cursor() as c:
        c.executescript(SCHEMA)


def _unit():
    return "fantasy pts" if model.scoring_mode() == "points" else "value"


def _per_game_asof(ctx, pids, as_of: str) -> dict:
    """Each player's projected per-game production as the model saw it on `as_of`
    (projections rebuilt from games before that date, so a backdated entry is judged on
    what was knowable then, not on what we know now)."""
    if not pids:
        return {}
    if as_of >= date.today().isoformat():
        col = ctx.values["value"]
        return {p: float(col.get(p, 0.0)) for p in pids}
    proj = model.project(ctx.logs, ctx.params, as_of=as_of)
    rows = proj.loc[[p for p in pids if p in proj.index]]
    if rows.empty:
        return {p: 0.0 for p in pids}
    if model.scoring_mode() == "points":
        pw = model.point_weights()
        vals = sum(rows[s] * w for s, w in pw.items() if s in rows)
    else:
        vals = model.line_values(rows.reset_index()) .set_axis(rows.index)
    return {p: float(vals.get(p, 0.0)) for p in pids}


def predict(ctx, kind, ins, outs, effective, other_key=None) -> dict:
    """The model's view at the time. Expected wins only where it's well defined: a 1-for-1
    add/drop (full simulation) or a trade (season formula); otherwise per-game numbers."""
    from . import recommend, sim
    pg = _per_game_asof(ctx, list(ins) + list(outs), effective)
    out = {"unit": _unit(), "per_game_in": {str(p): pg.get(p, 0) for p in ins},
           "per_game_out": {str(p): pg.get(p, 0) for p in outs},
           "per_game_diff": sum(pg.get(p, 0) for p in ins) - sum(pg.get(p, 0) for p in outs),
           "as_of": effective}
    try:
        if kind == "trade" and ins and outs:
            r = recommend.evaluate_trade(ctx, list(outs), list(ins))
            out["season_dwins"] = r["my_dwins"]
            out["their_dwins"] = r["their_dwins"]
        elif len(ins) == 1 and len(outs) == 1 and effective >= date.today().isoformat():
            B = recommend.baseline(ctx, n_sims=2000)
            m = recommend.score_swap(ctx, B, ins[0], outs[0])
            out.update(season_dwins=m["season_dwins"], week_dwin=m["week_dwin"], score=m["score"],
                       raw_confidence=m["raw_confidence"], confidence=learning.calibrate(m["raw_confidence"]))
        elif ins or outs:
            before = list(ctx.rosters.get(ctx.my_key, []))  # record() sets the pre-move roster
            after = [p for p in before if p not in outs] + [p for p in ins if p not in before]
            out["season_dwins"] = (sim.season_strength(ctx, after) - sim.season_strength(ctx, before)) \
                * recommend.effective_weeks(ctx)
    except Exception as e:  # a prediction is nice-to-have; never block recording the move
        out["error"] = f"{type(e).__name__}: {e}"
    return out


def record(ctx, kind: str, ins: list, outs: list, effective: str | None = None, other_key=None,
           source="manual", note="", move_id=None, apply=False, yahoo_key=None) -> int:
    """Log a roster change on your team. apply=True also makes the change to the pasted
    rosters (manual mode); the roster editor and pastes call this after they've applied."""
    _ensure()
    from . import manual
    effective = effective or date.today().isoformat()
    ins, outs = [int(p) for p in ins], [int(p) for p in outs]
    me = ctx.my_key
    # The model's view must be of the roster BEFORE the move. With apply=True the stored
    # roster still is; otherwise the caller already changed it, so rebuild the before-state.
    current = list(ctx.rosters.get(me, []))
    before = current if apply else [p for p in current if p not in ins] + [p for p in outs if p not in current]
    saved = dict(ctx.rosters)
    other_before = None
    if kind == "trade" and other_key and not apply:
        other_before = [p for p in ctx.rosters.get(other_key, []) if p not in outs] + ins
    try:
        ctx.rosters[me] = before
        if other_before is not None:
            ctx.rosters[other_key] = other_before
        ctx._pools.pop("_opp_dists", None)
        pred = predict(ctx, kind, ins, outs, effective, other_key)
    finally:
        ctx.rosters.clear()
        ctx.rosters.update(saved)
        ctx._pools.pop("_opp_dists", None)
    if apply:
        _apply(ctx, kind, ins, outs, other_key)
    with db.cursor() as c:
        cur = c.execute("""INSERT INTO transactions(created, effective, kind, team_key, other_key, players_in,
                           players_out, source, note, move_id, yahoo_key, prediction)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (db.now_iso(), effective, kind, me, other_key, json.dumps(ins), json.dumps(outs),
                         source, note, move_id, yahoo_key, json.dumps(pred, default=float)))
        tid = cur.lastrowid
    if source != "yahoo-log":   # the Yahoo log stores exact times itself
        _log_moves(me, kind, ins, outs, other_key, effective, db.now_iso(), source)
    if me:
        db.kv_set("my_roster_last", _roster(me))
    return tid


def _log_moves(me, kind, ins, outs, other_key, effective, created, source):
    """Timestamped adds/drops for week-to-date scoring. Entered today: the time it was
    entered; backdated: start of that day (local)."""
    from datetime import datetime
    when = datetime.fromisoformat(created).astimezone()
    ts = when.isoformat() if when.date().isoformat() == effective else \
        datetime.fromisoformat(effective).astimezone().isoformat()
    for p in ins:
        db.add_move(me, ts, p, "add", source)
    for p in outs:
        db.add_move(me, ts, p, "drop", source)
    if kind == "trade" and other_key:
        for p in outs:
            db.add_move(other_key, ts, p, "add", source)
        for p in ins:
            db.add_move(other_key, ts, p, "drop", source)


def backfill_moves():
    """One-off: timestamps for History entries made before league_moves existed."""
    _ensure()
    with db.cursor() as c:
        rows = c.execute("SELECT * FROM transactions WHERE deleted=0 AND source<>'yahoo-log'").fetchall()
    for r in rows:
        _log_moves(r["team_key"], r["kind"], json.loads(r["players_in"]), json.loads(r["players_out"]),
                   r["other_key"], r["effective"], r["created"], r["source"])
    return len(rows)


def _roster(key):
    with db.cursor() as c:
        return [r[0] for r in c.execute("SELECT nba_id FROM rosters WHERE team_key=?", (key,))]


def _apply(ctx, kind, ins, outs, other_key):
    """Make the change to the stored rosters (manual mode), validating ownership first."""
    me = ctx.my_key
    with db.cursor() as c:
        owner = {r["nba_id"]: r["team_key"] for r in c.execute("SELECT nba_id, team_key FROM rosters")}
        names = {r["team_key"]: r["name"] for r in c.execute("SELECT team_key, name FROM fantasy_teams")}
    for p in outs:
        if owner.get(p) != me:
            raise ValueError(f"{ctx.name(p)} isn't on your team")
    for p in ins:
        o = owner.get(p)
        if kind == "trade":
            if o != other_key:
                raise ValueError(f"{ctx.name(p)} isn't on {names.get(other_key, 'the other team')}")
        elif o:
            raise ValueError(f"{ctx.name(p)} is on {names.get(o, o)}; record a trade instead")
    with db.cursor() as c:
        for p in outs:
            c.execute("DELETE FROM rosters WHERE team_key=? AND nba_id=?", (me, p))
            if kind == "trade":
                c.execute("INSERT OR REPLACE INTO rosters VALUES (?,?,?)", (other_key, p, "BN"))
        for p in ins:
            if kind == "trade":
                c.execute("DELETE FROM rosters WHERE team_key=? AND nba_id=?", (other_key, p))
            c.execute("INSERT OR REPLACE INTO rosters VALUES (?,?,?)", (me, p, "BN"))
    from . import manual
    for k in filter(None, (me, other_key if kind == "trade" else None)):
        manual._touch(k)


def update(tid: int, effective=None, note=None):
    _ensure()
    with db.cursor() as c:
        if effective:
            # a new date means a new scoring window: re-evaluate from scratch
            c.execute("UPDATE transactions SET effective=?, eval=NULL, evaluated_at=NULL WHERE id=?", (effective, tid))
        if note is not None:
            c.execute("UPDATE transactions SET note=? WHERE id=?", (note, tid))


def delete(tid: int, revert=True) -> dict:
    """Remove a mistaken entry. revert=True also undoes its roster change, its timestamped
    moves and its 'executed' mark on a suggestion, as long as the roster still shows the
    move (if you've changed those players since, the roster is left alone and that's said)."""
    _ensure()
    with db.cursor() as c:
        t = c.execute("SELECT * FROM transactions WHERE id=?", (tid,)).fetchone()
        if t is None:
            raise ValueError("No such entry")
        c.execute("UPDATE transactions SET deleted=1 WHERE id=?", (tid,))
    out = {"deleted": tid, "roster_reverted": False}
    if not revert:
        return out
    ins, outs = json.loads(t["players_in"]), json.loads(t["players_out"])
    me, other = t["team_key"], t["other_key"]
    with db.cursor() as c:
        held = {r["nba_id"]: r["team_key"] for r in c.execute("SELECT team_key, nba_id FROM rosters")}
        still = all(held.get(p) == me for p in ins) and all(held.get(p) in (None, other) for p in outs)
        if still:
            for p in ins:
                c.execute("DELETE FROM rosters WHERE team_key=? AND nba_id=?", (me, p))
                if t["kind"] == "trade" and other:
                    c.execute("INSERT OR REPLACE INTO rosters VALUES (?,?,?)", (other, p, "BN"))
            for p in outs:
                c.execute("DELETE FROM rosters WHERE nba_id=?", (p,))
                c.execute("INSERT OR REPLACE INTO rosters VALUES (?,?,?)", (me, p, "BN"))
            out["roster_reverted"] = True
        else:
            out["note"] = "Roster not changed: those players have moved since. Fix it in the roster editor."
        c.execute("DELETE FROM league_moves WHERE source=? AND team_key IN (?, ?) AND nba_id IN (%s)"
                  % ",".join("?" * len(ins + outs)), [t["source"], me, other or me] + ins + outs) if ins + outs else None
        if t["move_id"]:
            c.execute("UPDATE moves SET status='suggested', executed_at=NULL WHERE id=? AND status='executed'", (t["move_id"],))
    if me:
        db.kv_set("my_roster_last", _roster(me))
    return out


def evaluate(ctx) -> int:
    """Score every transaction: production of players in vs out from the effective date on.
    Running total until the horizon passes, then final."""
    _ensure()
    pw = model.point_weights() if model.scoring_mode() == "points" else None
    with db.cursor() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM transactions WHERE deleted=0")]
    today = date.today().isoformat()
    n = 0
    for t in rows:
        prev = json.loads(t["eval"]) if t["eval"] else None
        if prev and prev.get("final"):
            continue
        end = (date.fromisoformat(t["effective"]) + timedelta(days=HORIZON_DAYS.get(t["kind"], DEFAULT_HORIZON))).isoformat()
        g = ctx.logs[(ctx.logs["game_date"] >= t["effective"]) & (ctx.logs["game_date"] <= end)]
        def total(ids):
            sel = g[g["nba_id"].isin(ids)]
            if sel.empty:
                return 0.0, 0
            v = sum(sel[s] * w for s, w in pw.items()) if pw else model.line_values(sel)
            return float(v.sum()), int(len(sel))
        ins, outs = json.loads(t["players_in"]), json.loads(t["players_out"])
        pin, gin = total(ins)
        pout, gout = total(outs)
        res = {"in": pin, "out": pout, "games_in": gin, "games_out": gout, "diff": pin - pout,
               "through": min(today, end), "horizon_end": end, "final": today > end,
               "success": (pin - pout) > 0 if (gin or gout) else None}
        with db.cursor() as c:
            c.execute("UPDATE transactions SET eval=?, evaluated_at=? WHERE id=?",
                      (json.dumps(res), db.now_iso(), t["id"]))
        n += 1
    return n


def report(ctx) -> dict:
    _ensure()
    with db.cursor() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM transactions WHERE deleted=0 ORDER BY effective DESC, id DESC")]
        weeks = [dict(r) for r in c.execute("SELECT week, win_prob, result, opp_key FROM matchup_predictions "
                                            "WHERE kind='opening' ORDER BY week DESC")]
        names = {r["team_key"]: r["name"] for r in c.execute("SELECT team_key, name FROM fantasy_teams")}
    items = []
    for t in rows:
        ins, outs = json.loads(t["players_in"]), json.loads(t["players_out"])
        items.append({**{k: t[k] for k in ("id", "created", "effective", "kind", "source", "note", "move_id")},
                      "other_team": names.get(t["other_key"]),
                      "in": [{"nba_id": p, "name": ctx.name(p), "team": ctx.team_of(p)} for p in ins],
                      "out": [{"nba_id": p, "name": ctx.name(p), "team": ctx.team_of(p)} for p in outs],
                      "prediction": json.loads(t["prediction"] or "{}"),
                      "eval": json.loads(t["eval"]) if t["eval"] else None})
    def rate(sel):
        done = [i for i in sel if i["eval"] and i["eval"].get("final") and i["eval"].get("success") is not None]
        return {"n": len(done), "hits": sum(1 for i in done if i["eval"]["success"]),
                "net": sum(i["eval"]["diff"] for i in done)}
    yours = [i for i in items if i["source"] != "suggestion"]
    followed = [i for i in items if i["source"] == "suggestion"]
    for w in weeks:
        w["opponent"] = names.get(w.pop("opp_key"))
    return {"items": items, "weeks": weeks, "unit": _unit(),
            "scorecard": {"your_calls": rate(yours), "followed_suggestions": rate(followed)}}


def calibration_pairs() -> list:
    """(raw confidence, success) for finished 1-for-1 moves, for the confidence calibration."""
    _ensure()
    out = []
    with db.cursor() as c:
        for r in c.execute("SELECT prediction, eval FROM transactions WHERE deleted=0 AND eval IS NOT NULL"):
            p, e = json.loads(r["prediction"] or "{}"), json.loads(r["eval"])
            if e.get("final") and e.get("success") is not None and "raw_confidence" in p:
                out.append((p["raw_confidence"], 1 if e["success"] else 0))
    return out



def adds_this_week(ctx) -> int:
    """Players brought in this fantasy week (adds, add/drops, trades, roster changes with ins)."""
    _ensure()
    with db.cursor() as c:
        rows = c.execute("SELECT kind, players_in FROM transactions WHERE deleted=0 AND effective BETWEEN ? AND ?",
                         (ctx.week_start, ctx.week_end)).fetchall()
    # trades don't use an add in Yahoo; everything else that brought someone in does
    return sum(len(json.loads(r["players_in"])) for r in rows if r["kind"] != "trade")


def add_budget(ctx) -> dict:
    limit = db.kv_get("max_adds_week")
    used = adds_this_week(ctx)
    ya = db.kv_get("yahoo_adds") if db.kv_get("league_source") == "yahoo" else None
    if ya and ya["week"] == db.kv_get("current_week"):
        # Yahoo's count is authoritative; the log can still be ahead by a move made since the sync
        used = max(used, ya["used"])
    return {"limit": limit, "used": used, "left": None if not limit else max(0, limit - used)}
