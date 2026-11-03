"""Per-game projections, availability, and fantasy valuation.

Projection = exponentially-weighted mean of a player's recent games, with prior-season
games down-weighted. Both knobs (half-life, prior-season weight) are tuned by the
learning module against out-of-sample games, so the defaults in config are only a start.
"""
import json
import re
import math
from datetime import date, timedelta

import numpy as np
import pandas as pd

from . import config, db

STATS = ["min", "fgm", "fga", "fg3m", "fg3a", "ftm", "fta", "oreb", "dreb", "reb",
         "ast", "stl", "blk", "tov", "pts", "dd", "td"]
MAX_GAMES = 60  # older games carry ~nothing at any sensible half-life
PRIOR_GAMES = 3.0

# "won't be ready for the start of the season" is NOT season-ending; match real phrases only
SEASON_ENDING = re.compile(r"out for the (?:rest of the |remainder of the )?season|season-ending|"
                           r"rest of the season|remainder of the season|miss the (?:entire |whole )?season")

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


PRIOR_SEASON_HALF_LIFE = 30.0  # within last season: mostly a full-season average
THIN_SEASON_GAMES = 25         # fewer games than this last season -> also use the season before
OLDER_SEASON_WEIGHT = 0.7      # a game two seasons back counts this much vs last season


def _season_estimates(df: pd.DataFrame, cols, hl_rate, hl_min) -> dict:
    """Recency-weighted minutes, per-minute rates and per-game second moments, one season."""
    df = df.sort_values(["nba_id", "game_date"], ascending=[True, False])
    rec = df.groupby("nba_id").cumcount().to_numpy()
    keep = rec < MAX_GAMES
    df, rec = df[keep], rec[keep]
    w, wm = 0.5 ** (rec / hl_rate), 0.5 ** (rec / hl_min)
    ids, inv = np.unique(df["nba_id"].to_numpy(), return_inverse=True)
    vals, mins = df[cols].to_numpy(dtype=float), df["min"].to_numpy(dtype=float)
    ws = lambda x, ww: np.bincount(inv, weights=ww * x, minlength=len(ids))
    sw, sw2 = np.bincount(inv, weights=w), np.bincount(inv, weights=w ** 2)
    return {
        "ids": ids, "n": np.bincount(inv).astype(float), "n_eff": sw ** 2 / sw2,
        "min": ws(mins, wm) / np.bincount(inv, weights=wm),
        "rate": np.stack([ws(vals[:, j], w) for j in range(len(cols))], 1) / np.maximum(ws(mins, w), 1e-6)[:, None],
        "sq": np.stack([ws(vals[:, j] ** 2, w) for j in range(len(cols))], 1) / sw[:, None],
    }


def _align(est, ids, width=None):
    """Re-index a season estimate onto `ids`; missing players get n=0 (so they carry no weight)."""
    pos = {p: i for i, p in enumerate(est["ids"])}
    idx = np.array([pos.get(p, -1) for p in ids])
    has = idx >= 0
    out = {}
    for k, v in est.items():
        if k == "ids":
            continue
        arr = np.zeros((len(ids),) + v.shape[1:])
        arr[has] = v[idx[has]]
        out[k] = arr
    return out


def project(logs: pd.DataFrame, params: dict, as_of=None) -> pd.DataFrame:
    """Per-game projection (mean, std) for every player, using only games before `as_of`.

    projection = expected minutes x per-minute production, each estimated as a Bayesian blend:

        this season (recency-weighted, short memory)  -- the evidence
        last season (near full-season average)         -- a prior worth k games

    so in October last season's average dominates, and by December this season's form does.
    Minutes and rates get separate half-lives and separate k: minutes jump with injuries,
    rotations and trades; per-minute production is closer to a stable skill. A single
    recency weight across the offseason projected players from their last 5 games of the
    previous season (rookie walls, tanking, shutdowns), which is a bad October predictor.
    """
    if as_of is not None:
        logs = logs[logs["game_date"] < pd.Timestamp(as_of)]
    seasons = sorted(logs["season"].unique())
    cur_s = seasons[-1]
    prev_s = seasons[-2] if len(seasons) > 1 else None
    prev2_s = seasons[-3] if len(seasons) > 2 else None
    logs = logs[logs["season"].isin([cur_s, prev_s, prev2_s])]
    pcts = league_pcts(logs)
    df = _with_linear_ratio_stats(logs, pcts)
    cols = STATS + [k + "_lin" for k, d in config.CATEGORY_DEFS.items() if d[1] == "ratio"]
    ids = np.unique(df["nba_id"].to_numpy())
    C = _align(_season_estimates(df[df["season"] == cur_s], cols,
                                 params["half_life_games"], params.get("half_life_minutes", 4.0)), ids)
    if prev_s is not None and (df["season"] == prev_s).any():
        P = _align(_season_estimates(df[df["season"] == prev_s], cols,
                                     PRIOR_SEASON_HALF_LIFE, PRIOR_SEASON_HALF_LIFE), ids)
    else:
        P = {k: np.zeros_like(v) for k, v in C.items()}
    if prev2_s is not None and (df["season"] == prev2_s).any():
        # Players whose last season was cut short (Achilles, ACL...) would otherwise be
        # projected from a handful of games and shrunk to a generic rotation player. For them,
        # top up last season's prior with the season before, counted at a discount for age.
        P2 = _align(_season_estimates(df[df["season"] == prev2_s], cols,
                                      PRIOR_SEASON_HALF_LIFE, PRIOR_SEASON_HALF_LIFE), ids)
        thin = (P["n"] < THIN_SEASON_GAMES) & (P2["n"] > 0)
        w1, w2 = P["n"][thin], OLDER_SEASON_WEIGHT * P2["n"][thin]
        for k in ("min", "rate", "sq"):
            a, b = P[k][thin], P2[k][thin]
            shape = (-1,) + (1,) * (a.ndim - 1)
            P[k][thin] = (w1.reshape(shape) * a + w2.reshape(shape) * b) / (w1 + w2).reshape(shape)
        P["n"][thin] = w1 + w2
    # a prior can't be worth more games than it was built from
    k_r = np.minimum(P["n"], params.get("prior_strength_games", 15.0))
    k_m = np.minimum(P["n"], params.get("prior_strength_minutes", 8.0))
    nc = C["n"]
    blend = lambda c, p, k: (nc.reshape(-1, *[1] * (c.ndim - 1)) * c + k.reshape(-1, *[1] * (c.ndim - 1)) * p) / \
        np.maximum(nc + k, 1e-9).reshape(-1, *[1] * (c.ndim - 1))
    proj_min, rate, sq = blend(C["min"], P["min"], k_m), blend(C["rate"], P["rate"], k_r), blend(C["sq"], P["sq"], k_r)
    # Then shrink toward a generic rotation player with PRIOR_GAMES pseudo-games, so a
    # rookie's one hot (or cold) game can't define him.
    n = nc + k_r
    rot = ((nc + P["n"]) >= 10) & (proj_min >= 12)   # rotation players, judged on all games seen
    if not rot.any():
        raise ValueError("no rotation players to build the league prior from (not enough games)")
    alpha = n / (n + PRIOR_GAMES)
    proj_min = alpha * proj_min + (1 - alpha) * proj_min[rot].mean()
    rate = alpha[:, None] * rate + (1 - alpha[:, None]) * rate[rot].mean(0)
    sq = alpha[:, None] * sq + (1 - alpha[:, None]) * sq[rot].mean(0)
    mean = rate * proj_min[:, None]
    mean[:, 0] = proj_min
    std = np.sqrt(np.maximum(sq - mean ** 2, 0))
    out = pd.DataFrame(mean, index=ids, columns=cols)
    out = out.join(pd.DataFrame(std, index=ids, columns=[c + "_sd" for c in cols]))
    out["n_games"] = np.round(nc + P["n"]).astype(int)
    out["cur_games"] = nc.astype(int)
    out["n_eff"] = C["n_eff"] + k_r
    out.index.name = "nba_id"
    out.attrs["pcts"] = pcts
    out.attrs["season"] = cur_s
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


GAMES_PER_DAY = 82 / 174          # an NBA team plays ~82 games in ~174 days
SEASON_END = (4, 12)               # regular season ends mid-April
_NUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
        "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "a": 1, "a couple of": 2, "several": 3}
_NUM_RE = r"(\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|a couple of|several|a)"
_MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september",
           "october", "november", "december"]


def _num(tok):
    return float(tok) if tok.isdigit() else float(_NUM.get(tok, 1))


def season_games_left(today=None) -> float:
    today = today or date.today()
    end = date(today.year + (1 if today.month >= 8 else 0), *SEASON_END)
    return max(0.0, (end - today).days * GAMES_PER_DAY)


def injury_games_out(comment: str, status: str = "", today=None):
    """Expected games missed, read from the injury note's own timeline. None if it gives none.
    Examples: "at least the next four weeks" -> ~13; "between January and February" -> to
    mid-January-ish; "out for the season" -> rest of season."""
    if not comment:
        return None
    c = comment.lower()
    today = today or date.today()
    if SEASON_ENDING.search(c):
        return season_games_left(today)
    if re.search(r"\b(?:out|miss|sit|not play)\w*\b.{0,25}\b(?:\w+day'?s|tonight'?s|the)\s+"
                 r"(?:season opener|opener|game|contest|matchup)\b", c):
        return 1.0  # a single named game
    m = re.search(_NUM_RE + r"(?:\s*(?:to|-|or)\s*" + _NUM_RE + r")?\s+weeks?", c)
    if m:
        lo = _num(m.group(1))
        hi = _num(m.group(2)) if m.group(2) else lo
        return (lo + hi) / 2 * 7 * GAMES_PER_DAY
    m = re.search(_NUM_RE + r"(?:\s*(?:to|-|or)\s*" + _NUM_RE + r")?\s+months?", c)
    if m:
        lo = _num(m.group(1))
        hi = _num(m.group(2)) if m.group(2) else lo
        return (lo + hi) / 2 * 30 * GAMES_PER_DAY
    found = [i for i, name in enumerate(_MONTHS) if re.search(r"\b" + name + r"\b", c)]
    if found and re.search(r"return|back|until|sideline|out|ready|recover", c):
        # first month mentioned that's still ahead of us; return around its midpoint
        best = None
        for i in found:
            target = date(today.year, i + 1, 15)
            if target <= today:
                target = date(today.year + 1, i + 1, 15)
            if best is None or target < best:
                best = target
        if len(found) > 1:  # "between January and February": aim between the two
            ordered = sorted(date(today.year + (1 if (i + 1) < today.month else 0), i + 1, 1) for i in found)
            best = ordered[0] + (ordered[-1] - ordered[0]) / 2 + timedelta(days=14)
        return max(0.0, (best - today).days * GAMES_PER_DAY)
    # No timeline, but the injury itself says it's long (Oct 2026: J. Williams' fractured fibula
    # "will require surgery" read as no information, so he projected to play 64% of the season)
    if SURGERY.search(c):
        return SURGERY_WEEKS * 7 * GAMES_PER_DAY
    if STRUCTURAL.search(c):
        return STRUCTURAL_WEEKS * 7 * GAMES_PER_DAY
    if "day-to-day" in c or "game-time decision" in c:
        return 0.5
    # A rest day or one named night, worded without "game" (Nov 2026: "Siakam will be rested
    # against the Hawks on Monday" read as no timeline, so Out all week). Checked last so a
    # real timeline elsewhere in the note ("hurt on Monday, out two weeks") wins.
    if re.search(r"\brest(?:ed|ing)\b|\b(?:out|miss\w*|sit\w*|not play|won't play)\b.{0,50}"
                 r"\b(?:on (?:mon|tues|wednes|thurs|fri|satur|sun)day|tonight)\b", c):
        return 1.0
    return None


# Priors for notes that name a serious injury but no timeline; any stated timeline wins.
SURGERY = re.compile(r"\bsurger|\boperation\b|\bprocedure\b")
SURGERY_WEEKS = 8
STRUCTURAL = re.compile(r"\bfractur|\bbroken\b|\btorn\b|\btear\b|\brupture|\bdislocat")
STRUCTURAL_WEEKS = 4


def _games_out(pid, injuries, events):
    """News-extracted timeline first (more specific), then the injury note's own wording."""
    ev = events.get(pid)
    inj = injuries.get(pid)
    note = None
    if inj and (inj["status"] or "").lower() in ("out", "doubtful", "suspension"):
        note = injury_games_out(inj["comment"] or "", inj["status"] or "")
    if ev and ev["games_out"] is not None:
        g = float(ev["games_out"])
        # Only good news (a return date) can shorten the note's timeline. A routine "out
        # Saturday" item about a long injury isn't a new timeline (Nov 2026: Sharpe, torn
        # meniscus, six months, read as one game out and recommended as a pickup).
        if note is not None and (ev.get("direction") or 0) < 0:
            return max(g, note)
        return g
    return note


# Consecutive team games already missed by players listed Out (set when a context loads).
# Floor on the remaining absence: a note that only says "out for Saturday's game" about a
# player who hasn't played all season (Oct 2026: Aldama, Harris) shouldn't read as one game.
ABSENCE_STREAK: dict = {}
STREAK_MIN = 2

# Share of games a player with no injury listing still misses (new injuries, rest, illness).
# Learned from box scores by miss_rates(); set from params when a context loads.
HEALTHY_GAME = 0.94     # per-game chance a non-listed player suits up this week
HEALTHY_ROS = 0.90      # share of the rest of the season a non-listed player plays


def miss_rates(logs: pd.DataFrame, season: str) -> dict:
    """How often rotation players miss their team's games, from one season of box scores.
    short: misses in runs of 1-3 games (mostly unannounced: rest, illness, knocks)
    all:   every miss, including long injuries (announced, in practice, once they happen)"""
    s = logs[logs["season"] == season]
    team_dates = {t: sorted(g.unique()) for t, g in s.groupby("team")["game_date"]}
    short = total = team_games = 0
    for (pid, team), g in s.groupby(["nba_id", "team"]):
        if len(g) < 20:
            continue
        played = set(g["game_date"])
        span = [d for d in team_dates[team] if g["game_date"].min() <= d <= g["game_date"].max()]
        run = 0
        for d in span + [None]:
            if d is not None and d not in played:
                run += 1
                continue
            total += run
            short += run if run <= 3 else 0
            run = 0
        team_games += len(span)
    return {"short": short / max(team_games, 1), "all": total / max(team_games, 1), "player_team_games": team_games}


def availability(pid, game_dates, injuries, events, b2b, team_dates_set):
    """Probability the player suits up for each of `game_dates` (ISO strings, sorted)."""
    inj = injuries.get(pid)
    base = HEALTHY_GAME
    if inj:
        status = (inj["status"] or "").lower()
        base = INJURY_AVAIL.get(status, 0.7)
        if status == "out" and SEASON_ENDING.search((inj["comment"] or "").lower()):
            base = 0.0
    games_out = _games_out(pid, injuries, events)
    if games_out is not None and inj and inj.get("date"):
        # the note's clock started when it was written: games the team has played since then
        # already count toward it (e.g. "out for Wednesday's opener", read on Thursday)
        noted = str(inj["date"])[:10]
        elapsed = sum(1 for d in team_dates_set if noted <= d < date.today().isoformat())
        games_out = max(0.0, games_out - elapsed)
        if games_out == 0:
            base = max(base, 0.8)  # timeline served; still listed, so not fully trusted yet
    streak = _streak(pid, injuries)
    if streak:
        games_out = max(games_out or 0.0, streak)
    out = []
    for i, d in enumerate(game_dates):
        p = base
        if games_out is not None and games_out > 0:
            p = 0.0 if i < round(games_out) else max(base, 0.85)
        prev = (date.fromisoformat(d) - timedelta(days=1)).isoformat()
        if prev in team_dates_set:
            p *= 1 - b2b.get(pid, 0.05)
        out.append(p)
    return out


def ros_availability(pid, injuries, events) -> float:
    """Expected share of the rest of the season's games the player suits up for."""
    inj = injuries.get(pid)
    games_out = _games_out(pid, injuries, events)
    streak = _streak(pid, injuries)
    if streak:
        games_out = max(games_out or 0.0, streak)
    left = max(season_games_left(), 1.0)
    if games_out is not None:
        return max(0.0, 1 - games_out / left) * HEALTHY_ROS
    if not inj:
        return HEALTHY_ROS
    status = (inj["status"] or "").lower()
    return HEALTHY_ROS * {"out": 0.85, "doubtful": 0.92, "questionable": 0.97}.get(status, 0.98)


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



def _streak(pid, injuries):
    """Games already missed in a row, if he's listed Out (else None)."""
    inj = injuries.get(pid)
    if not inj or (inj.get("status") or "").lower() != "out":
        return None
    n = ABSENCE_STREAK.get(pid, 0)
    return float(n) if n >= STREAK_MIN else None


def absence_streaks(logs: pd.DataFrame, players: pd.DataFrame, injuries: dict) -> dict:
    """{pid: team games this season since his last game} for players listed Out."""
    if logs.empty:
        return {}
    season = logs["season"].max()
    cur = logs[logs["season"] == season]
    team_dates = cur.groupby("team")["game_date"].apply(lambda s: sorted(set(s.astype(str).str[:10])))
    last = logs.groupby("nba_id")["game_date"].max().astype(str).str[:10]
    out = {}
    for pid, inj in injuries.items():
        if (inj.get("status") or "").lower() != "out" or pid not in players.index:
            continue
        team = players.at[pid, "team"]
        if not isinstance(team, str) or team not in team_dates.index:
            continue
        lp = last.get(pid, "")
        out[pid] = sum(1 for d in team_dates[team] if d > lp)
    return out


def expected_games_out(pid, injuries, events):
    """Best estimate of games still to miss (timeline, floored by the absence so far), or None."""
    g = _games_out(pid, injuries, events)
    st = _streak(pid, injuries)
    return max(g or 0.0, st or 0.0) if (g is not None or st) else None
