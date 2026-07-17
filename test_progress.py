"""Tests for progress.py — the live progress feed behind the webapp's
run/backtest progress bars — and its wiring into the grading loop."""
from datetime import date, timedelta

import backtest
import progress


def test_report_get_clear_roundtrip():
    progress.clear("run")
    assert progress.get("run") is None
    progress.report("run", "Fetching game lines", 3, 15, note="mlb")
    s = progress.get("run")
    assert s["phase"] == "Fetching game lines"
    assert s["done"] == 3 and s["total"] == 15 and s["note"] == "mlb"
    assert s["ts"] > 0
    # Snapshot is a copy — mutating it can't corrupt shared state
    s["done"] = 999
    assert progress.get("run")["done"] == 3
    progress.clear("run")
    assert progress.get("run") is None


def test_phase_only_report():
    progress.report("backtest", "Writing report")
    s = progress.get("backtest")
    assert s["phase"] == "Writing report"
    assert s["done"] is None and s["total"] is None
    progress.clear("backtest")


def test_run_backtest_reports_progress(monkeypatch):
    """The grading loop must emit progress with the pool label."""
    seen = []
    monkeypatch.setattr(progress, "report",
                        lambda *a, **k: seen.append((a, k)))
    monkeypatch.setattr(backtest, "fetch_espn_results", lambda s, d: [])
    monkeypatch.setattr(backtest, "fetch_espn_games", lambda s, d: [])
    yday = (date.today() - timedelta(days=1)).strftime("%Y%m%d")
    picks = [{"sport": "mlb", "game": "A vs B", "game_date": yday,
              "market": "H2H", "pick": "A", "line": None, "odds": -110,
              "decimal": 1.909, "bet_amount": 10.0, "ev_pct": 2.0,
              "result": None, "pnl": None}]
    backtest.run_backtest(picks, progress_label="EV pool")
    phases = {a[1] for a, k in seen}
    assert any(p.startswith("Grading EV pool") for p in phases)
