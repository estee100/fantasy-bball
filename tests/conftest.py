"""Shared fixtures.

Tests that need real data run against a *copy* of data/fbb.db in a temp folder, so the test
suite can never touch your actual rosters, history or learned parameters. They're skipped if
there's no local database yet (fresh clone before `python -m fbb init`).
"""
import sqlite3
import sys
from pathlib import Path

import pytest

import os
os.environ["FBB_NO_ALERTS"] = "1"   # the suite must never push real notifications

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fbb import config, db  # noqa: E402

REAL_DB = config.DB_PATH


@pytest.fixture
def league_db(tmp_path, monkeypatch):
    if not REAL_DB.exists():
        pytest.skip("no local database (run `python -m fbb init` first)")
    copy = tmp_path / "fbb.db"
    (tmp_path / "leagues").mkdir()
    pairs = [(REAL_DB, copy)] + [(p, tmp_path / "leagues" / p.name)
                                 for p in (REAL_DB.parent / "leagues").glob("*.db")]
    for a, b in pairs:   # SQLite's backup, not a file copy: picks up uncheckpointed WAL pages
        src, dst = sqlite3.connect(a), sqlite3.connect(b)
        src.backup(dst)
        src.close()
        dst.close()
    monkeypatch.setattr(config, "DB_PATH", copy)
    db.close_all()
    monkeypatch.setattr(db, "_conns", {})        # force new connections to the copy
    from fbb import pipeline
    monkeypatch.setattr(pipeline, "_ctx", {})
    yield copy
    db.close_all()


@pytest.fixture
def ctx(league_db):
    from fbb import pipeline
    return pipeline.context(reload=True)


@pytest.fixture
def pid(ctx):
    def find(name):
        hits = [p for p in ctx.players.index if ctx.name(p) == name]
        assert hits, f"player not found: {name}"
        return hits[0]
    return find

