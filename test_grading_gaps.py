"""Tests for the TheSportsDB results source (KBO/NPB/CFL) and the
nightly grading-gap audit — the watchdog that DMs the admin when
scoreable picks silently stop grading."""
from datetime import date, timedelta

import pytest

import backtest
from backtest import (THESPORTSDB_SPORTS, SCOREABLE, ESPN_SPORT_MAP,
                      _fetch_thesportsdb_games, grading_gap_audit)


class _Resp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload


def test_scoreable_wiring():
    assert {"kbo", "npb", "cfl"} <= SCOREABLE
    assert "cfl" not in ESPN_SPORT_MAP          # ESPN dropped CFL


def test_fetch_thesportsdb_games(monkeypatch):
    events = [
        {"strHomeTeam": "Doosan Bears", "strAwayTeam": "SSG Landers",
         "intHomeScore": "7", "intAwayScore": "3", "strStatus": "FT",
         "idEvent": "111"},
        {"strHomeTeam": "Hanwha Eagles", "strAwayTeam": "NC Dinos",
         "intHomeScore": None, "intAwayScore": None, "strStatus": "POST",
         "idEvent": "222"},
        {"strHomeTeam": "KT Wiz", "strAwayTeam": "Kiwoom Heroes",
         "intHomeScore": "4", "intAwayScore": "4", "strStatus": "FT",
         "idEvent": "333"},
    ]
    captured = {}

    def fake_get(url, **kw):
        captured["url"] = url
        return _Resp({"events": events})
    monkeypatch.setattr(backtest._espn_session, "get", fake_get)

    games = _fetch_thesportsdb_games(4830, "20260708")
    assert "d=2026-07-08" in captured["url"] and "l=4830" in captured["url"]
    assert len(games) == 3

    final = games[0]
    assert final["completed"] and final["winner"] == "Doosan Bears"
    assert final["home_score"] == 7 and final["total"] == 10

    postponed = games[1]
    assert not postponed["completed"]
    # POST maps to SCHEDULED so run_backtest reads it as NOT_STARTED
    assert postponed["status"] == "SCHEDULED"

    tie = games[2]                               # KBO allows 12-inning ties
    assert tie["completed"] and tie["winner"] == ""


def test_fetch_thesportsdb_games_error_paths(monkeypatch):
    monkeypatch.setattr(backtest._espn_session, "get",
                        lambda url, **kw: _Resp({}, status=500))
    assert _fetch_thesportsdb_games(4830, "20260708") == []

    def boom(url, **kw):
        raise OSError("network down")
    monkeypatch.setattr(backtest._espn_session, "get", boom)
    assert _fetch_thesportsdb_games(4830, "20260708") == []


def test_dispatch_routes_kbo_to_thesportsdb(monkeypatch):
    monkeypatch.setattr(backtest, "_espn_all_cache", {})
    monkeypatch.setattr(backtest, "_fetch_thesportsdb_games",
                        lambda lid, d: [{"league_id": lid, "date": d}])
    games = backtest.fetch_espn_games("kbo", "20260708")
    assert games == [{"league_id": 4830, "date": "20260708"}]
    games = backtest.fetch_espn_games("npb", "20260708")
    assert games == [{"league_id": 4591, "date": "20260708"}]


def test_grading_gap_audit_categorises(monkeypatch):
    yday   = (date.today() - timedelta(days=1)).strftime("%Y%m%d")
    future = (date.today() + timedelta(days=1)).strftime("%Y%m%d")

    def pick(sport, result, gd):
        return {"sport": sport, "result": result, "game_date": gd,
                "game": "A vs B", "market": "H2H"}

    picks = [
        pick("mlb", "WIN", yday),               # settled
        pick("kbo", "NO_RESULT", yday),         # anomaly: scoreable, past
        pick("mlb", "NOT_STARTED", yday),       # anomaly: stale not-started
        pick("mlb", "NOT_STARTED", future),     # fine: genuinely future
        pick("pga", "MANUAL", yday),            # expected: manual sport
        pick("mysterysport", "NO_RESULT", yday),  # expected: no source
    ]
    monkeypatch.setattr(backtest, "load_archive_ev_edges",
                        lambda s, e, m: picks)
    monkeypatch.setattr(backtest, "run_backtest", lambda p: p)

    audit = grading_gap_audit(days=3)
    assert audit["settled"] == 1
    assert {(a["sport"], a["status"]) for a in audit["anomalies"]} == {
        ("kbo", "NO_RESULT"), ("mlb", "NOT_STARTED")}
    assert audit["expected"] == {"pga": 1, "mysterysport": 1}
    assert "2 anomalies" in audit["text"]
