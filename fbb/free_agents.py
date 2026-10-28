"""Yahoo's free-agent list, pasted: who is actually available, and who is on waivers.

Without it, "free agent" is inferred from the pasted rosters, which go stale as other managers
move. Paste Yahoo's Players page (Status: All Available Players) and every listed player is
marked confirmed available, with waiver status:

  FA            add him now
  W (Oct 24)    on waivers until Oct 24: a claim, not an instant add

Yahoo shows 25 players a page, so a paste covers the best few dozen available players. A
player the app thinks is free but who isn't on the pasted pages is still treated as free:
Yahoo's ranking differs a lot from the model's (Oct 2026: 25 of the model's top 30 free agents
were beyond Yahoo's first 100, all genuinely free), so only his waiver status is unknown.
"""
import re
from datetime import date, datetime

from . import db, news
from .manual import _PLAYER_LINE, clean_line

FRESH_DAYS = 3
SESSION_MINUTES = 30  # pages pasted within this long of each other build one list
_WAIVER = re.compile(r"^W \(([A-Z][a-z]{2}) (\d{1,2})\)")
_MONTHS = {m: i for i, m in enumerate(["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}


def _waiver_date(mon: str, day: str, today=None) -> str:
    today = today or date.today()
    d = date(today.year, _MONTHS[mon], int(day))
    if (d - today).days < -180:      # e.g. pasted in late December for a January date
        d = date(today.year + 1, d.month, d.day)
    return d.isoformat()


def parse(text: str, today=None) -> tuple[list, list]:
    """-> ([{nba_id, status, until, positions}], [names that looked like players but didn't match])."""
    idx = news.NameIndex()
    found, unmatched, current = {}, [], None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        m = _PLAYER_LINE.match(clean_line(line))
        if m:
            name = m.group(1)
            hits = idx.find_in_text(name)
            pid = hits[0] if hits else idx.resolve(name)
            if pid is None:
                unmatched.append(name)
                current = None
                continue
            current = pid
            found.setdefault(pid, {"nba_id": int(pid), "status": None, "until": None, "positions": m.group(3)})
            continue
        if current is None or found[current]["status"]:
            continue
        if line == "FA":
            found[current]["status"] = "FA"
        else:
            w = _WAIVER.match(line)
            if w:
                found[current]["status"] = "W"
                found[current]["until"] = _waiver_date(w.group(1), w.group(2), today)
    players = [dict(p, status=p["status"] or "FA") for p in found.values()]
    return players, sorted(set(unmatched))


def import_list(text: str, append=None) -> dict:
    """append=None (the default): add to the list if the last page was pasted within
    SESSION_MINUTES (you're pasting page 2, 3, ...), otherwise start a fresh list."""
    players, unmatched = parse(text)
    if not players:
        raise ValueError("No players found. Paste Yahoo's Players page (Status: All Available Players).")
    old = db.kv_get("fa_list") or {}
    if append is None:
        recent = old.get("created") and (datetime.now().astimezone() - datetime.fromisoformat(old["created"])).total_seconds() < SESSION_MINUTES * 60
        append = bool(recent)
    prev = old.get("players", {}) if append else {}
    merged = dict(prev)
    merged.update({str(p["nba_id"]): p for p in players})
    db.kv_set("fa_list", {"created": db.now_iso(), "players": merged})
    cleaned, on_mine = [], []
    with db.cursor() as c:
        for p in players:  # bonus: Yahoo's real position eligibility for these players
            c.execute("UPDATE players SET positions=? WHERE nba_id=?", (p["positions"], p["nba_id"]))
        names = {r["team_key"]: (r["name"], r["is_mine"]) for r in c.execute("SELECT team_key, name, is_mine FROM fantasy_teams")}
        pnames = {r["nba_id"]: r["name"] for r in c.execute("SELECT nba_id, name FROM players")}
        for p in players:
            for r in c.execute("SELECT team_key FROM rosters WHERE nba_id=?", (p["nba_id"],)).fetchall():
                team, mine = names.get(r["team_key"], (r["team_key"], 0))
                if mine:
                    on_mine.append(pnames.get(p["nba_id"]))
                else:
                    # Yahoo's list is newer than that team's paste: they must have dropped him
                    c.execute("DELETE FROM rosters WHERE team_key=? AND nba_id=?", (r["team_key"], p["nba_id"]))
                    cleaned.append(f"{pnames.get(p['nba_id'])} (was on {team})")
    new_ids = {str(p["nba_id"]) for p in players} - set(prev)
    return {"players": len(merged), "added_now": len(players), "new": len(new_ids), "appended": append,
            "on_waivers": sum(1 for p in merged.values() if p["status"] == "W"), "unmatched": unmatched,
            "removed_from_outdated_rosters": cleaned, "listed_but_on_your_roster": on_mine}


def snapshot() -> dict | None:
    """The pasted list if it's fresh enough to trust, else None."""
    fa = db.kv_get("fa_list")
    if not fa:
        return None
    age = (datetime.now().astimezone() - datetime.fromisoformat(fa["created"])).days
    return dict(fa, age_days=age) if age <= FRESH_DAYS else None


def set_yahoo_waivers(players: list):
    """The Yahoo sync's full waiver list. Unlike a paste it covers everyone, so a player not on
    it (and not rostered) is a confirmed free agent."""
    db.kv_set("yahoo_waivers", {"created": db.now_iso(), "players": {str(p["nba_id"]): p for p in players}})


def _yahoo_waivers() -> dict | None:
    if db.kv_get("league_source") != "yahoo":
        return None
    w = db.kv_get("yahoo_waivers")
    if not w or (datetime.now().astimezone() - datetime.fromisoformat(w["created"])).total_seconds() > 86400:
        return None  # sync hasn't fetched it for a day: fall back to a paste, if any
    return w


def availability(pid) -> dict:
    """{'known': bool, 'listed': bool, 'status': 'FA'|'W'|None, 'until': date|None}"""
    yw = _yahoo_waivers()
    if yw is not None:
        p = yw["players"].get(str(int(pid)))
        until = p.get("until") if p else None
        on_waivers = bool(until and until > date.today().isoformat())
        return {"known": True, "listed": True, "status": "W" if on_waivers else "FA",
                "until": until if on_waivers else None}
    fa = snapshot()
    if not fa:
        return {"known": False, "listed": False, "status": None, "until": None}
    p = fa["players"].get(str(int(pid)))
    if not p:
        return {"known": True, "listed": False, "status": None, "until": None}
    until = p.get("until")
    on_waivers = p["status"] == "W" and until and until > date.today().isoformat()
    return {"known": True, "listed": True, "status": "W" if on_waivers else "FA", "until": until if on_waivers else None}


def listed_ids() -> list:
    yw = _yahoo_waivers()
    if yw is not None:
        return [int(k) for k in yw["players"]]
    fa = snapshot()
    return [int(k) for k in fa["players"]] if fa else []


def clear():
    db.kv_set("fa_list", None)
