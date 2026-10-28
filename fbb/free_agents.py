"""Who's on waivers, from the Yahoo sync's waiver list.

A player on the list is a waiver claim until his clear date. Anyone else not on a roster is a
free agent: the synced rosters already say who's rostered, so the list settles everyone.
"""
from datetime import date, datetime

from . import db


def set_yahoo_waivers(players: list):
    """The Yahoo sync's full waiver list: [{nba_id, status: "W", until, positions}]."""
    db.kv_set("yahoo_waivers", {"created": db.now_iso(), "players": {str(p["nba_id"]): p for p in players}})


def _yahoo_waivers() -> dict | None:
    if db.kv_get("league_source") != "yahoo":
        return None
    w = db.kv_get("yahoo_waivers")
    if not w or (datetime.now().astimezone() - datetime.fromisoformat(w["created"])).total_seconds() > 86400:
        return None  # the sync hasn't fetched it for a day: waiver status unknown
    return w


def availability(pid) -> dict:
    """{'known': bool, 'status': 'FA'|'W'|None, 'until': date|None}"""
    yw = _yahoo_waivers()
    if yw is None:
        return {"known": False, "status": None, "until": None}
    p = yw["players"].get(str(int(pid)))
    until = p.get("until") if p else None
    on_waivers = bool(until and until > date.today().isoformat())
    return {"known": True, "status": "W" if on_waivers else "FA", "until": until if on_waivers else None}


def listed_ids() -> list:
    """Players on waivers: always worth screening, since a claim may be the only way to get them."""
    yw = _yahoo_waivers()
    return [int(k) for k in yw["players"]] if yw else []
