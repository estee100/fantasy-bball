"""Phone alerts via ntfy (https://ntfy.sh): free, no account, iOS and Android apps.

You subscribe to a long random channel name in the ntfy app; the app pushes short text alerts
to it. Only alert text is sent (player names and advice), never Yahoo data or your history.

Alert types (each can be switched off):
  lineup   today's lineup changes, late morning; again if the plan changes before tip-off
  moves    recommendations labelled "Do today"
  news     Claude-extracted news on your players (injury, role, minutes)
  injury   injury status changes on your players

Each alert is sent once (deduplicated), and nothing goes out during quiet hours (it waits).
Set FBB_NO_ALERTS=1 to disable sending entirely (tests and throwaway servers do).
"""
import hashlib
import json
import logging
import os
import secrets
import unicodedata
from datetime import date, datetime, timedelta

import requests

from . import db

log = logging.getLogger(__name__)
SERVER = "https://ntfy.sh"
LEAD_MINUTES = 90  # lineup alert this long before your first tip-off of the day
DEFAULTS = {"enabled": False, "topic": None, "types": {"lineup": True, "moves": True, "news": True, "injury": True},
            "lineup_hour": 11, "last_tip_hour": 19, "quiet_start": 23, "quiet_end": 8, "min_lineup_gain": 3.0}


def settings() -> dict:
    s = dict(DEFAULTS)
    s.update(db.kv_get("alerts_cfg") or {})
    s["types"] = dict(DEFAULTS["types"], **(s.get("types") or {}))
    return s


def save(**changes) -> dict:
    s = settings()
    s.update({k: v for k, v in changes.items() if v is not None})
    if s.get("enabled") and not s.get("topic"):
        s["topic"] = new_topic()
    db.kv_set("alerts_cfg", s)
    return s


def new_topic() -> str:
    # long and random: the channel name is the only thing keeping alerts private on ntfy.sh
    return "fbb-" + secrets.token_hex(12)


def _ascii(s: str) -> str:
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()


def send(title: str, message: str, tags="basketball", priority=3, topic=None) -> bool:
    if os.getenv("FBB_NO_ALERTS"):
        return False
    topic = topic or settings().get("topic")
    if not topic:
        return False
    try:
        r = requests.post(f"{SERVER}/{topic}", data=message.encode("utf-8"), timeout=15,
                          headers={"Title": _ascii(title), "Tags": tags, "Priority": str(priority)})
        r.raise_for_status()
        return True
    except requests.RequestException as e:
        log.warning("alert not sent: %s", e)
        return False


def _sent() -> dict:
    return db.kv_get("alerts_sent") or {}


def _mark(key: str):
    s = _sent()
    s[key] = db.now_iso()
    if len(s) > 2000:  # keep the dedupe log small
        s = dict(sorted(s.items(), key=lambda kv: kv[1])[-1500:])
    db.kv_set("alerts_sent", s)


def _once(key: str, title: str, message: str, **kw) -> bool:
    if key in _sent():   # per league: the same lineup or news alert can be due in each
        return False
    if len(db.league_ids()) > 1:
        title = f"[{db.league_name(db.current_league())}] {title}"
    if send(title, message, **kw):
        _mark(key)
        return True
    return False


def quiet(s, now) -> bool:
    h = now.hour
    return h >= s["quiet_start"] or h < s["quiet_end"]


def run(ctx, now=None) -> dict:
    """Called after every refresh. Returns what was sent."""
    s = settings()
    if not s["enabled"] or not s.get("topic") or os.getenv("FBB_NO_ALERTS"):
        return {"sent": 0, "reason": "disabled"}
    now = now or datetime.now()
    if quiet(s, now):
        return {"sent": 0, "reason": "quiet hours"}
    sent = []
    mine = set(ctx.rosters.get(ctx.my_key, []))
    T = s["types"]

    if T["lineup"]:
        from . import lineup
        today = date.today().isoformat()
        plan = lineup.day_plan(ctx, ctx.my_key, today)
        if plan.get("first_tip_utc"):
            first = datetime.fromisoformat(plan["first_tip_utc"]).astimezone().replace(tzinfo=None)
            window_open = now >= max(first - timedelta(minutes=LEAD_MINUTES), now.replace(hour=9, minute=0))
        else:
            window_open = s["lineup_hour"] <= now.hour < s["last_tip_hour"]
        if window_open and plan["gain"] >= s["min_lineup_gain"] and (plan["start"] or plan["sit"]):
            sig = hashlib.sha1(json.dumps([sorted(plan["start"]), sorted(plan["sit"])]).encode()).hexdigest()[:10]
            msg = []
            if plan["start"]:
                msg.append("Start: " + ", ".join(plan["start"]))
            if plan["sit"]:
                msg.append("Bench: " + ", ".join(plan["sit"]))
            msg.append(f"+{plan['gain']:.0f} expected pts. First tip-off {plan.get('first_tip') or 'tonight'}: set it in Yahoo before then.")
            if _once(f"lineup:{today}:{sig}", "Lineup changes today", "\n".join(msg), tags="clipboard", priority=4):
                sent.append("lineup")

    if T["moves"]:
        recs = (db.kv_get("recommendations") or {}).get("moves", [])
        for m in recs:
            if m.get("urgency") == "Do today":
                key = f"move:{m['add']}:{m['drop']}:{date.today().isoformat()}"
                msg = f"Add {m['add_name']}, drop {m['drop_name']}: {m['score']:+.2f} expected wins. {m.get('urgency_reason', '')}"
                if _once(key, "Move to make today", msg, tags="rotating_light", priority=4):
                    sent.append("move")

    if T["news"] and mine:
        with db.cursor() as c:
            rows = c.execute("SELECT id, nba_id, event_type, direction, games_out, confidence, takeaway FROM news_events "
                             "WHERE date >= ? ORDER BY id", ((date.today()).replace(day=1).isoformat(),)).fetchall()
        for r in rows:
            if r["nba_id"] in mine and (r["confidence"] or 0) >= 0.6 and (r["direction"] or r["games_out"]):
                if _once(f"news:{r['id']}", f"{ctx.name(r['nba_id'])}: {r['event_type'].replace('_', ' ')}",
                         r["takeaway"] or "", tags="newspaper"):
                    sent.append("news")

    if T["injury"] and mine:
        prev = db.kv_get("alerts_injury_snapshot") or {}
        cur = {str(p): (ctx.injuries.get(p) or {}).get("status") or "Active" for p in mine}
        for p, st in cur.items():
            if p in prev and prev[p] != st:
                comment = (ctx.injuries.get(int(p)) or {}).get("comment") or ""
                if _once(f"injury:{p}:{st}:{date.today().isoformat()}", f"{ctx.name(int(p))}: {prev[p]} -> {st}",
                         comment[:300], tags="ambulance" if st != "Active" else "white_check_mark", priority=4):
                    sent.append("injury")
        db.kv_set("alerts_injury_snapshot", cur)
    return {"sent": len(sent), "types": sent}
