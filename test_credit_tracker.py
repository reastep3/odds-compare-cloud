"""Tests for the Odds API credit tracker / CLV pause-switch.

State persistence is exercised via isolated DATA_DIR per test so the
tests don't share `credit_state.json` with each other or the real
running container.
"""
import json
import os

import pytest

import credit_tracker


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch):
    """Each test gets a fresh DATA_DIR — no cross-test state leaks."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    yield tmp_path


@pytest.fixture
def force_thresholds(monkeypatch):
    """Helper to override module-level constants without re-importing."""
    def _apply(*, pause_pct=None, explicit_limit=None):
        if pause_pct is not None:
            monkeypatch.setattr(credit_tracker,
                                 "CLV_PAUSE_AT_USAGE_PCT", pause_pct)
        # EXPLICIT_MONTHLY_LIMIT is None by default; tests may set it.
        monkeypatch.setattr(credit_tracker,
                             "EXPLICIT_MONTHLY_LIMIT", explicit_limit)
    return _apply


# ── record() input handling ─────────────────────────────────────────

def test_record_persists_int(isolated_data_dir):
    credit_tracker.record(1000)
    state_path = os.path.join(str(isolated_data_dir), "credit_state.json")
    assert os.path.exists(state_path)
    with open(state_path) as f:
        state = json.load(f)
    assert state["remaining"] == 1000
    assert state["peak_remaining"] == 1000


def test_record_parses_string_header():
    # Odds API returns the header as a string; tracker must coerce.
    credit_tracker.record("750")
    assert credit_tracker.get_status()["remaining"] == 750


def test_record_ignores_none():
    credit_tracker.record(None)
    assert credit_tracker.get_status()["remaining"] is None


def test_record_ignores_invalid_string():
    # "?" is the fallback the call sites pass when the header is missing
    credit_tracker.record("?")
    assert credit_tracker.get_status()["remaining"] is None


def test_record_ignores_negative():
    credit_tracker.record(-5)
    assert credit_tracker.get_status()["remaining"] is None


# ── Peak watermark ──────────────────────────────────────────────────

def test_peak_grows_monotonically():
    credit_tracker.record(500)
    credit_tracker.record(450)
    credit_tracker.record(400)
    s = credit_tracker.get_status()
    assert s["remaining"] == 400
    assert s["peak"] == 500


def test_peak_promotes_on_first_above_previous():
    # New high (still inside the rollover-ratio band) — peak updates.
    credit_tracker.record(900)
    credit_tracker.record(950)   # 950 / 900 = 1.05 < 1.5, not a rollover
    s = credit_tracker.get_status()
    assert s["peak"] == 950


# ── Rollover detection ──────────────────────────────────────────────

def test_rollover_resets_pause_when_remaining_jumps(force_thresholds):
    force_thresholds(pause_pct=0.85)
    # Start at peak, then drain to "paused" territory.
    credit_tracker.record(10_000)
    credit_tracker.record(800)            # 92% used → paused
    assert credit_tracker.should_pause_clv() is True

    # Month rollover: remaining jumps from 800 → 10_000.
    credit_tracker.record(10_000)
    assert credit_tracker.should_pause_clv() is False
    s = credit_tracker.get_status()
    assert s["last_rollover_at"] is not None


def test_rollover_updates_peak_on_plan_upgrade():
    # Plan upgrade: peak was 5_000, new month starts at 20_000.
    credit_tracker.record(5_000)
    credit_tracker.record(100)            # near depleted
    credit_tracker.record(20_000)         # 200x jump → rollover
    s = credit_tracker.get_status()
    assert s["peak"] == 20_000


def test_no_rollover_on_small_upward_move():
    # 1.4× jump is under the 1.5× threshold — not a rollover.
    credit_tracker.record(700)
    credit_tracker.record(950)            # 950/700 = 1.357
    # Peak still climbs (it always tracks the high-water mark) but
    # last_rollover_at must not be set.
    s = credit_tracker.get_status()
    assert s["peak"] == 950
    assert s.get("last_rollover_at") in (None, s.get("last_updated"))
    # More precise: rollover wasn't recorded for this transition.
    state_path = os.path.join(os.environ["DATA_DIR"], "credit_state.json")
    with open(state_path) as f:
        raw = json.load(f)
    assert "last_rollover_at" not in raw


# ── should_pause_clv() thresholds ──────────────────────────────────

def test_paused_at_exact_threshold(force_thresholds):
    force_thresholds(pause_pct=0.85)
    # peak=10_000, used=8_500 (85%) → paused (>= threshold).
    credit_tracker.record(10_000)
    credit_tracker.record(1_500)
    assert credit_tracker.should_pause_clv() is True


def test_not_paused_just_below_threshold(force_thresholds):
    force_thresholds(pause_pct=0.85)
    # 84.99% used → still active.
    credit_tracker.record(10_000)
    credit_tracker.record(1_501)
    assert credit_tracker.should_pause_clv() is False


def test_not_paused_when_no_data_yet():
    # Fresh install: no record() has been called yet.
    assert credit_tracker.should_pause_clv() is False
    s = credit_tracker.get_status()
    assert s["known"] is False
    assert s["clv_paused"] is False


def test_not_paused_when_only_remaining_no_peak(isolated_data_dir):
    # Edge case: state file has remaining but peak_remaining=0 from a
    # pre-2026-05 install. Treat as unknown rather than dividing by zero.
    state_path = os.path.join(str(isolated_data_dir), "credit_state.json")
    with open(state_path, "w") as f:
        json.dump({"remaining": 100, "peak_remaining": 0,
                   "last_updated": "2026-05-22T00:00:00Z"}, f)
    assert credit_tracker.should_pause_clv() is False


# ── Explicit limit override ────────────────────────────────────────

def test_explicit_monthly_limit_overrides_peak(force_thresholds):
    # The override exists for mid-month new installs: peak hasn't been
    # observed at its true ceiling, so the percentage gate would be
    # too lenient. Here the bot started fresh and saw 8_000 remaining
    # — without the override, 3_000 remaining looks like 62.5% used
    # (8_000 -> 3_000) and CLV stays active. With the override pinning
    # the real plan size at 20_000, the same 3_000 remaining is
    # actually 85% used and CLV correctly pauses.
    force_thresholds(pause_pct=0.85, explicit_limit=20_000)
    credit_tracker.record(8_000)
    credit_tracker.record(3_000)
    s = credit_tracker.get_status()
    assert s["peak"] == 20_000
    assert s["source"] == "explicit"
    assert s["clv_paused"] is True

    # Without the override the same trace would NOT pause — proving
    # the override is the thing that changes the decision.
    force_thresholds(pause_pct=0.85, explicit_limit=None)
    s = credit_tracker.get_status()
    assert s["peak"] == 8_000
    assert s["source"] == "observed"
    assert s["clv_paused"] is False


# ── get_status() shape ─────────────────────────────────────────────

def test_get_status_includes_all_documented_keys():
    credit_tracker.record(5_000)
    credit_tracker.record(1_000)
    s = credit_tracker.get_status()
    for key in ("known", "remaining", "peak", "usage_pct", "threshold_pct",
                "clv_paused", "last_updated", "source"):
        assert key in s, f"missing key: {key}"


# ── CLV worker integration ─────────────────────────────────────────

def test_capture_pending_closes_early_returns_when_paused(
        monkeypatch, force_thresholds):
    force_thresholds(pause_pct=0.85)
    credit_tracker.record(10_000)
    credit_tracker.record(500)            # 95% used → paused
    assert credit_tracker.should_pause_clv() is True

    import closing_lines
    # Sentinel: if early-return works, the file walker is never reached.
    def fail_if_called(*a, **kw):
        raise AssertionError(
            "capture_pending_closes must not scan files when paused")
    monkeypatch.setattr(closing_lines, "_list_recent_pending_files",
                         lambda *a, **kw: ["this/should/never/open.json"])
    monkeypatch.setattr(closing_lines, "_load_pending", fail_if_called)

    stats = closing_lines.capture_pending_closes(output_dir="/tmp/ignored")
    assert stats["credit_paused"] is True
    assert stats["captured"] == 0
