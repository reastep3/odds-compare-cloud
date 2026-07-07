"""Tests for the SGP slip log + calibration loop (sgp_log.py).

The whole point of the log is closing the gap between the independence
approximation /sgpcheck quotes and the correlated reality books price in —
so the settlement rules and the dedup in calibration have to be exact.
All grading is monkeypatched; no network.
"""
import json
from datetime import datetime, timedelta, timezone

import pytest

import sgp_log


@pytest.fixture(autouse=True)
def _isolated_store(tmp_path, monkeypatch):
    monkeypatch.setattr(sgp_log, "STORE", str(tmp_path / "sgp_slips.jsonl"))


def _leg(game="LAL @ BOS", pick="Boston Celtics", market="spreads",
         game_date="20260706", **over):
    l = {"sport": "nba", "game": game, "game_date": game_date,
         "market": market, "pick": pick, "line": -3.5, "is_prop": False,
         "player": None, "prop_side": None, "decimal": 1.91}
    l.update(over)
    return l


def _slip(fair=0.20, legs=None, **over):
    s = {"chat_id": 1, "label": "SGP boost", "book": "DraftKings",
         "boost_pct": 25.0, "naive_dec": 6.0, "quoted_dec": 5.0,
         "boosted_quote_dec": 6.0, "fair_prob": fair, "ev_pct": 3.0,
         "kelly": 0.01, "verdict": "BET",
         "legs": legs or [_leg(), _leg(pick="Over", market="totals")]}
    s.update(over)
    return s


# ── joint settlement rules ─────────────────────────────────────────────

def test_joint_result_all_win():
    assert sgp_log._joint_result(["WIN", "WIN"]) == "WIN"

def test_joint_result_any_loss_loses():
    assert sgp_log._joint_result(["WIN", "LOSS"]) == "LOSS"

def test_joint_result_push_drops_off_ticket():
    assert sgp_log._joint_result(["WIN", "PUSH"]) == "WIN"

def test_joint_result_all_push_is_push():
    assert sgp_log._joint_result(["PUSH", "PUSH"]) == "PUSH"

def test_joint_result_none_while_unresolved():
    assert sgp_log._joint_result(["WIN", "NOT_STARTED"]) is None


# ── log → grade → summary round trip ───────────────────────────────────

def _fake_run_backtest(results):
    def run(picks, *a, **k):
        for p, r in zip(picks, results):
            p["result"] = r
        return picks
    return run


def test_grade_pending_resolves_slip(monkeypatch):
    sgp_log.log_slip(_slip())
    import backtest
    monkeypatch.setattr(backtest, "run_backtest",
                        _fake_run_backtest(["WIN", "WIN"]))
    stats = sgp_log.grade_pending()
    assert stats == {"graded": 1, "pending": 0, "ungradable": 0}
    (slip,) = sgp_log._load_slips()
    assert slip["result"] == "WIN"
    assert slip["leg_results"] == ["WIN", "WIN"]
    # Idempotent — nothing left to grade
    assert sgp_log.grade_pending() == {"graded": 0, "pending": 0,
                                       "ungradable": 0}


def test_grade_pending_leaves_recent_unresolved_slip(monkeypatch):
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    sgp_log.log_slip(_slip(legs=[_leg(game_date=today)]))
    import backtest
    monkeypatch.setattr(backtest, "run_backtest",
                        _fake_run_backtest(["NOT_STARTED"]))
    stats = sgp_log.grade_pending()
    assert stats == {"graded": 0, "pending": 1, "ungradable": 0}
    (slip,) = sgp_log._load_slips()
    assert slip["result"] is None


def test_grade_pending_expires_stale_unresolvable_slip(monkeypatch):
    old = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y%m%d")
    sgp_log.log_slip(_slip(legs=[_leg(game_date=old)]))
    import backtest
    monkeypatch.setattr(backtest, "run_backtest",
                        _fake_run_backtest(["NO_RESULT"]))
    stats = sgp_log.grade_pending()
    assert stats == {"graded": 0, "pending": 0, "ungradable": 1}
    (slip,) = sgp_log._load_slips()
    assert slip["result"] == "UNGRADABLE"


# ── calibration summary ────────────────────────────────────────────────

def test_calibration_none_without_graded_slips():
    sgp_log.log_slip(_slip())          # pending, not graded
    assert sgp_log.calibration_summary() is None


def test_calibration_dedups_requotes_of_same_ticket(monkeypatch):
    # Same ticket checked twice at different quotes → one observation
    sgp_log.log_slip(_slip(fair=0.20, result="WIN", ts="2026-07-06T01:00:00"))
    sgp_log.log_slip(_slip(fair=0.20, result="WIN", ts="2026-07-06T02:00:00",
                           quoted_dec=4.5))
    # A different ticket that lost
    sgp_log.log_slip(_slip(fair=0.30, result="LOSS",
                           legs=[_leg(game="NYY @ TOR", pick="Over",
                                      market="totals")]))
    text = sgp_log.calibration_summary()
    assert "2 graded slip(s)" in text
    # predicted = (0.20 + 0.30) / 2 = 25.0%; realized = 1/2 = 50.0%
    assert "predicted 25.0%" in text
    assert "realized 50.0%" in text
    assert "directional only" in text   # n < 50


def test_calibration_reports_book_haircut():
    sgp_log.log_slip(_slip(result="WIN", naive_dec=6.0, quoted_dec=5.0))
    text = sgp_log.calibration_summary()
    # (1 - 4/5) * 100 = 20% haircut
    assert "avg book haircut 20.0%" in text


# ── leg_from_edge ──────────────────────────────────────────────────────

def test_leg_from_edge_derives_et_game_date_and_prop_fields():
    edge = {"sport": "MLB", "game": "NYY @ TOR",
            "commence": "2026-07-07T02:30:00Z",   # 10:30 PM ET on the 6th
            "market": "strikeouts", "outcome": "Gerrit Cole O",
            "point": 6.5, "player": "Gerrit Cole", "prop_side": "over",
            "odds_decimal": 1.87}
    leg = sgp_log.leg_from_edge(edge)
    assert leg["game_date"] == "20260706"
    assert leg["is_prop"] is True
    assert leg["market"] == "strikeouts"
    assert leg["pick"] == "Gerrit Cole O"
