"""FastAPI app: JSON API + the dashboard, with the refresh pipeline on a background scheduler."""
import json
import logging
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path

import numpy as np
from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config, db, learning, model, news, pipeline, recommend, roles, sim

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"
REFRESH_MINUTES = 15


@asynccontextmanager
async def lifespan(app):
    sched = BackgroundScheduler()
    # next_run_time=None would add the job *paused* (APScheduler): leave it out unless running now
    first = _now_if_stale()
    sched.add_job(pipeline.refresh, "interval", minutes=REFRESH_MINUTES, id="refresh",
                  max_instances=1, coalesce=True, **({"next_run_time": first} if first else {}))
    sched.add_job(lambda: pipeline.refresh(daily=True), "cron", hour=9, minute=5, id="daily")
    sched.start()
    yield
    sched.shutdown(wait=False)


def _now_if_stale():
    from datetime import datetime, timezone
    last = db.kv_get("last_refresh")
    if not last or (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds() > REFRESH_MINUTES * 60:
        return datetime.now()
    return None


app = FastAPI(title="Fantasy Hoops HQ", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


def _json(obj):
    return JSONResponse(json.loads(json.dumps(obj, default=db._jsonable, allow_nan=False)
                                   if not _has_nan(obj) else json.dumps(_clean_nan(obj), default=db._jsonable)))


def _has_nan(o):
    try:
        json.dumps(o, default=db._jsonable, allow_nan=False)
        return False
    except ValueError:
        return True


def _clean_nan(o):
    if isinstance(o, float) and (o != o or o in (float("inf"), float("-inf"))):
        return None
    if isinstance(o, dict):
        return {k: _clean_nan(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean_nan(v) for v in o]
    if hasattr(o, "item"):
        return _clean_nan(o.item())
    return o


@app.middleware("http")
async def _no_stale_assets(request, call_next):
    # without this, browsers reuse an old app.js for hours after an update (new page, old code)
    resp = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.get("/")
def index():
    from fastapi.responses import HTMLResponse
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    for f in ("app.js", "style.css"):  # version the asset URLs so an update is always fetched
        v = int((STATIC / f).stat().st_mtime)
        html = html.replace(f'/static/{f}"', f'/static/{f}?v={v}"')
    return HTMLResponse(html)


def _player_row(ctx, pid, dates, slot=None):
    p = ctx.proj.loc[pid] if pid in ctx.proj.index else None
    v = ctx.values.loc[pid] if pid in ctx.values.index else None
    inj = ctx.injuries.get(pid)
    games = sim.player_games(ctx, pid, dates)
    recent = ctx.logs[ctx.logs["nba_id"] == pid].sort_values("game_date").tail(5)
    trend = None
    if len(recent) and v is not None and db.kv_get("value_scales"):
        trend = float(model.line_values(recent).mean() - v["var"])
    row = {"nba_id": int(pid), "name": ctx.name(pid), "team": ctx.team_of(pid), "slot": slot,
           "positions": ctx.players["positions"].get(pid) if "positions" in ctx.players else None,
           "injury": {"status": inj["status"], "comment": inj["comment"]} if inj else None,
           "games_left": len(games), "expected_games": round(sum(a for _, a in games), 2),
           "value": float(v["value"]) if v is not None else None,
           "var": float(v["var"]) if v is not None else None,
           "fp": float(v["value"]) if v is not None and model.scoring_mode() == "points" else None,
           "take": bool(ctx.takes.get(pid)),
           "trend": trend, "n_games": int(p["n_games"]) if p is not None else 0}
    if p is not None:
        row["proj"] = {s: round(float(p[s]), 2) for s in ("min", "pts", "reb", "ast", "stl", "blk", "fg3m", "tov")}
        row["proj"]["fg_pct"] = round(float(p["fgm"] / p["fga"]), 3) if p["fga"] else None
        row["proj"]["ft_pct"] = round(float(p["ftm"] / p["fta"]), 3) if p["fta"] else None
        row["z"] = {k: round(float(v[f"z_{k}"]), 2) for k in ctx.cats if v is not None and f"z_{k}" in v}
    return row


@app.get("/api/state")
def state():
    ctx = pipeline.context()
    dates = sim.remaining_dates(ctx)
    with db.cursor() as c:
        slots = {r["nba_id"]: r["slot"] for r in c.execute("SELECT nba_id, slot FROM rosters WHERE team_key=?",
                                                           (ctx.my_key,))}
        alerts = [dict(r) for r in c.execute(
            "SELECT a.*, p.name, p.team FROM alerts a JOIN players p USING(nba_id) WHERE a.active=1 "
            "ORDER BY a.severity DESC LIMIT 40")]
    owner = {p: k for k, ids in ctx.rosters.items() for p in ids}
    for a in alerts:
        a["detail"] = json.loads(a["detail"]) if a["detail"] else {}
        a["owner"] = "mine" if owner.get(a["nba_id"]) == ctx.my_key else ("rostered" if a["nba_id"] in owner else "free agent")
    mine = sorted(ctx.rosters.get(ctx.my_key, []), key=lambda p: -ctx.values["var"].get(p, -99))
    opp = sorted(ctx.rosters.get(ctx.opp_key, []), key=lambda p: -ctx.values["var"].get(p, -99))
    return _json({
        "league": {"name": db.kv_get("league_name"), "source": db.kv_get("league_source"),
                   "week": ctx.week, "week_range": [ctx.week_start, ctx.week_end],
                   "categories": [{"key": k, "label": config.CATEGORY_DEFS[k][0],
                                   "higher": config.CATEGORY_DEFS[k][4]} for k in ctx.cats],
                   "scoring_mode": model.scoring_mode(), "active_slots": ctx.active_slots,
                   "me": ctx.teams.get(ctx.my_key, {}).get("name"),
                   "opponent": ctx.teams.get(ctx.opp_key, {}).get("name"),
                   "weeks_remaining": ctx.weeks_remaining},
        "last_refresh": db.kv_get("last_refresh"),
        "readiness": pipeline.readiness(),
        "pipeline": db.kv_get("pipeline_status", {}),
        "matchup": db.kv_get("matchup"),
        "recommendations": db.kv_get("recommendations"),
        "start_sit": db.kv_get("start_sit", []),
        "alerts": alerts,
        "roster": [_player_row(ctx, p, dates, slots.get(p)) for p in mine],
        "opp_roster": [_player_row(ctx, p, dates) for p in opp],
        "league_table": db.kv_get("league_table", []),
        "calibration": db.kv_get("calibration"),
        "manual": _manual_summary(),
    })


def _manual_summary():
    if db.kv_get("league_source") != "manual":
        return None
    from . import manual
    return manual.summary()


@app.get("/api/news")
def get_news(player: int | None = None, limit: int = 60):
    ctx = pipeline.context()
    with db.cursor() as c:
        if player:
            rows = c.execute("SELECT * FROM news WHERE player_ids LIKE ? ORDER BY published DESC LIMIT ?",
                             (f"%{player}%", limit)).fetchall()
        else:
            rows = c.execute("SELECT * FROM news ORDER BY published DESC LIMIT ?", (limit,)).fetchall()
    owner = {p: k for k, ids in ctx.rosters.items() for p in ids}
    out = []
    for r in rows:
        pids = json.loads(r["player_ids"] or "[]")
        if player and player not in pids:
            continue
        ext = json.loads(r["extracted"]) if r["extracted"] else None
        out.append({"id": r["id"], "source": r["source"], "title": r["title"], "summary": r["summary"][:400],
                    "url": r["url"], "published": r["published"],
                    "players": [{"nba_id": p, "name": ctx.name(p),
                                 "mine": owner.get(p) == ctx.my_key, "rostered": p in owner} for p in pids[:4]],
                    "events": (ext or {}).get("events", []), "method": (ext or {}).get("method")})
    return _json(out)


@app.get("/api/player/{pid}")
def player(pid: int):
    ctx = pipeline.context()
    if pid not in ctx.players.index:
        raise HTTPException(404, "unknown player")
    row = _player_row(ctx, pid, sim.remaining_dates(ctx))
    with db.cursor() as c:
        from . import overrides
        take = overrides.active().get(pid)
        raw = ctx.raw_proj.loc[pid] if ctx.raw_proj is not None and pid in ctx.raw_proj.index else None
        row["take"] = {"active": take, "model_min": float(raw["min"]) if raw is not None else None,
                       "model_per_game": overrides.per_game(raw) if raw is not None else None,
                       "your_per_game": float(ctx.values["value"].get(pid, 0)) if take else None}
        row["alerts"] = [dict(r) | {"detail": json.loads(r["detail"])} for r in
                         c.execute("SELECT * FROM alerts WHERE nba_id=? AND active=1", (pid,))]
        row["scouting"] = [dict(r) | {"strengths": json.loads(r["strengths"]), "weaknesses": json.loads(r["weaknesses"])}
                           for r in c.execute("SELECT * FROM scouting WHERE nba_id=? ORDER BY created DESC", (pid,))]
        row["events"] = [dict(r) for r in c.execute(
            "SELECT * FROM news_events WHERE nba_id=? ORDER BY date DESC LIMIT 10", (pid,))]
    ranks = roles.baseline_skill_ranks(ctx.logs, [k for k in ctx.cats if k in roles.SKILLS] or list(roles.SKILLS))
    top = [k for k in ranks.loc[pid].dropna().sort_values(ascending=False).index][:3] if pid in ranks.index else ["pts"]
    row["skills"] = [roles.series(ctx, pid, k) for k in top]
    g = ctx.logs[ctx.logs["nba_id"] == pid].sort_values("game_date").tail(15)
    row["game_log"] = [{"date": d.strftime("%m-%d"), "team": t, "matchup": m, "min": mi, "pts": p, "reb": r,
                        "ast": a, "stl": s, "blk": b, "fg3m": f3, "tov": to, "fg": f"{int(fm)}/{int(fa)}"}
                       for d, t, m, mi, p, r, a, s, b, f3, to, fm, fa in
                       g[["game_date", "team", "matchup", "min", "pts", "reb", "ast", "stl", "blk", "fg3m", "tov",
                          "fgm", "fga"]].itertuples(index=False)][::-1]
    return _json(row)


@app.get("/api/players/search")
def search(q: str, limit: int = 12, scope: str = "all"):
    ctx = pipeline.context()
    ql = q.lower()
    owner = {p: k for k, ids in ctx.rosters.items() for p in ids}
    pool = ctx.rosters.get(ctx.my_key, []) if scope == "mine" else         [p for p in ctx.players.index if owner.get(p) != ctx.my_key] if scope == "others" else ctx.players.index
    hits = [p for p in pool if ql in str(ctx.players["name"].get(p, "")).lower()]
    hits.sort(key=lambda p: -ctx.values["var"].get(p, -99))
    return _json([{"nba_id": int(p), "name": ctx.name(p), "team": ctx.team_of(p),
                   "owner": ctx.teams.get(owner.get(p), {}).get("name", "Free agent")} for p in hits[:limit]])


class TradeIn(BaseModel):
    give: list[int]
    get: list[int]


@app.post("/api/trade")
def trade(t: TradeIn):
    ctx = pipeline.context()
    try:
        res = recommend.evaluate_trade(ctx, t.give, t.get)
    except ValueError as e:
        raise HTTPException(400, str(e))
    res["other_team_name"] = ctx.teams.get(res["other_team"], {}).get("name")
    return _json(res)


class StatusIn(BaseModel):
    status: str


@app.post("/api/moves/{move_id}")
def move_status(move_id: int, s: StatusIn):
    if s.status not in ("executed", "dismissed", "suggested"):
        raise HTTPException(400, "bad status")
    applied = False
    if s.status == "executed":
        with db.cursor() as c:
            cur = c.execute("SELECT status FROM moves WHERE id=?", (move_id,)).fetchone()
        if cur and cur["status"] == "executed":
            # a second click (the card stays up until the model re-runs) used to re-apply the
            # move to a roster that already had it and fail with a bare 400
            return {"ok": True, "roster_updated": False, "already": True}
        # A waiver player can't be added yet: it's a claim. Changing the roster now made the app
        # show a move that hadn't happened (Oct 2026: Bane in / White out before the claim ran).
        from . import free_agents
        with db.cursor() as c:
            mv = c.execute("SELECT add_id FROM moves WHERE id=?", (move_id,)).fetchone()
        av = free_agents.availability(mv["add_id"]) if mv else {}
        if av.get("status") == "W":
            learning.set_move_status(move_id, "claimed")
            return {"ok": True, "roster_updated": False, "claimed": True, "until": av.get("until")}
    if s.status == "executed" and db.kv_get("league_source") == "manual":
        # manual mode has no Yahoo sync to notice the change: apply it to the pasted roster
        from . import manual
        ctx = pipeline.context()
        with db.cursor() as c:
            mv = c.execute("SELECT add_id, drop_id FROM moves WHERE id=?", (move_id,)).fetchone()
        if mv and ctx.my_key:
            from . import history
            try:
                history.record(ctx, "add/drop", [mv["add_id"]], [mv["drop_id"]], source="suggestion",
                               move_id=move_id, apply=True)
                applied = True
            except ValueError as e:
                raise HTTPException(400, str(e))
    learning.set_move_status(move_id, s.status)
    if applied:
        _manual_changed()
    return {"ok": True, "roster_updated": applied}


@app.get("/api/moves/lookup")
def move_lookup(add: int, drop: int):
    ctx = pipeline.context()
    with db.cursor() as c:
        r = c.execute("SELECT id, status FROM moves WHERE add_id=? AND drop_id=? AND week=?", (add, drop, ctx.week)).fetchone()
    return dict(r) if r else {"id": None}


@app.get("/api/learning")
def learning_report():
    ctx = pipeline.context()
    rep = learning.report()
    for m in rep["moves"]:
        m["add_name"], m["drop_name"] = ctx.name(m["add_id"]), ctx.name(m["drop_id"])
    with db.cursor() as c:
        mb = db.kv_get("matchup_backtest")
        rep["matchup_backtest"] = {k: v for k, v in mb.items() if k not in ("rows", "form_table")} if mb else None
        rep["news_events_by_type"] = [dict(r) for r in c.execute(
            "SELECT event_type, count(*) n FROM news_events GROUP BY event_type ORDER BY n DESC")]
    return _json(rep)


@app.post("/api/refresh")
def do_refresh(daily: bool = False):
    import threading
    threading.Thread(target=pipeline.refresh, kwargs={"daily": daily}, daemon=True).start()
    return {"started": True}


class ReportIn(BaseModel):
    text: str
    source: str = "pasted"


@app.post("/api/reports")
def add_report(r: ReportIn):
    if len(r.text) < 50:
        raise HTTPException(400, "report too short")
    return news.ingest_report(r.text, r.source)


# ---------------------------------------------------------------- manual league mode

class ManualTeamIn(BaseModel):
    name: str
    text: str
    mine: bool = False


class ManualConfigIn(BaseModel):
    num_teams: int = 10
    active_slots: int = 10
    roster_size: int = 13
    point_weights: dict | None = None
    categories: list | None = None


class OpponentIn(BaseModel):
    team_key: str | None


def _manual_changed():
    """Re-run the model on the new rosters (in the background: takes a few seconds)."""
    import threading
    pipeline.context(reload=True)
    threading.Thread(target=pipeline.refresh, daemon=True).start()


@app.get("/api/manual")
def manual_get():
    from . import manual
    return _json(manual.summary())


@app.post("/api/manual/config")
def manual_config(cfg: ManualConfigIn):
    from . import manual
    manual.configure(cfg.num_teams, cfg.active_slots, cfg.roster_size, cfg.point_weights, cfg.categories)
    _manual_changed()
    return _json(manual.summary())


@app.post("/api/manual/team")
def manual_team(t: ManualTeamIn):
    from . import manual
    if db.kv_get("league_source") != "manual":
        raise HTTPException(400, "Save league settings first")
    if not t.name.strip():
        raise HTTPException(400, "Team name is required")
    try:
        res = manual.set_team(t.name.strip(), t.text, t.mine, pipeline.context())
    except ValueError as e:
        raise HTTPException(400, str(e))
    _manual_changed()
    return _json(res)


@app.delete("/api/manual/team/{key}")
def manual_delete(key: str):
    from . import manual
    manual.remove_team(key)
    _manual_changed()
    return {"ok": True}


@app.post("/api/manual/opponent")
def manual_opponent(o: OpponentIn):
    from . import manual
    manual.set_opponent(o.team_key)
    _manual_changed()
    return {"ok": True}



class RosterEditIn(BaseModel):
    team_key: str
    add: int | None = None
    drop: int | None = None
    slot: str = "BN"


class SlotIn(BaseModel):
    team_key: str
    nba_id: int
    slot: str


@app.get("/api/manual/roster/{key}")
def manual_roster(key: str):
    from . import manual
    return _json({"team_key": key, "players": manual.roster(key), "slots": manual.SLOT_CHOICES})


@app.post("/api/manual/roster")
def manual_roster_edit(e: RosterEditIn):
    from . import manual
    try:
        manual.edit_roster(e.team_key, e.add, e.drop, e.slot, pipeline.context())
    except ValueError as err:
        raise HTTPException(400, str(err))
    _manual_changed()
    return {"ok": True}


@app.post("/api/manual/slot")
def manual_slot(e: SlotIn):
    from . import manual
    try:
        manual.set_slot(e.team_key, e.nba_id, e.slot)
    except ValueError as err:
        raise HTTPException(400, str(err))
    _manual_changed()
    return {"ok": True}


# ---------------------------------------------------------------- watchlist

class WatchIn(BaseModel):
    nba_id: int
    compare_id: int | None = None
    note: str = ""


class CompareIn(BaseModel):
    compare_id: int


@app.get("/api/watchlist")
def watch_get():
    from . import watchlist
    ctx = pipeline.context()
    mine = [{"nba_id": int(p), "name": ctx.name(p)} for p in
            sorted(ctx.rosters.get(ctx.my_key, []), key=lambda p: ctx.values["value"].get(p, -99))]
    return _json({"items": watchlist.report(ctx), "my_roster": mine,
                  "unit": "fantasy pts" if model.scoring_mode() == "points" else "value"})


@app.post("/api/watchlist")
def watch_add(w: WatchIn):
    from . import watchlist
    try:
        return watchlist.add(pipeline.context(), w.nba_id, w.compare_id, w.note)
    except ValueError as err:
        raise HTTPException(400, str(err))


@app.post("/api/watchlist/{wid}")
def watch_compare(wid: int, c: CompareIn):
    from . import watchlist
    watchlist.set_compare(pipeline.context(), wid, c.compare_id)
    return {"ok": True}


@app.delete("/api/watchlist/{wid}")
def watch_remove(wid: int):
    from . import watchlist
    watchlist.remove(wid)
    return {"ok": True}



# ---------------------------------------------------------------- lineup optimizer

@app.get("/api/lineup")
def lineup_get():
    from . import lineup
    ctx = pipeline.context()
    if not ctx.my_key:
        return _json({"days": []})
    return _json(lineup.week_plan(ctx))


@app.post("/api/lineup/apply")
def lineup_apply():
    from . import lineup
    ctx = pipeline.context()
    res = lineup.apply_today(ctx)
    _manual_changed()
    return _json(res)



# ---------------------------------------------------------------- pickup check

class EvalIn(BaseModel):
    nba_id: int


@app.post("/api/evaluate")
def evaluate(e: EvalIn):
    ctx = pipeline.context()
    if e.nba_id not in ctx.players.index:
        raise HTTPException(404, "unknown player")
    if not ctx.my_key:
        raise HTTPException(400, "Set up your team first")
    return _json(recommend.evaluate_add(ctx, e.nba_id))



# ---------------------------------------------------------------- transaction history

class TxIn(BaseModel):
    kind: str                 # add | drop | add/drop | trade
    ins: list[int] = []
    outs: list[int] = []
    effective: str | None = None
    other_key: str | None = None
    note: str = ""


class TxEdit(BaseModel):
    effective: str | None = None
    note: str | None = None


@app.get("/api/history")
def history_get():
    from . import history
    return _json(history.report(pipeline.context()))


@app.post("/api/history")
def history_add(t: TxIn):
    from . import history
    if t.kind not in ("add", "drop", "add/drop", "trade"):
        raise HTTPException(400, "kind must be add, drop, add/drop or trade")
    if t.kind == "trade" and not (t.ins and t.outs and t.other_key):
        raise HTTPException(400, "A trade needs players both ways and the other team")
    if not (t.ins or t.outs):
        raise HTTPException(400, "Pick at least one player")
    if t.effective and t.effective > date.today().isoformat():
        raise HTTPException(400, "The date can't be in the future")
    ctx = pipeline.context()
    try:
        tid = history.record(ctx, t.kind, t.ins, t.outs, t.effective, t.other_key, source="manual",
                             note=t.note, apply=db.kv_get("league_source") == "manual")
    except ValueError as e:
        raise HTTPException(400, str(e))
    history.evaluate(pipeline.context(reload=True))
    _manual_changed()
    return {"ok": True, "id": tid}


@app.post("/api/history/{tid}")
def history_edit(tid: int, e: TxEdit):
    from . import history
    history.update(tid, e.effective, e.note)
    history.evaluate(pipeline.context())
    return {"ok": True}


@app.delete("/api/history/{tid}")
def history_delete(tid: int):
    from . import history
    try:
        res = history.delete(tid)
    except ValueError as e:
        raise HTTPException(404, str(e))
    if res.get("roster_reverted"):
        _manual_changed()
    return _json(dict(res, ok=True))



# ---------------------------------------------------------------- your take

class TakeIn(BaseModel):
    nba_id: int
    kind: str
    value: float
    note: str = ""


@app.get("/api/takes")
def takes_get():
    from . import overrides
    return _json(overrides.report(pipeline.context()))


@app.post("/api/take")
def take_set(t: TakeIn):
    from . import overrides
    try:
        res = overrides.set_take(pipeline.context(), t.nba_id, t.kind, t.value, t.note)
    except ValueError as e:
        raise HTTPException(400, str(e))
    _manual_changed()
    return _json(res)


@app.delete("/api/take/{nba_id}")
def take_clear(nba_id: int):
    from . import overrides
    overrides.clear(nba_id)
    _manual_changed()
    return {"ok": True}



# ---------------------------------------------------------------- phone alerts

class AlertCfgIn(BaseModel):
    enabled: bool | None = None
    types: dict | None = None
    lineup_hour: int | None = None
    new_topic: bool = False


@app.get("/api/alerts")
def alerts_get():
    from . import alerts
    return _json(alerts.settings())


@app.post("/api/alerts")
def alerts_set(a: AlertCfgIn):
    from . import alerts
    if a.new_topic:
        alerts.save(topic=alerts.new_topic())
    return _json(alerts.save(enabled=a.enabled, types=a.types, lineup_hour=a.lineup_hour))


@app.post("/api/alerts/test")
def alerts_test():
    from . import alerts
    s = alerts.settings()
    if not s.get("topic"):
        raise HTTPException(400, "Turn alerts on first")
    ok = alerts.send("Fantasy Hoops HQ", "Test alert: you're all set. Lineup and move alerts will arrive here.",
                     tags="white_check_mark")
    if not ok:
        raise HTTPException(502, "Couldn't reach ntfy.sh")
    return {"ok": True}



# ---------------------------------------------------------------- trade finder (runs in the background)

_trade_job = {"running": False, "done": 0, "total": 0, "result": None, "error": None, "at": None}


@app.post("/api/trades/find")
def trades_find():
    import threading
    from . import trades
    if _trade_job["running"]:
        return {"running": True}

    def work():
        _trade_job.update(running=True, done=0, total=0, error=None)
        try:
            res = trades.find(pipeline.context(), progress=lambda d, t: _trade_job.update(done=d, total=t))
            _trade_job.update(result=res, at=db.now_iso())
        except Exception as e:  # surface it in the UI instead of failing silently
            _trade_job["error"] = f"{type(e).__name__}: {e}"
        finally:
            _trade_job["running"] = False
    threading.Thread(target=work, daemon=True).start()
    return {"running": True}


@app.get("/api/trades/find")
def trades_status():
    return _json(_trade_job)



# ---------------------------------------------------------------- Yahoo free-agent list

class FaIn(BaseModel):
    text: str
    append: bool | None = None  # None: add to a list started in the last 30 min


@app.get("/api/free-agents")
def fa_get():
    from . import free_agents
    fa = db.kv_get("fa_list")
    if not fa:
        return {"pasted": False}
    snap = free_agents.snapshot()
    return {"pasted": True, "created": fa["created"], "fresh": snap is not None, "players": len(fa["players"]),
            "on_waivers": sum(1 for p in fa["players"].values() if p["status"] == "W")}


@app.post("/api/free-agents")
def fa_import(f: FaIn):
    from . import free_agents
    try:
        res = free_agents.import_list(f.text, append=f.append)
    except ValueError as e:
        raise HTTPException(400, str(e))
    _manual_changed()
    return _json(res)



class LimitIn(BaseModel):
    max_adds_week: int | None = None


@app.post("/api/add-limit")
def add_limit(l: LimitIn):
    db.kv_set("max_adds_week", l.max_adds_week if l.max_adds_week and l.max_adds_week > 0 else None)
    _manual_changed()
    return {"ok": True, "max_adds_week": db.kv_get("max_adds_week")}


@app.get("/api/add-limit")
def add_limit_get():
    from . import history
    return _json(history.add_budget(pipeline.context()))



# ---------------------------------------------------------------- Yahoo transaction log

class TxnIn(BaseModel):
    text: str


@app.post("/api/yahoo-log")
def yahoo_log_import(t: TxnIn):
    from . import yahoo_log
    try:
        res = yahoo_log.import_log(t.text)
    except ValueError as e:
        raise HTTPException(400, str(e))
    _manual_changed()
    return _json(res)



# ---------------------------------------------------------------- the model's own team

@app.get("/api/model-team")
def model_team():
    from . import shadow
    return _json(shadow.report(pipeline.context()))
