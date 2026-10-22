"""Refresh pipeline: fetch -> model -> simulate -> recommend -> learn. Run by the scheduler."""
import logging
import threading
import time
import traceback
from datetime import date, datetime, timedelta, timezone

from . import config, db, learning, model, news, nba, recommend, roles, sim

log = logging.getLogger(__name__)
_lock = threading.Lock()
_ctx = None


def context(reload=False) -> sim.Context:
    global _ctx
    if _ctx is None or reload:
        _ctx = sim.load_context()
    return _ctx


def _step(status, name, fn, *a):
    t = time.time()
    try:
        out = fn(*a)
        status[name] = {"ok": True, "secs": round(time.time() - t, 2), "result": _short(out), "at": db.now_iso()}
        return out
    except Exception as e:
        log.error("step %s failed: %s\n%s", name, e, traceback.format_exc())
        status[name] = {"ok": False, "error": f"{type(e).__name__}: {e}", "at": db.now_iso()}
        return None


def _short(x):
    if isinstance(x, (int, float, str)) or x is None:
        return x
    if isinstance(x, dict):
        return {k: v for k, v in x.items() if isinstance(v, (int, float, str))}
    return str(type(x).__name__)


def refresh(daily=False) -> dict:
    if not _lock.acquire(blocking=False):
        return {"skipped": "refresh already running"}
    try:
        status = db.kv_get("pipeline_status", {})
        if daily or db.kv_get("daily_done") != date.today().isoformat():
            _step(status, "player_directory", nba.ingest_player_directory)
            _step(status, "schedule", nba.ingest_schedule)
            for season in (config.PRIOR_SEASON, config.PRIOR2_SEASON):  # 2 back: tuning only
                if not _has_season(season):
                    _step(status, f"logs_{season}", nba.ingest_logs, [season])
        _step(status, "game_logs", nba.ingest_logs, [config.SEASON])
        _step(status, "injuries", news.fetch_injuries)
        _step(status, "news", news.fetch_news)
        if config.yahoo_authorized():
            from . import yahoo
            _step(status, "yahoo", yahoo.sync)
        elif not db.kv_get("league_source"):
            from . import demo
            _step(status, "demo_league", demo.build)

        ctx = context(reload=True)
        fa = [int(p) for p in ctx.values.sort_values("var", ascending=False).index
              if p not in ctx.rostered()][:60]
        db.kv_set("top_free_agents", fa)
        _step(status, "news_extraction", news.extract_events)
        ctx = context(reload=True)  # pick up new news events

        _step(status, "roles", lambda: roles.store(roles.scan(ctx)) or "ok")
        m = _step(status, "matchup_sim", sim.simulate_matchup, ctx)
        if m:
            learning.record_matchup_prediction(ctx, m)
            db.kv_set("matchup", {k: v for k, v in m.items() if k not in ("a", "b")})
        r = _step(status, "recommendations", recommend.recommend, ctx)
        if r:
            db.kv_set("recommendations", r)
        _step(status, "start_sit", lambda: db.kv_set("start_sit", recommend.start_sit_today(ctx)) or "ok")
        _step(status, "evaluate_moves", learning.evaluate_moves, ctx.logs)
        _step(status, "league_table", lambda: db.kv_set("league_table", league_table(ctx)) or "ok")

        if daily or db.kv_get("daily_done") != date.today().isoformat():
            _step(status, "tune_projections", learning.tune_projections)
            _step(status, "news_multipliers", learning.learn_news_multipliers)
            _step(status, "scouting_folder", lambda: len(news.ingest_report_folder()))
            db.kv_set("daily_done", date.today().isoformat())
            context(reload=True)
        db.kv_set("pipeline_status", status)
        db.kv_set("last_refresh", db.now_iso())
        return status
    finally:
        _lock.release()


def _has_season(season) -> bool:
    with db.cursor() as c:
        return c.execute("SELECT 1 FROM game_logs WHERE season=? LIMIT 1", (season,)).fetchone() is not None


def league_table(ctx) -> list:
    """Each fantasy team's typical-week category totals and league rank, plus overall strength."""
    avg = sim.league_average_dist(ctx)
    rows = []
    for key, pids in ctx.rosters.items():
        dist = sim.team_season_dist(ctx, pids)
        rows.append({"team_key": key, "name": ctx.teams.get(key, {}).get("name", key),
                     "is_mine": key == ctx.my_key, "is_opp": key == ctx.opp_key,
                     "strength": sim.season_strength(ctx, pids, avg) if avg else None,
                     "cats": {k: v[0] for k, v in dist.items()},
                     "record": [ctx.teams.get(key, {}).get(x) for x in ("wins", "losses", "ties")]})
    for k in (rows[0]["cats"] if rows else {}):
        higher = config.CATEGORY_DEFS[k][4] if k in config.CATEGORY_DEFS else True
        order = sorted(rows, key=lambda r: -r["cats"][k] if higher else r["cats"][k])
        for i, r in enumerate(order):
            r.setdefault("ranks", {})[k] = i + 1
    rows.sort(key=lambda r: -(r["strength"] or 0))
    return rows


def readiness() -> list:
    """Is the data good enough to trust today's numbers? Shown on the dashboard."""
    now = datetime.now(timezone.utc)

    def age_min(key):
        v = db.kv_get(key)
        return (now - datetime.fromisoformat(v)).total_seconds() / 60 if v else None

    checks = []
    with db.cursor() as c:
        last_log = c.execute("SELECT max(game_date) FROM game_logs WHERE season=?", (config.SEASON,)).fetchone()[0]
        last_game = c.execute("SELECT max(game_date) FROM schedule WHERE game_date < ?",
                              (date.today().isoformat(),)).fetchone()[0]
    checks.append({"name": "Box scores", "ok": bool(last_log and last_game and last_log >= last_game),
                   "detail": f"latest {last_log or 'none'} (last completed game day {last_game or 'n/a'})"})
    inj = age_min("injuries_fetched")
    checks.append({"name": "Injury report", "ok": inj is not None and inj < 60,
                   "detail": "never fetched" if inj is None else f"{inj:.0f} min old"})
    src = db.kv_get("league_source")
    if src == "yahoo":
        y = age_min("yahoo_synced")
        checks.append({"name": "Yahoo rosters", "ok": y is not None and y < 90,
                       "detail": "never synced" if y is None else f"{y:.0f} min old"})
        um = db.kv_get("unmatched_players", [])
        checks.append({"name": "Player matching", "ok": not um,
                       "detail": "all matched" if not um else "unmatched: " + ", ".join(um[:5])})
    elif config.yahoo_configured():
        checks.append({"name": "Yahoo rosters", "ok": False,
                       "detail": "credentials set, not authorized yet: run `python -m fbb auth` in a terminal"})
    else:
        checks.append({"name": "Yahoo rosters", "ok": False, "detail": "demo league: add Yahoo credentials to .env"})
    checks.append({"name": "News extraction", "ok": bool(config.ANTHROPIC_KEY),
                   "detail": "Claude" if config.ANTHROPIC_KEY else "keyword tagger only (no ANTHROPIC_API_KEY)"})
    return checks
