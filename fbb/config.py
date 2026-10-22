import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
REPORTS_DIR = DATA_DIR / "reports"
DB_PATH = DATA_DIR / "fbb.db"
ENV_PATH = ROOT / ".env"

load_dotenv(ENV_PATH)

YAHOO_KEY = os.getenv("YAHOO_CONSUMER_KEY", "")
YAHOO_SECRET = os.getenv("YAHOO_CONSUMER_SECRET", "")
YAHOO_LEAGUE_ID = os.getenv("YAHOO_LEAGUE_ID", "")
ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "")
SEASON = os.getenv("NBA_SEASON", "2026-27")
PRIOR_SEASON = os.getenv("NBA_PRIOR_SEASON", "2025-26")

CLAUDE_MODEL = "claude-opus-5-5"


def yahoo_configured() -> bool:
    return bool(YAHOO_KEY and YAHOO_SECRET and YAHOO_LEAGUE_ID)


# Canonical stat keys used everywhere internally. Game logs store these columns.
COUNT_STATS = ["min", "fgm", "fga", "fg3m", "fg3a", "ftm", "fta", "oreb", "dreb",
               "reb", "ast", "stl", "blk", "tov", "pts", "dd", "td"]

# Category definitions: key -> (label, kind, numerator, denominator, higher_is_better)
CATEGORY_DEFS = {
    "pts": ("PTS", "count", "pts", None, True),
    "reb": ("REB", "count", "reb", None, True),
    "oreb": ("OREB", "count", "oreb", None, True),
    "dreb": ("DREB", "count", "dreb", None, True),
    "ast": ("AST", "count", "ast", None, True),
    "stl": ("STL", "count", "stl", None, True),
    "blk": ("BLK", "count", "blk", None, True),
    "fg3m": ("3PM", "count", "fg3m", None, True),
    "tov": ("TO", "count", "tov", None, False),
    "fgm": ("FGM", "count", "fgm", None, True),
    "ftm": ("FTM", "count", "ftm", None, True),
    "dd": ("DD", "count", "dd", None, True),
    "td": ("TD", "count", "td", None, True),
    "fg_pct": ("FG%", "ratio", "fgm", "fga", True),
    "ft_pct": ("FT%", "ratio", "ftm", "fta", True),
    "fg3_pct": ("3P%", "ratio", "fg3m", "fg3a", True),
}

DEFAULT_CATEGORIES = ["fg_pct", "ft_pct", "fg3m", "pts", "reb", "ast", "stl", "blk", "tov"]

# Yahoo NBA stat_id -> canonical key (scoring stats only)
YAHOO_STAT_IDS = {
    3: "fga", 4: "fgm", 5: "fg_pct", 6: "fta", 7: "ftm", 8: "ft_pct",
    9: "fg3a", 10: "fg3m", 11: "fg3_pct", 12: "pts", 13: "oreb", 14: "dreb",
    15: "reb", 16: "ast", 17: "stl", 18: "blk", 19: "tov", 27: "dd", 28: "td",
}

# Default 13-man roster: 10 active slots, 3 bench
DEFAULT_ACTIVE_SLOTS = 10

# Tunable model parameters; the learning module overwrites these in the DB.
DEFAULT_PARAMS = {
    "half_life_games": 15.0,       # recency weighting for per-game projections
    "prior_season_weight": 0.5,    # how much a prior-season game counts vs a current one
    "future_week_discount": 0.85,  # how much a future week's edge counts vs this week
    "news_multipliers": {},        # event_type -> learned production multiplier
}
