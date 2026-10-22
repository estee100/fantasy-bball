"""The feedback loop: log every call the model makes, check it against what happened, adjust.

Three things are learned, from three data sources of very different size:

1. Projection knobs (half-life, prior-season weight): walk-forward backtest over *every*
   player-game in the league. Thousands of samples a week, so this is where real learning happens.
2. News impact multipliers per event type: did "moved to bench" actually cut minutes 30%?
   Dozens to hundreds of events a season.
3. Confidence calibration of move recommendations: when the model says 75%, does the added
   player beat the dropped one 75% of the time? Only your league's moves count here
   (~30-60/season incl. dismissed suggestions, which are scored as counterfactuals), so
   calibration is shrunk hard toward the raw estimate until there's enough history.
"""
import json
import math
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd

from . import config, db, model

BINS = [0.0, 0.55, 0.65, 0.75, 0.85, 1.01]
CALIBRATION_PSEUDO = 6       # pseudo-observations per bin pulling toward the raw estimate
EVAL_HORIZON_DAYS = 21
PROVISIONAL_DAYS = 7


# ------------------------------------------------------------------ move ledger

def log_suggestions(ctx, moves):
    with db.cursor() as c:
        for m in moves:
            c.execute("""INSERT INTO moves(created, week, add_id, drop_id, pred_week_dwin, pred_season_dwins,
                         confidence, raw_confidence, rationale, status, kind, horizon_end)
                         VALUES (?,?,?,?,?,?,?,?,?, 'suggested', ?, ?)
                         ON CONFLICT(add_id, drop_id, week) DO UPDATE SET
                           pred_week_dwin=excluded.pred_week_dwin, pred_season_dwins=excluded.pred_season_dwins,
                           confidence=excluded.confidence, raw_confidence=excluded.raw_confidence,
                           rationale=excluded.rationale, kind=excluded.kind, horizon_end=excluded.horizon_end
                         WHERE moves.status='suggested'""",
                      (db.now_iso(), ctx.week, int(m["add"]), int(m["drop"]), m["week_dwin"],
                       m["season_dwins"], m["confidence"], m["raw_confidence"], json.dumps(m["rationale"]),
                       m["kind"], horizon_end(ctx, m["kind"])))


def horizon_end(ctx, kind) -> str:
    if kind == "stream":
        return ctx.week_end
    return (date.today() + timedelta(days=EVAL_HORIZON_DAYS)).isoformat()


def set_move_status(move_id: int, status: str):
    assert status in ("executed", "dismissed", "suggested")
    with db.cursor() as c:
        c.execute("UPDATE moves SET status=?, executed_at=? WHERE id=?",
                  (status, db.now_iso() if status == "executed" else None, move_id))


def record_roster_change(ctx, old: list, new: list):
    """Called by the Yahoo sync when your roster changed. Matches the change to a suggestion
    if there was one; otherwise logs it as your own ('manual') call so it gets scored too."""
    from . import recommend, sim
    adds = [p for p in new if p not in old]
    drops = [p for p in old if p not in new]
    dates = sim.remaining_dates(ctx)
    with db.cursor() as c:
        for a, d in zip(adds, drops):
            row = c.execute("SELECT id FROM moves WHERE add_id=? AND drop_id=? AND status='suggested'",
                            (a, d)).fetchone()
            if row:
                c.execute("UPDATE moves SET status='executed', executed_at=? WHERE id=?",
                          (db.now_iso(), row["id"]))
                continue
            edge, raw = recommend.horizon_edge(ctx, a, d, dates, "upgrade")
            c.execute("""INSERT OR IGNORE INTO moves(created, week, add_id, drop_id, confidence, raw_confidence,
                         rationale, status, executed_at, kind, horizon_end) VALUES (?,?,?,?,?,?,?, 'manual', ?, ?, ?)""",
                      (db.now_iso(), ctx.week, a, d, calibrate(raw), raw,
                       json.dumps(["Your own move (detected from Yahoo)"]), db.now_iso(),
                       "upgrade", horizon_end(ctx, "upgrade")))


def evaluate_moves(logs: pd.DataFrame | None = None):
    """Score moves by realized value above replacement: added player vs dropped player,
    over the games after the move. Provisional at 7 days, final at 21."""
    from . import nba
    logs = logs if logs is not None else nba.load_logs()
    if db.kv_get("value_scales") is None:
        return 0
    now = datetime.now(timezone.utc)
    n = 0
    with db.cursor() as c:
        rows = c.execute("SELECT * FROM moves").fetchall()
    for m in rows:
        # score from when the move actually happened, if it did
        created = datetime.fromisoformat(m["executed_at"] or m["created"])
        age = (now - created).days
        prev = json.loads(m["eval"]) if m["eval"] else None
        start = created.date().isoformat()
        end = m["horizon_end"] or (created.date() + timedelta(days=EVAL_HORIZON_DAYS)).isoformat()
        done = date.today().isoformat() > end
        if (prev and prev.get("final")) or (not done and age < PROVISIONAL_DAYS):
            continue
        window = logs[(logs["game_date"] > start) & (logs["game_date"] <= end)]
        res = {}
        for side, pid in (("add", m["add_id"]), ("drop", m["drop_id"])):
            g = window[window["nba_id"] == pid]
            res[side + "_games"] = int(len(g))
            res[side + "_value"] = float(model.line_values(g).sum()) if len(g) else 0.0
        res["realized_edge"] = res["add_value"] - res["drop_value"]
        res["success"] = res["realized_edge"] > 0
        res["final"] = done
        res["horizon_end"] = end
        with db.cursor() as c:
            c.execute("UPDATE moves SET eval=?, evaluated_at=? WHERE id=?",
                      (json.dumps(res), db.now_iso(), m["id"]))
        n += 1
    update_calibration()
    return n


def update_calibration():
    with db.cursor() as c:
        rows = c.execute("SELECT raw_confidence, confidence, status, eval FROM moves WHERE eval IS NOT NULL").fetchall()
    bins = [{"lo": BINS[i], "hi": BINS[i + 1], "n": 0, "hits": 0} for i in range(len(BINS) - 1)]
    by_status = {}
    for r in rows:
        ev = json.loads(r["eval"])
        hit = 1 if ev["success"] else 0
        for b in bins:
            if b["lo"] <= (r["raw_confidence"] or 0.5) < b["hi"]:
                b["n"] += 1
                b["hits"] += hit
        s = by_status.setdefault(r["status"], {"n": 0, "hits": 0})
        s["n"] += 1
        s["hits"] += hit
    db.kv_set("calibration", {"bins": bins, "by_status": by_status, "n": len(rows)})


def calibrate(raw: float) -> float:
    """Map the model's raw confidence to its track record, shrunk toward raw when history is thin."""
    cal = db.kv_get("calibration")
    if not cal:
        return raw
    for b in cal["bins"]:
        if b["lo"] <= raw < b["hi"]:
            return (b["hits"] + CALIBRATION_PSEUDO * raw) / (b["n"] + CALIBRATION_PSEUDO)
    return raw


# ------------------------------------------------------------------ matchup predictions

def record_matchup_prediction(ctx, result):
    if not ctx.my_key or not ctx.opp_key:
        return
    cats = json.dumps(result["cat_probs"])
    with db.cursor() as c:
        c.execute("""INSERT OR IGNORE INTO matchup_predictions(created, week, team_key, opp_key, win_prob,
                     cat_probs, kind) VALUES (?,?,?,?,?,?, 'opening')""",
                  (db.now_iso(), ctx.week, ctx.my_key, ctx.opp_key, result["win_prob"], cats))
        c.execute("""INSERT INTO matchup_predictions(created, week, team_key, opp_key, win_prob, cat_probs, kind)
                     VALUES (?,?,?,?,?,?, 'latest') ON CONFLICT(week, team_key, kind) DO UPDATE SET
                     created=excluded.created, win_prob=excluded.win_prob, cat_probs=excluded.cat_probs
                     WHERE matchup_predictions.result IS NULL""",
                  (db.now_iso(), ctx.week, ctx.my_key, ctx.opp_key, result["win_prob"], cats))


def resolve_matchups(results: dict):
    """results: week -> 1 (win) / 0.5 (tie) / 0 (loss) for your team, from Yahoo's scoreboard."""
    with db.cursor() as c:
        for week, res in results.items():
            c.execute("UPDATE matchup_predictions SET result=?, resolved_at=? WHERE week=? AND result IS NULL",
                      (res, db.now_iso(), int(week)))


def matchup_report() -> dict:
    with db.cursor() as c:
        rows = c.execute("SELECT week, kind, win_prob, result FROM matchup_predictions WHERE result IS NOT NULL "
                         "AND kind='opening' ORDER BY week").fetchall()
    if not rows:
        return {"n": 0}
    p = np.array([r["win_prob"] for r in rows])
    y = np.array([r["result"] for r in rows])
    return {"n": len(rows), "brier": float(np.mean((p - y) ** 2)),
            "brier_coinflip": 0.25, "weeks": [dict(r) for r in rows]}


# ------------------------------------------------------------------ projection tuning

MIN_IMPROVEMENT = 0.005  # keep current settings unless a new one is >0.5% better: no chasing noise
GRID_HALF_LIFE = [4, 6, 10, 15, 25, 40]      # per-minute production, this season
GRID_HALF_LIFE_MIN = [2, 3, 4, 6, 10, 20]    # minutes, this season
GRID_PRIOR_K = [6, 12, 20, 35, 60, 100]      # how many games last season's rates are worth
GRID_PRIOR_K_MIN = [0.5, 1, 2, 4, 8]         # ... and last season's minutes


def _backtest_error(logs, params, dates, cats):
    errs, n = 0.0, 0
    for d in dates:
        proj = model.project(logs, params, as_of=d)
        actual = logs[logs["game_date"] == d]
        actual = model._with_linear_ratio_stats(actual, proj.attrs["pcts"]).set_index("nba_id")
        common = actual.index.intersection(proj.index)
        if len(common) == 0:
            continue
        for k in cats:
            if k == "_points":  # points league: the error that matters is in fantasy points
                pw = model.point_weights()
                pred = sum(proj.loc[common, s] * w for s, w in pw.items())
                act = sum(actual.loc[common, s] * w for s, w in pw.items())
                sd = pred.std() or 1.0
                e = (((act - pred) / sd) ** 2).to_numpy()
                if np.isnan(e).any():
                    raise ValueError(f"NaN projection error for fantasy points on {d}")
                errs += float(e.sum())
                continue
            col = model.cat_column(k)
            sd = proj[col].std() or 1.0
            e = (((actual.loc[common, col] - proj.loc[common, col]) / sd) ** 2).to_numpy()
            if np.isnan(e).any():  # never let pandas silently skip bad projections
                raise ValueError(f"NaN projection error for {col} on {d}")
            errs += float(e.sum())
        n += len(common)
    return errs / max(n, 1), n


def _grid(logs, params, dates, cats, grid: dict):
    keys = list(grid)
    results = []

    def rec(i, cur):
        if i == len(keys):
            err, n = _backtest_error(logs, dict(params, **cur), dates, cats)
            results.append((err, dict(cur), n))
            return
        for v in grid[keys[i]]:
            rec(i + 1, dict(cur, **{keys[i]: v}))
    rec(0, {})
    results.sort(key=lambda r: r[0])
    return results


def tune_projections(max_dates=10):
    """Walk-forward backtest: for each test date, project from earlier games only, score that
    date's actual box scores. No look-ahead. Two questions, two test windows:

      in-season memory (half-lives): the most recent dates of this season, once there are
          enough; until then, dates spread across the middle of last season
      how much last season counts (prior strength k): the first 4 weeks of last season,
          predicted with the season before it as the prior. That's exactly the situation in
          October, which is when it matters most.
    """
    from . import nba
    logs = nba.load_logs()
    cats = model.league_categories() if model.scoring_mode() == "categories" else ["_points"]
    params = db.get_params()
    out = {"objective": "fantasy points" if cats == ["_points"] else "category z-scores"}

    cur_dates = sorted(logs[logs["season"] == config.SEASON]["game_date"].unique())
    if len(cur_dates) >= 10:
        mem_logs, mem_dates = logs, cur_dates[-max_dates:]
    else:
        prior = sorted(logs[logs["season"] == config.PRIOR_SEASON]["game_date"].unique())
        lo, hi = int(len(prior) * 0.2), int(len(prior) * 0.85)
        mem_logs = logs[logs["season"] != config.SEASON]
        mem_dates = [prior[int(i)] for i in np.linspace(lo, hi, max_dates)] if prior else []
    early = logs[logs["season"].isin([config.PRIOR2_SEASON, config.PRIOR_SEASON])]
    p_dates = sorted(early[early["season"] == config.PRIOR_SEASON]["game_date"].unique())
    k_dates = []
    if (early["season"] == config.PRIOR2_SEASON).any() and p_dates:
        first = [d for d in p_dates if d <= p_dates[0] + np.timedelta64(28, "D")]
        k_dates = [first[int(i)] for i in np.linspace(1, len(first) - 1, min(max_dates, len(first) - 1))]

    # The two windows interact (k matters a bit mid-season, half-lives a bit in October),
    # so alternate: memory -> prior strength -> memory.
    for step in ("memory", "prior_strength", "memory"):
        if step == "memory":
            grid = {"half_life_games": GRID_HALF_LIFE, "half_life_minutes": GRID_HALF_LIFE_MIN}
            res = _grid(mem_logs, params, mem_dates, cats, grid)
        elif k_dates:
            grid = {"prior_strength_games": GRID_PRIOR_K, "prior_strength_minutes": GRID_PRIOR_K_MIN}
            res = _grid(early, params, k_dates, cats, grid)
        else:
            continue
        test_logs, test_dates = (mem_logs, mem_dates) if step == "memory" else (early, k_dates)
        current = {k: params.get(k) for k in res[0][1]}
        cur_err, _ = _backtest_error(test_logs, params, test_dates, cats)
        adopted = res[0][0] < cur_err * (1 - MIN_IMPROVEMENT)
        if adopted:
            params.update(res[0][1])
        out[step] = {"best": res[0][1], "error": res[0][0], "n": res[0][2], "current": current,
                     "current_error": cur_err, "adopted": adopted,
                     "grid": [dict(p, err=e) for e, p, _ in res]}

    # baselines on the same in-season dates: flat averages, and last-N-games-only
    flat = dict(params, half_life_games=10_000, half_life_minutes=10_000)
    out["baseline_error"], _ = _backtest_error(mem_logs, flat, mem_dates, cats)
    out["model_error"], n = _backtest_error(mem_logs, params, mem_dates, cats)
    best = {k: params[k] for k in ("half_life_games", "half_life_minutes",
                                   "prior_strength_games", "prior_strength_minutes")}
    stored = db.kv_get("model_params", {})
    stored.update(best)
    db.kv_set("model_params", stored)
    with db.cursor() as c:
        c.execute("INSERT INTO backtests(created, params, model_err, baseline_err, n) VALUES (?,?,?,?,?)",
                  (db.now_iso(), json.dumps(best), out["model_error"], out["baseline_error"], n))
    out["params"] = best
    return out


def learn_news_multipliers(min_events=3):
    """For each news event type, compare minutes in the next 5 games to the projection
    made just before the news. Shrunk geometric mean of actual/projected."""
    from . import nba
    logs = nba.load_logs()
    params = db.get_params()
    cutoff = (date.today() - timedelta(days=5)).isoformat()
    with db.cursor() as c:
        evs = c.execute("SELECT nba_id, date, event_type FROM news_events WHERE date <= ? AND nba_id IS NOT NULL",
                        (cutoff,)).fetchall()
    ratios = {}
    proj_cache = {}
    for e in evs:
        d = e["date"][:10]
        if d not in proj_cache:
            proj_cache[d] = model.project(logs, params, as_of=d)
        proj = proj_cache[d]
        after = logs[(logs["nba_id"] == e["nba_id"]) & (logs["game_date"] > d)].sort_values("game_date").head(5)
        if len(after) < 2 or e["nba_id"] not in proj.index:
            continue
        expected = proj.loc[e["nba_id"], "min"]
        if expected < 5:
            continue
        ratios.setdefault(e["event_type"], []).append(math.log(max(after["min"].mean(), 1) / expected))
    k = 8  # pseudo-events at ratio 1.0
    mult = {t: math.exp(sum(r) / (len(r) + k)) for t, r in ratios.items() if len(r) >= min_events}
    stored = db.kv_get("model_params", {})
    stored["news_multipliers"] = mult
    db.kv_set("model_params", stored)
    return {t: {"multiplier": mult.get(t), "n": len(r)} for t, r in ratios.items()}


def report() -> dict:
    with db.cursor() as c:
        moves = [dict(r) for r in c.execute("SELECT * FROM moves ORDER BY created DESC LIMIT 100")]
        bt = [dict(r) for r in c.execute("SELECT * FROM backtests ORDER BY created DESC LIMIT 30")]
    for m in moves:
        m["eval"] = json.loads(m["eval"]) if m["eval"] else None
        m["rationale"] = json.loads(m["rationale"]) if m["rationale"] else []
    return {"moves": moves, "calibration": db.kv_get("calibration", {"bins": [], "n": 0}),
            "matchups": matchup_report(), "backtests": bt, "params": db.get_params()}



def resolve_from_box_scores(ctx) -> int:
    """Manual mode has no Yahoo scoreboard: once a week is over, decide each logged matchup from
    box scores (starters by pasted slot), so weekly forecasts still get graded."""
    from datetime import date
    from . import sim
    if db.kv_get("league_source") == "yahoo":
        return 0
    with db.cursor() as c:
        rows = c.execute("SELECT DISTINCT week, team_key, opp_key FROM matchup_predictions "
                         "WHERE result IS NULL").fetchall()
    done = 0
    for r in rows:
        start, end = sim.week_range_for(r["week"])
        if end >= date.today().isoformat() or r["team_key"] not in ctx.rosters or r["opp_key"] not in ctx.rosters:
            continue
        a = sim.actual_totals(ctx, r["team_key"], start, end)
        b = sim.actual_totals(ctx, r["opp_key"], start, end)
        res, _, _ = sim.score_totals(ctx, a[None, :], b[None, :])
        resolve_matchups({r["week"]: res})
        done += 1
    return done
