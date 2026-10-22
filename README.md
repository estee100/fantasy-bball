# Fantasy Hoops HQ

A self-hosted fantasy basketball manager for Yahoo leagues: live dashboard, matchup simulator,
waiver/trade recommendations, role-change detection, news + scouting ingestion, and a learning
loop that scores its own advice.

```
python -m fbb init      # download 3 seasons of NBA data + schedule (≈1 min)
python -m fbb serve     # dashboard at http://127.0.0.1:8000, refreshes every 15 min
```

To have it start hidden in the background every time you log in to Windows (no console
window; logs in `data/server.log`):

```
python -m fbb autostart install    # undo with: python -m fbb autostart remove
```

Without Yahoo credentials it runs on a demo league (10 teams snake-drafted from real projections)
so everything works before you connect.

## Connect your Yahoo league

1. Create an app at <https://developer.yahoo.com/apps/create/>
   - Homepage URL: `https://localhost:8080` (required by the form, never visited)
   - Redirect URI: `https://localhost:8080` (must match `YAHOO_REDIRECT_URI` in `.env`)
   - OAuth client type: **Confidential Client**
   - API permissions: **Fantasy Sports → Read**
2. `cp .env.example .env` and fill in `YAHOO_CONSUMER_KEY`, `YAHOO_CONSUMER_SECRET`, and
   `YAHOO_LEAGUE_ID` (the number in `basketball.fantasysports.yahoo.com/nba/<LEAGUE_ID>`).
3. `python -m fbb auth` opens a browser; click Agree. Yahoo then redirects to
   `https://localhost:8080/?code=...`, which shows a "can't connect" error. That's expected:
   copy the `code` value from the address bar and paste it into the terminal. The token is
   saved to `.env` and refreshed automatically after that.
4. Optional: set `ANTHROPIC_API_KEY` so news and scouting reports are read by Claude
   (otherwise a keyword tagger handles news and scouting ingestion is off).

The app is **read-only** against Yahoo. It recommends moves; you make them in the Yahoo app,
and the next sync detects the roster change and logs it.

## How it decides

| Piece | Method |
|---|---|
| Projections | minutes × per-minute production. Each is a Bayesian blend of this season (recency-weighted) and last season (a prior worth *k* games). Shrunk toward a generic rotation player for small samples. |
| Availability | ESPN injury report + Claude-extracted timelines + each player's history of sitting the 2nd night of back-to-backs. |
| This week's odds | Monte Carlo: 2,000 weeks resampling each player's real box scores (keeps FG%/volume correlation), daily active-slot cap, week-to-date stats from Yahoo. |
| Move value | Expected matchup wins added = Δ P(win this week) + Δ P(win a typical week) × effective weeks left. Streams and upgrades compete on the same scale. |
| Confidence | P(added player outproduces dropped player over the move's horizon): estimate uncertainty + game-to-game noise, then calibrated against the move ledger. |
| Role alerts | Per-36 opportunity rate behind each player's top skills (usage, assists, 3PA, FTA, rebounds, blocks, steals), recent vs baseline (Welch z + minimum relative change), separated from minutes changes. Context: team change, same-skill arrivals, news. |

## What it learns (daily, `python -m fbb tune`)

- **Projection memory**: walk-forward backtest over every player-game (no look-ahead) picks
  the half-lives and how much last season counts. The October question ("how much should last
  season count?") is answered by backtesting the start of last season with the season before as
  the prior.
- **News impact**: actual vs projected minutes after each news event type.
- **Calibration**: every suggestion (made, dismissed, or ignored) and every manual move is
  scored against real box scores at 7 days and at its horizon; stated confidence is corrected by
  that record.
- **Matchup forecasts**: Brier score of weekly win probabilities once weeks resolve.

The Model & learning tab shows all of it.

## Limits worth knowing

- Positional eligibility isn't modeled in the daily lineup cap or start/sit suggestions.
- Weekly add limits and waiver priority / FAAB aren't modeled.
- Returning players who missed most of last season (e.g. long injuries) are projected from thin
  data and shrunk hard toward an average rotation player until they play.
- News feeds are free RSS/APIs: occasionally late compared to paid services.

## Layout

```
fbb/
  nba.py        stats.nba.com ingest (game logs, schedule, rosters)
  yahoo.py      Yahoo league sync
  news.py       feeds, injuries, Claude extraction, scouting reports
  model.py      projections, availability, valuation, analytic odds
  sim.py        Monte Carlo matchup engine
  recommend.py  add/drop + trade evaluation
  roles.py      role-change monitor
  learning.py   ledger, scoring, calibration, tuning
  pipeline.py   refresh pipeline + data readiness checks
  server.py     FastAPI + scheduler
  static/       dashboard
```
