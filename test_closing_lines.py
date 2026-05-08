"""Unit tests for closing-line capture logic.

Closing-line value (CLV) is what tells us if our edge is real, so the
parsing and math here have to be airtight. These tests cover the pure
functions only — no Odds API calls.
"""
import json
import os
import tempfile
from datetime import datetime, timedelta, timezone

import pytest

from closing_lines import (
    BOOK_DISPLAY_TO_KEY,
    INTERNAL_TO_API_MARKET,
    _due_for_capture,
    _outcome_matches,
    _build_close_record,
    _capture_close_for_pick,
    _list_recent_pending_files,
    compute_clv_pct,
    load_closing_lines_for_dates,
)


# ── compute_clv_pct ────────────────────────────────────────────────────

def test_clv_zero_when_open_equals_close():
    # If the line didn't move, CLV is exactly 0
    assert compute_clv_pct(50.0, 50.0) == pytest.approx(0.0)

def test_clv_positive_when_close_implied_higher_than_open():
    # You bet at 50% implied (+100, decent payout). Close moved to 60%
    # implied (-150). The market caught up to your view — your pick
    # became MORE favored — so you got better odds than the close.
    # CLV = (60 - 50) / 60 ≈ +16.667%.
    assert compute_clv_pct(50.0, 60.0) == pytest.approx(16.667, abs=0.01)

def test_clv_negative_when_close_implied_lower_than_open():
    # You bet at 60% implied (-150, paying premium). Close moved to 50%
    # implied (+100). The market moved AWAY from your pick — you
    # overpaid relative to the eventual close. CLV = -20%.
    assert compute_clv_pct(60.0, 50.0) == pytest.approx(-20.0)

def test_clv_none_when_open_missing():
    assert compute_clv_pct(None, 50.0) is None

def test_clv_none_when_close_zero_or_negative():
    # Guard against div-by-zero — bad data shouldn't NaN out the pipeline
    assert compute_clv_pct(50.0, 0.0) is None
    assert compute_clv_pct(50.0, -5.0) is None


# ── INTERNAL_TO_API_MARKET ─────────────────────────────────────────────

def test_market_map_passes_through_game_lines():
    # h2h/spreads/totals are the same key in both worlds
    assert INTERNAL_TO_API_MARKET["h2h"] == "h2h"
    assert INTERNAL_TO_API_MARKET["spreads"] == "spreads"
    assert INTERNAL_TO_API_MARKET["totals"] == "totals"

def test_market_map_resolves_nba_pts_prop():
    # NBA pts prop maps to player_points in the Odds API
    assert INTERNAL_TO_API_MARKET["pts"] == "player_points"

def test_market_map_resolves_mlb_strikeouts():
    assert INTERNAL_TO_API_MARKET["strikeouts"] == "pitcher_strikeouts"


# ── BOOK_DISPLAY_TO_KEY ────────────────────────────────────────────────

def test_book_map_dk_and_fd():
    # Picks store display names; API returns lowercase keys
    assert BOOK_DISPLAY_TO_KEY["DraftKings"] == "draftkings"
    assert BOOK_DISPLAY_TO_KEY["FanDuel"] == "fanduel"


# ── _due_for_capture ───────────────────────────────────────────────────

def _make_pick(commence_offset_min: float, close=None) -> dict:
    """Build a pick whose commence is `commence_offset_min` from now."""
    commence = (datetime.now(timezone.utc)
                + timedelta(minutes=commence_offset_min)).isoformat()
    return {"commence": commence, "close": close}


def test_due_inside_capture_window():
    # 3 min from now → in the 0-5 window
    pick = _make_pick(3)
    assert _due_for_capture(pick, datetime.now(timezone.utc)) is True

def test_not_due_too_far_out():
    # 30 min from now → outside the 5-min window
    pick = _make_pick(30)
    assert _due_for_capture(pick, datetime.now(timezone.utc)) is False

def test_not_due_after_commence():
    # Game already started — we don't fetch in-play prices as the close
    pick = _make_pick(-5)
    assert _due_for_capture(pick, datetime.now(timezone.utc)) is False

def test_not_due_when_already_captured():
    pick = _make_pick(3, close={"odds_american": -110})
    assert _due_for_capture(pick, datetime.now(timezone.utc)) is False

def test_not_due_with_unparseable_commence():
    pick = {"commence": "not-a-date", "close": None}
    assert _due_for_capture(pick, datetime.now(timezone.utc)) is False


# ── _outcome_matches: game lines ───────────────────────────────────────

def test_match_h2h_team_name_case_insensitive():
    pick = {"outcome": "Los Angeles Lakers", "point": None}
    api_outcome = {"name": "los angeles lakers", "price": -150}
    assert _outcome_matches(pick, api_outcome, "h2h") is True

def test_match_h2h_rejects_wrong_team():
    pick = {"outcome": "Lakers", "point": None}
    api_outcome = {"name": "Warriors", "price": +130}
    assert _outcome_matches(pick, api_outcome, "h2h") is False

def test_match_spread_with_matching_point():
    pick = {"outcome": "Lakers", "point": -3.5}
    api_outcome = {"name": "Lakers", "point": -3.5, "price": -110}
    assert _outcome_matches(pick, api_outcome, "spreads") is True

def test_match_spread_rejects_different_point():
    # -3.5 vs -2.5 are different bets even on the same team
    pick = {"outcome": "Lakers", "point": -3.5}
    api_outcome = {"name": "Lakers", "point": -2.5, "price": -110}
    assert _outcome_matches(pick, api_outcome, "spreads") is False

def test_match_totals_over_under():
    pick = {"outcome": "Over", "point": 220.5}
    api_outcome = {"name": "Over", "point": 220.5, "price": -108}
    assert _outcome_matches(pick, api_outcome, "totals") is True

def test_match_within_quarter_point_tolerance():
    # 0.5-bucket tolerance absorbs rounding drift between sources
    pick = {"outcome": "Over", "point": 220.5}
    api_outcome = {"name": "Over", "point": 220.5 + 0.2, "price": -108}
    assert _outcome_matches(pick, api_outcome, "totals") is True


# ── _outcome_matches: player props ─────────────────────────────────────

def test_match_prop_over():
    pick = {"outcome": "LeBron James O", "point": 25.5}
    api_outcome = {
        "name": "Over", "description": "LeBron James",
        "point": 25.5, "price": -110,
    }
    assert _outcome_matches(pick, api_outcome, "player_points") is True

def test_match_prop_under():
    pick = {"outcome": "LeBron James U", "point": 25.5}
    api_outcome = {
        "name": "Under", "description": "LeBron James",
        "point": 25.5, "price": -110,
    }
    assert _outcome_matches(pick, api_outcome, "player_points") is True

def test_match_prop_binary_yes():
    # Binary markets (anytime HR) use Y/N labels
    pick = {"outcome": "Aaron Judge Y", "point": None}
    api_outcome = {
        "name": "Yes", "description": "Aaron Judge",
        "point": None, "price": +250,
    }
    assert _outcome_matches(pick, api_outcome, "batter_home_runs") is True

def test_match_prop_rejects_wrong_player():
    pick = {"outcome": "LeBron James O", "point": 25.5}
    api_outcome = {
        "name": "Over", "description": "Stephen Curry",
        "point": 25.5, "price": -110,
    }
    assert _outcome_matches(pick, api_outcome, "player_points") is False

def test_match_prop_rejects_wrong_side():
    pick = {"outcome": "LeBron James O", "point": 25.5}
    api_outcome = {
        "name": "Under", "description": "LeBron James",
        "point": 25.5, "price": -110,
    }
    assert _outcome_matches(pick, api_outcome, "player_points") is False


# ── _build_close_record ────────────────────────────────────────────────

def test_build_close_records_americanand_clv():
    # Pick was -150 (60% implied, paying premium), close at -110
    # (~52.4% implied). Market moved AWAY from your pick → you got
    # WORSE odds than the close → negative CLV.
    # CLV = (52.38 - 60.0) / 52.38 ≈ -14.55%
    pick = {
        "open_implied_pct": 60.0,
        "commence": (datetime.now(timezone.utc) + timedelta(minutes=4))
                     .isoformat(),
    }
    api_outcome = {"price": -110}
    now = datetime.now(timezone.utc)
    rec = _build_close_record(pick, api_outcome, now)
    assert rec["odds_american"] == -110
    assert rec["implied_pct"] == pytest.approx(52.4, abs=0.1)
    assert rec["clv_pct"] == pytest.approx(-14.55, abs=0.1)
    assert 3.5 < rec["minutes_before_commence"] <= 4.0


def test_clv_sign_matches_real_world_screenshot():
    """Regression test for the sign-inversion bug surfaced 2026-05-07.

    User reported a Diamondbacks -1.5 pick at -128 closing at -154 was
    showing CLV in red. -128 → -154 is the line moving TOWARD the pick
    (more favored at close), so the bettor got better-than-close odds
    and CLV should be POSITIVE.
    """
    open_implied  = 128 / (128 + 100) * 100   # 56.14% (open at -128)
    close_implied = 154 / (154 + 100) * 100   # 60.63% (close at -154)
    clv = compute_clv_pct(open_implied, close_implied)
    assert clv > 0
    assert clv == pytest.approx(7.41, abs=0.05)

def test_build_close_handles_missing_commence():
    pick = {"open_implied_pct": 50.0, "commence": None}
    rec = _build_close_record(pick, {"price": +100}, datetime.now(timezone.utc))
    assert rec["minutes_before_commence"] is None


# ── _capture_close_for_pick: end-to-end matching ───────────────────────

def test_capture_finds_matching_outcome():
    pick = {
        "book": "DraftKings", "market": "h2h",
        "outcome": "Lakers", "point": None,
        "open_implied_pct": 55.0,
        "commence": (datetime.now(timezone.utc) + timedelta(minutes=3))
                     .isoformat(),
    }
    event_data = {
        "bookmakers": [{
            "key": "draftkings",
            "markets": [{
                "key": "h2h",
                "outcomes": [
                    {"name": "Lakers", "price": -150},
                    {"name": "Warriors", "price": +130},
                ],
            }],
        }],
    }
    rec, reason = _capture_close_for_pick(
        pick, event_data, datetime.now(timezone.utc))
    assert rec is not None
    assert reason is None
    assert rec["odds_american"] == -150

def test_capture_returns_none_when_book_missing():
    pick = {"book": "DraftKings", "market": "h2h",
            "outcome": "Lakers", "point": None,
            "open_implied_pct": 55.0, "commence": "2099-01-01T00:00:00Z"}
    event_data = {
        "bookmakers": [{
            "key": "fanduel",  # different book
            "markets": [{
                "key": "h2h",
                "outcomes": [{"name": "Lakers", "price": -150}],
            }],
        }],
    }
    rec, reason = _capture_close_for_pick(
        pick, event_data, datetime.now(timezone.utc))
    assert rec is None
    assert reason is not None

def test_capture_returns_none_for_unknown_book():
    pick = {"book": "BetMGM", "market": "h2h",  # not in BOOK_DISPLAY_TO_KEY
            "outcome": "Lakers", "point": None,
            "open_implied_pct": 55.0, "commence": "2099-01-01T00:00:00Z"}
    event_data = {"bookmakers": []}
    rec, reason = _capture_close_for_pick(
        pick, event_data, datetime.now(timezone.utc))
    assert rec is None
    assert reason is not None


# ── _list_recent_pending_files ─────────────────────────────────────────

def test_list_recent_returns_existing_only(tmp_path):
    from datetime import date
    today = date.today().isoformat()
    p_today = tmp_path / f"pending_closes_{today}.json"
    p_today.write_text("{}")
    p_other = tmp_path / "pending_closes_2020-01-01.json"
    p_other.write_text("{}")
    found = _list_recent_pending_files(str(tmp_path))
    # Should include today, exclude the ancient unrelated file
    assert any(today in p for p in found)
    assert not any("2020-01-01" in p for p in found)


# ── load_closing_lines_for_dates ───────────────────────────────────────

def _write_pending_file(dirpath, date_iso, picks_dict):
    """Helper: write a pending_closes JSON in the given dir."""
    payload = {"scan_timestamp": "2026-05-05T12:00:00Z", "picks": picks_dict}
    path = os.path.join(str(dirpath), f"pending_closes_{date_iso}.json")
    with open(path, "w") as f:
        json.dump(payload, f)
    return path

def test_load_closes_returns_empty_when_no_files(tmp_path):
    lookup = load_closing_lines_for_dates(
        "2026-05-05", "2026-05-06", str(tmp_path))
    assert lookup == {}

def test_load_closes_includes_only_captured(tmp_path):
    # Two picks in one file: one captured, one not. Note: lookup keys
    # are (game_date_yyyymmdd, pick_key) — date is derived from each
    # pick's commence in ET (May 5 22:00 UTC = May 5 18:00 ET).
    _write_pending_file(tmp_path, "2026-05-05", {
        "captured-key": {
            "commence": "2026-05-05T22:00:00Z",
            "close": {"odds_american": -110, "implied_pct": 52.4, "clv_pct": 1.5},
        },
        "uncaptured-key": {
            "commence": "2026-05-05T22:00:00Z",
            "close": None,
        },
    })
    lookup = load_closing_lines_for_dates(
        "2026-05-05", "2026-05-05", str(tmp_path))
    assert ("20260505", "captured-key") in lookup
    assert ("20260505", "uncaptured-key") not in lookup
    assert lookup[("20260505", "captured-key")]["clv_pct"] == 1.5

def test_load_closes_latest_file_wins(tmp_path):
    # Same pick (same date, same key) in two files; user's mental model
    # is "latest scan = canonical open", so the later file's close (with
    # that scan's CLV) should win.
    _write_pending_file(tmp_path, "2026-05-05", {
        "shared-key": {
            "commence": "2026-05-06T22:00:00Z",
            "close": {"odds_american": -110, "implied_pct": 52.4, "clv_pct": 5.0},
        },
    })
    _write_pending_file(tmp_path, "2026-05-06", {
        "shared-key": {
            "commence": "2026-05-06T22:00:00Z",
            "close": {"odds_american": -110, "implied_pct": 52.4, "clv_pct": 8.5},
        },
    })
    lookup = load_closing_lines_for_dates(
        "2026-05-05", "2026-05-06", str(tmp_path))
    # 12 AM May 6 scan would write the May 6 file; its CLV (8.5%) wins.
    assert lookup[("20260506", "shared-key")]["clv_pct"] == 8.5

def test_load_closes_buffers_one_day_each_side(tmp_path):
    # Pick captured by a scan run the day BEFORE the requested window
    # should still come through (scans often run a day ahead).
    _write_pending_file(tmp_path, "2026-05-04", {
        "early-scan-key": {
            "commence": "2026-05-05T22:00:00Z",
            "close": {"odds_american": -120, "implied_pct": 54.5, "clv_pct": 2.0},
        },
    })
    lookup = load_closing_lines_for_dates(
        "2026-05-05", "2026-05-05", str(tmp_path))
    assert ("20260505", "early-scan-key") in lookup

def test_load_closes_no_cross_day_collision(tmp_path):
    """Same matchup on consecutive days must NOT cross-contaminate.

    Real bug seen 2026-05-07: NY Yankees ML on FD was captured at -174
    on 5/6, the same matchup repeated 5/7 with no close yet. Without
    date namespacing the 5/6 close attached to the 5/7 pick (visible
    on the dashboard as a closing line for a game that hadn't started).
    """
    _write_pending_file(tmp_path, "2026-05-06", {
        "yankees-fd-h2h": {
            "commence": "2026-05-06T23:00:00Z",   # ET: May 6
            "close":    {"odds_american": -174, "implied_pct": 63.5, "clv_pct": -2.0},
        },
    })
    _write_pending_file(tmp_path, "2026-05-07", {
        "yankees-fd-h2h": {
            "commence": "2026-05-07T23:00:00Z",   # ET: May 7
            "close":    None,                     # not captured yet
        },
    })
    lookup = load_closing_lines_for_dates(
        "2026-05-06", "2026-05-07", str(tmp_path))
    # May 6 close stays bound to May 6
    assert lookup[("20260506", "yankees-fd-h2h")]["clv_pct"] == -2.0
    # May 7 has no captured close → must NOT inherit May 6's
    assert ("20260507", "yankees-fd-h2h") not in lookup

def test_load_closes_skips_picks_without_commence(tmp_path):
    # A close without a parseable commence is dropped — without a date
    # we can't safely match it to any specific game.
    _write_pending_file(tmp_path, "2026-05-05", {
        "no-commence": {
            "commence": None,
            "close": {"odds_american": -110, "implied_pct": 52.4, "clv_pct": 1.0},
        },
    })
    lookup = load_closing_lines_for_dates(
        "2026-05-05", "2026-05-05", str(tmp_path))
    assert lookup == {}

def test_load_closes_handles_corrupt_json(tmp_path):
    # Don't crash if a file has malformed JSON — log and continue
    bad_path = tmp_path / "pending_closes_2026-05-05.json"
    bad_path.write_text("{this is not valid json")
    lookup = load_closing_lines_for_dates(
        "2026-05-05", "2026-05-05", str(tmp_path))
    assert lookup == {}
