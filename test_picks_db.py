"""Tests for the picks.db parse-cache: ingest, incremental sync,
filtering, re-ingest on file change, schema rebuild, and the backtest
loaders' fallback when the cache is unavailable.

Readers are monkeypatched — reader OUTPUT fidelity is proven by the
loader-parity check against the real archive (2026-07-09: all four tabs
byte-identical, 210 files); these tests own the cache mechanics.
"""
import json
import os
import sqlite3
from datetime import date

import pytest

import backtest
import picks_db


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolated DB + fake archive with two dated files (one AN-only)."""
    archive = tmp_path / "archive"
    archive.mkdir()
    (archive / "ev_edges_20260701_0100.xlsx").write_bytes(b"x")
    (archive / "ev_edges_20260702_0100_ANONLY.xlsx").write_bytes(b"y")

    monkeypatch.setattr(picks_db, "DB_PATH", str(tmp_path / "picks.db"))
    monkeypatch.setattr(backtest, "ARCHIVE_DIR", str(archive))
    monkeypatch.setattr(backtest, "SCRIPT_DIR", str(tmp_path / "empty"))

    def reader_for(tab):
        def read(path, fd):
            # Carries every field _dedup_picks touches, so the real
            # loaders can run end-to-end over these fakes.
            return [{"tab": tab, "file": os.path.basename(path),
                     "file_date": fd, "pick": f"{tab}-pick",
                     "sport": "mlb", "game": f"A vs B {path[-6:]}",
                     "market": "H2H", "book": "DraftKings", "line": None,
                     "odds": -110, "ev_pct": 2.0}]
        return read
    monkeypatch.setattr(backtest, "_read_ev_edges", reader_for("ev"))
    monkeypatch.setattr(backtest, "_read_play_these", reader_for("pt"))
    monkeypatch.setattr(backtest, "_read_promo_picks", reader_for("promo"))
    monkeypatch.setattr(backtest, "_read_props_from_odds_comparison",
                        reader_for("prop"))
    return archive


def test_sync_ingests_once_then_incremental(env):
    s1 = picks_db.sync()
    assert s1["new_files"] == 2 and s1["new_rows"] == 8  # 2 files × 4 tabs
    s2 = picks_db.sync()
    assert s2["new_files"] == 0                          # idempotent
    # New file appears → only it is ingested
    (env / "ev_edges_20260703_0100.xlsx").write_bytes(b"z")
    s3 = picks_db.sync()
    assert s3["new_files"] == 1 and s3["new_rows"] == 4


def test_load_raw_filters_and_restores_dates(env):
    picks_db.sync()
    rows = picks_db.load_raw("ev", date(2026, 7, 1), date(2026, 7, 2),
                             "exclude")
    assert len(rows) == 1                                # ANONLY excluded
    assert rows[0]["file_date"] == date(2026, 7, 1)      # date restored
    assert len(picks_db.load_raw("ev", date(2026, 7, 1),
                                 date(2026, 7, 2), "include")) == 2
    only = picks_db.load_raw("ev", date(2026, 7, 1), date(2026, 7, 2),
                             "only")
    assert len(only) == 1 and "ANONLY" in only[0]["file"]
    # Date-range filter
    assert picks_db.load_raw("ev", date(2026, 8, 1), date(2026, 8, 2),
                             "include") == []
    with pytest.raises(ValueError):
        picks_db.load_raw("bogus", date(2026, 7, 1), date(2026, 7, 2))


def test_changed_file_reingested(env):
    picks_db.sync()
    # Same name, different size → regenerated → rows replaced, not doubled
    (env / "ev_edges_20260701_0100.xlsx").write_bytes(b"xxxx")
    s = picks_db.sync()
    assert s["new_files"] == 1
    rows = picks_db.load_raw("ev", date(2026, 7, 1), date(2026, 7, 1),
                             "include")
    assert len(rows) == 1


def test_schema_version_bump_rebuilds(env, monkeypatch):
    picks_db.sync()
    assert picks_db.stats()["files"] == 2
    monkeypatch.setattr(picks_db, "SCHEMA_VERSION", 999)
    assert picks_db.stats()["files"] == 0                # dropped on open
    s = picks_db.sync()
    assert s["new_files"] == 2                           # re-ingested


def test_loader_falls_back_when_db_unavailable(env, monkeypatch):
    """The cache must never block a backtest: break it and the loaders
    take the xlsx path (whose readers are our fakes here)."""
    def boom():
        raise sqlite3.OperationalError("disk I/O error")
    monkeypatch.setattr(picks_db, "sync", boom)
    picks = backtest.load_archive_picks(date(2026, 7, 1), date(2026, 7, 2),
                                        "exclude")
    assert len(picks) == 1 and picks[0]["pick"] == "pt-pick"


def test_loader_uses_db_rows(env):
    picks_db.sync()
    picks = backtest.load_archive_ev_edges(date(2026, 7, 1),
                                           date(2026, 7, 2), "include")
    assert {p["pick"] for p in picks} == {"ev-pick"}
    assert len(picks) == 2
