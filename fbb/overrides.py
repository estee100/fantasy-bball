"""Your take: override the model's projection for a player, and keep score.

Two kinds:
  minutes  "I think he plays 28 a night"     -> scales his counting stats by 28 / model minutes
  pct      "he'll produce 15% more than that" -> scales his counting stats by 1.15

A take changes everything downstream (simulations, lineups, recommendations, trades) because it
is applied to the projection table itself when the league context loads. It never touches the
model's own learning: tuning and backtests build projections without it.

Each take stores the model's number and yours when you set it; every game after that, both are
compared with what actually happened, so the History tab can show whose projections were closer.
"""
import json
from datetime import date

from . import config, db, model

SCALE_LIMITS = (0.3, 2.5)


def active() -> dict:
    with db.cursor() as c:
        return {r["nba_id"]: dict(r) for r in c.execute("SELECT * FROM overrides WHERE active=1 ORDER BY id")}


def factor(o, model_min) -> float:
    if o["kind"] == "minutes":
        f = o["value"] / max(model_min, 1.0)
    else:
        f = 1 + o["value"] / 100.0
    return float(min(max(f, SCALE_LIMITS[0]), SCALE_LIMITS[1]))


def apply(proj):
    """Scale overridden players' projected production in place. Returns {pid: factor}."""
    applied = {}
    stat_cols = [c for c in proj.columns if not c.endswith("_sd") and c not in ("n_games", "cur_games", "n_eff")]
    for pid, o in active().items():
        if pid not in proj.index:
            continue
        f = factor(o, proj.at[pid, "min"])
        for c in stat_cols:
            proj.at[pid, c] = proj.at[pid, c] * f
        applied[pid] = f
    return applied


def per_game(row) -> float:
    pw = model.point_weights()
    if pw:
        return float(sum(row[s] * w for s, w in pw.items() if s in row))
    return float("nan")


def set_take(ctx, pid: int, kind: str, value: float, note: str = "") -> dict:
    """ctx must be a context loaded WITHOUT this player's take (the model's own view)."""
    if kind not in ("minutes", "pct"):
        raise ValueError("kind must be 'minutes' or 'pct'")
    if kind == "minutes" and not (0 < value <= 48):
        raise ValueError("Minutes must be between 0 and 48")
    if kind == "pct" and not (-70 <= value <= 150):
        raise ValueError("Percent change must be between -70 and +150")
    raw = ctx.raw_proj.loc[pid] if pid in ctx.raw_proj.index else None
    if raw is None:
        raise ValueError("No projection for this player yet")
    o = {"kind": kind, "value": value}
    model_pg = per_game(raw)
    yours = model_pg * factor(o, raw["min"])
    today = date.today().isoformat()
    with db.cursor() as c:
        c.execute("UPDATE overrides SET active=0, cleared=? WHERE nba_id=? AND active=1", (today, pid))
        c.execute("""INSERT INTO overrides(created, since, nba_id, kind, value, note, active, model_min,
                     model_per_game, your_per_game) VALUES (?,?,?,?,?,?,1,?,?,?)""",
                  (db.now_iso(), today, int(pid), kind, float(value), note, float(raw["min"]), model_pg, yours))
    return {"model_per_game": model_pg, "your_per_game": yours}


def clear(pid: int):
    with db.cursor() as c:
        c.execute("UPDATE overrides SET active=0, cleared=? WHERE nba_id=? AND active=1", (date.today().isoformat(), pid))


def report(ctx) -> dict:
    """Every take (active or cleared), judged on the player's games since it was set:
    whose per-game number was closer to what he actually averaged?"""
    pw = model.point_weights()
    with db.cursor() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM overrides ORDER BY created DESC")]
    items, yours_closer, judged = [], 0, 0
    for o in rows:
        end = o["cleared"] or "9999-12-31"
        g = ctx.logs[(ctx.logs["nba_id"] == o["nba_id"]) & (ctx.logs["game_date"] > o["since"])
                     & (ctx.logs["game_date"] <= end)]
        actual = float(sum(g[s] * w for s, w in pw.items()).mean()) if (pw and len(g)) else None
        item = {**{k: o[k] for k in ("id", "created", "since", "nba_id", "kind", "value", "note", "active",
                                     "model_min", "model_per_game", "your_per_game", "cleared")},
                "name": ctx.name(o["nba_id"]), "games": int(len(g)), "actual_per_game": actual,
                "actual_min": float(g["min"].mean()) if len(g) else None}
        if actual is not None:
            item["model_error"] = abs(actual - o["model_per_game"])
            item["your_error"] = abs(actual - o["your_per_game"])
            item["closer"] = "you" if item["your_error"] < item["model_error"] else "model"
            if len(g) >= 3:  # one game says almost nothing
                judged += 1
                yours_closer += item["closer"] == "you"
        items.append(item)
    return {"items": items, "judged": judged, "yours_closer": yours_closer,
            "unit": "fantasy pts" if pw else "value"}
