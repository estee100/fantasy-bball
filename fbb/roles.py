"""Role monitor: is a player's best skill still something his team needs him for?

For each player: find his top fantasy skills from his baseline (games before the recent
window), then test whether the *opportunity rate* behind each skill dropped in recent games,
per 36 minutes so it's separated from playing time:

    skill_not_needed  minutes roughly stable, but the skill rate fell hard
                      (e.g. a PG still playing 32 mpg whose assists/36 fell 35% after the team
                      added another ball-handler: the team uses him differently)
    minutes_cut       playing time itself fell
    role_expansion    skill rate or minutes jumped (mostly interesting for free agents)

Statistical test: Welch z on per-game per-36 rates, recent vs baseline, plus a minimum
relative change so tiny-but-"significant" shifts don't fire. Then attach context that might
explain it: team change, newly-arrived teammates with the same skill, recent news.
"""
import json
import math

import numpy as np
import pandas as pd

from . import db

# fantasy category -> (label, per-36 opportunity metric built from box-score columns)
SKILLS = {
    "pts": ("scoring", lambda g: g["fga"] + 0.44 * g["fta"] + g["tov"]),   # usage proxy
    "ast": ("playmaking", lambda g: g["ast"]),
    "reb": ("rebounding", lambda g: g["reb"]),
    "stl": ("steals", lambda g: g["stl"]),
    "blk": ("rim protection", lambda g: g["blk"]),
    "fg3m": ("three-point volume", lambda g: g["fg3a"]),
    "ft_pct": ("getting to the line", lambda g: g["fta"]),
    "fg_pct": ("shot volume", lambda g: g["fga"]),
}
RECENT_N = 8
MIN_RECENT = 3
BASELINE_N = 40
Z_FIRE = 2.0
REL_FIRE = 0.25
MIN_REL_MINUTES = 0.15
MIN_BASE_MPG = 15        # per-36 rates of deep-bench players are noise
MIN_GAME_MINUTES = 10    # garbage-time cameos distort per-36 rates


def _per36(g: pd.DataFrame, metric) -> pd.Series:
    return metric(g) / g["min"].clip(lower=6) * 36


def _welch(recent: pd.Series, base: pd.Series):
    mr, mb = recent.mean(), base.mean()
    se = math.sqrt(recent.var(ddof=1) / len(recent) + base.var(ddof=1) / len(base)) if len(recent) > 1 else 0
    z = (mr - mb) / se if se > 0 else 0.0
    rel = (mr - mb) / mb if mb > 0 else 0.0
    return float(mr), float(mb), float(z), float(rel)


def split_windows(logs: pd.DataFrame, pid, as_of=None):
    if as_of is not None:
        logs = logs[logs["game_date"] < pd.Timestamp(as_of)]
    # "recent" always means the league's current season: a player who hasn't played yet
    # must not have the end of last season presented as his current role
    season = logs["season"].max()
    g = logs[logs["nba_id"] == pid].sort_values("game_date")
    if g.empty:
        return None, None
    recent = g[g["season"] == season].tail(RECENT_N)
    base = g.drop(recent.index).tail(BASELINE_N)
    if len(recent) < MIN_RECENT or len(base) < 10:
        return None, None
    return recent, base


def baseline_skill_ranks(logs: pd.DataFrame, cats, as_of=None) -> pd.DataFrame:
    """z-score of each player's baseline per-36 skill rate vs rotation players."""
    df = logs if as_of is None else logs[logs["game_date"] < pd.Timestamp(as_of)]
    df = df.sort_values("game_date").groupby("nba_id").tail(BASELINE_N + RECENT_N)
    sums = df.groupby("nba_id")[["min", "fga", "fta", "tov", "ast", "reb", "stl", "blk", "fg3a"]].sum()
    n = df.groupby("nba_id").size()
    rot = sums[(n >= 10) & (sums["min"] / n >= 15)]
    out = pd.DataFrame(index=sums.index)
    for k in cats:
        if k not in SKILLS:
            continue
        rate = SKILLS[k][1](sums) / sums["min"].clip(lower=1) * 36
        rr = rate.loc[rot.index]
        out[k] = (rate - rr.mean()) / (rr.std() or 1)
    return out


def scan(ctx, pids=None, as_of=None) -> list:
    logs = ctx.logs
    cats = [k for k in ctx.cats if k in SKILLS] or list(SKILLS)
    ranks = baseline_skill_ranks(logs, cats, as_of)
    if pids is None:
        fa = [p for p in ctx.values.sort_values("var", ascending=False).index if p not in ctx.rostered()][:60]
        pids = list(ctx.rostered()) + fa
    owner = {p: k for k, ids in ctx.rosters.items() for p in ids}
    found = []
    for pid in pids:
        recent, base = split_windows(logs, pid, as_of)
        if recent is None or pid not in ranks.index:
            continue
        m_r, m_b, m_z, m_rel = _welch(recent["min"], base["min"])
        top = [k for k in ranks.loc[pid].dropna().sort_values(ascending=False).index
               if ranks.loc[pid, k] > 0.5][:2]
        base_team = base["team"].mode().iloc[0]
        cur_team = recent["team"].iloc[-1]
        context = {"team_changed": base_team != cur_team, "from_team": base_team, "team": cur_team,
                   "minutes": [m_b, m_r]}
        mine = owner.get(pid) == ctx.my_key
        rostered = pid in owner
        relevance = max(float(ctx.values["var"].get(pid, 0)), 0.5)

        if m_z <= -Z_FIRE and m_rel <= -MIN_REL_MINUTES - 0.05:
            found.append(_alert(ctx, pid, "minutes_cut", relevance * abs(m_rel),
                                f"Minutes cut: {m_b:.1f} → {m_r:.1f} mpg", dict(context, z=m_z)))
        rr, bb = recent[recent["min"] >= MIN_GAME_MINUTES], base[base["min"] >= MIN_GAME_MINUTES]
        if m_b < MIN_BASE_MPG or len(rr) < MIN_RECENT or len(bb) < 10:
            top = []
        for k in top:
            label, metric = SKILLS[k]
            r_r, r_b, z, rel = _welch(_per36(rr, metric), _per36(bb, metric))
            info = dict(context, skill=k, skill_label=label, rate=[r_b, r_r], z=z, rel=rel,
                        skill_rank_z=float(ranks.loc[pid, k]))
            if z <= -Z_FIRE and rel <= -REL_FIRE and m_rel > -MIN_REL_MINUTES:
                info["competition"] = arrivals_with_skill(logs, ranks, cur_team, k, pid, as_of)
                why = []
                if context["team_changed"]:
                    why.append(f"new team ({base_team} → {cur_team})")
                if info["competition"]:
                    why.append("arrivals with the same skill: " + ", ".join(
                        ctx.name(c) for c in info["competition"][:2]))
                title = (f"Best skill not needed? {label.capitalize()} rate down {abs(rel):.0%} "
                         f"with minutes steady ({m_b:.0f} → {m_r:.0f})")
                info["explanation"] = why
                found.append(_alert(ctx, pid, "skill_not_needed", relevance * abs(rel) * 1.5, title, info))
            elif (z >= Z_FIRE and rel >= REL_FIRE) and not mine:
                found.append(_alert(ctx, pid, "role_expansion", relevance * rel * (1.3 if not rostered else 0.7),
                                    f"Role growing: {label} rate up {rel:.0%}", info))
        if m_z >= Z_FIRE and m_rel >= REL_FIRE and not rostered:
            found.append(_alert(ctx, pid, "role_expansion", relevance * m_rel * 1.3,
                                f"Minutes jump: {m_b:.1f} → {m_r:.1f} mpg (free agent)", dict(context, z=m_z)))
    return found


def arrivals_with_skill(logs, ranks, team, skill, pid, as_of=None):
    """Teammates now on `team` who weren't in its recent baseline and are good at `skill`."""
    df = logs if as_of is None else logs[logs["game_date"] < pd.Timestamp(as_of)]
    recent_dates = df[(df["nba_id"] == pid)].sort_values("game_date").tail(RECENT_N)["game_date"]
    if recent_dates.empty:
        return []
    start = recent_dates.min()
    now_on = set(df[(df["team"] == team) & (df["game_date"] >= start)]["nba_id"])
    before = set(df[(df["team"] == team) & (df["game_date"] < start)].sort_values("game_date")
                 .tail(15 * 12)["nba_id"])
    new = [p for p in now_on - before - {pid} if p in ranks.index and ranks.loc[p].get(skill, 0) > 0.75]
    return sorted(new, key=lambda p: -ranks.loc[p, skill])


def _alert(ctx, pid, kind, severity, title, detail):
    return {"nba_id": int(pid), "name": ctx.name(pid), "kind": kind, "severity": float(severity),
            "title": title, "detail": detail}


def store(alerts: list):
    with db.cursor() as c:
        c.execute("UPDATE alerts SET active=0")
        for a in alerts:
            c.execute("""INSERT INTO alerts(created, nba_id, kind, severity, title, detail, active)
                         VALUES (?,?,?,?,?,?,1) ON CONFLICT(nba_id, kind) DO UPDATE SET
                         severity=excluded.severity, title=excluded.title, detail=excluded.detail, active=1""",
                      (db.now_iso(), a["nba_id"], a["kind"], a["severity"], a["title"],
                       json.dumps(a["detail"], default=float)))


def series(ctx, pid, skill=None, n=30) -> dict:
    """Per-game per-36 rate for charts, with the baseline mean/band."""
    g = ctx.logs[ctx.logs["nba_id"] == pid].sort_values("game_date").tail(n + BASELINE_N)
    if g.empty:
        return {}
    recent, base = split_windows(ctx.logs, pid)
    skill = skill or "pts"
    metric = SKILLS.get(skill, SKILLS["pts"])[1]
    rate = _per36(g, metric)
    tail = g.tail(n)
    out = {"dates": tail["game_date"].dt.strftime("%Y-%m-%d").tolist(),
           "rate": rate.loc[tail.index].round(2).tolist(), "minutes": tail["min"].round(1).tolist(),
           "team": tail["team"].tolist(), "skill": skill, "skill_label": SKILLS.get(skill, SKILLS["pts"])[0]}
    if base is not None:
        b = _per36(base, metric)
        out["baseline"] = {"mean": float(b.mean()), "sd": float(b.std())}
        out["recent_start"] = recent["game_date"].min().strftime("%Y-%m-%d")
    return out
