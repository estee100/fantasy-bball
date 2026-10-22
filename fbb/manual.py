"""Manual league mode: rosters pasted from Yahoo's web pages, while API access is pending.

Paste a whole team page (Ctrl+A, Ctrl+C on the team's roster page) and the parser keeps
what matters: the slot code on its own line (PG, SG, Util, BN, IL...) followed by a line
containing a known player's name. Everything else on the page is ignored.
"""
import re

from . import db, learning, news

SLOTS = {"PG", "SG", "G", "SF", "PF", "F", "C", "UTIL", "BN", "IL", "IL+", "NA"}

POINTS_PRESET = {"pts": 1.0, "reb": 1.2, "ast": 1.5, "stl": 3.0, "blk": 3.0, "tov": -1.0}


def team_key(name: str) -> str:
    return "manual." + re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


# Yahoo glues badges straight onto names: "Kevin DurantNew Player Note HOU - SG,SF",
# "Coby WhiteOPlayer Note CHA - PG,SG" (O = out, also DTD/GTD/INJ/SUSP).
_BADGES = re.compile(r"(?:O|DTD|GTD|INJ|SUSP|NA|IL|IL\+)?(?:New )?Player Note")
_PLAYER_LINE = re.compile(r"^(.*?)\s+([A-Z]{2,4}) - ([A-Z,]+)\s*$")


def clean_line(line: str) -> str:
    return _BADGES.sub(" ", line).strip()


def parse_roster(text: str) -> tuple[list, list]:
    """-> ([(nba_id, slot)], [lines that looked like a player but didn't match])."""
    idx = news.NameIndex()
    found, seen, slot = [], set(), None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.upper() in SLOTS:
            slot = "Util" if line.upper() == "UTIL" else line.upper()
            continue
        m = _PLAYER_LINE.match(clean_line(line))
        candidate = m.group(1) if m else line
        pids = idx.find_in_text(candidate) or ([idx.resolve(candidate)] if m and idx.resolve(candidate) else [])
        for pid in pids[:1]:
            if pid not in seen:
                seen.add(pid)
                found.append((pid, slot or "?"))
                slot = None
    return found, []


def unmatched_candidates(text: str, matched_ids) -> list:
    """Player lines ('Name TEAM - POS') whose name we couldn't resolve to an NBA player."""
    idx = news.NameIndex()
    out = []
    for line in text.splitlines():
        m = _PLAYER_LINE.match(clean_line(line.strip()))
        if m and not idx.find_in_text(m.group(1)) and not idx.resolve(m.group(1)):
            out.append(m.group(1))
    return sorted(set(out))


def configure(num_teams: int = 10, active_slots: int = 10, roster_size: int = 13,
              point_weights: dict | None = None, categories: list | None = None):
    if db.kv_get("league_source") in (None, "demo"):
        # wipe the demo league so its rosters, suggestions and forecasts don't leak in
        with db.cursor() as c:
            c.execute("DELETE FROM fantasy_teams")
            c.execute("DELETE FROM rosters")
            c.execute("DELETE FROM moves")
            c.execute("DELETE FROM matchup_predictions")
        for k in ("calibration", "my_roster_last", "opponent_key", "week_to_date", "matchup",
                  "recommendations", "league_table"):
            db.kv_set(k, None)
    db.kv_set("league_source", "manual")
    from . import config
    db.kv_set("league_name", f"Yahoo league {config.YAHOO_LEAGUE_ID or ''} (pasted rosters)".replace("  ", " "))
    db.kv_set("num_teams", num_teams)
    db.kv_set("active_slots", active_slots)
    db.kv_set("roster_size", roster_size)
    if point_weights:
        db.kv_set("scoring_mode", "points")
        db.kv_set("point_weights", point_weights)
        # role monitor watches the stats that score
        db.kv_set("categories", [k for k in ("pts", "reb", "ast", "stl", "blk", "tov") if k in point_weights])
    elif categories:
        db.kv_set("scoring_mode", "categories")
        db.kv_set("categories", categories)


def set_team(name: str, text: str, mine: bool = False, ctx=None) -> dict:
    players, _ = parse_roster(text)
    if not players:
        raise ValueError("No known players found in that paste")
    key = team_key(name)
    with db.cursor() as c:
        if mine:
            c.execute("UPDATE fantasy_teams SET is_mine=0")
        exists = c.execute("SELECT 1 FROM fantasy_teams WHERE team_key=?", (key,)).fetchone()
        if exists:
            c.execute("UPDATE fantasy_teams SET name=?, is_mine=? WHERE team_key=?", (name, 1 if mine else 0, key))
        else:
            c.execute("INSERT INTO fantasy_teams VALUES (?,?,?,?,0,0,0,0)", (key, name, "manual", 1 if mine else 0))
        c.execute("DELETE FROM rosters WHERE team_key=?", (key,))
        c.executemany("INSERT OR REPLACE INTO rosters VALUES (?,?,?)", [(key, pid, slot) for pid, slot in players])
    updated = db.kv_get("manual_updated", {})
    updated[key] = db.now_iso()
    db.kv_set("manual_updated", updated)
    if mine:
        before = db.kv_get("my_roster_last")
        now = [pid for pid, _ in players]
        if before is not None and set(before) != set(now) and ctx is not None:
            learning.record_roster_change(ctx, before, now)
        db.kv_set("my_roster_last", now)
    return {"team_key": key, "players": len(players),
            "unmatched": unmatched_candidates(text, [p for p, _ in players])}


def remove_team(key: str):
    with db.cursor() as c:
        c.execute("DELETE FROM rosters WHERE team_key=?", (key,))
        c.execute("DELETE FROM fantasy_teams WHERE team_key=?", (key,))
    if db.kv_get("opponent_key") == key:
        db.kv_set("opponent_key", None)


def set_opponent(key: str | None):
    db.kv_set("opponent_key", key)


def summary() -> dict:
    with db.cursor() as c:
        teams = [dict(r) for r in c.execute(
            "SELECT t.team_key, t.name, t.is_mine, count(r.nba_id) AS players FROM fantasy_teams t "
            "LEFT JOIN rosters r USING(team_key) GROUP BY t.team_key ORDER BY t.is_mine DESC, t.name")]
    upd = db.kv_get("manual_updated", {})
    for t in teams:
        t["updated"] = upd.get(t["team_key"])
    return {"source": db.kv_get("league_source"), "teams": teams, "opponent_key": db.kv_get("opponent_key"),
            "num_teams": db.kv_get("num_teams", 10), "active_slots": db.kv_get("active_slots", 10),
            "roster_size": db.kv_get("roster_size", 13), "scoring_mode": db.kv_get("scoring_mode", "categories"),
            "point_weights": db.kv_get("point_weights", {})}
