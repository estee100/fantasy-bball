"""Playoff schedule: who plays the most games in the fantasy playoff weeks.

Over the playoff weeks NBA teams play 9 to 12 games. A player with 12 gets a third more
chances to score than one with 9, which is worth more than most trades between similar
players. The edge is (his games - the league average) x his projected points per game.
The schedule is fixed months ahead, so it can be traded for before the deadline.
"""
from . import db, model, nba, recommend

LIMIT = 15


def playoff_range():
    start_wk, end_wk = db.kv_get("playoff_start_week"), db.kv_get("end_week")
    yw = db.kv_get("game_weeks") or {}
    if not (start_wk and end_wk and str(start_wk) in yw and str(end_wk) in yw):
        return None
    return yw[str(start_wk)][0], yw[str(end_wk)][1]


def team_games(start, end) -> dict:
    return {t: sum(1 for d in ds if start <= d <= end) for t, ds in nba.team_games(start, end).items()}


def scan(ctx) -> dict:
    rng = playoff_range()
    if not rng:
        return {"available": False, "reason": "Needs the playoff weeks from Yahoo (synced with the league settings)."}
    start, end = rng
    games = team_games(start, end)
    avg = sum(games.values()) / max(len(games), 1)
    owner = {p: k for k, ids in ctx.rosters.items() for p in ids}

    def row(p):
        g = games.get(ctx.team_of(p), 0)
        # fantasy pts per game when he plays (ros_value is value over a replacement player);
        # games he's expected to miss come off through ros availability
        v = float(ctx.values["value"].get(p, 0.0)) * model.ros_availability(p, ctx.injuries, ctx.events) / model.HEALTHY_ROS
        k = owner.get(p)
        return {"nba_id": int(p), "name": ctx.name(p), "team": ctx.team_of(p), "games": g,
                "per_game": v, "playoff_pts": v * g, "edge": v * (g - avg),
                "owner": "mine" if k == ctx.my_key else (ctx.teams.get(k, {}).get("name") if k else "Free agent")}

    mine = sorted((row(p) for p in ctx.rosters.get(ctx.my_key, [])), key=lambda r: r["edge"])
    floor = min((r["per_game"] for r in mine), default=0.0)
    others = [row(p) for k, ids in ctx.rosters.items() if k != ctx.my_key for p in ids]
    fa = [row(p) for p in recommend.best_free_agents(ctx)[:60]]
    pool = [r for r in others + fa if r["per_game"] > floor and r["games"] > avg]
    targets = sorted(pool, key=lambda r: -r["playoff_pts"])[:LIMIT]
    teams = sorted(({"team": t, "games": g} for t, g in games.items()), key=lambda r: (-r["games"], r["team"]))
    return {"available": True, "start": start, "end": end, "avg_games": avg,
            "trade_deadline": db.kv_get("trade_end_date"),
            "playoff_teams": db.kv_get("num_playoff_teams"),
            "mine": mine, "targets": targets, "teams": teams}
