"""Backtest the weekly matchup predictor on last season, and calibrate it.

For each week of the backtest season: build a few 12-team leagues from the players the model
would have rated as rosterable *using only games before that Monday*, pair teams up, predict
P(win) with the same Monte Carlo engine the dashboard uses, then score each matchup from the
real box scores of that week (each day, a team's best 10 by pre-week projection who actually
played are the starters, the same lineup rule the simulator assumes).

Hundreds of matchups answer "when it says 70%, does that team win ~70%?" long before a single
fantasy season could. A single calibration knob is fitted: a temperature T applied to the
win-probability log-odds (T > 1 = predictions were overconfident, pull toward 50%).

Known gap: no historical injury reports, so the backtest model sees fewer absences coming than
the live one does. That makes these backtest predictions *less* informed than live ones.
"""
import math
import random

import numpy as np
import pandas as pd

from . import config, db, model, nba, sim

LEAGUES_PER_WEEK = 3
N_SIMS = 1000
TEMP_GRID = [0.6, 0.7, 0.8, 0.9, 1.0, 1.1, 1.25, 1.4, 1.6, 1.8, 2.0, 2.5, 3.0]
TEMP_PSEUDO = 150  # matchups' worth of pull toward T=1, so a small sample can't swing it far


def logit(p):
    p = min(max(p, 1e-4), 1 - 1e-4)
    return math.log(p / (1 - p))


def apply_temperature(p, T):
    return 1 / (1 + math.exp(-logit(p) / T))


def _weeks(logs, season):
    d = logs[logs["season"] == season]["game_date"]
    mon = d.min().normalize() - pd.Timedelta(days=d.min().weekday())
    out = []
    while mon + pd.Timedelta(days=6) <= d.max():
        out.append(mon)
        mon += pd.Timedelta(days=7)
    return out


def _context(logs_all, ws, params, num_teams, roster_size, active_slots):
    hist = logs_all[logs_all["game_date"] < ws]
    proj = model.project(hist, params)
    values = model.value_players(proj, num_teams, roster_size)
    last = hist.sort_values("game_date").groupby("nba_id").tail(1).set_index("nba_id")
    players = pd.DataFrame({"name": last["player_name"] if "player_name" in last else last.index.astype(str),
                            "team": last["team"]})
    with db.cursor() as c:
        pos = pd.read_sql_query("SELECT nba_id, positions, espn_pos FROM players", c).set_index("nba_id")
    players = players.join(pos, how="left")
    we = ws + pd.Timedelta(days=6)
    ctx = sim.Context(logs=hist, proj=proj, values=values, params=params, injuries={}, events={},
                      b2b=model.back_to_back_sit_rates(hist), players=players, rosters={}, teams={},
                      my_key=None, opp_key=None, week=0, week_start=ws.date().isoformat(),
                      week_end=we.date().isoformat(), active_slots=active_slots, roster_size=roster_size,
                      num_teams=num_teams, weeks_remaining=0, wtd={})
    ctx.today = ctx.week_start
    if params.get("tm_adjust") or params.get("bt_known_out"):
        from . import teammates
        share, out_set = teammates.known_out_share(logs_all, proj, ws)
        if params.get("bt_known_out"):
            # the backtest's injury report: whoever missed his team's last game is listed out
            # (same information for both arms of an A/B comparison)
            for p in out_set:
                ctx.injuries[p] = {"nba_id": p, "status": "Out", "comment": "missed last game", "date": ws.date().isoformat()}
        if params.get("tm_adjust"):
            ctx._pools["_tm_fixed"], ctx._pools["_tm_out_set"] = share, out_set
    if params.get("opp_adjust"):
        from . import matchups
        ctx.defense = matchups.defense_table(logs_all, ws, params.get("opp_k", 80))
        s_games = logs_all[(logs_all["game_date"] >= ws) & (logs_all["game_date"] <= ws + pd.Timedelta(days=6))]
        ctx._pools["_opp_of"] = {(t, d.strftime("%Y-%m-%d")): o for t, d, o in
                                 zip(s_games["team"], s_games["game_date"], matchups.opponent_of(s_games["matchup"]))}
    season = logs_all[logs_all["game_date"] >= ws]["season"].iloc[0]
    s = logs_all[logs_all["season"] == season]
    # the schedule is known in advance, so using the season's game dates is not look-ahead
    ctx._team_dates = {t: set(g.dt.strftime("%Y-%m-%d")) for t, g in s.groupby("team")["game_date"]}
    ctx._team_dates["_loaded"] = True
    ctx._team_dates["_played"] = set()
    return ctx


def _draft(ctx, seed):
    rng = random.Random(seed)
    v = ctx.values
    eligible = [p for p in v.sort_values("var", ascending=False).index
                if p in ctx.players.index and ctx.proj.loc[p, "n_games"] >= 5]
    pool = eligible[: ctx.num_teams * ctx.roster_size + 30]
    # managers don't draft in perfect value order: jitter by ~1 sd of a mid-round pick's value
    noisy = sorted(pool, key=lambda p: -(v.loc[p, "var"] + rng.gauss(0, 1.5)))
    teams = [[] for _ in range(ctx.num_teams)]
    it = iter(noisy)
    for rnd in range(ctx.roster_size):
        order = range(ctx.num_teams) if rnd % 2 == 0 else reversed(range(ctx.num_teams))
        for t in order:
            teams[t].append(next(it))
    rng.shuffle(teams)
    return teams


def _actual_totals(ctx, logs_all, pids, ws):
    we = ws + pd.Timedelta(days=6)
    g = logs_all[(logs_all["game_date"] >= ws) & (logs_all["game_date"] <= we) & logs_all["nba_id"].isin(pids)]
    tot = np.zeros(len(sim.SIM_STATS))
    val = ctx.values["value"]
    for _, day in g.groupby("game_date"):
        day = day.assign(v=day["nba_id"].map(val).fillna(-99)).nlargest(ctx.active_slots, "v")
        tot += day[sim.SIM_STATS].to_numpy(dtype=float).sum(0)
    return tot


def run(season=None, weeks_skip=2, max_weeks=None, num_teams=12, roster_size=13, active_slots=10,
        overrides=None, logs_all=None) -> dict:
    season = season or config.PRIOR_SEASON
    logs_all = logs_all if logs_all is not None else nba.load_logs()
    params = dict(db.get_params(), **(overrides or {}))
    # The backtest has no injury reports, so every absence is a surprise to it: use the
    # all-absences rate for healthy players (the live model only needs the short-run rate).
    rates = model.miss_rates(logs_all, season)
    saved_rates = (model.HEALTHY_GAME, model.HEALTHY_ROS)
    saved_streak, model.ABSENCE_STREAK = model.ABSENCE_STREAK, {}
    model.HEALTHY_GAME, model.HEALTHY_ROS = 1 - rates["all"], 1 - rates["all"]
    saved_scales = db.kv_get("value_scales")  # value_players() writes these; restore after
    rows = []
    try:
        weeks = _weeks(logs_all, season)[weeks_skip:]
        if max_weeks:
            weeks = weeks[:max_weeks]
        for wi, ws in enumerate(weeks):
            ctx = _context(logs_all, ws, params, num_teams, roster_size, active_slots)
            dates = sim.remaining_dates(ctx)
            for lg in range(LEAGUES_PER_WEEK):
                teams = _draft(ctx, seed=wi * 100 + lg)
                for a, b in zip(teams[0::2], teams[1::2]):
                    A = sim.simulate_team(ctx, a, dates, N_SIMS, seed=wi * 1000 + lg)
                    B = sim.simulate_team(ctx, b, dates, N_SIMS, seed=wi * 1000 + lg + 1)
                    p, _, _ = sim.score_totals(ctx, A, B)
                    # the rest-of-season formula, applied to this exact week
                    p_an = model.matchup_odds(sim.team_dist(ctx, a, dates, per_week=False),
                                              sim.team_dist(ctx, b, dates, per_week=False))[0]
                    ra, rb = _actual_totals(ctx, logs_all, a, ws), _actual_totals(ctx, logs_all, b, ws)
                    res, _, _ = sim.score_totals(ctx, ra[None, :], rb[None, :])
                    rows.append({"week": ws.date().isoformat(), "p": p, "p_an": p_an, "result": res})
    finally:
        db.kv_set("value_scales", saved_scales)
        model.HEALTHY_GAME, model.HEALTHY_ROS = saved_rates
        model.ABSENCE_STREAK = saved_streak
    out = summarize(rows)
    out["rows"] = rows
    out["miss_rates"] = rates
    return out


def _metrics(ps, ys):
    ps, ys = np.asarray(ps), np.asarray(ys)
    pc = np.clip(ps, 1e-4, 1 - 1e-4)
    return {"n": int(len(ps)), "brier": float(np.mean((ps - ys) ** 2)),
            "log_loss": float(-np.mean(ys * np.log(pc) + (1 - ys) * np.log(1 - pc))),
            "accuracy": float(np.mean((ps > 0.5) == (ys > 0.5)))}


def _fit_T(ps, ys):
    best = min(TEMP_GRID, key=lambda T: _metrics([apply_temperature(p, T) for p in ps], ys)["log_loss"])
    return best


def _bins(ps, ys, edges=(0, .2, .35, .5, .65, .8, 1.0001)):
    out = []
    for lo, hi in zip(edges[:-1], edges[1:]):
        sel = [(p, y) for p, y in zip(ps, ys) if lo <= p < hi]
        if sel:
            out.append({"lo": lo, "hi": min(hi, 1), "n": len(sel), "mean_p": float(np.mean([p for p, _ in sel])),
                        "win_rate": float(np.mean([y for _, y in sel]))})
    return out


def summarize(rows) -> dict:
    ps, ys = [r["p"] for r in rows], [r["result"] for r in rows]
    weeks = sorted({r["week"] for r in rows})
    half = set(weeks[: len(weeks) // 2])
    tr = [r for r in rows if r["week"] in half]
    te = [r for r in rows if r["week"] not in half]
    # out-of-sample check: fit T on the first half of the season, score the second half
    T_tr = _fit_T([r["p"] for r in tr], [r["result"] for r in tr])
    te_raw = _metrics([r["p"] for r in te], [r["result"] for r in te])
    te_cal = _metrics([apply_temperature(r["p"], T_tr) for r in te], [r["result"] for r in te])
    T_all = _fit_T(ps, ys)
    n = len(rows)
    T_used = 1 + (T_all - 1) * n / (n + TEMP_PSEUDO)
    return {"n_matchups": n, "weeks": len(weeks), "raw": _metrics(ps, ys),
            "coinflip": {"brier": 0.25, "log_loss": math.log(2)},
            "calibrated": _metrics([apply_temperature(p, T_used) for p in ps], ys),
            "holdout": {"T_fit_first_half": T_tr, "second_half_raw": te_raw, "second_half_calibrated": te_cal},
            "T_fit": T_all, "T_used": T_used,
            "bins_raw": _bins(ps, ys), "bins_calibrated": _bins([apply_temperature(p, T_used) for p in ps], ys)}


FORM_GRID = [0.0, 0.1, 0.15, 0.2, 0.3, 0.4]


def calibrate(season=None) -> dict:
    """Learn the simulator's variance from last season, honestly:
      1. absence rates: counted from box scores (no tuning)
      2. weekly form spread: the one tuned knob, chosen on the FIRST half of the season
      3. checked on the SECOND half, which played no part in choosing it
      4. a temperature on top only for whatever miscalibration is left (shrunk toward 1)"""
    season = season or config.PRIOR_SEASON
    logs_all = nba.load_logs()
    rates = model.miss_rates(logs_all, season)
    runs = {}
    for sd in FORM_GRID:
        runs[sd] = run(season, overrides={"weekly_form_sd": sd, "win_prob_temperature": 1.0}, logs_all=logs_all)
    weeks = sorted({r["week"] for r in runs[0.0]["rows"]})
    first = set(weeks[: len(weeks) // 2])

    def ll(rows, keep):
        sel = [r for r in rows if (r["week"] in first) == keep]
        return _metrics([r["p"] for r in sel], [r["result"] for r in sel])

    table = [{"sd": sd, "first_half": ll(r["rows"], True), "second_half": ll(r["rows"], False)} for sd, r in runs.items()]
    best_sd = min(table, key=lambda t: t["first_half"]["log_loss"])["sd"]
    chosen = runs[best_sd]
    report = {k: v for k, v in chosen.items() if k != "rows"}
    report["form_sd"] = best_sd
    # the season formula, scored on the same matchups (form spread as chosen above)
    an = summarize([{"week": r["week"], "p": r["p_an"], "result": r["result"]} for r in chosen["rows"]])
    report["season_formula"] = {k: an[k] for k in ("raw", "calibrated", "holdout", "T_fit", "T_used", "bins_raw")}
    report["form_table"] = table
    report["miss_rates"] = rates
    return report


def store(report: dict):
    stored = db.kv_get("model_params", {})
    stored["weekly_form_sd"] = report["form_sd"]
    stored["win_prob_temperature"] = report["T_used"]
    if report.get("season_formula"):
        stored["season_temperature"] = report["season_formula"]["T_used"]
    stored["miss_rate_short"] = report["miss_rates"]["short"]
    stored["miss_rate_all"] = report["miss_rates"]["all"]
    db.kv_set("model_params", stored)
    db.kv_set("matchup_backtest", dict(report, created=db.now_iso()))
