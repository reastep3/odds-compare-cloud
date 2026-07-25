"""Tests for the shadow calibration layer (isotonic fit + lookup).

Money math again: a broken curve silently distorts every shadow EV the
switch decision will later be judged on, so the PAVA and the lookup get
exact-case coverage."""
from datetime import date

import pytest

import calibration


def test_pava_pools_violators_exactly():
    # x: .2 .4 .6 .8 with outcomes 0 1 0 1 → the .4/.6 pair violates
    # monotonicity and pools to 0.5; ends monotone.
    curve = calibration.fit_isotonic(
        [(0.2, 0), (0.4, 1), (0.6, 0), (0.8, 1)])
    assert curve == [[0.2, 0.0], [0.4, 0.5], [0.8, 1.0]]
    # Already-monotone input is untouched
    curve = calibration.fit_isotonic([(0.3, 0), (0.7, 1)])
    assert curve == [[0.3, 0.0], [0.7, 1.0]]


def test_calibrate_lookup_and_fallbacks():
    model = {"segments": {"pinn": [[0.0, 0.10], [0.5, 0.60]],
                          "nopin": []}}
    assert calibration.calibrate(0.30, True, model) == pytest.approx(0.10)
    assert calibration.calibrate(0.70, True, model) == pytest.approx(0.60)
    assert calibration.calibrate(0.50, True, model) == pytest.approx(0.60)
    # Empty segment → identity; no model → identity; garbage → identity
    assert calibration.calibrate(0.42, False, model) == 0.42
    assert calibration.calibrate(0.42, True, {}) == 0.42
    assert calibration.calibrate(0.42, True, {"segments": None}) == 0.42


def test_pairs_from_graded_filters_and_inverts():
    picks = [
        # ev 5% at decimal 2.0 → fair = 1.05/2 = 0.525, pinn segment
        {"result": "WIN", "market": "H2H", "decimal": 2.0, "ev_pct": 5.0,
         "pinnacle": True},
        {"result": "LOSS", "market": "SPREADS", "decimal": 1.909,
         "ev_pct": 2.0, "pinnacle": False},
        {"result": "PUSH", "market": "H2H", "decimal": 2.0, "ev_pct": 5.0,
         "pinnacle": True},                                # push → dropped
        {"result": "WIN", "market": "HR", "decimal": 5.0, "ev_pct": 9.0,
         "pinnacle": False},                               # non-game-line
    ]
    pairs = calibration._pairs_from_graded(picks)
    assert len(pairs["pinn"]) == 1
    assert pairs["pinn"][0] == (pytest.approx(0.525), 1)
    assert len(pairs["nopin"]) == 1
    assert pairs["nopin"][0][1] == 0


def test_fit_from_history_thin_segment_is_identity(monkeypatch):
    import backtest
    picks = [{"result": "WIN", "market": "H2H", "decimal": 2.0,
              "ev_pct": 2.0, "pinnacle": True}] * 50   # < MIN_SEGMENT_N
    monkeypatch.setattr(backtest, "load_archive_ev_edges",
                        lambda s, e, m: picks)
    monkeypatch.setattr(backtest, "run_backtest",
                        lambda p, **k: p)
    model = calibration.fit_from_history(date(2026, 3, 1), date(2026, 7, 1))
    assert model["segments"]["pinn"] == []             # identity, not noise
    assert model["n"]["pinn"] == 50
    # And the identity actually holds through calibrate()
    assert calibration.calibrate(0.61, True, model) == 0.61


def test_save_load_roundtrip_and_cache(tmp_path, monkeypatch):
    path = str(tmp_path / "calibration.json")
    monkeypatch.setattr(calibration, "MODEL_PATH", path)
    monkeypatch.setattr(calibration, "_cache", None)
    model = {"version": 1, "fitted_at": "t", "window": ["a", "b"],
             "n": {"pinn": 400, "nopin": 400},
             "segments": {"pinn": [[0.0, 0.2]], "nopin": [[0.0, 0.3]]}}
    calibration.save_model(model)
    loaded = calibration.load_model()
    assert loaded["segments"]["pinn"] == [[0.0, 0.2]]
    # calibrate() with model=None reads through the cached loader
    assert calibration.calibrate(0.5, True) == pytest.approx(0.2)
    assert calibration.calibrate(0.5, False) == pytest.approx(0.3)
    # Missing file → identity
    monkeypatch.setattr(calibration, "MODEL_PATH", str(tmp_path / "nope.json"))
    monkeypatch.setattr(calibration, "_cache", None)
    assert calibration.calibrate(0.5, True) == 0.5
