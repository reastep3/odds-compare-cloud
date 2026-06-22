"""Tests for the human-approved gate override store and its consult by
odds_compare._qualifies. The key safety property under test: an empty store is
a no-op, and overrides only ever flip a decision for the exact matching cell."""
import pytest

import gate_overrides as go


@pytest.fixture
def store(tmp_path, monkeypatch):
    """Point the store at a temp file and force overrides ON."""
    monkeypatch.setattr(go, "STORE", str(tmp_path / "gate_overrides.json"))
    monkeypatch.setenv("GATE_OVERRIDES_ENABLED", "1")
    return tmp_path


def test_empty_store_is_noop(store):
    assert go.load() == []
    assert go.decision_override(False, "MLB", "h2h", 2.5) is None


def test_exclude_forces_skip_within_band_only(store):
    go.add({"kind": "EXCLUDE", "is_prop": False, "sport": "TENNIS",
            "market": "h2h", "ev_lo": 2.0, "ev_hi": 2.5})
    assert go.decision_override(False, "TENNIS", "h2h", 2.25) is False
    assert go.decision_override(False, "TENNIS", "h2h", 2.75) is None   # outside band
    assert go.decision_override(False, "TENNIS", "h2h", 2.0) is False   # inclusive low
    assert go.decision_override(False, "MLB", "h2h", 2.25) is None      # wrong sport
    assert go.decision_override(False, "TENNIS", "spreads", 2.25) is None  # wrong market


def test_promote_forces_play_props_sport_agnostic(store):
    go.add({"kind": "PROMOTE", "is_prop": True, "sport": "",
            "market": "reb", "ev_lo": 3.0, "ev_hi": 5.0})
    assert go.decision_override(True, "", "reb", 4.0) is True
    assert go.decision_override(True, "ANYTHING", "reb", 4.0) is True
    assert go.decision_override(True, "", "pts", 4.0) is None           # wrong market
    assert go.decision_override(False, "", "reb", 4.0) is None          # game line, not prop


def test_exclude_wins_conflict(store):
    common = dict(is_prop=False, sport="NBA", market="spreads", ev_lo=2.0, ev_hi=3.0)
    go.add({"kind": "PROMOTE", **common})
    go.add({"kind": "EXCLUDE", **common})
    assert go.decision_override(False, "NBA", "spreads", 2.5) is False


def test_kill_switch_disables_all(store, monkeypatch):
    go.add({"kind": "EXCLUDE", "is_prop": False, "sport": "TENNIS",
            "market": "h2h", "ev_lo": 2.0, "ev_hi": 2.5})
    monkeypatch.setenv("GATE_OVERRIDES_ENABLED", "0")
    assert go.decision_override(False, "TENNIS", "h2h", 2.25) is None


def test_add_dedup_and_remove(store):
    e = go.add({"kind": "EXCLUDE", "is_prop": False, "sport": "TENNIS",
                "market": "h2h", "ev_lo": 2.0, "ev_hi": 2.5})
    go.add({"kind": "EXCLUDE", "is_prop": False, "sport": "TENNIS",
            "market": "h2h", "ev_lo": 2.0, "ev_hi": 2.5})
    assert len(go.load()) == 1                  # dedup by id
    assert go.remove(e["id"]) is True
    assert go.load() == []
    assert go.remove(e["id"]) is False          # already gone


def test_entry_from_proposal_normalizes(store):
    e = go.entry_from_proposal({
        "kind": "EXCLUDE", "is_prop": False, "sport": "tennis", "market": "h2h",
        "ev_lo": 2.0, "ev_hi": 2.5, "n": 28, "flat_roi": -40.6,
        "ci_lo": -76, "ci_hi": -5, "droi": -22, "exact": True})
    assert e["sport"] == "TENNIS"
    assert e["evidence"]["n"] == 28


def test_invalid_entry_rejected(store):
    with pytest.raises(ValueError):
        go.add({"kind": "BOGUS", "is_prop": False, "market": "h2h",
                "ev_lo": 2.0, "ev_hi": 2.5})


def test_ev_none_is_noop(store):
    go.add({"kind": "EXCLUDE", "is_prop": False, "sport": "TENNIS",
            "market": "h2h", "ev_lo": 2.0, "ev_hi": 2.5})
    assert go.decision_override(False, "TENNIS", "h2h", None) is None


def test_qualifies_consults_override(store, monkeypatch):
    """The live gate honours an approved override but is unchanged without one."""
    import odds_compare as oc
    monkeypatch.setattr(oc, "_starts_within_days", lambda *a, **k: True)
    e = {"sport": "TENNIS", "market": "h2h", "ev_pct": 2.75,
         "num_other_books": 10, "pinnacle_anchored": False,
         "commence": "2099-01-01T00:00:00Z"}
    assert oc._qualifies_base(e) is True            # tennis non-Pinn floor is 2.5
    assert oc._qualifies(e) is True                 # no override -> base
    go.add({"kind": "EXCLUDE", "is_prop": False, "sport": "TENNIS",
            "market": "h2h", "ev_lo": 2.5, "ev_hi": 3.0})
    assert oc._qualifies(e) is False                # override flips it
    assert oc._qualifies({**e, "ev_pct": 3.5}) is True  # outside band untouched
