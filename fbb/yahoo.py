"""Yahoo Fantasy sync (read-only): league settings, every team's roster, matchups, week-to-date stats.

Moves are recommended, never executed: you make them in the Yahoo app, and the next sync
detects the roster change and logs it to the learning ledger.
"""
import logging
from datetime import date, datetime, timezone

from . import config, db, learning
from .nba import norm_name

log = logging.getLogger(__name__)

# Yahoo editorial team abbreviations that differ from NBA tricodes
TEAM_FIX = {"GS": "GSW", "NO": "NOP", "NY": "NYK", "SA": "SAS", "PHO": "PHX", "UTAH": "UTA",
            "WSH": "WAS", "BKN": "BKN", "BRK": "BKN", "CHO": "CHA"}
INACTIVE_SLOTS = {"BN", "IL", "IL+", "NA"}


def query():
    import yahoo_oauth.oauth
    from yfpy.query import YahooFantasySportsQuery
    # yfpy doesn't expose the redirect URI and yahoo_oauth defaults to "oob", which Yahoo's
    # current app form won't accept. Yahoo rejects logins whose redirect URI doesn't match
    # the registered one exactly, so point the library at the configured value.
    yahoo_oauth.oauth.CALLBACK_URI = config.YAHOO_REDIRECT_URI
    if not config.yahoo_configured():
        raise RuntimeError("Set YAHOO_CONSUMER_KEY, YAHOO_CONSUMER_SECRET and YAHOO_LEAGUE_ID in .env")
    return YahooFantasySportsQuery(
        config.YAHOO_LEAGUE_ID, game_code="nba",
        yahoo_consumer_key=config.YAHOO_KEY, yahoo_consumer_secret=config.YAHOO_SECRET,
        env_file_location=config.ROOT, save_token_data_to_env_file=True,
    )


def _unwrap(x, key=None):
    """yfpy sometimes hands back {"stat": Stat} / {"team": Team} wrappers instead of objects."""
    if isinstance(x, dict) and len(x) == 1:
        return next(iter(x.values()))
    return x


def _get(obj, *path, default=None):
    for p in path:
        obj = _unwrap(obj)
        if obj is None:
            return default
        obj = obj.get(p) if isinstance(obj, dict) else getattr(obj, p, None)
    return default if obj is None else obj


def _s(x):
    return x.decode() if isinstance(x, bytes) else (str(x) if x is not None else "")


def sync_settings(q) -> dict:
    meta = q.get_league_metadata()
    settings = q.get_league_settings()
    scoring_type = _s(_get(meta, "scoring_type")).lower()   # head | headpoint | point | roto
    cats, points = [], {}
    for st in _get(settings, "stat_categories", "stats", default=[]) or []:
        st = _unwrap(st)
        if int(_get(st, "is_only_display_stat", default=0) or 0):
            continue
        key = config.YAHOO_STAT_IDS.get(int(_get(st, "stat_id", default=-1)))
        if key in config.CATEGORY_DEFS:
            cats.append(key)
        elif key is None:
            log.warning("unmapped Yahoo stat category %s", _s(_get(st, "display_name")))
    for st in _get(settings, "stat_modifiers", "stats", default=[]) or []:
        st = _unwrap(st)
        key = config.YAHOO_STAT_IDS.get(int(_get(st, "stat_id", default=-1)))
        if key:
            points[key] = float(_get(st, "value", default=0))
    active = roster = il = 0
    for rp in _get(settings, "roster_positions", default=[]) or []:
        rp = _unwrap(rp)
        pos, n = _s(_get(rp, "position")), int(_get(rp, "count", default=0) or 0)
        if pos not in ("IL", "IL+", "NA"):
            roster += n
        if pos in ("IL", "IL+"):
            il += n
        if pos not in INACTIVE_SLOTS:
            active += n
    mode = "points" if "point" in scoring_type else "categories"
    league_key = _s(q.get_league_key())
    game_id = int(league_key.split(".")[0])
    week = int(_get(meta, "current_week", default=1))
    end_week = int(_get(meta, "end_week", default=week))
    weeks = {int(_get(w, "week")): (_s(_get(w, "start")), _s(_get(w, "end")))
             for w in (q.get_game_weeks_by_game_id(game_id) or [])}
    db.kv_set("league_name", _s(_get(meta, "name")))
    db.kv_set("league_source", "yahoo")
    db.kv_set("scoring_type", scoring_type)
    db.kv_set("scoring_mode", mode)
    if cats:
        db.kv_set("categories", cats)
    db.kv_set("point_weights", points)
    db.kv_set("num_teams", int(_get(meta, "num_teams", default=10)))
    db.kv_set("active_slots", active or config.DEFAULT_ACTIVE_SLOTS)
    db.kv_set("roster_size", roster or 13)
    db.kv_set("il_slots", il or None)
    db.kv_set("current_week", week)
    max_adds = int(_get(settings, "max_weekly_adds", default=0) or 0)
    if max_adds > 0:
        db.kv_set("max_adds_week", max_adds)
    db.kv_set("weeks_remaining", max(0, end_week - week))
    if week in weeks:
        db.kv_set("week_range", list(weeks[week]))
    db.kv_set("league_key", league_key)
    return {"mode": mode, "categories": cats, "week": week, "active_slots": active}


def _player_index():
    with db.cursor() as c:
        rows = c.execute("SELECT nba_id, norm_name, team FROM players").fetchall()
    idx = {}
    for r in rows:
        idx.setdefault(r["norm_name"], []).append((r["nba_id"], r["team"]))
    return idx


def _match(idx, name, team):
    cands = idx.get(norm_name(name), [])
    if len(cands) == 1:
        return cands[0][0]
    team = TEAM_FIX.get(team.upper(), team.upper())
    for pid, t in cands:
        if t == team:
            return pid
    if not cands:
        from rapidfuzz import process
        hit = process.extractOne(norm_name(name), list(idx), score_cutoff=92)
        if hit:
            return idx[hit[0]][0][0]
    return cands[0][0] if cands else None


def sync_rosters(q) -> dict:
    idx = _player_index()
    today = date.today().isoformat()
    teams = q.get_league_teams()
    standings = {}
    try:
        for t in _get(q.get_league_standings(), "teams", default=[]) or []:
            t = _unwrap(t)
            ot = _get(t, "team_standings", "outcome_totals")
            standings[_s(_get(t, "team_key"))] = (int(_get(ot, "wins", default=0) or 0),
                                                  int(_get(ot, "losses", default=0) or 0),
                                                  int(_get(ot, "ties", default=0) or 0),
                                                  int(_get(t, "team_standings", "rank", default=0) or 0))
    except Exception as e:  # standings are nice-to-have
        log.warning("standings unavailable: %s", e)
    unmatched, rosters, my_key = [], {}, None
    for t in teams:
        t = _unwrap(t)
        key = _s(_get(t, "team_key"))
        mine = bool(int(_get(t, "is_owned_by_current_login", default=0) or 0))
        if mine:
            my_key = key
        managers = _get(t, "managers", default=[]) or []
        mgr = _s(_get(managers[0], "nickname")) if managers else ""
        w, l, ti, rk = standings.get(key, (0, 0, 0, 0))
        with db.cursor() as c:
            c.execute("INSERT OR REPLACE INTO fantasy_teams VALUES (?,?,?,?,?,?,?,?)",
                      (key, _s(_get(t, "name")), mgr, 1 if mine else 0, w, l, ti, rk))
        players = q.get_team_roster_player_info_by_date(_get(t, "team_id"), today) or []
        rosters[key] = []
        for p in players:
            p = _unwrap(p)
            name = _s(_get(p, "name", "full"))
            pid = _match(idx, name, _s(_get(p, "editorial_team_abbr")))
            if not pid:
                unmatched.append(name)
                continue
            slot = _s(_get(p, "selected_position", "position"))
            rosters[key].append((pid, slot))
            # yfpy's eligible_positions keeps only the first position (yfpy 17: Mitchell ['PG']);
            # display_position is Yahoo's full base-position list, "PG,SG", same as a paste
            positions = _s(_get(p, "display_position")) or ",".join(
                _s(x) for x in (_get(p, "eligible_positions") or []))
            with db.cursor() as c:
                c.execute("UPDATE players SET yahoo_key=?, positions=?, yahoo_status=? WHERE nba_id=?",
                          (_s(_get(p, "player_key")), positions, _s(_get(p, "status")), pid))
    with db.cursor() as c:
        old = {r["team_key"]: r["name"] for r in c.execute("SELECT team_key, name FROM fantasy_teams")
               if r["team_key"] not in rosters}
        names = {r["name"].strip().lower(): r["team_key"] for r in c.execute(
            "SELECT team_key, name FROM fantasy_teams") if r["team_key"] in rosters}
    if rosters and old:
        # a pasted league's History, predictions and lineups carry its own team keys: move them
        # onto the Yahoo team of the same name before those teams go
        rekey_teams({k: names[n.strip().lower()] for k, n in old.items() if n.strip().lower() in names})
    with db.cursor() as c:
        # teams from an earlier demo or pasted league would otherwise linger (one still flagged mine)
        if rosters:  # an empty answer from Yahoo must not wipe the league
            c.execute(f"DELETE FROM fantasy_teams WHERE team_key NOT IN ({','.join('?' * len(rosters))})",
                      list(rosters))
        c.execute("DELETE FROM rosters")
        for key, plist in rosters.items():
            c.executemany("INSERT OR REPLACE INTO rosters VALUES (?,?,?)", [(key, pid, s) for pid, s in plist])
    db.kv_set("unmatched_players", unmatched)
    return {"my_key": my_key, "rosters": {k: [p for p, _ in v] for k, v in rosters.items()}}


TEAM_KEY_COLUMNS = [("transactions", "team_key"), ("transactions", "other_key"),
                    ("matchup_predictions", "team_key"), ("matchup_predictions", "opp_key"),
                    ("lineup_days", "team_key"), ("league_moves", "team_key"), ("shadow_days", "opp_key")]


def rekey_teams(mapping: dict) -> None:
    """Re-point every stored team key in mapping (old -> new). Rows that would collide with
    one already under the new key are the same record twice; the new key's copy is kept."""
    from . import history, shadow
    history._ensure()
    shadow._ensure()
    with db.cursor() as c:
        for old, new in mapping.items():
            for table, col in TEAM_KEY_COLUMNS:
                c.execute(f"UPDATE OR IGNORE {table} SET {col}=? WHERE {col}=?", (new, old))
                c.execute(f"DELETE FROM {table} WHERE {col}=?", (old,))


def sync_matchups(q, my_key, week) -> None:
    sb = q.get_league_scoreboard_by_week(week)
    opp = None
    for m in _get(sb, "matchups", default=[]) or []:
        keys = [_s(_get(t, "team_key")) for t in (_get(m, "teams", default=[]) or [])]
        if my_key in keys:
            opp = next((k for k in keys if k != my_key), None)
    db.kv_set("opponent_key", opp)
    # resolve last week's prediction from the actual result
    if week > 1:
        try:
            prev = q.get_league_scoreboard_by_week(week - 1)
            for m in _get(prev, "matchups", default=[]) or []:
                keys = [_s(_get(t, "team_key")) for t in (_get(m, "teams", default=[]) or [])]
                if my_key in keys:
                    if int(_get(m, "is_tied", default=0) or 0):
                        res = 0.5
                    else:
                        res = 1.0 if _s(_get(m, "winner_team_key")) == my_key else 0.0
                    learning.resolve_matchups({week - 1: res})
        except Exception as e:
            log.warning("could not resolve week %s: %s", week - 1, e)
    # week-to-date stats for both sides, from Yahoo's own numbers
    wtd = {}
    for key in filter(None, (my_key, opp)):
        try:
            raw = q.get_response(f"https://fantasysports.yahooapis.com/fantasy/v2/team/{key}/stats;type=week;week={week}").json()
            wtd[key] = parse_team_stats(raw)
        except Exception as e:
            log.warning("week-to-date stats unavailable for %s: %s", key, e)
    db.kv_set("week_to_date", wtd)


def sync_adds(q, my_key) -> None:
    """Yahoo's own count of adds used this week. The local transaction log misses any move
    made in the Yahoo app that wasn't logged here, so this is the number that counts."""
    try:
        raw = q.get_response(f"https://fantasysports.yahooapis.com/fantasy/v2/team/{my_key}/metadata").json()
        ra = parse_roster_adds(raw)
    except Exception as e:
        log.warning("weekly adds unavailable: %s", e)
        ra = None
    db.kv_set("yahoo_adds", ra)


def parse_roster_adds(node):
    """{"week": n, "used": n} from a team metadata response, or None."""
    if isinstance(node, dict):
        ra = node.get("roster_adds")
        if isinstance(ra, dict) and ra.get("coverage_type") == "week":
            return {"week": int(ra["coverage_value"]), "used": int(ra.get("value") or 0)}
        node = list(node.values())
    if isinstance(node, list):
        for v in node:
            hit = parse_roster_adds(v)
            if hit:
                return hit
    return None


def parse_waivers(raw) -> list:
    """[(name, team, positions, waiver_date)] from a league players;status=W;out=ownership page."""
    out = []
    # not _get: it unwraps single-key dicts, and {"fantasy_content": ...} is one
    league = (raw.get("fantasy_content") or {}).get("league") or []
    players = league[1].get("players") if len(league) > 1 and isinstance(league[1], dict) else {}
    for k, v in (players or {}).items():
        if k == "count":
            continue
        meta = {kk: vv for d in v["player"][0] if isinstance(d, dict) for kk, vv in d.items()}
        own = next((d["ownership"] for d in v["player"][1:] if isinstance(d, dict) and "ownership" in d), {})
        out.append((meta["name"]["full"], meta.get("editorial_team_abbr", ""), meta.get("display_position", ""),
                    own.get("waiver_date")))
    return out


def sync_waivers(q, league_key) -> None:
    """Who's on waivers and until when. Everyone else not on a roster is a free agent, which the
    synced rosters already tell us, so this is the one thing the pasted free-agent list added."""
    from . import free_agents
    idx, found, start = _player_index(), [], 0
    while True:
        raw = q.get_response(f"https://fantasysports.yahooapis.com/fantasy/v2/league/{league_key}"
                             f"/players;status=W;count=25;start={start};out=ownership").json()
        page = parse_waivers(raw)
        for name, team, pos, until in page:
            pid = _match(idx, name, team)
            if pid:
                found.append({"nba_id": int(pid), "status": "W", "until": until, "positions": pos})
        if len(page) < 25:
            break
        start += 25
    with db.cursor() as c:
        c.executemany("UPDATE players SET positions=? WHERE nba_id=?",
                      [(p["positions"], p["nba_id"]) for p in found if p["positions"]])
    free_agents.set_yahoo_waivers(found)


def parse_transactions(raw) -> list:
    """[{key, type, status, when (UTC), moves: [(name, team, kind, from_team_key, to_team_key)]}]
    from a league transactions page, newest first as Yahoo lists them. kind is add/drop/trade;
    a team key is None for free agents or waivers."""
    league = (raw.get("fantasy_content") or {}).get("league") or []
    txs = league[1].get("transactions") if len(league) > 1 and isinstance(league[1], dict) else {}
    out = []
    for k, v in (txs or {}).items():
        if k == "count":
            continue
        head = v["transaction"][0]
        body = v["transaction"][1] if len(v["transaction"]) > 1 else {}
        moves = []
        for pk, pv in ((body or {}).get("players") or {}).items():
            if pk == "count":
                continue
            meta = {kk: vv for d in pv["player"][0] if isinstance(d, dict) for kk, vv in d.items()}
            td = pv["player"][1]["transaction_data"]
            td = td[0] if isinstance(td, list) else td   # Yahoo sends a list for adds, a dict for drops
            moves.append((meta["name"]["full"], meta.get("editorial_team_abbr", ""), td["type"],
                          td.get("source_team_key"), td.get("destination_team_key")))
        out.append({"key": head["transaction_key"], "type": head["type"], "status": head.get("status"),
                    "when": datetime.fromtimestamp(int(head["timestamp"]), timezone.utc), "moves": moves})
    return out


def sync_transactions(q, league_key, my_key) -> None:
    """Every add, drop and trade in the league at the time Yahoo made it, from Yahoo's log. Moves
    made while this app wasn't running still land at the right time, which week-to-date scoring
    needs (who was on whose roster for each game); yours also go to History."""
    known = set(db.kv_get("yahoo_txn_keys") or [])
    first = not known
    new, start = [], 0
    while True:
        page = parse_transactions(q.get_response(f"https://fantasysports.yahooapis.com/fantasy/v2/league/{league_key}"
                                                 f"/transactions;types=add,drop,trade;count=25;start={start}").json())
        fresh = [t for t in page if t["key"] not in known]
        new += fresh
        if len(page) < 25 or len(fresh) < len(page):  # newest first: past this point, all seen
            break
        start += 25
    idx, mine = _player_index(), []
    for t in reversed(new):   # oldest first, so History stays in order
        if t["status"] != "successful":
            continue          # e.g. a pending trade: picked up once it goes through
        ts = t["when"].astimezone().isoformat()
        ins, outs, other = [], [], None
        for name, team, kind, src, dst in t["moves"]:
            pid = _match(idx, name, team)
            if not pid:
                log.warning("transaction %s: no match for %s", t["key"], name)
                continue
            if dst:
                db.add_move(dst, ts, pid, "add", "yahoo-api")
            if src:
                db.add_move(src, ts, pid, "drop", "yahoo-api")
            if dst == my_key:
                ins.append(pid)
                other = other or src
            if src == my_key:
                outs.append(pid)
                other = other or dst
        if ins or outs:
            mine.append((t, ins, outs, other))
        known.add(t["key"])
    if first and new:
        # The first read walks the whole season, so Yahoo's times now cover every move: the
        # approximate ones (roster editor, paste minutes, roster comparisons) are superseded.
        with db.cursor() as c:
            c.execute("DELETE FROM league_moves WHERE source<>'yahoo-api'")
    if mine:
        from . import sim
        ctx = sim.load_context()
        for t, ins, outs, other in mine:
            _log_my_transaction(ctx, my_key, t, ins, outs, other)
    db.kv_set("yahoo_txn_keys", sorted(known))


def _log_my_transaction(ctx, my_key, t, ins, outs, other):
    import json
    from datetime import timedelta
    from . import history
    history._ensure()
    kind = "trade" if t["type"] == "trade" else "add/drop" if ins and outs else "add" if ins else "drop"
    when = t["when"].astimezone()
    eff = when.date()
    with db.cursor() as c:
        if c.execute("SELECT 1 FROM transactions WHERE yahoo_key=?", (t["key"],)).fetchone():
            return
        # Already in History (roster editor, a paste, a suggestion marked done, or an earlier
        # sync's roster comparison): link it to Yahoo's record and take Yahoo's date and times.
        for r in c.execute("SELECT id, players_in, players_out, source, effective FROM transactions"
                           " WHERE deleted=0 AND yahoo_key IS NULL AND effective BETWEEN ? AND ?",
                           ((eff - timedelta(days=3)).isoformat(), (eff + timedelta(days=3)).isoformat())).fetchall():
            if sorted(json.loads(r["players_in"])) == sorted(ins) and sorted(json.loads(r["players_out"])) == sorted(outs):
                c.execute("UPDATE transactions SET yahoo_key=?, effective=?,"
                          " kind=CASE WHEN kind='roster change' THEN ? ELSE kind END WHERE id=?",
                          (t["key"], eff.isoformat(), kind, r["id"]))
                players = ins + outs
                c.execute("DELETE FROM league_moves WHERE team_key IN (?, ?) AND source=? AND substr(ts, 1, 10)=?"
                          f" AND nba_id IN ({','.join('?' * len(players))})",
                          [my_key, other or my_key, r["source"], r["effective"]] + players)
                return
    mv = _followed_suggestion(ins, outs, before=t["when"].isoformat())
    if mv:
        with db.cursor() as c:  # executed when Yahoo made the move, not when the sync noticed
            c.execute("UPDATE moves SET status='executed', executed_at=? WHERE id=?",
                      (t["when"].isoformat(timespec="seconds"), mv))
    history.record(ctx, kind, ins, outs, effective=eff.isoformat(), other_key=other if kind == "trade" else None,
                   source="suggestion" if mv else "yahoo", move_id=mv, yahoo_key=t["key"], moves_logged=True,
                   note="from Yahoo's transaction log")


def _find_stats(node):
    if isinstance(node, dict):
        if "stat_id" in node and "value" in node:
            yield node
        for v in node.values():
            yield from _find_stats(v)
    elif isinstance(node, list):
        for v in node:
            yield from _find_stats(v)


def parse_team_stats(raw) -> dict:
    out = {}
    for st in _find_stats(raw):
        sid, val = int(st["stat_id"]), str(st["value"] or "")
        if sid in (9004003, 9007006) and "/" in val:          # "FGM/FGA", "FTM/FTA"
            made, att = (float(x or 0) for x in val.split("/"))
            m, a = ("fgm", "fga") if sid == 9004003 else ("ftm", "fta")
            out[m], out[a] = made, att
        else:
            key = config.YAHOO_STAT_IDS.get(sid)
            if key is None or key.endswith("_pct"):  # percentages are rebuilt from makes/attempts
                continue
            try:
                out[key] = float(val)
            except ValueError:
                pass
    return out


TOKEN_KEYS = ("YAHOO_ACCESS_TOKEN", "YAHOO_GUID", "YAHOO_REFRESH_TOKEN", "YAHOO_TOKEN_TIME",
              "YAHOO_TOKEN_TYPE", "YAHOO_ACCESS_TOKEN_JSON")


def clear_token():
    """Forget any saved Yahoo login, so the next query runs a fresh browser login.
    A token's permissions are fixed when it's issued; re-running auth must not reuse it."""
    import os
    lines = config.ENV_PATH.read_text(encoding="utf-8").splitlines(keepends=True) if config.ENV_PATH.exists() else []
    keep = [ln for ln in lines if ln.split("=", 1)[0].strip() not in TOKEN_KEYS]
    config.ENV_PATH.write_text("".join(keep), encoding="utf-8")
    for k in TOKEN_KEYS:
        os.environ.pop(k, None)


def diagnose(q) -> list:
    """Probe what the current token can do. Used when the first sync fails, to tell an invalid
    token apart from a token without fantasy scope or a league-access problem."""
    import base64
    import json as _json
    out = []
    tok = getattr(q.oauth, "access_token", "") or ""
    parts = tok.split(".")
    if len(parts) == 3:  # JWT: scopes are readable without calling anything
        try:
            pad = parts[1] + "=" * (-len(parts[1]) % 4)
            claims = _json.loads(base64.urlsafe_b64decode(pad))
            out.append(("token claims", {k: claims.get(k) for k in ("scope", "scp", "aud", "exp") if k in claims}
                        or sorted(claims)))
        except ValueError as e:
            out.append(("token claims", f"not decodable: {e}"))
    else:
        out.append(("token format", f"opaque ({len(tok)} chars), scopes not visible locally"))
    probes = [
        ("account (OpenID userinfo)", "https://api.login.yahoo.com/openid/v1/userinfo"),
        ("fantasy: your games", "https://fantasysports.yahooapis.com/fantasy/v2/users;use_login=1/games"),
        ("fantasy: NBA game", "https://fantasysports.yahooapis.com/fantasy/v2/game/nba"),
        ("fantasy: your NBA leagues", "https://fantasysports.yahooapis.com/fantasy/v2/users;use_login=1/games;game_codes=nba/leagues"),
    ]
    for name, url in probes:
        try:
            r = q.oauth.session.get(url, params={"format": "json"})
            body = " ".join(r.text[:300].split())
            out.append((name, f"HTTP {r.status_code}: {body}"))
        except Exception as e:  # report every probe, whatever goes wrong
            out.append((name, f"request failed: {type(e).__name__}: {e}"))
    return out


def _followed_suggestion(ins, outs, before=None):
    """The open suggestion (or pending waiver claim) this one-for-one roster change carries out.
    before: the move's UTC ISO time; a suggestion made after it can't be what you followed."""
    if len(ins) != 1 or len(outs) > 1:
        return None
    with db.cursor() as c:
        row = c.execute("SELECT id FROM moves WHERE add_id=? AND (drop_id=? OR (? IS NULL AND drop_id IS NULL))"
                        " AND status IN ('suggested', 'claimed') AND created <= ? ORDER BY created DESC LIMIT 1",
                        (ins[0], outs[0] if outs else None, outs[0] if outs else None,
                         before or db.now_iso())).fetchone()
    return row["id"] if row else None


def sync() -> dict:
    """Full sync. Detects your own roster changes and logs them for the learning loop."""
    q = query()
    if db.kv_get("league_source") == "demo":
        # the demo league's suggestions and forecasts would pollute calibration for the real one
        with db.cursor() as c:
            c.execute("DELETE FROM moves")
            c.execute("DELETE FROM matchup_predictions")
        db.kv_set("calibration", None)
        db.kv_set("my_roster_last", None)
    info = sync_settings(q)
    before = db.kv_get("my_roster_last")
    r = sync_rosters(q)
    if r["my_key"]:
        sync_matchups(q, r["my_key"], info["week"])
        now = r["rosters"].get(r["my_key"], [])
        try:
            sync_transactions(q, db.kv_get("league_key"), r["my_key"])
            logged = True
        except Exception as e:  # fall back to comparing rosters: right moves, approximate times
            log.warning("transaction log unavailable: %s", e)
            logged = False
        if not logged and before is not None and set(before) != set(now):
            from . import history, sim
            ctx = sim.load_context()
            ins, outs = [p for p in now if p not in before], [p for p in before if p not in now]
            mv = _followed_suggestion(ins, outs)
            if mv:
                # made in the Yahoo app: still a followed suggestion for the scorecard
                learning.set_move_status(mv, "executed")
                history.record(ctx, "add/drop" if outs else "add", ins, outs, source="suggestion", move_id=mv)
            else:
                history.record(ctx, "roster change", ins, outs, source="yahoo")
        db.kv_set("my_roster_last", now)
        sync_adds(q, r["my_key"])
    try:
        sync_waivers(q, db.kv_get("league_key"))
    except Exception as e:  # without it, waiver status is just unknown
        log.warning("waiver list unavailable: %s", e)
    db.kv_set("yahoo_synced", db.now_iso())
    return dict(info, my_key=r["my_key"], unmatched=db.kv_get("unmatched_players", []))



def probe() -> bool:
    """Is the Fantasy API answering yet? Records it and alerts once when it starts working."""
    last = db.kv_get("yahoo_probe_at")
    if last and (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds() < 3600:
        return False  # hourly is plenty
    db.kv_set("yahoo_probe_at", db.now_iso())
    try:
        q = query()
        q.get_league_metadata()
    except ValueError as e:
        # Yahoo's token endpoint sometimes answers with a non-JSON page: not live, try later
        log.warning("yahoo probe: unreadable response (%s)", e)
        return False
    except Exception as e:
        if "not authorized" in str(e):
            return False
        raise
    db.kv_set("yahoo_access_live", db.now_iso())
    from . import alerts
    alerts.send("Yahoo API access is on", "The fantasy app can now read your league. Tell Claude so it can verify the sync and switch over.",
                tags="tada", priority=4)
    return True
