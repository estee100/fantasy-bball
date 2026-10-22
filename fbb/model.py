"""Per-game projections, availability, and fantasy valuation.

Projection = exponentially-weighted mean of a player's recent games, with prior-season
games down-weighted. Both knobs (half-life, prior-season weight) are tuned by the
learning module against out-of-sample games, so the defaults in config are only a start.
"""
import json
import math
from datetime import date, timedelta

import numpy as np
import pandas as pd

from . import config, db

STATS = ["min", "fgm", "fga", "fg3m", "fg3a", "ftm", "fta", "oreb", "dreb", "reb",
         "ast", "stl", "blk", "tov", "pts", "dd", "td"]
MAX_GAMES = 60  # older games carry ~nothing at any sensible half-life
PRIOR_GAMES = 3.0

INJURY_AVAIL = {"out": 0.05, "doubtful": 0.25, "questionable": 0.55, "day-to-day": 0.65,
                "probable": 0.9, "suspension": 0.0}


def league_pcts(logs: pd.DataFrame) -> dict:
    s = logs[["fgm", "fga", "ftm", "fta", "fg3m", "fg3a"]].sum()
    return {"fg_pct": s.fgm / s.fga, "ft_pct": s.ftm / s.fta, "fg3_pct": s.fg3m / s.fg3a}


def _with_linear_ratio_stats(df: pd.DataFrame, pcts: dict) -> pd.DataFrame:
    # Ratio categories are linearised as "makes above league-average on this volume".
    # This turns FG%/FT% into additive per-game stats, so variance and z-scores work like counts.
    df = df.copy()
    for key, (_, kind, num, den, _) in config.CATEGORY_DEFS.items():
        if kind == "ratio":
            df[key + "_lin"] = df[num] - pcts[key] * df[den]
    return df


def recency_weights(logs: pd.DataFrame, params: dict) -> pd.DataFrame:
    df = logs.sort_values(["nba_id", "game_date"], ascending=[True, False]).copy()
    df["recency"] = df.groupby("nba_id").cumcount()
    df = df[df["recency"] < MAX_GAMES]
    season_w = np.where(df["season"] == config.SEASON, 1.0, params["prior_season_weight"])
    df["w"] = 0.5 ** (df["recency"] / params["half_life_games"]) * season_w
    return df


def project(logs: pd.DataFrame, params: dict, as_of=None) -> pd.DataFrame:
    """Per-game projection (mean, std) for every player, using only games before `as_of`."""
    if as_of is not None:
        logs = logs[logs["game_date"] < pd.Timestamp(as_of)]
    pcts = league_pcts(logs)
    df = _with_linear_ratio_stats(recency_weights(logs, params), pcts)
    cols = STATS + [k + "_lin" for k, d in config.CATEGORY_DEFS.items() if d[1] == "ratio"]
    w = df["w"].to_numpy()[:, None]
    vals = df[cols].to_numpy(dtype=float)
    g = df["nba_id"].to_numpy()
    ids, inv = np.unique(g, return_inverse=True)
    sw = np.bincount(inv, weights=w[:, 0])
    sw2 = np.bincount(inv, weights=w[:, 0] ** 2)
    mean = np.stack([np.bincount(inv, weights=w[:, 0] * vals[:, j]) for j in range(len(cols))], 1) / sw[:, None]
    sq = np.stack([np.bincount(inv, weights=w[:, 0] * vals[:, j] ** 2) for j in range(len(cols))], 1) / sw[:, None]
    # Shrink toward a generic rotation player with PRIOR_GAMES pseudo-games, so one hot
    # (or cold) game can't define a player. Rotation player = 12+ min, 10+ games.
    rot = (np.bincount(inv) >= 10) & (mean[:, 0] >= 12)
    prior_mu, prior_sq = mean[rot].mean(0), sq[rot].mean(0)
    # Blend on raw games played, not decayed weight: a full season sums to only ~11 weight
    # units at the default half-life, which would over-shrink established players.
    n = np.bincount(inv).astype(float)
    alpha = (n / (n + PRIOR_GAMES))[:, None]
    mean = alpha * mean + (1 - alpha) * prior_mu
    sq = alpha * sq + (1 - alpha) * prior_sq
    std = np.sqrt(np.maximum(sq - mean ** 2, 0))
    out = pd.DataFrame(mean, index=ids, columns=cols)
    out = out.join(pd.DataFrame(std, index=ids, columns=[c + "_sd" for c in cols]))
    out["n_games"] = np.bincount(inv)
    out["n_eff"] = sw ** 2 / sw2
    out["cur_games"] = df.assign(cur=df["season"] == config.SEASON).groupby("nba_id")["cur"].sum().reindex(ids).to_numpy()
    out.index.name = "nba_id"
    out.attrs["pcts"] = pcts
    return out


# ---------------------------------------------------------------- availability

def back_to_back_sit_rates(logs: pd.DataFrame) -> pd.Series:
    """P(player sits the 2nd night of a back-to-back | played the 1st), shrunk to the league rate."""
    team_dates = logs.groupby("team")["game_date"].apply(lambda s: set(s.dt.normalize()))
    played = set(zip(logs["nba_id"], logs["game_date"].dt.normalize()))
    counts = {pid: [0, 0] for pid in logs["nba_id"].unique()}
    by_player_team = logs.groupby(["nba_id", "team"])["game_date"].apply(lambda s: set(s.dt.normalize()))
    for (pid, team), dates in by_player_team.items():
        tdates = team_dates.get(team, set())
        for d in dates:
            nxt = d + pd.Timedelta(days=1)
            if nxt in tdates:
                counts[pid][1] += 1
                if (pid, nxt) not in played:
                    counts[pid][0] += 1
    sits = sum(v[0] for v in counts.values())
    opps = sum(v[1] for v in counts.values())
    base = sits / opps if opps else 0.05
    k = 6.0  # pseudo-count: a player needs a real track record before we believe he rests B2Bs
    return pd.Series({pid: (s + k * base) / (n + k) for pid, (s, n) in counts.items()})


def injury_table() -> dict:
    with db.cursor() as c:
        return {r["nba_id"]: dict(r) for r in c.execute("SELECT * FROM injuries")}


def recent_news_events(days=14) -> dict:
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    with db.cursor() as c:
        rows = c.execute("SELECT * FROM news_events WHERE date >= ? ORDER BY date", (cutoff,)).fetchall()
    latest = {}
    for r in rows:
        latest[r["nba_id"]] = dict(r)  # later rows overwrite: keep the most recent event
    return latest


def availability(pid, game_dates, injuries, events, b2b, team_dates_set):
    """Probability the player suits up for each of `game_dates` (ISO strings, sorted)."""
    inj = injuries.get(pid)
    base = 1.0
    if inj:
        status = (inj["status"] or "").lower()
        base = INJURY_AVAIL.get(status, 0.7)
        if "season" in (inj["comment"] or "").lower() and status == "out":
            base = 0.0
    ev = events.get(pid)
    games_out = ev["games_out"] if ev and ev["games_out"] is not None else None
    out = []
    for i, d in enumerate(game_dates):
        p = base
        if games_out is not None:
            p = 0.0 if i < games_out else max(base, 0.85)
        prev = (date.fromisoformat(d) - timedelta(days=1)).isoformat()
        if prev in team_dates_set:
            p *= 1 - b2b.get(pid, 0.05)
        out.append(p)
    return out


def ros_availability(pid, injuries, events) -> float:
    inj = injuries.get(pid)
    if not inj:
        return 0.95
    status = (inj["status"] or "").lower()
    if status == "out" and "season" in (inj["comment"] or "").lower():
        return 0.0
    ev = events.get(pid)
    if ev and ev["games_out"]:
        return max(0.0, 1 - ev["games_out"] / 60)
    return {"out": 0.8, "doubtful": 0.88, "questionable": 0.92}.get(status, 0.93)


def production_multiplier(pid, events, params) -> float:
    """News-driven scaling of counting stats (role/minutes changes), learned per event type."""
    ev = events.get(pid)
    if not ev:
        return 1.0
    if ev["minutes_multiplier"]:
        return float(np.clip(ev["minutes_multiplier"], 0.3, 1.6))
    learned = params.get("news_multipliers", {}).get(ev["event_type"])
    return float(learned) if learned else 1.0


# ---------------------------------------------------------------- valuation

def league_categories() -> list:
    return db.kv_get("categories", config.DEFAULT_CATEGORIES)


def scoring_mode() -> str:
    """'categories' (H2H cats / roto) or 'points'."""
    return db.kv_get("scoring_mode", "categories")


def point_weights() -> dict:
    return db.kv_get("point_weights", {})


def cat_column(key: str) -> str:
    kind = config.CATEGORY_DEFS[key][1]
    return key + "_lin" if kind == "ratio" else config.CATEGORY_DEFS[key][2]


def value_players(proj: pd.DataFrame, num_teams=10, roster_size=13) -> pd.DataFrame:
    """Per-game value. Categories: sum of z-scores over the draftable pool. Points: fantasy points."""
    cats = league_categories()
    out = pd.DataFrame(index=proj.index)
    if scoring_mode() == "points":
        pw = point_weights()
        out["value"] = sum(proj[s] * w for s, w in pw.items() if s in proj)
        out["value_sd"] = np.sqrt(sum((proj[s + "_sd"] * w) ** 2 for s, w in pw.items() if s + "_sd" in proj)) \
            / np.sqrt(proj["n_eff"].clip(lower=1))
        repl = float(out["value"].nlargest(num_teams * roster_size).min())
        out["var"] = out["value"] - repl
        db.kv_set("value_scales", {"scales": {}, "replacement": repl, "pcts": proj.attrs["pcts"]})
        return out
    eligible = proj[(proj["min"] >= 12) & (proj["n_games"] >= 3)]
    pool = eligible.index
    for _ in range(2):  # recompute pool stats on the top-N so replacement level is realistic
        z = {}
        for k in cats:
            col = cat_column(k)
            mu, sd = proj.loc[pool, col].mean(), proj.loc[pool, col].std() or 1.0
            sign = 1 if config.CATEGORY_DEFS[k][4] else -1
            z[k] = sign * (proj[col] - mu) / sd
        total = sum(z.values())
        pool = total.loc[eligible.index].nlargest(num_teams * roster_size).index
    scales = {}
    for k in cats:
        out["z_" + k] = z[k]
        col = cat_column(k)
        scales[k] = (float(proj.loc[pool, col].mean()), float(proj.loc[pool, col].std() or 1.0))
    out["value"] = total
    # Replacement level = the last rostered player. Value above replacement (var) is the unit
    # that adds up over games: a game at replacement level is worth 0, same as a missed game.
    repl = float(total.loc[pool].min())
    out["var"] = total - repl
    out.attrs["scales"] = scales
    out.attrs["replacement"] = repl
    db.kv_set("value_scales", {"scales": scales, "replacement": repl, "pcts": proj.attrs["pcts"]})
    # uncertainty of the value estimate itself (shrinks with effective games observed)
    sds = []
    for k in cats:
        col = cat_column(k)
        sd_pool = proj.loc[pool, col].std() or 1.0
        sds.append((proj[col + "_sd"] / sd_pool) ** 2)
    out["value_sd"] = np.sqrt(sum(sds)) / np.sqrt(proj["n_eff"].clip(lower=1))
    return out


def line_values(lines: pd.DataFrame) -> pd.Series:
    """Value above replacement of actual box-score lines, on the same scale as projections."""
    sc = db.kv_get("value_scales")
    if scoring_mode() == "points":
        pw = point_weights()
        return sum(lines[s] * w for s, w in pw.items() if s in lines) - sc["replacement"]
    lines = _with_linear_ratio_stats(lines, sc["pcts"])
    total = 0
    for k, (mu, sd) in sc["scales"].items():
        sign = 1 if config.CATEGORY_DEFS[k][4] else -1
        total = total + sign * (lines[cat_column(k)] - mu) / sd
    return total - sc["replacement"]


_game_sd_cache = {}


def game_value_sd(logs: pd.DataFrame) -> float:
    """Typical single-game spread of value: median per-player std over recent games."""
    key = (len(logs), scoring_mode())
    if key not in _game_sd_cache:
        recent = logs.sort_values("game_date").groupby("nba_id").tail(30)
        lv = line_values(recent)
        sds = lv.groupby(recent["nba_id"]).std().dropna()
        _game_sd_cache[key] = float(sds.median()) if len(sds) else 3.0
    return _game_sd_cache[key]


# ---------------------------------------------------------------- analytic weekly odds

def _phi(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def poisson_binomial_win(cat_probs: list) -> float:
    n = len(cat_probs)
    dist = np.zeros(n + 1)
    dist[0] = 1.0
    for p in cat_probs:
        dist[1:] = dist[1:] * (1 - p) + dist[:-1] * p
        dist[0] *= 1 - p
    half = n / 2
    wins = sum(dist[k] for k in range(n + 1) if k > half)
    ties = sum(dist[k] for k in range(n + 1) if k == half)
    return float(wins + 0.5 * ties)


def team_week_dist(player_games: dict, proj: pd.DataFrame, mult: dict) -> dict:
    """Normal approximation of a team's weekly totals. player_games: pid -> expected games played."""
    cats = league_categories() if scoring_mode() == "categories" else ["_points"]
    pw = point_weights()
    res = {}
    for k in cats:
        mu = var = 0.0
        for pid, g in player_games.items():
            if pid not in proj.index or g <= 0:
                continue
            row = proj.loc[pid]
            m = mult.get(pid, 1.0)
            if k == "_points":
                pm = sum(row[s] * w for s, w in pw.items() if s in row)
                ps = sum((row[s + "_sd"] * w) ** 2 for s, w in pw.items() if s + "_sd" in row)
                mu += g * m * pm
                var += g * ps
            else:
                col = cat_column(k)
                mu += g * m * row[col]
                var += g * row[col + "_sd"] ** 2
        res[k] = (mu, var)
    return res


def matchup_odds(dist_a: dict, dist_b: dict) -> tuple:
    probs = {}
    for k, (ma, va) in dist_a.items():
        mb, vb = dist_b[k]
        sd = math.sqrt(va + vb) or 1e-9
        higher = config.CATEGORY_DEFS[k][4] if k in config.CATEGORY_DEFS else True
        z = (ma - mb) / sd if higher else (mb - ma) / sd
        probs[k] = _phi(z)
    if list(probs) == ["_points"]:
        return probs["_points"], probs
    return poisson_binomial_win(list(probs.values())), probs


def avg_opponent_dist(team_dists: list) -> dict:
    keys = team_dists[0].keys()
    return {k: (np.mean([d[k][0] for d in team_dists]), np.mean([d[k][1] for d in team_dists]))
            for k in keys}


def dump(obj) -> str:
    return json.dumps(obj, default=lambda o: o.item() if hasattr(o, "item") else str(o))
