"""Demo league: a 10-team, 9-cat league snake-drafted from real projections.

Lets the whole pipeline (sim, recommendations, alerts, dashboard) run before Yahoo is
connected. `python -m fbb sync` against Yahoo replaces all of it.
"""
from . import config, db, model, nba

TEAM_NAMES = ["My Team", "Pick and Roll Call", "Brick City", "Dunk Tank", "Swish Army",
              "Free Throw Freddies", "Glass Cleaners", "Fast Break Bros", "Zone Defense",
              "Bench Mob"]


def build(num_teams=10, roster_size=13):
    logs = nba.load_logs()
    db.kv_set("categories", config.DEFAULT_CATEGORIES)
    db.kv_set("scoring_mode", "categories")
    proj = model.project(logs, db.get_params())
    vals = model.value_players(proj, num_teams, roster_size)["value"]
    with db.cursor() as c:
        active = {r[0] for r in c.execute("SELECT nba_id FROM players WHERE team IS NOT NULL")}
    order = [p for p in vals.sort_values(ascending=False).index if p in active]
    picks = {i: [] for i in range(num_teams)}
    it = iter(order)
    for rnd in range(roster_size):
        seq = range(num_teams) if rnd % 2 == 0 else reversed(range(num_teams))
        for t in seq:
            picks[t].append(next(it))
    with db.cursor() as c:
        c.execute("DELETE FROM fantasy_teams")
        c.execute("DELETE FROM rosters")
        for t in range(num_teams):
            key = f"demo.t.{t + 1}"
            c.execute("INSERT INTO fantasy_teams VALUES (?,?,?,?,0,0,0,?)",
                      (key, TEAM_NAMES[t], "demo", 1 if t == 0 else 0, t + 1))
            # rows: bench the last 3 picks
            for i, pid in enumerate(picks[t]):
                c.execute("INSERT INTO rosters VALUES (?,?,?)", (key, pid, "BN" if i >= 10 else "UTIL"))
    db.kv_set("league_name", "Demo League (connect Yahoo to replace)")
    db.kv_set("league_source", "demo")
    db.kv_set("num_teams", num_teams)
    db.kv_set("roster_size", roster_size)
    db.kv_set("active_slots", config.DEFAULT_ACTIVE_SLOTS)
    db.kv_set("current_week", 1)
    db.kv_set("opponent_key", "demo.t.2")
    db.kv_set("week_to_date", {})
    for k in ("week_range", "weeks_remaining"):
        with db.cursor() as c:
            c.execute("DELETE FROM kv WHERE key=?", (k,))
    return {t: len(p) for t, p in picks.items()}
