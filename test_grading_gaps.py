"""Tests for the TheSportsDB results source (KBO/NPB/CFL) and the
nightly grading-gap audit — the watchdog that DMs the admin when
scoreable picks silently stop grading."""
import json
from datetime import date, timedelta

import pytest

import backtest
from backtest import (THESPORTSDB_SPORTS, SCOREABLE, ESPN_SPORT_MAP,
                      _fetch_thesportsdb_games, grading_gap_audit)


class _Resp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.text = json.dumps(payload)   # fetcher sniffs .text for CF errors

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
    # POST passes through raw so the grader's VOID branch sees it
    assert postponed["status"] == "POST"

    tie = games[2]                               # KBO allows 12-inning ties
    assert tie["completed"] and tie["winner"] == ""


def test_fetch_thesportsdb_games_error_paths(monkeypatch):
    monkeypatch.setattr(backtest.time, "sleep", lambda s: None)
    monkeypatch.setattr(backtest._espn_session, "get",
                        lambda url, **kw: _Resp({}, status=500))
    assert _fetch_thesportsdb_games(4830, "20260708") == []

    def boom(url, **kw):
        raise OSError("network down")
    monkeypatch.setattr(backtest._espn_session, "get", boom)
    assert _fetch_thesportsdb_games(4830, "20260708") == []


def test_fetch_thesportsdb_rate_limit_retries(monkeypatch):
    """Cloudflare 1015 must retry with backoff, then succeed — and an
    unrecovered limit returns [] only after both attempts (never parsing
    the error body as an empty slate)."""
    sleeps = []
    monkeypatch.setattr(backtest.time, "sleep", lambda s: sleeps.append(s))

    calls = {"n": 0}
    ok = [{"strHomeTeam": "Doosan Bears", "strAwayTeam": "SSG Landers",
           "intHomeScore": "2", "intAwayScore": "1", "strStatus": "FT",
           "idEvent": "1"}]

    class _Limited:
        status_code = 200
        text = "error code: 1015"
        def json(self):
            raise ValueError("not json")

    def get(url, **kw):
        calls["n"] += 1
        return _Limited() if calls["n"] == 1 else _Resp({"events": ok})
    monkeypatch.setattr(backtest._espn_session, "get", get)

    games = _fetch_thesportsdb_games(4830, "20260708")
    assert len(games) == 1 and games[0]["completed"]
    assert calls["n"] == 2 and 4 in sleeps          # backed off then retried

    # Permanent limiting → both attempts fail → [] (ungraded, not wrong)
    monkeypatch.setattr(backtest._espn_session, "get",
                        lambda url, **kw: _Limited())
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
    stale  = (date.today() - timedelta(days=3)).strftime("%Y%m%d")
    future = (date.today() + timedelta(days=1)).strftime("%Y%m%d")

    def pick(sport, result, gd):
        return {"sport": sport, "result": result, "game_date": gd,
                "game": "A vs B", "market": "H2H"}

    picks = [
        pick("mlb", "WIN", yday),               # settled
        pick("kbo", "NO_RESULT", yday),         # anomaly: no fixture found
        pick("mlb", "IN_PROGRESS", yday),       # fine: grace (suspended /
        pick("afl", "NOT_STARTED", yday),       #   overseas evening game)
        pick("mlb", "IN_PROGRESS", stale),      # anomaly: stuck 3 days
        pick("mlb", "NOT_STARTED", future),     # fine: genuinely future
        pick("mlb", "VOID", yday),              # expected: postponed, refunded
        pick("pga", "MANUAL", yday),            # expected: manual sport
        pick("mysterysport", "NO_RESULT", yday),  # expected: no source
    ]
    monkeypatch.setattr(backtest, "load_archive_ev_edges",
                        lambda s, e, m: picks)
    monkeypatch.setattr(backtest, "run_backtest", lambda p: p)

    audit = grading_gap_audit(days=3)
    assert audit["settled"] == 1
    assert {(a["sport"], a["status"]) for a in audit["anomalies"]} == {
        ("kbo", "NO_RESULT"), ("mlb", "IN_PROGRESS")}
    assert audit["expected"] == {"pga": 1, "mysterysport": 1, "mlb": 1}
    assert "2 anomalies" in audit["text"]


def test_gap_audit_remember_alerts_once(monkeypatch, tmp_path):
    """remember=True: an anomaly DMs the first night only — known
    ungradeables (club-card bouts) must not nag until they age out."""
    yday = (date.today() - timedelta(days=1)).strftime("%Y%m%d")
    picks = [{"sport": "boxing", "result": "NO_RESULT", "game_date": yday,
              "game": "A vs B", "market": "H2H"}]
    monkeypatch.setattr(backtest, "load_archive_ev_edges",
                        lambda s, e, m: picks)
    monkeypatch.setattr(backtest, "run_backtest", lambda p: p)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))

    first = grading_gap_audit(days=3, remember=True)
    assert len(first["anomalies"]) == 1 and len(first["new_anomalies"]) == 1
    second = grading_gap_audit(days=3, remember=True)
    assert len(second["anomalies"]) == 1          # still visible in logs
    assert second["new_anomalies"] == []          # but no repeat DM
    # remember=False (manual/analysis use) never touches the state file
    third = grading_gap_audit(days=3, remember=False)
    assert len(third["new_anomalies"]) == 1


def test_run_backtest_voids_postponed(monkeypatch):
    """A same-day fixture with a POSTPONED/ABD status must grade VOID —
    not IN_PROGRESS forever, and never against the makeup game."""
    yday = (date.today() - timedelta(days=1))
    pick = {"sport": "mlb", "game": "Milwaukee Brewers @ Pittsburgh Pirates",
            "game_date": yday.strftime("%Y%m%d"), "market": "H2H",
            "pick": "Milwaukee Brewers", "line": None, "odds": -110,
            "decimal": 1.909, "bet_amount": 10.0, "ev_pct": 2.0,
            "result": None, "pnl": None}
    postponed_game = {
        "home_team": "Pittsburgh Pirates", "home_abbr": "PIT",
        "home_short": "Pirates", "home_score": 0,
        "away_team": "Milwaukee Brewers", "away_abbr": "MIL",
        "away_short": "Brewers", "away_score": 0,
        "winner": "", "total": 0, "event_id": "x",
        "completed": False, "status": "STATUS_POSTPONED",
    }
    monkeypatch.setattr(backtest, "fetch_espn_results", lambda s, d: [])
    monkeypatch.setattr(backtest, "fetch_espn_games",
                        lambda s, d: [postponed_game])
    graded = backtest.run_backtest([pick])
    assert graded[0]["result"] == "VOID"
    assert graded[0]["pnl"] is None
