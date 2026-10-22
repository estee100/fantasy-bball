"""CLI: python -m fbb <command>"""
import argparse
import json
import logging

from . import config, db


def main():
    ap = argparse.ArgumentParser(prog="fbb", description="Fantasy basketball manager")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init", help="download two seasons of NBA data, schedule, rosters (first run)")
    sub.add_parser("auth", help="one-time Yahoo login (opens browser, paste the code back here)")
    sub.add_parser("sync", help="sync your Yahoo league now")
    r = sub.add_parser("refresh", help="run the whole pipeline once")
    r.add_argument("--daily", action="store_true", help="also re-tune the model and re-scan reports")
    sub.add_parser("tune", help="re-tune projection parameters by walk-forward backtest")
    sub.add_parser("demo", help="(re)build the demo league")
    s = sub.add_parser("serve", help="run the dashboard + scheduler")
    s.add_argument("--port", type=int, default=8000)
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if a.cmd == "init":
        from . import nba
        print("player directory:", nba.ingest_player_directory())
        print("schedule games:", nba.ingest_schedule())
        print("game logs:", nba.ingest_logs([config.PRIOR_SEASON, config.SEASON]))
        if not config.yahoo_configured():
            from . import demo
            demo.build()
            print("No Yahoo credentials yet: built a demo league so the dashboard works.")
    elif a.cmd in ("auth", "sync"):
        from . import yahoo
        print(json.dumps(yahoo.sync(), indent=2, default=str))
    elif a.cmd == "refresh":
        from . import pipeline
        print(json.dumps(pipeline.refresh(daily=a.daily), indent=2, default=str))
    elif a.cmd == "tune":
        from . import learning
        print(json.dumps(learning.tune_projections(), indent=2))
    elif a.cmd == "demo":
        from . import demo
        demo.build()
    elif a.cmd == "serve":
        import uvicorn
        uvicorn.run("fbb.server:app", host="127.0.0.1", port=a.port)


if __name__ == "__main__":
    main()
