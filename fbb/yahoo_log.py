"""Yahoo's league transaction log, pasted: keeps every pasted roster current.

Yahoo's League > Transactions page copies as blocks: the players first, each followed by where
he came from or went, then the team and time on the line after them:

    Jared McCain OKC - PG,SG
    Waiver                        <- added (claimed off waivers; "Free Agent" = added as FA)
    Jalen Suggs ORL - PG,SG
    To Waivers                    <- dropped
    Sheldon's Superb Team Oct 22, 4:04 am logo

A block whose team line was cut off by the copy is credited to the team holding the dropped
player. Each transaction is applied once (remembered by signature), and only if it is newer
than that team's roster paste, so re-pasting the same log is harmless. Your own moves go to
History unless the same players are already recorded there.
"""
import json
import re
from datetime import date, datetime, timedelta

from . import db, news

_PLAYER = re.compile(r"^(.*?)\s+([A-Z]{2,4}) - ([A-Z,]+)(?:\s+(?:O|DTD|GTD|INJ|SUSP|NA|IL\+?))?\s*$")
_TEAM = re.compile(r"^(.*?)\s+([A-Z][a-z]{2}) (\d{1,2}), (\d{1,2}):(\d{2}) (am|pm)(?:\s+logo)?\s*$")
_MONTHS = {m: i for i, m in enumerate(["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}
ADD_FROM = {"free agent", "waiver", "waivers", "fa"}
WAIVER_DAYS = 2
# Yahoo shows log times in US Eastern, not your local zone: verified Oct 2026 (4:04 am moves were
# already in rosters pasted at 2:26 am Mountain)
YAHOO_TZ = "America/New_York"


def _when(mon, day, hh, mm, ampm, today=None) -> datetime:
    today = today or date.today()
    h = int(hh) % 12 + (12 if ampm == "pm" else 0)
    year = today.year if _MONTHS[mon] <= today.month + 1 else today.year - 1  # Jan paste of a Dec move
    from zoneinfo import ZoneInfo
    return datetime(year, _MONTHS[mon], int(day), h, int(mm), tzinfo=ZoneInfo(YAHOO_TZ))


def parse(text: str, today=None) -> tuple[list, list]:
    """-> ([{team, when, adds: [{pid, how}], drops: [{pid, how}]}] oldest first, [unrecognized lines])."""
    idx = news.NameIndex()
    txns, cur, pending, odd = [], {"adds": [], "drops": []}, None, []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        t = _TEAM.match(line)
        if t and not _PLAYER.match(line):
            if cur["adds"] or cur["drops"]:
                txns.append(dict(cur, team=t.group(1).strip(), when=_when(*t.groups()[1:], today=today)))
            cur, pending = {"adds": [], "drops": []}, None
            continue
        p = _PLAYER.match(line)
        if p:
            hits = idx.find_in_text(p.group(1))
            pending = hits[0] if hits else idx.resolve(p.group(1))
            if pending is None:
                odd.append(line)
            continue
        if pending is not None:
            low = line.lower()
            if low in ADD_FROM:
                cur["adds"].append({"pid": int(pending), "how": "waiver" if low.startswith("waiver") else "fa"})
            elif low.startswith("to "):
                cur["drops"].append({"pid": int(pending), "how": "waivers" if "waiver" in low else "fa"})
            else:
                odd.append(line)  # e.g. a trade line: not handled yet
            pending = None
    if cur["adds"] or cur["drops"]:
        txns.append(dict(cur, team=None, when=None))  # team line cut off by the copy
    txns.reverse()  # Yahoo lists newest first
    return txns, odd


def _sig(t) -> str:
    return json.dumps([t["team_key"], t["when"].isoformat() if t["when"] else None,
                       sorted(a["pid"] for a in t["adds"]), sorted(d["pid"] for d in t["drops"])])


def import_log(text: str, today=None) -> dict:
    from . import history, pipeline
    txns, odd = parse(text, today)
    if not txns:
        raise ValueError("No transactions found. Paste Yahoo's League > Transactions page.")
    with db.cursor() as c:
        teams = {r["name"].strip().lower(): r["team_key"] for r in c.execute("SELECT name, team_key FROM fantasy_teams")}
        mine = c.execute("SELECT team_key FROM fantasy_teams WHERE is_mine=1").fetchone()
        mine = mine["team_key"] if mine else None
        names = {r["nba_id"]: r["name"] for r in c.execute("SELECT nba_id, name FROM players")}
        holder = {r["nba_id"]: r["team_key"] for r in c.execute("SELECT team_key, nba_id FROM rosters")}
    pasted = db.kv_get("manual_updated", {})
    seen = set(db.kv_get("yahoo_txn_seen") or [])
    out = {"applied": [], "already_reflected": [], "already_seen": 0, "your_moves_logged": [],
           "skipped": [], "unrecognized": odd}
    mine_moves = []
    for t in txns:
        key = teams.get((t["team"] or "").strip().lower())
        if key is None and t["team"] is None:
            key = next((holder.get(d["pid"]) for d in t["drops"] if holder.get(d["pid"])), None)
            if key is None:  # drop already applied: credit whoever now holds the added player
                key = next((holder.get(a["pid"]) for a in t["adds"] if holder.get(a["pid"])), None)
        desc = ", ".join([f"+{names.get(a['pid'])}" for a in t["adds"]] + [f"-{names.get(d['pid'])}" for d in t["drops"]])
        if key is None:
            out["skipped"].append(f"{desc}: team unknown ({t['team'] or 'cut off'})")
            continue
        t["team_key"] = key
        sig = _sig(t)
        if sig in seen:
            out["already_seen"] += 1
            continue
        seen.add(sig)
        label = f"{t['team'] or key}: {desc}"
        if key == mine:
            mine_moves.append(t)
        # roster change, only if newer than that team's paste (otherwise the paste already shows it)
        newer = t["when"] is None or not pasted.get(key) or t["when"] > datetime.fromisoformat(pasted[key])
        if newer and key != mine:
            with db.cursor() as c:
                for d in t["drops"]:
                    c.execute("DELETE FROM rosters WHERE team_key=? AND nba_id=?", (key, d["pid"]))
                for a in t["adds"]:
                    c.execute("DELETE FROM rosters WHERE nba_id=? AND team_key<>?", (a["pid"], key))
                    c.execute("INSERT OR IGNORE INTO rosters(team_key, nba_id, slot) VALUES (?,?,?)", (key, a["pid"], "BN"))
            out["applied"].append(label)
        elif key != mine:
            out["already_reflected"].append(label)
        _update_fa_list(t)
    db.kv_set("yahoo_txn_seen", sorted(seen))

    # your moves -> History, unless those players are already recorded there
    if mine_moves:
        ctx = pipeline.context(reload=True)
        history._ensure()
        with db.cursor() as c:
            recent = c.execute("SELECT players_in, players_out FROM transactions WHERE deleted=0 AND effective >= ?",
                               ((date.today() - timedelta(days=14)).isoformat(),)).fetchall()
        got_in = {p for r in recent for p in json.loads(r["players_in"])}
        got_out = {p for r in recent for p in json.loads(r["players_out"])}
        roster = set(ctx.rosters.get(mine, []))
        for t in mine_moves:
            ins, outs = [a["pid"] for a in t["adds"]], [d["pid"] for d in t["drops"]]
            desc = ", ".join([f"+{names.get(p)}" for p in ins] + [f"-{names.get(p)}" for p in outs])
            if set(ins) <= got_in and set(outs) <= got_out:
                out["already_reflected"].append(f"you: {desc} (already in History)")
                continue
            kind = "add/drop" if ins and outs else "add" if ins else "drop"
            eff = (t["when"] or datetime.now().astimezone()).date().isoformat()
            # apply only what your roster doesn't show yet
            apply = (not set(ins) <= roster) or bool(set(outs) & roster)
            history.record(ctx, kind, ins, outs, effective=eff, source="yahoo-log", apply=apply,
                           note="from the pasted Yahoo transaction log")
            out["your_moves_logged"].append(desc)
    return out


def _update_fa_list(t):
    """Keep the pasted free-agent list in step: added players are gone, dropped ones on waivers."""
    fa = db.kv_get("fa_list")
    if not fa or t["when"] is None:
        return
    P = fa["players"]
    for a in t["adds"]:
        P.pop(str(a["pid"]), None)
    for d in t["drops"]:
        if d["how"] == "waivers":
            until = (t["when"] + timedelta(days=WAIVER_DAYS)).date().isoformat()
            P[str(d["pid"])] = dict(P.get(str(d["pid"]), {}), nba_id=d["pid"], status="W", until=until,
                                    positions=P.get(str(d["pid"]), {}).get("positions"))
    db.kv_set("fa_list", fa)
