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

GRID_HALF_LIFE = [3, 4, 6, 8, 10, 15, 25]
GRID_PRIOR_W = [0.25, 0.5, 0.75, 1.0]


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
            col = model.cat_column(k)
            sd = proj[col].std() or 1.0
            errs += float((((actual.loc[common, col] - proj.loc[common, col]) / sd) ** 2).sum())
        n += len(common)
    return errs / max(n, 1), n


def tune_projections(max_dates=8):
    """Walk-forward: for each test date, project using only earlier games, score that date.
    No look-ahead. Picks the (half-life, prior weight) with the lowest error."""
    from . import nba
    logs = nba.load_logs()
    cats = model.league_categories() if model.scoring_mode() == "categories" else \
        [k for k in ("pts", "reb", "ast", "stl", "blk", "tov", "fg3m")]
    cur = logs[logs["season"] == config.SEASON]
    cur_dates = sorted(cur["game_date"].unique())
    if len(cur_dates) >= 10:
        dates = cur_dates[-max_dates:]
        grid_prior = GRID_PRIOR_W
    else:
        # Too early in the season: tune recency on the end of last season instead. The
        # prior-season weight can't be learned there (it's all one season), so keep it.
        prior = logs[logs["season"] == config.PRIOR_SEASON]
        dates = sorted(prior["game_date"].unique())[-40::5][:max_dates]
        grid_prior = [db.get_params()["prior_season_weight"]]
    params = db.get_params()
    results = []
    for hl in GRID_HALF_LIFE:
        for pw in grid_prior:
            p = dict(params, half_life_games=hl, prior_season_weight=pw)
            err, n = _backtest_error(logs, p, dates, cats)
            results.append((err, hl, pw, n))
    results.sort()
    best_err, hl, pw, n = results[0]
    # naive baseline: flat season average (no recency weighting)
    base_err, _ = _backtest_error(logs, dict(params, half_life_games=10_000, prior_season_weight=1.0), dates, cats)
    stored = db.kv_get("model_params", {})
    stored.update({"half_life_games": hl, "prior_season_weight": pw})
    db.kv_set("model_params", stored)
    with db.cursor() as c:
        c.execute("INSERT INTO backtests(created, params, model_err, baseline_err, n) VALUES (?,?,?,?,?)",
                  (db.now_iso(), json.dumps({"half_life_games": hl, "prior_season_weight": pw}),
                   best_err, base_err, n))
    return {"half_life_games": hl, "prior_season_weight": pw, "error": best_err,
            "baseline_error": base_err, "n_player_games": n,
            "grid": [{"err": e, "half_life": h, "prior_w": w} for e, h, w, _ in results]}


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
