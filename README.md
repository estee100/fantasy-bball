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

### More than one league

Put every league ID in `YAHOO_LEAGUE_ID`, comma-separated, then run `python -m fbb sync` (one
Yahoo login covers all of them). The dashboard gets a league picker in the header. NBA data,
news and injuries are downloaded once and shared. Each league keeps its own rosters, moves,
forecasts, takes, watchlist and tuned settings in `data/leagues/<id>.db`; a new league starts
from an existing league's tuning until its own first daily tune. CLI commands run every league
unless you pass `--league <id>` (e.g. `python -m fbb --league 312045 sync`). Phone alerts are
prefixed with the league name.

Upgrading from a single-league install moves the league's data out of `data/fbb.db` into its
league file automatically on first start (a backup is written to `data/backups/` first).
Restart the server after updating so no old process keeps writing to the old layout.

### Draft

The Draft tab is a big board plus a pick tracker:

- **Ranking**: rest-of-season value (per-game value × expected games left, after injuries and
  rest), minus replacement level (the last player who'd be drafted). Your takes count.
- **Next pick / After**: the chance each player is still there at your next two picks, from a
  few hundred simulated drafts where the other managers pick off Yahoo's rank with some noise.
- **Suggestion** (when you're on the clock): value now + the best player likely left at your
  next pick. It takes the player who won't last over a slightly better one who will. Late in
  the draft it only suggests players who fill an empty starting slot.
- **Settings** (all optional, blank = Yahoo/league): live or mock mode, teams, your slot,
  rounds, snake or linear, auction and budget, how the others pick (Yahoo rank, ADP, or this
  model), how predictable they are, and whether injuries discount value. ✕ on the board keeps
  a player out of suggestions.

Live mode reads the order and picks from Yahoo (tick *Follow Yahoo live* during the draft to
poll every 8 s; *Draft* fills in a pick Yahoo hasn't reported yet). Mock mode ignores Yahoo:
you record every pick, *Undo* and *Reset* work on those.

The app is **read-only** against Yahoo. It recommends moves; you make them in the Yahoo app,
and the next sync reads them from Yahoo's transaction log (exact times, every team) and logs
yours to History, linked to the suggestion if you followed one. Rosters, lineup slots, waivers,
the weekly add limit and adds used all come from the sync too.

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
- **Calibration**: every suggestion (made, dismissed, or ignored) and every move of your own is
  scored against real box scores at 7 days and at its horizon; stated confidence is corrected by
  that record.
- **Matchup forecasts**: Brier score of weekly win probabilities once weeks resolve.

The Model & learning tab shows all of it.

## Limits worth knowing

- Lineups use real slots (PG/SG/G/SF/PF/F/C/C/Util/Util). The lineup card solves each day
  exactly; the simulator uses a fast fill that matches the exact answer ~98% of the time
  (misses cost about one bench decision). Players with unknown positions count as any
  position in the simulator and Util-only on the lineup card (flagged there).
- Waiver priority / FAAB aren't modeled (who's on waivers, and until when, is).
- Players with an injury-shortened last season fall back to the season before; players with
  no NBA history at all (rookies before their first games) lean on a generic rotation prior.
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

## Tests

```
python -m pytest -q
```

Tests that need data run on a temporary copy of `data/fbb.db` and `data/leagues/`, never the real ones.
