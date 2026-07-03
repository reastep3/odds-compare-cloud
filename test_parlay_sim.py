"""Tests for parlay/SGP logic: correlation guardrails, the live parlay
builder's game-uniqueness rules, the backtest parlay simulation, and the
Telegram formatting around them.

Parlay math compounds — a bad leg pairing or a mispriced push silently
distorts every simulated ROI number the user reads, so the rules get a
test floor like the core betting math does.
"""
from datetime import datetime, timedelta, timezone

import pytest

from odds_compare import (
    _sgp_legs_compatible,
    _sgp_combo_ok,
    _build_parlay_rows,
)
from backtest import (
    _sim_fair_prob,
    _sim_price,
    _sim_day_pool,
    _grade_sim_parlay,
    simulate_parlays,
    _PARLAY_SIM_UNIT,
)


# ── SGP correlation guardrails ─────────────────────────────────────────

def _leg(market, outcome):
    return {"market": market, "outcome": outcome}


@pytest.mark.parametrize("a,b,expected", [
    (_leg("h2h", "Lakers"),   _leg("spreads", "Lakers"),  False),  # near-dup
    (_leg("h2h", "Lakers"),   _leg("spreads", "Celtics"), False),  # near-contradiction
    (_leg("totals", "Over"),  _leg("totals_1st", "Over"), False),  # same pace
    (_leg("pts", "LeBron James"), _leg("reb", "LeBron James"), False),
    (_leg("pts", "LeBron James"), _leg("pts", "Jayson Tatum"), True),
    (_leg("h2h", "Lakers"),   _leg("totals", "Over"),     True),
    (_leg("h2h", "Lakers"),   _leg("pts", "LeBron James"), True),
    (_leg("spreads", "Lakers"), _leg("totals_1st", "Under"), True),
])
def test_sgp_legs_compatible(a, b, expected):
    assert _sgp_legs_compatible(a, b) is expected
    assert _sgp_legs_compatible(b, a) is expected  # symmetric


def test_sgp_combo_ok_rejects_any_bad_pair():
    good = [_leg("h2h", "Lakers"), _leg("totals", "Over"),
            _leg("pts", "LeBron James")]
    assert _sgp_combo_ok(good)
    assert not _sgp_combo_ok(good + [_leg("spreads", "Lakers")])


# ── Live parlay builder ────────────────────────────────────────────────

def _edge(game, market, outcome, dec=1.6, fair=68.0, book="DraftKings"):
    commence = (datetime.now(timezone.utc)
                + timedelta(minutes=30)).isoformat().replace("+00:00", "Z")
    return {
        "book": book, "sport": "nba", "sport_key": "basketball_nba",
        "game": game, "market": market, "outcome": outcome, "point": None,
        "odds_decimal": dec, "odds_american": 100, "fair_prob": fair,
        "ev_pct": 5.0, "kelly_pct": 2.0, "commence": commence,
    }


def _parlay_promo(**over):
    promo = {
        "label": "30% parlay boost", "book": "DraftKings",
        "promo_type": "parlay", "boost_pct": 30, "num_legs": 3,
        "sport": "all", "market": "all",
    }
    promo.update(over)
    return promo


def test_cross_game_parlay_takes_one_leg_per_game():
    # Two legs in Game A: if both were combined the book would reprice the
    # ticket as an SGP, so the builder must take at most one of them.
    edges = [
        _edge("A vs B", "h2h",    "A", fair=72.0),
        _edge("A vs B", "totals", "Over", fair=71.0),
        _edge("C vs D", "h2h",    "C", fair=70.0),
        _edge("E vs F", "h2h",    "E", fair=69.0),
        _edge("G vs H", "h2h",    "G", fair=68.0),
    ]
    rows = _build_parlay_rows(edges, [_parlay_promo()], 1000, 1000)
    assert rows and rows[0]["legs"] is not None
    games = [l["game"] for l in rows[0]["legs"]]
    assert len(games) == len(set(games))


def test_sgp_parlay_respects_guardrails():
    # One game, five legs: ML+spread can never appear together, and the
    # two LeBron props can never appear together.
    edges = [
        _edge("A vs B", "h2h",     "A", fair=72.0),
        _edge("A vs B", "spreads", "A", fair=71.5),
        _edge("A vs B", "totals",  "Over", fair=71.0),
        _edge("A vs B", "pts",     "LeBron James", fair=70.0),
        _edge("A vs B", "reb",     "LeBron James", fair=69.0),
    ]
    rows = _build_parlay_rows(edges, [_parlay_promo(sgp=True)], 1000, 1000)
    assert rows and rows[0]["legs"] is not None
    assert _sgp_combo_ok(rows[0]["legs"])


# ── Backtest parlay simulation ─────────────────────────────────────────

def _pick(game, market, pick, result, dec=1.6, ev=5.0,
          book="DraftKings", day="20260615"):
    return {
        "game": game, "market": market, "pick": pick, "result": result,
        "decimal": dec, "ev_pct": ev, "book": book, "game_date": day,
        "sport": "nba",
    }


def test_sim_fair_prob_inverts_ev():
    # ev = (fair × dec − 1) × 100 → fair = (1 + ev/100) / dec
    assert _sim_fair_prob({"decimal": 2.0, "ev_pct": 5.0}) == pytest.approx(0.525)
    assert _sim_fair_prob({"decimal": 0, "ev_pct": 5.0}) == 0.0


def test_sim_price_haircut_then_boost():
    # dec 4.0 → 20% haircut on the payout → 1 + 3.0·0.8 = 3.4
    # → 25% boost on the profit → 1 + 2.4·1.25 = 4.0
    assert _sim_price(4.0, 0, 0) == pytest.approx(4.0)
    assert _sim_price(4.0, 25, 20) == pytest.approx(4.0)
    assert _sim_price(4.0, 0, 20) == pytest.approx(3.4)
    assert _sim_price(4.0, 25, 0) == pytest.approx(4.75)


def test_grade_sim_parlay_push_reprices():
    combo = [
        _pick("A vs B", "H2H", "A", "WIN", dec=1.5),
        _pick("C vs D", "H2H", "C", "PUSH", dec=1.6),
        _pick("E vs F", "H2H", "E", "WIN", dec=2.0),
    ]
    result, pnl, priced = _grade_sim_parlay(combo, 0, 0)
    assert result == "WIN"
    assert priced == pytest.approx(3.0)          # 1.5 × 2.0, push dropped
    assert pnl == pytest.approx(_PARLAY_SIM_UNIT * 2.0)

    combo[2]["result"] = "LOSS"
    result, pnl, _ = _grade_sim_parlay(combo, 0, 0)
    assert result == "LOSS" and pnl == -_PARLAY_SIM_UNIT

    all_push = [_pick("A vs B", "H2H", "A", "PUSH")]
    assert _grade_sim_parlay(all_push, 0, 0) == ("PUSH", 0.0, 1.0)


def test_sim_day_pool_unique_games_dedup():
    legs = [
        _pick("A vs B", "H2H", "A", "WIN", ev=8.0),
        _pick("A vs B", "TOTALS", "Over", "WIN", ev=3.0),
        _pick("C vs D", "H2H", "C", "WIN", ev=5.0),
    ]
    pool = _sim_day_pool(legs, unique_games=True)
    assert len(pool) == 2
    assert {p["game"] for p in pool} == {"A vs B", "C vs D"}
    # keeps the higher-fair-prob leg of the duplicated game
    assert next(p for p in pool if p["game"] == "A vs B")["ev_pct"] == 8.0


def test_simulate_parlays_cross_game():
    # 3 winners (higher fair prob) + 1 loser (lower) in one day: the best
    # 3-leg combo by pick-time EV is the three high-fair legs — which is a
    # selection made blind to results, and here they happen to all win.
    picks = [
        _pick("A vs B", "H2H", "A", "WIN",  ev=8.0),
        _pick("C vs D", "H2H", "C", "WIN",  ev=7.0),
        _pick("E vs F", "H2H", "E", "WIN",  ev=6.0),
        _pick("G vs H", "H2H", "G", "LOSS", ev=1.0),
    ]
    sim = simulate_parlays(picks)
    row = next(r for r in sim["summary"]
               if (r["kind"], r["n_legs"], r["boost_pct"]) == ("cross", 3, 25))
    assert row["parlays"] == 1 and row["wins"] == 1
    priced = _sim_price(1.6 ** 3, 25, 0)
    assert row["pnl"] == pytest.approx(
        round(_PARLAY_SIM_UNIT * (priced - 1), 2))
    assert row["roi_pct"] > 0


def test_simulate_parlays_sgp_haircut_lowers_payout():
    picks = [
        _pick("A vs B", "H2H", "A", "WIN", ev=8.0),
        _pick("A vs B", "PTS", "LeBron James", "WIN", ev=7.0),
        _pick("A vs B", "PTS", "Jayson Tatum", "WIN", ev=6.0),
        _pick("A vs B", "SPREADS", "A", "WIN", ev=9.0),
    ]
    sim = simulate_parlays(picks)
    rows = {r["haircut_pct"]: r for r in sim["summary"]
            if (r["kind"], r["n_legs"], r["boost_pct"]) == ("sgp", 3, 30)}
    assert rows[0]["parlays"] == 1
    # ML+spread can't combine, so the winning ticket exists at every cut
    # that stays +EV — and a deeper cut always pays less.
    placed = [h for h in sorted(rows) if rows[h]["parlays"]]
    for lo, hi in zip(placed, placed[1:]):
        assert rows[hi]["pnl"] < rows[lo]["pnl"]


def test_simulate_parlays_selection_is_blind_to_results():
    # A huge-decimal loser must not be excluded because it lost — it's
    # excluded only if the leg cap says so. Here it passes the cap and has
    # the best pick-time EV, so the sim takes it and eats the loss.
    picks = [
        _pick("A vs B", "H2H", "A", "LOSS", dec=2.4, ev=12.0),
        _pick("C vs D", "H2H", "C", "WIN",  ev=6.0),
        _pick("E vs F", "H2H", "E", "WIN",  ev=5.0),
        _pick("G vs H", "H2H", "G", "WIN",  ev=1.0),
    ]
    sim = simulate_parlays(picks)
    row = next(r for r in sim["summary"]
               if (r["kind"], r["n_legs"], r["boost_pct"]) == ("cross", 3, 30))
    assert row["parlays"] == 1
    assert row["wins"] == 0 and row["pnl"] == -_PARLAY_SIM_UNIT


# ── Telegram formatting ────────────────────────────────────────────────

def test_format_parlay_picks_numbers_rows_and_flags_sgp():
    from telegram_bot import format_parlay_picks
    legs = [{"sport": "nba", "game": "A vs B", "market": "h2h",
             "outcome": "A", "point": None, "odds_american": 100}]
    rows = [
        {"promo": {"label": "boost", "book": "DraftKings", "sgp": False},
         "legs": legs, "num_legs": 1, "parlay_american": "+100",
         "boost_american": "+130", "unboosted_ev": 2.0, "boosted_ev": 8.0,
         "parlay_fair_prob": 52.0, "bet": 10.0, "profit": 13.0},
        {"promo": {"label": "sgp boost", "book": "DraftKings", "sgp": True},
         "legs": legs, "num_legs": 1, "parlay_american": "+100",
         "boost_american": "+130", "unboosted_ev": 2.0, "boosted_ev": 8.0,
         "parlay_fair_prob": 52.0, "bet": 10.0, "profit": 13.0},
    ]
    msg = format_parlay_picks(rows)
    assert "<b>1. boost" in msg and "<b>2. sgp boost" in msg
    assert msg.count("/sgpcheck") == 1          # only the SGP row
    assert "/sgpcheck 2" in msg                 # numbered like the display
    assert "EV +2.0% -&gt; +8.0%" in msg


def test_format_parlay_sim_results():
    from telegram_bot import format_parlay_sim_results
    sim = {"summary": [
        {"kind": "cross", "n_legs": 3, "boost_pct": 30, "haircut_pct": 0,
         "parlays": 10, "wins": 3, "pushes": 0, "staked": 100.0,
         "pnl": 12.0, "roi_pct": 12.0},
        {"kind": "sgp", "n_legs": 3, "boost_pct": 30, "haircut_pct": 20,
         "parlays": 5, "wins": 1, "pushes": 0, "staked": 50.0,
         "pnl": -8.0, "roi_pct": -16.0},
        {"kind": "sgp", "n_legs": 3, "boost_pct": 30, "haircut_pct": 0,
         "parlays": 6, "wins": 2, "pushes": 0, "staked": 60.0,
         "pnl": 9.0, "roi_pct": 15.0},
    ], "detail": {}}
    msg = format_parlay_sim_results(sim)
    assert "Cross-game" in msg and "SGP" in msg
    assert "3-leg +30%: 3W/10 tickets | ROI +12.0%" in msg
    assert "0/20% cut" in msg
    assert format_parlay_sim_results(None) is None
    assert format_parlay_sim_results({"summary": []}) is None
