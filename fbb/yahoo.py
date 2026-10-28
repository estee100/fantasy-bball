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
    active = roster = 0
    for rp in _get(settings, "roster_positions", default=[]) or []:
        rp = _unwrap(rp)
        pos, n = _s(_get(rp, "position")), int(_get(rp, "count", default=0) or 0)
        if pos not in ("IL", "IL+", "NA"):
            roster += n
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
        # teams from an earlier demo or pasted league would otherwise linger (one still flagged mine)
        if rosters:  # an empty answer from Yahoo must not wipe the league
            c.execute(f"DELETE FROM fantasy_teams WHERE team_key NOT IN ({','.join('?' * len(rosters))})",
                      list(rosters))
        c.execute("DELETE FROM rosters")
        for key, plist in rosters.items():
            c.executemany("INSERT OR REPLACE INTO rosters VALUES (?,?,?)", [(key, pid, s) for pid, s in plist])
    db.kv_set("unmatched_players", unmatched)
    return {"my_key": my_key, "rosters": {k: [p for p, _ in v] for k, v in rosters.items()}}


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
        if before is not None and set(before) != set(now):
            from . import history, sim
            ctx = sim.load_context()
            history.record(ctx, "roster change", [p for p in now if p not in before],
                           [p for p in before if p not in now], source="yahoo")
        db.kv_set("my_roster_last", now)
        sync_adds(q, r["my_key"])
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
