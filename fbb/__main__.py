"""CLI: python -m fbb <command>"""
import argparse
import json
import logging

from . import config, db


def main():
    ap = argparse.ArgumentParser(prog="fbb", description="Fantasy basketball manager")
    ap.add_argument("--league", help="league id for auth/sync/refresh/tune (default: every configured league)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init", help="download two seasons of NBA data, schedule, rosters (first run)")
    sub.add_parser("auth", help="one-time Yahoo login (opens browser, paste the code back here)")
    sub.add_parser("sync", help="sync your Yahoo league(s) now")
    r = sub.add_parser("refresh", help="run the whole pipeline once")
    r.add_argument("--daily", action="store_true", help="also re-tune the model and re-scan reports")
    sub.add_parser("tune", help="re-tune projection parameters by walk-forward backtest")
    sub.add_parser("demo", help="(re)build the demo league")
    sub.add_parser("leagues", help="list configured leagues")
    au = sub.add_parser("autostart", help="start the dashboard hidden at Windows login")
    au.add_argument("action", choices=["install", "remove", "restart"])
    s = sub.add_parser("serve", help="run the dashboard + scheduler")
    s.add_argument("--port", type=int, default=8000)
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if a.league and a.league not in db.league_ids():
        ap.error(f"league {a.league} isn't in YAHOO_LEAGUE_ID in .env (configured: {', '.join(db.league_ids())})")
    lids = [a.league] if a.league else db.league_ids()

    if a.cmd == "init":
        from . import nba
        print("player directory:", nba.ingest_player_directory())
        print("schedule games:", nba.ingest_schedule())
        print("game logs:", nba.ingest_logs([config.PRIOR_SEASON, config.SEASON]))
        if not config.yahoo_configured():
            from . import demo
            with db.use_league("demo"):
                demo.build()
            print("No Yahoo credentials yet: built a demo league so the dashboard works.")
    elif a.cmd in ("auth", "sync"):
        from . import yahoo
        if a.cmd == "auth":
            yahoo.clear_token()  # always a fresh login, never a stale token
        failed = False
        for lid in lids:   # one Yahoo login covers every league on the account
            with db.use_league(lid):
                print(f"--- league {lid}")
                try:
                    print(json.dumps(yahoo.sync(), indent=2, default=str))
                except Exception as e:
                    failed = True
                    print(f"\nSync failed: {type(e).__name__}: {e}\n\nDiagnostics (paste these back):")
                    try:
                        for name, result in yahoo.diagnose(yahoo.query()):
                            print(f"  - {name}: {result}")
                    except Exception as d:
                        print(f"  could not run diagnostics: {type(d).__name__}: {d}")
        if failed:
            raise SystemExit(1)
    elif a.cmd == "refresh":
        from . import pipeline
        print(json.dumps(pipeline.refresh(daily=a.daily, leagues=lids), indent=2, default=str))
    elif a.cmd == "tune":
        from . import learning
        for lid in lids:
            with db.use_league(lid):
                print(f"--- league {lid}")
                print(json.dumps(learning.tune_projections(), indent=2))
    elif a.cmd == "demo":
        from . import demo
        with db.use_league("demo"):
            demo.build()
    elif a.cmd == "leagues":
        for lid in db.league_ids():
            print(f"{lid}\t{db.league_name(lid)}")
    elif a.cmd == "autostart":
        from . import autostart
        {"install": autostart.install, "remove": autostart.remove,
         "restart": autostart.restart}[a.action]()
    elif a.cmd == "serve":
        import uvicorn
        uvicorn.run("fbb.server:app", host="127.0.0.1", port=a.port)


if __name__ == "__main__":
    main()
