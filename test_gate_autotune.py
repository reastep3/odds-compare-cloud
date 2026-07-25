"""Tests for the gate auto-tuner's exact inference and live-gate probes.

Two 2026-07-25 fixes under guard here:
  1. pb_pvalues — proposals must clear an exact Poisson-binomial test,
     not just the (anti-conservative) Wald CI.
  2. Probe dicts — the Jul-9 odds band made price-less probes auto-fail,
     silently disabling EXCLUDE proposals for two Sundays.
"""
import csv

import pytest

import gate_autotune as ga


# ── pb_pvalues exact math ──────────────────────────────────────────────

def _row(won, dec):
    return {"won": won, "dec": dec}


def test_pb_pvalues_even_odds_exact():
    # Two picks at decimal 2.0 (fair 50%): W has distribution
    # {0: .25, 1: .5, 2: .25}.
    picks = [_row(0, 2.0), _row(0, 2.0)]
    p_under, p_over = ga.pb_pvalues(picks)
    assert p_under == pytest.approx(0.25)
    assert p_over == pytest.approx(1.0)

    picks = [_row(1, 2.0), _row(1, 2.0)]
    p_under, p_over = ga.pb_pvalues(picks)
    assert p_under == pytest.approx(1.0)
    assert p_over == pytest.approx(0.25)


def test_pb_pvalues_mixed_odds():
    # One heavy favorite (dec 1.25, p=.8) lost, one dog (dec 4.0, p=.25)
    # lost: P(W=0) = .2 * .75 = .15 → p_under = .15.
    picks = [_row(0, 1.25), _row(0, 4.0)]
    p_under, p_over = ga.pb_pvalues(picks)
    assert p_under == pytest.approx(0.15)
    assert p_over == pytest.approx(1.0)


# ── Live-gate probes ───────────────────────────────────────────────────

def test_probe_reflects_live_gate_base_rules():
    # In-band, near-term, well-referenced probe must mirror _qualifies:
    assert ga.gate_plays(False, "MLB", "spreads", 2.5) is True
    assert ga.gate_plays(False, "MLB", "spreads", 6.0) is False  # EV cap
    assert ga.gate_plays(False, "EPL", "h2h", 3.0) is False      # blocked
    assert ga.gate_plays(True, "", "pts", 3.5) is True
    assert ga.gate_plays(True, "", "strikeouts", 2.0) is False   # floor 4


# ── End-to-end: EXCLUDE proposal fires and carries exact_p ─────────────

def test_exclude_proposal_requires_and_reports_exact_p(tmp_path):
    # 45 settled MLB spreads picks at -110 in the 2-2.5% band, all
    # losses: the gate plays this cohort, the data says stop — both the
    # CI and the exact test agree, so an EXCLUDE must fire.
    path = tmp_path / "training.csv"
    cols = ["won", "odds", "ev", "is_prop", "market", "sport",
            "bet", "pnl", "clv_pct", "game_date"]
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for i in range(45):
            w.writerow({"won": "0", "odds": "-110", "ev": "2.2",
                        "is_prop": "0", "market": "spreads",
                        "sport": "MLB", "bet": "10", "pnl": "-10",
                        "clv_pct": "", "game_date": "2026-07-01"})
    text, props = ga.analysis_report(str(path))
    excl = [p for p in props if p["kind"] == "EXCLUDE"
            and p["sport"] == "MLB" and p["market"] == "spreads"]
    assert len(excl) == 1
    assert excl[0]["exact_p"] < 0.001          # 45 straight losses
    assert "PRE-REGISTERED DECISIONS" in text  # Sunday DM carries the block
    assert "2026-08-08" in text
