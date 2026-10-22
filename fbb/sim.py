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
POOL_HALF_LIFE = 15.0  # sampling window for the shape of outcomes (not the mean)


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
    today: str | None = None                    # backtests replay the past: "today" is the week start
    raw_proj: object = None                     # the model's own projections, before your takes
    takes: dict = field(default_factory=dict)   # pid -> scale factor from your take
    defense: dict = field(default_factory=dict)  # team -> opponent factor (1.0 = average defense)
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
    apply_rates(params)
    logs = nba.load_logs()
    proj = model.project(logs, params)
    raw_proj = proj.copy()
    from . import overrides
    takes = overrides.apply(proj)  # your takes reshape everything downstream; learning never sees them
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
    week = db.kv_get("current_week", 1) if db.kv_get("league_source") == "yahoo" else season_week()
    ws, we = db.kv_get("week_range") if db.kv_get("league_source") == "yahoo" and db.kv_get("week_range") \
        else week_range_for(season_week())
    ctx = Context(logs=logs, proj=proj, values=values, params=params,
                  injuries=model.injury_table(), events=model.recent_news_events(),
                  b2b=model.back_to_back_sit_rates(logs), players=players, rosters=rosters,
                  teams=teams, my_key=my_key, opp_key=db.kv_get("opponent_key"), week=week,
                  week_start=ws, week_end=we, active_slots=db.kv_get("active_slots", config.DEFAULT_ACTIVE_SLOTS),
                  roster_size=roster_size, num_teams=num_teams,
                  weeks_remaining=db.kv_get("weeks_remaining", default_weeks_remaining()),
                  wtd=db.kv_get("week_to_date") or {}, raw_proj=raw_proj, takes=takes)
    if params.get("opp_adjust"):
        from . import matchups
        ctx.defense = matchups.defense_table(logs, date.today().isoformat(), params.get("opp_k", 80))
    return ctx


def apply_rates(params: dict):
    """Push learned absence rates into the availability model."""
    model.HEALTHY_GAME = 1 - params.get("miss_rate_short", 0.06)
    model.HEALTHY_ROS = 1 - params.get("miss_rate_all", 0.10)


def season_week(today=None) -> int:
    """Week number counted from the Monday of the season's first game week (Yahoo's week 1)."""
    with db.cursor() as c:
        first = c.execute("SELECT min(game_date) FROM schedule").fetchone()[0]
    if not first:
        return 1
    f = date.fromisoformat(first)
    f_mon = f - timedelta(days=f.weekday())
    t = today or date.today()
    t_mon = t - timedelta(days=t.weekday())
    return max(1, (t_mon - f_mon).days // 7 + 1)


def week_range_for(week: int):
    with db.cursor() as c:
        first = c.execute("SELECT min(game_date) FROM schedule").fetchone()[0]
    f = date.fromisoformat(first)
    start = f - timedelta(days=f.weekday()) + timedelta(days=7 * (week - 1))
    return start.isoformat(), (start + timedelta(days=6)).isoformat()


def calibrated(p: float, params: dict) -> float:
    """Apply the backtest-fitted temperature to a raw simulated win probability."""
    T = params.get("win_prob_temperature", 1.0) or 1.0
    if abs(T - 1.0) < 1e-6:
        return p
    p = min(max(p, 1e-4), 1 - 1e-4)
    return 1 / (1 + ((1 - p) / p) ** (1 / T))


def actual_totals(ctx, team_key, start: str, end: str):
    """A team's real totals for a past week from box scores, starters only where slots are known."""
    pids = ctx.rosters.get(team_key, [])
    slots = team_slots(ctx).get(team_key, {})
    if slots and any(s not in ("?", None) for s in slots.values()):
        pids = [p for p in pids if slots.get(p) not in BENCH_SLOTS]
    g = ctx.logs[(ctx.logs["game_date"] >= start) & (ctx.logs["game_date"] <= end) & ctx.logs["nba_id"].isin(pids)]
    val = ctx.values["value"]
    tot = np.zeros(len(SIM_STATS))
    for _, day in g.groupby("game_date"):
        day = day.assign(v=day["nba_id"].map(val).fillna(-99)).nlargest(ctx.active_slots, "v")
        tot += day[SIM_STATS].to_numpy(dtype=float).sum(0)
    return tot


def default_week_range():
    today = date.today()
    start = today - timedelta(days=today.weekday())
    return start.isoformat(), (start + timedelta(days=6)).isoformat()


def default_weeks_remaining():
    # Yahoo fantasy regular seasons typically end in mid-March before playoffs.
    end = date(date.today().year + (1 if date.today().month >= 8 else 0), 3, 15)
    return max(0, (end - date.today()).days // 7)


def remaining_dates(ctx: Context) -> list:
    start = max(ctx.week_start, ctx.today or date.today().isoformat())
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
    slots = team_slots(ctx).get(team_key_of(ctx, pids), {})
    if slots and any(s not in ("?", None) for s in slots.values()):
        # Lineup slots known (pasted from Yahoo): bench/IL players' games don't count.
        # Slots are as of the paste, so a mid-week lineup change is an approximation.
        pids = [p for p in pids if slots.get(p) not in BENCH_SLOTS]
    cur = ctx.logs[(ctx.logs["game_date"] >= ctx.week_start) & ctx.logs["nba_id"].isin(pids)]
    value = ctx.values["value"]
    tot = {s: 0.0 for s in SIM_STATS}
    for _, day in cur.groupby("game_date"):
        day = day.assign(v=day["nba_id"].map(value).fillna(-99)).nlargest(ctx.active_slots, "v")
        for s in SIM_STATS:
            tot[s] += float(day[s].sum())
    return tot


BENCH_SLOTS = {"BN", "IL", "IL+", "NA"}


def team_slots(ctx: Context) -> dict:
    if "_slots" not in ctx._pools:
        out = {}
        with db.cursor() as c:
            for r in c.execute("SELECT team_key, nba_id, slot FROM rosters"):
                out.setdefault(r["team_key"], {})[r["nba_id"]] = r["slot"]
        ctx._pools["_slots"] = out
    return ctx._pools["_slots"]


def team_key_of(ctx: Context, pids):
    s = set(pids)
    return next((k for k, ids in ctx.rosters.items() if set(ids) == s), None)


def week_to_date(ctx: Context, team_key) -> dict:
    return ctx.wtd.get(team_key) or estimate_wtd(ctx, ctx.rosters.get(team_key, []))


def _pool(ctx: Context, pid):
    if pid in ctx._pools:
        return ctx._pools[pid]
    g = ctx.logs[ctx.logs["nba_id"] == pid].sort_values("game_date", ascending=False).head(POOL_GAMES)
    mult = model.production_multiplier(pid, ctx.events, ctx.params)
    if len(g) >= 5:
        lines = g[SIM_STATS].to_numpy(dtype=float)
        # The projection's half-life is tuned for the *mean* and can be very short; sampling
        # with it would bootstrap from ~8 games. Use a long window for the shape of outcomes,
        # then rescale each stat so the pool's mean equals the projection.
        w = model.recency_weights(g, dict(ctx.params, half_life_games=POOL_HALF_LIFE))["w"].to_numpy()
        probs = w / w.sum()
        if pid in ctx.proj.index:
            target = ctx.proj.loc[pid, SIM_STATS].to_numpy(dtype=float)
            pool_mean = probs @ lines
            scale = np.clip(np.divide(target, pool_mean, out=np.ones_like(target), where=pool_mean > 0), 0.5, 2.0)
            lines = lines * scale
            ix = {s: j for j, s in enumerate(SIM_STATS)}
            for m, a in (("fgm", "fga"), ("fg3m", "fg3a"), ("ftm", "fta")):
                lines[:, ix[m]] = np.minimum(lines[:, ix[m]], lines[:, ix[a]])
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
    # A player's level isn't known exactly and drifts week to week (role, minutes, matchups):
    # each simulated week draws one shared multiplier per player (mean 1, learned spread).
    sd = float(ctx.params.get("weekly_form_sd", 0.0) or 0.0)
    form = {}
    for pid in pids:
        z = np.random.default_rng([seed, int(pid), 999]).standard_normal(n_sims)
        form[pid] = np.exp(sd * z - sd * sd / 2)[:, None] if sd > 0 else 1.0
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
            draws.append(lines[rng.choice(len(lines), n_sims, p=probs)] * form[pid]
                         * game_factor(ctx, pid, d))
        masks = np.stack(masks, 1)
        # Real lineup slots (PG/SG/G/SF/PF/F/C/C/Util/Util): for each pattern of who's
        # available in a simulated day, fill the slots best-first with position eligibility.
        taken = fill_patterns(ctx, playing, masks)
        for k in range(len(playing)):
            totals += draws[k] * taken[:, k:k + 1]
    return totals


def _slot_setup(ctx):
    if "_slots_setup" not in ctx._pools:
        from . import lineup
        ctx._pools["_slots_setup"] = (lineup.slot_list(), {}, {})
    return ctx._pools["_slots_setup"]


def player_slot_mask(ctx, pid) -> int:
    from . import lineup
    slots, pmask, _ = _slot_setup(ctx)
    if pid not in pmask:
        mode = ctx.params.get("position_mode", "widened")
        if mode == "off":  # every player fits every slot: the old 'best 10 each day' rule
            pmask[pid] = (1 << len(slots)) - 1
        else:
            pmask[pid] = lineup.slot_masks(lineup.positions(ctx, pid, mode)[0], slots)
    return pmask[pid]


def started_flags(ctx, avail_players: tuple) -> list:
    """Who starts, given the players available on a day (in value order). Memoized: within a
    day most simulated weeks share a pattern, and candidate swaps change one player."""
    from . import lineup
    slots, _, memo = _slot_setup(ctx)
    hit = memo.get(avail_players)
    if hit is None:
        hit = lineup.greedy_fill([player_slot_mask(ctx, p) for p in avail_players], len(slots))
        memo[avail_players] = hit
    return hit


def fill_patterns(ctx, playing: list, masks: np.ndarray) -> np.ndarray:
    """masks: (n_sims, k) availability, players in value order -> (n_sims, k) started."""
    k = len(playing)
    keys = masks.astype(np.int64) @ (np.int64(1) << np.arange(k, dtype=np.int64))
    uniq, inv = np.unique(keys, return_inverse=True)
    taken_u = np.zeros((len(uniq), k), dtype=bool)
    for ui, key in enumerate(uniq):
        idx = [j for j in range(k) if (int(key) >> j) & 1]
        flags = started_flags(ctx, tuple(playing[j] for j in idx))
        for f, j in zip(flags, idx):
            taken_u[ui, j] = f
    return taken_u[inv]


def expected_starts(ctx, pids, d, avail) -> dict:
    """Expected games *started* on day d, deterministically (no sampling noise: sampled draws
    flipped 30 of 414 close backtest matchups). Best player first, each brings his expected
    availability (e.g. 0.75 of a game) and pours it into open slot capacity he's eligible for,
    least-needed slots first. Bench players soak up capacity left by starters' expected
    absences. With positions off this is exactly the old 'best 10 each day' fill.
    Returns {pid: (expected starts, scheduled-and-considered flag)}."""
    playing = [p for p in pids if d in team_dates(ctx, ctx.team_of(p))]
    if not playing:
        return {}
    playing.sort(key=lambda p: -ctx.values["value"].get(p, -99))
    slots, _, _ = _slot_setup(ctx)
    masks = [player_slot_mask(ctx, p) for p in playing]
    cap = [1.0] * len(slots)
    out = {}
    for j, p in enumerate(playing):
        need = float(avail[p])
        got = 0.0
        open_ = [i for i in range(len(slots)) if (masks[j] >> i) & 1 and cap[i] > 1e-12]
        considered = bool(open_)
        # least demanded by the players still to come, then least flexible
        open_.sort(key=lambda i: (sum((m >> i) & 1 for m in masks[j + 1:]), bin(masks[j]).count("1")))
        for i in open_:
            if need <= 1e-12:
                break
            take = min(need, cap[i])
            cap[i] -= take
            need -= take
            got += take
        out[p] = (got, considered)
    return out


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
    raw_win = win
    win = calibrated(win, ctx.params)
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
    out = {"win_prob": win, "win_prob_raw": raw_win, "cat_probs": cats, "expected": summary,
           "cats_won_hist": hist, "dates": dates, "a": a, "b": b}
    if model.scoring_mode() == "points":
        out.update(points_summary(ctx, a, b, ctx.my_key, ctx.opp_key))
    return out


def points_summary(ctx: Context, a, b, my_key, opp_key) -> dict:
    """Projected final fantasy points, where they come from, and the margin distribution."""
    ix = {s: j for j, s in enumerate(SIM_STATS)}
    pw = model.point_weights()
    fa = sum(a[:, ix[s]] * w for s, w in pw.items() if s in ix)
    fb = sum(b[:, ix[s]] * w for s, w in pw.items() if s in ix)
    wa, wb = week_to_date(ctx, my_key), week_to_date(ctx, opp_key)
    breakdown = {s: [float(a[:, ix[s]].mean() * w), float(b[:, ix[s]].mean() * w)]
                 for s, w in pw.items() if s in ix}
    margin = fa - fb
    edges = np.linspace(np.percentile(margin, 1), np.percentile(margin, 99), 21)
    counts, edges = np.histogram(margin, bins=edges)
    return {"points": {
        "projected": [float(fa.mean()), float(fb.mean())],
        "so_far": [sum(wa.get(s, 0) * w for s, w in pw.items()), sum(wb.get(s, 0) * w for s, w in pw.items())],
        "range_me": [float(np.percentile(fa, 10)), float(np.percentile(fa, 90))],
        "range_opp": [float(np.percentile(fb, 10)), float(np.percentile(fb, 90))],
        "breakdown": breakdown,
        "margin_hist": {"counts": counts.tolist(), "edges": edges.tolist()},
    }}


# ------------------------------------------------------------ season-long (analytic)

def expected_week_games(ctx: Context, pid, games_per_week=3.5) -> float:
    return games_per_week * model.ros_availability(pid, ctx.injuries, ctx.events)


def season_strength(ctx: Context, pids, opp_dist=None, exclude_key=None) -> float:
    """Expected weekly win rate over the rest of the season, using ROS projections.

    Averaged over the league's actual teams (you play each of them, not an 'average team');
    falls back to a league-average opponent when other rosters aren't known."""
    dist = team_season_dist(ctx, pids)
    if opp_dist is not None:
        return calibrated_season(model.matchup_odds(dist, opp_dist)[0], ctx.params)
    opps = opponent_dists(ctx, exclude_key if exclude_key is not None else ctx.my_key)
    if not opps:
        return calibrated_season(model.matchup_odds(dist, league_average_dist(ctx))[0], ctx.params)
    return float(np.mean([calibrated_season(model.matchup_odds(dist, od)[0], ctx.params) for od in opps]))


def opponent_dists(ctx: Context, exclude_key) -> list:
    cache = ctx._pools.setdefault("_opp_dists", {})
    if exclude_key not in cache:
        cache[exclude_key] = [team_season_dist(ctx, ids) for k, ids in ctx.rosters.items()
                              if k != exclude_key and ids]
    return cache[exclude_key]


def calibrated_season(p: float, params: dict) -> float:
    T = params.get("season_temperature", 1.0) or 1.0
    if abs(T - 1.0) < 1e-6:
        return p
    p = min(max(p, 1e-4), 1 - 1e-4)
    return 1 / (1 + ((1 - p) / p) ** (1 / T))


def player_fp_sd(ctx: Context, pid) -> float | None:
    """Single-game fantasy-point spread from the player's own recent games. Summing per-stat
    variances would understate it: points, rebounds and assists rise and fall together."""
    cache = ctx._pools.setdefault("_fp_sd", {})
    if pid not in cache:
        pw = model.point_weights()
        g = ctx.logs[ctx.logs["nba_id"] == pid].sort_values("game_date").tail(40)
        if len(g) >= 8 and pw:
            fp = sum(g[s] * w for s, w in pw.items() if s in g)
            cache[pid] = float(fp.std())
        else:
            cache[pid] = None
    return cache[pid]


def team_dist(ctx: Context, pids, dates, per_week=True) -> dict:
    """Normal approximation of a team's totals over `dates` (rescaled to 7 days if per_week),
    with the same variance sources the simulator has:
      game-to-game spread  x expected games started
      games-played spread  (absences: each scheduled game is a coin flip at the availability rate)
      weekly form          (the player's level itself drifts: weekly_form_sd)"""
    tds = {p: team_dates(ctx, ctx.team_of(p)) for p in pids}
    avail = {p: model.ros_availability(p, ctx.injuries, ctx.events) for p in pids}
    ranked = sorted(pids, key=lambda p: -ctx.values["value"].get(p, -99))
    used = {p: 0.0 for p in pids}
    used_adj = {p: 0.0 for p in pids}
    nvar = {p: 0.0 for p in pids}
    for d in dates:
        for p, (s, considered) in expected_starts(ctx, pids, d, avail).items():
            used[p] += s
            used_adj[p] += s * game_factor(ctx, p, d)
            if considered:
                nvar[p] += avail[p] * (1 - avail[p])  # does he play at all: the games coin flip
    scale = 7 / len(dates) if per_week and dates else 1.0
    form_sd = float(ctx.params.get("weekly_form_sd", 0.0) or 0.0)
    cats = model.league_categories() if model.scoring_mode() == "categories" else ["_points"]
    pw = model.point_weights()
    res = {}
    for k in cats:
        mu = var = 0.0
        for p in pids:
            if p not in ctx.proj.index or used[p] <= 0:
                continue
            row = ctx.proj.loc[p]
            m = model.production_multiplier(p, ctx.events, ctx.params)
            if k == "_points":
                pm = m * sum(row[s] * w for s, w in pw.items() if s in row)
                sd = player_fp_sd(ctx, p)
                ps = sd * sd if sd is not None else sum((row[s + "_sd"] * w) ** 2 for s, w in pw.items() if s + "_sd" in row)
            else:
                col = model.cat_column(k)
                pm, ps = m * row[col], row[col + "_sd"] ** 2
            g, gv = used[p] * scale, nvar[p] * scale
            mu += used_adj[p] * scale * pm   # same games, weighted by the defenses they face
            var += g * ps + gv * pm * pm + (form_sd * g * pm) ** 2
        res[k] = (mu, var)
    return res


SEASON_LOOKAHEAD_DAYS = 28


def lookahead_dates(ctx: Context) -> list:
    if "_look" not in ctx._pools:
        start = max(date.fromisoformat(ctx.week_end) + timedelta(days=1), date.today())
        ctx._pools["_look"] = [(start + timedelta(days=i)).isoformat() for i in range(SEASON_LOOKAHEAD_DAYS)]
    return ctx._pools["_look"]


def used_games_per_week(ctx: Context, pids) -> dict:
    """Expected games each player actually gets *started* in a typical upcoming week: walk
    the next 4 weeks of real schedule and fill active slots day by day, best player first.
    Bench players count when the starters are off, which is most nights."""
    dates = lookahead_dates(ctx)
    tds = {p: team_dates(ctx, ctx.team_of(p)) for p in pids}
    avail = {p: model.ros_availability(p, ctx.injuries, ctx.events) for p in pids}
    used = {p: 0.0 for p in pids}
    for d in dates:
        for p, (s, _) in expected_starts(ctx, pids, d, avail).items():
            used[p] += s
    weeks = len(dates) / 7
    return {p: u / weeks for p, u in used.items()}


def team_season_dist(ctx: Context, pids):
    """Typical-week distribution over the next 4 weeks of real schedule, slot cap included."""
    return team_dist(ctx, pids, lookahead_dates(ctx), per_week=True)


def league_average_dist(ctx: Context):
    if "_avg" not in ctx._pools:
        dists = [team_season_dist(ctx, pids) for key, pids in ctx.rosters.items()
                 if key != ctx.my_key and pids]
        if dists:
            ctx._pools["_avg"] = model.avg_opponent_dist(dists)
        else:
            # No other rosters known (manual mode, only your team pasted): a league-average
            # team is approximated by dealing the top N x roster_size players into N teams.
            mine = set(ctx.rosters.get(ctx.my_key, []))
            pool = [p for p in ctx.values["var"].sort_values(ascending=False).index if p not in mine]
            teams = [pool[i::ctx.num_teams - 1] for i in range(ctx.num_teams - 1)]
            teams = [t[:ctx.roster_size] for t in teams]
            ctx._pools["_avg"] = model.avg_opponent_dist([team_season_dist(ctx, t) for t in teams])
    return ctx._pools["_avg"]



def opp_factor(ctx, team, d) -> float:
    """Opponent defense multiplier for `team`'s game on day d (1.0 when off or unknown)."""
    if not ctx.defense or not isinstance(team, str):
        return 1.0
    if "_opp_of" not in ctx._pools:
        with db.cursor() as c:
            rows = c.execute("SELECT game_date, home, away FROM schedule").fetchall()
        m = {}
        for r in rows:
            m[(r["home"], r["game_date"])] = r["away"]
            m[(r["away"], r["game_date"])] = r["home"]
        ctx._pools["_opp_of"] = m
    opp = ctx._pools["_opp_of"].get((team, d))
    return float(ctx.defense.get(opp, 1.0)) if opp else 1.0


def game_factor(ctx, pid, d) -> float:
    """Everything that scales one specific game: opponent defense x teammates' absences."""
    from . import teammates
    return opp_factor(ctx, ctx.team_of(pid), d) * teammates.game_factor(ctx, pid, d)
