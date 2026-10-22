"""Monte Carlo weekly matchup simulator.

Each simulated game is a *real* past box score of that player (bootstrap, recent games more
likely), so correlations inside a stat line (a big scoring night is also a high-FGA night)
are preserved, which matters for FG%/FT%. Per player-day random streams are seeded by
(seed, player, day) — "common random numbers" — so when we compare two rosters that differ by
one player, every other player draws identical games and the difference is pure signal.
"""
from dataclasses import dataclass, field
from datetime import date, timedelta

import numpy as np
import pandas as pd

from . import config, db, model, nba

SIM_STATS = ["fgm", "fga", "fg3m", "fg3a", "ftm", "fta", "oreb", "dreb", "reb", "ast",
             "stl", "blk", "tov", "pts", "dd", "td"]
POOL_GAMES = 40


@dataclass
class Context:
    logs: pd.DataFrame
    proj: pd.DataFrame
    values: pd.DataFrame
    params: dict
    injuries: dict
    events: dict
    b2b: pd.Series
    players: pd.DataFrame
    rosters: dict                      # team_key -> [nba_id]
    teams: dict                        # team_key -> row dict
    my_key: str | None
    opp_key: str | None
    week: int
    week_start: str
    week_end: str
    active_slots: int
    roster_size: int
    num_teams: int
    weeks_remaining: int
    wtd: dict = field(default_factory=dict)     # team_key -> {stat: value} week-to-date
    _pools: dict = field(default_factory=dict)
    _team_dates: dict = field(default_factory=dict)

    @property
    def cats(self):
        return model.league_categories()

    def team_of(self, pid):
        return self.players["team"].get(pid)

    def name(self, pid):
        return self.players["name"].get(pid, str(pid))

    def rostered(self) -> set:
        return {p for ids in self.rosters.values() for p in ids}


def load_context() -> Context:
    params = db.get_params()
    logs = nba.load_logs()
    proj = model.project(logs, params)
    num_teams = db.kv_get("num_teams", 10)
    roster_size = db.kv_get("roster_size", 13)
    values = model.value_players(proj, num_teams, roster_size)
    with db.cursor() as c:
        players = pd.read_sql_query("SELECT * FROM players", c).set_index("nba_id")
        teams = {r["team_key"]: dict(r) for r in c.execute("SELECT * FROM fantasy_teams")}
        rosters = {}
        for r in c.execute("SELECT team_key, nba_id FROM rosters"):
            rosters.setdefault(r["team_key"], []).append(r["nba_id"])
    my_key = next((k for k, t in teams.items() if t["is_mine"]), None)
    week = db.kv_get("current_week", 1)
    ws, we = db.kv_get("week_range", default_week_range())
    ctx = Context(logs=logs, proj=proj, values=values, params=params,
                  injuries=model.injury_table(), events=model.recent_news_events(),
                  b2b=model.back_to_back_sit_rates(logs), players=players, rosters=rosters,
                  teams=teams, my_key=my_key, opp_key=db.kv_get("opponent_key"), week=week,
                  week_start=ws, week_end=we, active_slots=db.kv_get("active_slots", config.DEFAULT_ACTIVE_SLOTS),
                  roster_size=roster_size, num_teams=num_teams,
                  weeks_remaining=db.kv_get("weeks_remaining", default_weeks_remaining()),
                  wtd=db.kv_get("week_to_date", {}))
    return ctx


def default_week_range():
    today = date.today()
    start = today - timedelta(days=today.weekday())
    return start.isoformat(), (start + timedelta(days=6)).isoformat()


def default_weeks_remaining():
    # Yahoo fantasy regular seasons typically end in mid-March before playoffs.
    end = date(date.today().year + (1 if date.today().month >= 8 else 0), 3, 15)
    return max(0, (end - date.today()).days // 7)


def remaining_dates(ctx: Context) -> list:
    start = max(ctx.week_start, date.today().isoformat())
    d, out = date.fromisoformat(start), []
    while d.isoformat() <= ctx.week_end:
        out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def team_dates(ctx: Context, team: str) -> set:
    if "_loaded" not in ctx._team_dates:
        for t, ds in nba.team_games("2000-01-01", "2100-01-01").items():
            ctx._team_dates[t] = set(ds)
        ctx._team_dates["_loaded"] = True
    return ctx._team_dates.get(team, set())


def player_games(ctx: Context, pid, dates) -> list:
    """(date, p_available) for each remaining date this player's NBA team plays."""
    team = ctx.team_of(pid)
    tds = team_dates(ctx, team)
    # a game that already has box scores is history (week-to-date), not a future draw
    gd = [d for d in dates if d in tds and (team, d) not in played_team_dates(ctx)]
    av = model.availability(pid, gd, ctx.injuries, ctx.events, ctx.b2b, tds)
    return list(zip(gd, av))


def played_team_dates(ctx: Context) -> set:
    if "_played" not in ctx._team_dates:
        cur = ctx.logs[ctx.logs["game_date"] >= ctx.week_start]
        ctx._team_dates["_played"] = set(zip(cur["team"], cur["game_date"].dt.strftime("%Y-%m-%d")))
    return ctx._team_dates["_played"]


def estimate_wtd(ctx: Context, pids) -> dict:
    """Week-to-date totals from box scores, used when Yahoo's own numbers aren't available.
    Approximation: assumes your best players with a game each day were the ones started."""
    cur = ctx.logs[(ctx.logs["game_date"] >= ctx.week_start) & ctx.logs["nba_id"].isin(pids)]
    value = ctx.values["value"]
    tot = {s: 0.0 for s in SIM_STATS}
    for _, day in cur.groupby("game_date"):
        day = day.assign(v=day["nba_id"].map(value).fillna(-99)).nlargest(ctx.active_slots, "v")
        for s in SIM_STATS:
            tot[s] += float(day[s].sum())
    return tot


def week_to_date(ctx: Context, team_key) -> dict:
    return ctx.wtd.get(team_key) or estimate_wtd(ctx, ctx.rosters.get(team_key, []))


def _pool(ctx: Context, pid):
    if pid in ctx._pools:
        return ctx._pools[pid]
    g = ctx.logs[ctx.logs["nba_id"] == pid].sort_values("game_date", ascending=False).head(POOL_GAMES)
    mult = model.production_multiplier(pid, ctx.events, ctx.params)
    if len(g) >= 5:
        lines = g[SIM_STATS].to_numpy(dtype=float)
        w = model.recency_weights(g, ctx.params)["w"].to_numpy()
        probs = w / w.sum()
        # nudge the pool toward the shrunk projection when the sample is small
        if pid in ctx.proj.index and len(g) < 15:
            target = ctx.proj.loc[pid, SIM_STATS].to_numpy(dtype=float)
            lines = 0.5 * lines + 0.5 * target
    else:
        lines = _synthetic_lines(ctx, pid)
        probs = np.full(len(lines), 1 / len(lines))
    lines = lines * mult  # makes and attempts scale together, so percentages are unchanged
    ctx._pools[pid] = (lines, probs)
    return ctx._pools[pid]


def _synthetic_lines(ctx, pid, n=30):
    rng = np.random.default_rng(pid)
    if pid in ctx.proj.index:
        mu = ctx.proj.loc[pid, SIM_STATS].clip(lower=0)
    else:  # nobody knows anything: deep-bench line
        mu = pd.Series({"fgm": 2, "fga": 4.5, "fg3m": .6, "fg3a": 1.8, "ftm": .8, "fta": 1.1,
                        "oreb": .5, "dreb": 1.5, "reb": 2, "ast": 1, "stl": .4, "blk": .3,
                        "tov": .6, "pts": 5.4, "dd": 0, "td": 0})
    out = np.zeros((n, len(SIM_STATS)))
    for j, s in enumerate(SIM_STATS):
        out[:, j] = rng.poisson(max(mu[s], 0), n)
    ix = {s: j for j, s in enumerate(SIM_STATS)}
    for m, a in (("fgm", "fga"), ("fg3m", "fg3a"), ("ftm", "fta")):
        out[:, ix[m]] = np.minimum(out[:, ix[m]], out[:, ix[a]])
    return out


def simulate_team(ctx: Context, pids, dates, n_sims=2000, seed=7, wtd=None) -> np.ndarray:
    """(n_sims, len(SIM_STATS)) weekly totals for a roster, honoring the daily active-slot cap."""
    totals = np.zeros((n_sims, len(SIM_STATS)))
    if wtd:
        totals += np.array([wtd.get(s, 0.0) for s in SIM_STATS])
    value = ctx.values["value"]
    sched = {pid: dict(player_games(ctx, pid, dates)) for pid in pids}
    for di, d in enumerate(dates):
        playing = [p for p in pids if d in sched[p]]
        playing.sort(key=lambda p: -value.get(p, -99))
        if not playing:
            continue
        masks, draws = [], []
        for pid in playing:
            rng = np.random.default_rng([seed, int(pid), di])
            lines, probs = _pool(ctx, pid)
            masks.append(rng.random(n_sims) < sched[pid][d])
            draws.append(lines[rng.choice(len(lines), n_sims, p=probs)])
        masks = np.stack(masks, 1)
        # You can't start more players than active slots: the best available ones play.
        taken = masks & (np.cumsum(masks, 1) <= ctx.active_slots)
        for k in range(len(playing)):
            totals += draws[k] * taken[:, k:k + 1]
    return totals


def score_totals(ctx: Context, a: np.ndarray, b: np.ndarray):
    ix = {s: j for j, s in enumerate(SIM_STATS)}
    if model.scoring_mode() == "points":
        pw = model.point_weights()
        pa = sum(a[:, ix[s]] * w for s, w in pw.items() if s in ix)
        pb = sum(b[:, ix[s]] * w for s, w in pw.items() if s in ix)
        win = (pa > pb) + 0.5 * (pa == pb)
        return float(win.mean()), {"_points": float(win.mean())}, None
    cat_win = {}
    wins = np.zeros(len(a))
    for k in ctx.cats:
        _, kind, num, den, higher = config.CATEGORY_DEFS[k]
        if kind == "ratio":
            va = np.divide(a[:, ix[num]], a[:, ix[den]], out=np.zeros(len(a)), where=a[:, ix[den]] > 0)
            vb = np.divide(b[:, ix[num]], b[:, ix[den]], out=np.zeros(len(b)), where=b[:, ix[den]] > 0)
        else:
            va, vb = a[:, ix[num]], b[:, ix[num]]
        w = (va > vb) if higher else (va < vb)
        w = w + 0.5 * (va == vb)
        cat_win[k] = float(w.mean())
        wins += w
    n = len(ctx.cats)
    match = (wins > n / 2) + 0.5 * (wins == n / 2)
    return float(match.mean()), cat_win, wins


def simulate_matchup(ctx: Context, my_pids=None, opp_pids=None, n_sims=2000, seed=7):
    dates = remaining_dates(ctx)
    my_pids = my_pids if my_pids is not None else ctx.rosters.get(ctx.my_key, [])
    opp_pids = opp_pids if opp_pids is not None else ctx.rosters.get(ctx.opp_key, [])
    a = simulate_team(ctx, my_pids, dates, n_sims, seed, week_to_date(ctx, ctx.my_key))
    b = simulate_team(ctx, opp_pids, dates, n_sims, seed + 1, week_to_date(ctx, ctx.opp_key))
    win, cats, wins = score_totals(ctx, a, b)
    ix = {s: j for j, s in enumerate(SIM_STATS)}
    summary = {}
    for k in (ctx.cats if model.scoring_mode() == "categories" else []):
        _, kind, num, den, _ = config.CATEGORY_DEFS[k]
        if kind == "ratio":
            summary[k] = [float(a[:, ix[num]].sum() / max(a[:, ix[den]].sum(), 1)),
                          float(b[:, ix[num]].sum() / max(b[:, ix[den]].sum(), 1))]
        else:
            summary[k] = [float(a[:, ix[num]].mean()), float(b[:, ix[num]].mean())]
    hist = None
    if wins is not None:
        hist = np.bincount(wins.round().astype(int), minlength=len(ctx.cats) + 1).tolist()
    return {"win_prob": win, "cat_probs": cats, "expected": summary, "cats_won_hist": hist,
            "dates": dates, "a": a, "b": b}


# ------------------------------------------------------------ season-long (analytic)

def expected_week_games(ctx: Context, pid, games_per_week=3.5) -> float:
    return games_per_week * model.ros_availability(pid, ctx.injuries, ctx.events)


def season_strength(ctx: Context, pids, opp_dist=None) -> float:
    """Expected weekly win probability vs a league-average opponent, using ROS projections."""
    dist = team_season_dist(ctx, pids)
    if opp_dist is None:
        opp_dist = league_average_dist(ctx)
    return model.matchup_odds(dist, opp_dist)[0]


def team_season_dist(ctx: Context, pids):
    """Typical-week distribution. Only active slots score, so the best players' games are
    kept up to active_slots x 3.5 games (a rough average week), worst players' games dropped."""
    mult = {p: model.production_multiplier(p, ctx.events, ctx.params) for p in pids}
    ranked = sorted(pids, key=lambda p: -ctx.values["value"].get(p, -99))
    games, used, cap = {}, 0.0, ctx.active_slots * 3.5
    for p in ranked:
        g = min(expected_week_games(ctx, p), max(cap - used, 0))
        games[p] = g
        used += g
    return model.team_week_dist(games, ctx.proj, mult)


def league_average_dist(ctx: Context):
    if "_avg" not in ctx._pools:
        dists = [team_season_dist(ctx, pids) for key, pids in ctx.rosters.items()
                 if key != ctx.my_key]
        ctx._pools["_avg"] = model.avg_opponent_dist(dists) if dists else None
    return ctx._pools["_avg"]
