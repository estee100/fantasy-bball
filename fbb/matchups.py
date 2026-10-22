"""Opponent adjustment: a player scores more against generous defenses (bad, fast) and less
against stingy ones.

  defense factor(T) = fantasy points allowed per player-minute by team T, relative to league,
                      shrunk toward 1 with k pseudo-games:  (n*rate_T + k*league) / (n + k) / league

One knob (k: how much evidence before a defense is trusted to differ from average), chosen on
the first half of last season and judged on the second half, which plays no part in choosing.
It's used only if it beats no-adjustment by more than MIN_IMPROVEMENT on that held-out half.
"""
import numpy as np
import pandas as pd

from . import config, db, model

K_GRID = [5, 10, 20, 40, 80]
WINDOW_DAYS = 365
MIN_IMPROVEMENT = 0.005


def opponent_of(matchup: pd.Series) -> pd.Series:
    """'NYK vs. PHI' / 'BOS @ DET' -> the other team's tricode."""
    return matchup.str.split().str[-1]


def defense_table(logs: pd.DataFrame, as_of, k: float) -> dict:
    """{team: factor} from games in the WINDOW_DAYS before `as_of` (nothing on or after it)."""
    pw = model.point_weights()
    end = pd.Timestamp(as_of)
    g = logs[(logs["game_date"] < end) & (logs["game_date"] >= end - pd.Timedelta(days=WINDOW_DAYS))]
    if g.empty:
        return {}
    fp = sum(g[s] * w for s, w in pw.items())
    opp = opponent_of(g["matchup"])
    frame = pd.DataFrame({"opp": opp, "fp": fp, "min": g["min"], "game": g["game_id"]})
    by = frame.groupby("opp").agg(fp=("fp", "sum"), mins=("min", "sum"), games=("game", "nunique"))
    league = by["fp"].sum() / by["mins"].sum()
    rate = by["fp"] / by["mins"]
    shrunk = (by["games"] * rate + k * league) / (by["games"] + k)
    return (shrunk / league).to_dict()


def backtest(n_dates=36, season=None) -> dict:
    """Walk-forward on last season: projection as of each date (no adjustment) vs the same
    projection x opponent factor, scored on that date's real fantasy points."""
    from . import nba
    season = season or config.PRIOR_SEASON
    logs = nba.load_logs()
    params = db.get_params()
    pw = model.point_weights()
    dates = sorted(logs[logs["season"] == season]["game_date"].unique())
    lo, hi = int(len(dates) * 0.15), int(len(dates) * 0.95)
    test_dates = [dates[int(i)] for i in np.linspace(lo, hi, n_dates)]
    rows = []
    for d in test_dates:
        proj = model.project(logs, params, as_of=d)
        pred = sum(proj[s] * w for s, w in pw.items())
        day = logs[logs["game_date"] == d]
        day = day[day["nba_id"].isin(proj.index)]
        actual = sum(day[s] * w for s, w in pw.items())
        opp = opponent_of(day["matchup"])
        tables = {k: defense_table(logs, d, k) for k in K_GRID}
        for (_, r), a, o in zip(day.iterrows(), actual, opp):
            base = float(pred[r["nba_id"]])
            row = {"date": d, "actual": float(a), "base": base}
            for k in K_GRID:
                row[k] = base * tables[k].get(o, 1.0)
            rows.append(row)
    df = pd.DataFrame(rows)
    half = sorted(df["date"].unique())
    first = df["date"].isin(half[: len(half) // 2])
    mse = lambda col, sel: float(((df.loc[sel, col] - df.loc[sel, "actual"]) ** 2).mean())
    first_scores = {k: mse(k, first) for k in K_GRID}
    best_k = min(first_scores, key=first_scores.get)
    base_first, base_second = mse("base", first), mse("base", ~first)
    second = mse(best_k, ~first)
    improvement = 1 - second / base_second
    return {"player_games": int(len(df)), "dates": len(half),
            "first_half": {"no_adjustment": base_first, **{f"k={k}": v for k, v in first_scores.items()}},
            "chosen_k": best_k,
            "second_half": {"no_adjustment": base_second, f"k={best_k}": second,
                            "all_k": {f"k={k}": mse(k, ~first) for k in K_GRID}},
            "held_out_improvement": improvement, "adopt": improvement > MIN_IMPROVEMENT}



def recheck_and_store() -> dict:
    """Two gates, both required, re-checked weekly:
      1. player projections: held-out improvement > MIN_IMPROVEMENT
      2. weekly matchup forecasts (the 414-matchup backtest): not worse than without it
    First check (Oct 2026): gate 1 passed (+0.97%) but gate 2 failed (Brier 0.2363 vs 0.2345),
    so it stayed off."""
    from . import backtest as mb
    r = backtest()
    out = {"player_level": r["held_out_improvement"], "k": r["chosen_k"], "matchup_level": None, "adopt": False}
    if r["adopt"]:
        base = db.get_params()
        on = mb.run(overrides={"opp_adjust": True, "opp_k": r["chosen_k"], "win_prob_temperature": 1.0,
                               "weekly_form_sd": base.get("weekly_form_sd", 0.15)})
        off = mb.run(overrides={"opp_adjust": False, "win_prob_temperature": 1.0,
                                "weekly_form_sd": base.get("weekly_form_sd", 0.15)})
        out["matchup_level"] = {"on": on["raw"]["brier"], "off": off["raw"]["brier"]}
        out["adopt"] = on["raw"]["brier"] <= off["raw"]["brier"]
    stored = db.kv_get("model_params", {})
    stored["opp_adjust"] = out["adopt"]
    stored["opp_k"] = r["chosen_k"]
    db.kv_set("model_params", stored)
    db.kv_set("opp_adjust_backtest", dict(r, created=db.now_iso(), gates=out))
    return out
