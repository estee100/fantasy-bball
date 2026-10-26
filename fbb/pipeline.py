"""Refresh pipeline: fetch -> model -> simulate -> recommend -> learn. Run by the scheduler."""
import logging
import threading
import time
import traceback
from datetime import date, datetime, timedelta, timezone

from . import config, db, learning, model, news, nba, recommend, roles, sim  # noqa: F401

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
        if not (config.yahoo_authorized() and db.kv_get("yahoo_sync_verified")):
            status.pop("yahoo", None)  # sync isn't running: its old error is stale
        if daily or db.kv_get("daily_done") != date.today().isoformat():
            _step(status, "player_directory", nba.ingest_player_directory)
            _step(status, "schedule", nba.ingest_schedule)
            _step(status, "espn_positions", nba.ingest_espn_positions)
            for season in (config.PRIOR_SEASON, config.PRIOR2_SEASON):  # 2 back: tuning only
                if not _has_season(season):
                    _step(status, f"logs_{season}", nba.ingest_logs, [season])
        _step(status, "game_logs", nba.ingest_logs, [config.SEASON])
        _step(status, "injuries", news.fetch_injuries)
        _step(status, "news", news.fetch_news)
        if config.yahoo_authorized() and db.kv_get("yahoo_sync_verified"):
            from . import yahoo
            _step(status, "yahoo", yahoo.sync)
        elif config.yahoo_authorized() and not db.kv_get("yahoo_access_live"):
            # The sync has never run on real Yahoo data: until it's verified on a copy of the
            # database, only check whether Yahoo has switched access on (and say so once).
            from . import yahoo
            _step(status, "yahoo_probe", yahoo.probe)
        elif not db.kv_get("league_source"):
            from . import demo
            _step(status, "demo_league", demo.build)

        ctx = context(reload=True)
        fa = [int(p) for p in ctx.values.sort_values("var", ascending=False).index
              if p not in ctx.rostered()][:60]
        db.kv_set("top_free_agents", fa)
        err = db.kv_get("claude_error")
        if err and (err.get("key_tail") != config.ANTHROPIC_KEY[-6:] or err.get("workspace") != config.ANTHROPIC_WORKSPACE_ID):
            db.kv_set("claude_error", None)  # settings changed since the failure: try again
        _step(status, "news_extraction", news.extract_events)
        ctx = context(reload=True)  # pick up new news events

        _step(status, "roles", lambda: roles.store(roles.scan(ctx)) or "ok")
        m = None
        if ctx.rosters.get(ctx.opp_key):
            m = _step(status, "matchup_sim", sim.simulate_matchup, ctx)
        else:
            db.kv_set("matchup", None)  # no opponent known yet (manual mode)
        if m:
            learning.record_matchup_prediction(ctx, m)
            db.kv_set("matchup", {k: v for k, v in m.items() if k not in ("a", "b")})
        r = _step(status, "recommendations", recommend.recommend, ctx)
        if r:
            db.kv_set("recommendations", r)
        _step(status, "start_sit", lambda: db.kv_set("start_sit", recommend.start_sit_today(ctx)) or "ok")
        from . import shadow
        _step(status, "model_team", shadow.tick, ctx)
        _step(status, "evaluate_moves", learning.evaluate_moves, ctx.logs)
        _step(status, "resolve_matchups", learning.resolve_from_box_scores, ctx)
        from . import alerts, history
        _step(status, "history", history.evaluate, ctx)
        _step(status, "alerts", alerts.run, ctx)
        _step(status, "league_table", lambda: db.kv_set("league_table", league_table(ctx)) or "ok")

        if daily or db.kv_get("daily_done") != date.today().isoformat():
            _step(status, "tune_projections", learning.tune_projections)
            _step(status, "news_multipliers", learning.learn_news_multipliers)
            last_bt = db.kv_get("matchup_backtest") or {}
            if not last_bt or (datetime.now(timezone.utc) - datetime.fromisoformat(last_bt["created"])).days >= 7:
                from . import backtest  # weekly: re-check calibration of win probabilities
                from . import matchups
                _step(status, "opp_adjust_check", matchups.recheck_and_store)
                _step(status, "matchup_backtest", lambda: backtest.store(backtest.calibrate()) or "ok")
            _step(status, "scouting_folder", lambda: len(news.ingest_report_folder()))
            _step(status, "backup", db.backup)
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
    rows = []
    for key, pids in ctx.rosters.items():
        dist = sim.team_season_dist(ctx, pids)
        rows.append({"team_key": key, "name": ctx.teams.get(key, {}).get("name", key),
                     "is_mine": key == ctx.my_key, "is_opp": key == ctx.opp_key,
                     "strength": sim.season_strength(ctx, pids, exclude_key=key) if pids else None,
                     "cats": {k: v[0] for k, v in dist.items()},
                     "record": [ctx.teams.get(key, {}).get(x) for x in ("wins", "losses", "ties")]})
    if model.scoring_mode() == "points":
        # one 'category' says nothing new: break each team's typical week into where its
        # fantasy points come from (turnovers are negative, so higher is still better)
        pw = model.point_weights()
        labels = {"pts": "Scoring", "reb": "Rebounds", "ast": "Assists", "stl": "Steals",
                  "blk": "Blocks", "tov": "Turnovers"}
        for r in rows:
            pids = ctx.rosters[r["team_key"]]
            games = sim.used_games_per_week(ctx, pids)
            contrib = {}
            for s, w in pw.items():
                contrib["fp_" + s] = float(sum(g * ctx.proj.loc[p, s] * w for p, g in games.items()
                                               if p in ctx.proj.index))
            r["total_fp"] = r["cats"].get("_points")
            r["cats"] = contrib
            r["labels"] = {"fp_" + s: labels.get(s, s.upper()) for s in pw}
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
    elif config.yahoo_authorized() and db.kv_get("yahoo_access_live") and not db.kv_get("yahoo_sync_verified"):
        checks.append({"name": "Yahoo API", "ok": False,
                       "detail": "access is ON: tell Claude, so it can verify the sync on a copy and switch over"})
    elif config.yahoo_authorized() and not db.kv_get("yahoo_sync_verified"):
        # Approved 2026-10-23; access turns on once Yahoo processes the DocuSign + confirmation
        # form. Each refresh probes for it (see yahoo.probe); pasted rosters until then.
        checks.append({"name": "Yahoo API", "ok": False,
                       "detail": "approved; waiting for Yahoo to switch access on (checked every refresh)"})
    elif config.yahoo_configured() and not config.yahoo_authorized():
        checks.append({"name": "Yahoo rosters", "ok": False,
                       "detail": "credentials set, not authorized yet: run `python -m fbb auth` in a terminal"})
    else:
        checks.append({"name": "Yahoo rosters", "ok": False, "detail": "demo league: add Yahoo credentials to .env"})
    if src in ("manual", "yahoo"):
        # a roster over the limit usually means an IL move the app hasn't been told about,
        # and it inflates every projection for that team
        with db.cursor() as c:
            mine = c.execute("SELECT team_key FROM fantasy_teams WHERE is_mine=1").fetchone()
            if mine:
                n = c.execute("SELECT count(*) FROM rosters WHERE team_key=? AND coalesce(slot,'') NOT IN ('IL','IL+','NA')",
                              (mine[0],)).fetchone()[0]
                limit = db.kv_get("roster_size", 13)
                if n > limit:
                    checks.append({"name": "Roster size", "ok": False,
                                   "detail": f"your team has {n} active players, the league allows {limit}: set an IL slot "
                                             f"or record the drop in League setup (projections are inflated until then)"})
    if src == "manual":
        from . import manual
        info = manual.summary()
        others = [t for t in info["teams"] if not t["is_mine"]]
        stale = []
        log_at = db.kv_get("yahoo_log_imported")   # a transaction-log import keeps every roster current
        for t in info["teams"]:
            seen = max([x for x in (t["updated"], log_at) if x], default=None)
            if seen:
                age_d = (now - datetime.fromisoformat(seen)).days
                if age_d >= 7:
                    stale.append(t["name"])
        checks.append({"name": "Pasted rosters", "ok": len(others) >= info["num_teams"] - 1 and not stale,
                       "detail": f"{len(others)} of {info['num_teams'] - 1} other teams pasted"
                                 + (f"; not updated for a week (paste the transaction log): {', '.join(stale)}" if stale else "")
                                 + ("" if info["opponent_key"] else "; no opponent picked")})
        fa = db.kv_get("fa_list")
        if not fa:
            checks.append({"name": "Free-agent list", "ok": False,
                           "detail": "not pasted: availability is inferred from rosters (Setup > Free agents)"})
        else:
            from . import free_agents
            if free_agents.snapshot() is None:
                checks.append({"name": "Free-agent list", "ok": False,
                               "detail": "older than 3 days: re-paste it in Setup so pickups are current"})
    err = db.kv_get("claude_error")
    if err:
        checks.append({"name": "News extraction", "ok": False,
                       "detail": f"Claude key problem ({err['status']}): {err['message'][:160]} — using keywords meanwhile"})
    else:
        checks.append({"name": "News extraction", "ok": bool(config.ANTHROPIC_KEY),
                       "detail": "Claude" if config.ANTHROPIC_KEY else "keyword tagger only (no ANTHROPIC_API_KEY)"})
    return checks
