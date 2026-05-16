"""Unit tests for the core betting math in odds_compare.

Sign errors or boundary bugs in these tiny pure functions silently move real
money — keeping a test floor here protects the math during refactors.
"""
import pytest

from odds_compare import (
    american_to_decimal,
    decimal_to_implied_prob,
    calc_ev,
    kelly_bet,
    clamp_bet,
    format_american,
    _fmt_am,
    _fmt_pt,
    KELLY_FRACTION,
    MIN_BET,
)


# ── american_to_decimal ────────────────────────────────────────────────

@pytest.mark.parametrize("american,expected", [
    (100, 2.0),                     # even money
    (-100, 2.0),                    # also even — symmetric pivot
    (-110, 1.9090909090909090909),  # standard -110 juice
    (200, 3.0),                     # 2-to-1
    (-200, 1.5),
    (500, 6.0),
    (-500, 1.2),
])
def test_american_to_decimal_known(american, expected):
    assert american_to_decimal(american) == pytest.approx(expected, rel=1e-9)


# ── decimal_to_implied_prob ────────────────────────────────────────────

@pytest.mark.parametrize("decimal,expected", [
    (2.0, 0.5),
    (1.5, 2/3),
    (3.0, 1/3),
    (4.0, 0.25),
])
def test_decimal_to_implied_prob_known(decimal, expected):
    assert decimal_to_implied_prob(decimal) == pytest.approx(expected)


# ── calc_ev ────────────────────────────────────────────────────────────

def test_calc_ev_zero_at_fair_price():
    # 50% true at decimal 2.0 (+100) — break-even
    assert calc_ev(0.5, 2.0) == pytest.approx(0.0)

def test_calc_ev_positive():
    # 55% true at +100 → 10% edge
    assert calc_ev(0.55, 2.0) == pytest.approx(10.0)

def test_calc_ev_negative():
    # 45% true at +100 → -10% edge
    assert calc_ev(0.45, 2.0) == pytest.approx(-10.0)

def test_calc_ev_long_shot_with_edge():
    # 30% true at +250 (decimal 3.5) → (0.30 * 2.5 - 0.70) * 100 = 5%
    assert calc_ev(0.30, 3.5) == pytest.approx(5.0)


# ── kelly_bet ──────────────────────────────────────────────────────────

def test_kelly_default_fraction_is_half():
    # Half-Kelly is the documented industry-standard for +EV bettors;
    # if this changes, every recommended stake size shifts.
    assert KELLY_FRACTION == 0.5

def test_kelly_zero_at_fair_price():
    assert kelly_bet(0.5, 2.0) == 0.0

def test_kelly_negative_ev_returns_zero():
    # Kelly formula goes negative — clamped to 0, never short.
    assert kelly_bet(0.45, 2.0) == 0.0

def test_kelly_half_default():
    # 60% true at +100: full Kelly = 20%, half-Kelly = 10%
    assert kelly_bet(0.6, 2.0) == pytest.approx(0.10)

def test_kelly_full_when_fraction_one():
    assert kelly_bet(0.6, 2.0, fraction=1.0) == pytest.approx(0.20)

def test_kelly_zero_at_or_below_decimal_one():
    # b = decimal - 1 ≤ 0 means there's no profit to size against.
    assert kelly_bet(0.5, 1.0) == 0
    assert kelly_bet(0.99, 0.5) == 0


# ── clamp_bet ──────────────────────────────────────────────────────────

def test_clamp_zero_or_negative_returns_zero():
    assert clamp_bet(0, "DraftKings") == 0
    assert clamp_bet(-5, "DraftKings") == 0

def test_clamp_below_min_clamps_up():
    assert clamp_bet(0.01, "DraftKings") == MIN_BET["DraftKings"]
    assert clamp_bet(0.01, "FanDuel") == MIN_BET["FanDuel"]

def test_clamp_above_min_unchanged():
    assert clamp_bet(5.00, "DraftKings") == 5.00

def test_clamp_unknown_book_uses_default():
    # Unknown book falls back to a 0.10 default minimum.
    assert clamp_bet(0.01, "UnknownBook") == 0.10


# ── format_american ────────────────────────────────────────────────────

@pytest.mark.parametrize("decimal,expected", [
    (2.0, "+100"),
    (3.0, "+200"),
    (1.5, "-200"),
    (1.909, "-110"),  # standard -110 juice round-trip
])
def test_format_american_known(decimal, expected):
    assert format_american(decimal) == expected


# ── _fmt_am ────────────────────────────────────────────────────────────

def test_fmt_am_none():
    assert _fmt_am(None) == ""

def test_fmt_am_positive_gets_plus_prefix():
    assert _fmt_am(150) == "+150"

def test_fmt_am_negative_kept_as_is():
    assert _fmt_am(-110) == "-110"

def test_fmt_am_string_coerced_to_int():
    assert _fmt_am("150") == "+150"


# ── _fmt_pt ────────────────────────────────────────────────────────────

def test_fmt_pt_none():
    assert _fmt_pt(None) == ""

def test_fmt_pt_positive_spread_gets_plus():
    assert _fmt_pt(7.5, "spreads") == "+7.5"

def test_fmt_pt_negative_spread_kept_as_is():
    assert _fmt_pt(-7.5, "spreads") == "-7.5"

def test_fmt_pt_zero_spread():
    assert _fmt_pt(0, "spreads") == "0"

def test_fmt_pt_total_has_no_sign():
    assert _fmt_pt(7.5, "totals") == "7.5"

def test_fmt_pt_no_market_no_sign():
    assert _fmt_pt(7.5) == "7.5"


# ── _derive_promo_scope ────────────────────────────────────────────────
# Scope derivation drives the /run_promos fetch optimisation. Wrong scope
# = wasted API credits at best, missing picks at worst.

from odds_compare import _derive_promo_scope


def test_scope_none_for_empty_promos():
    assert _derive_promo_scope([]) is None
    assert _derive_promo_scope(None) is None


def test_scope_full_fetch_when_market_is_all():
    # Catch-all market means we can't narrow.
    assert _derive_promo_scope([
        {"book": "FanDuel", "sport": "mlb", "market": "all"}
    ]) is None


def test_scope_full_fetch_when_sport_is_all():
    assert _derive_promo_scope([
        {"book": "FanDuel", "sport": "all", "market": "h2h"}
    ]) is None


def test_scope_full_fetch_for_bonus_bet():
    # Bonus bets need broad pools to find longest-odds picks.
    assert _derive_promo_scope([
        {"book": "DraftKings", "sport": "nba", "market": "h2h",
         "promo_type": "bonus_bet"}
    ]) is None


def test_scope_full_fetch_for_parlay():
    assert _derive_promo_scope([
        {"book": "FanDuel", "sport": "mlb", "market": "h2h",
         "promo_type": "parlay"}
    ]) is None


def test_scope_full_fetch_for_unknown_market():
    # Defensive: if we don't recognise the market, don't guess.
    assert _derive_promo_scope([
        {"book": "FanDuel", "sport": "mlb", "market": "made_up_market"}
    ]) is None


def test_scope_hr_only():
    scope = _derive_promo_scope([
        {"book": "FanDuel", "sport": "mlb", "market": "hr"}
    ])
    assert scope is not None
    assert scope["need_hr"] is True
    assert scope["need_golf"] is False
    assert scope["sports"] == set()
    assert scope["prop_sport_markets"] == {}


def test_scope_game_line():
    scope = _derive_promo_scope([
        {"book": "DraftKings", "sport": "nba", "market": "spreads"}
    ])
    assert scope is not None
    assert scope["sports"] == {"nba"}
    assert scope["need_hr"] is False
    assert scope["prop_sport_markets"] == {}


def test_scope_player_prop_routed_to_correct_sport():
    scope = _derive_promo_scope([
        {"book": "FanDuel", "sport": "mlb", "market": "strikeouts"}
    ])
    assert scope is not None
    assert scope["prop_sport_markets"] == {"mlb": {"strikeouts"}}
    assert scope["sports"] == set()
    assert scope["need_hr"] is False


def test_scope_union_across_multiple_promos():
    scope = _derive_promo_scope([
        {"book": "FanDuel", "sport": "mlb", "market": "h2h"},
        {"book": "FanDuel", "sport": "mlb", "market": "hr"},
        {"book": "DraftKings", "sport": "nba", "market": "pts"},
    ])
    assert scope is not None
    assert scope["sports"] == {"mlb"}
    assert scope["need_hr"] is True
    assert scope["prop_sport_markets"] == {"nba": {"pts"}}


# ── _promo_no_pick_reason ──────────────────────────────────────────────
# The diagnostic message is what the user sees when a promo can't find a
# pick. Each branch points at a different root cause (odds floor, missing
# market, missing sport, dead fetch, dedup), so misclassifying them sends
# the user chasing the wrong fix.

from odds_compare import _promo_no_pick_reason


def _edge(book, sport, market, odds_decimal=2.0, outcome="Team A"):
    """Minimal edge dict that satisfies _promo_matches plumbing."""
    return {
        "book":         book,
        "sport":        sport,
        "market":       market,
        "odds_decimal": odds_decimal,
        "outcome":      outcome,
        "game":         f"{outcome} game",
    }


def test_no_pick_all_claimed_by_other_promos():
    promo = {"book": "FanDuel", "sport": "nba", "market": "h2h"}
    matched_all = [_edge("FanDuel", "NBA", "h2h")]
    reason = _promo_no_pick_reason(promo, matched_all, matched_all, pool=[])
    assert "already" in reason.lower()
    assert "1 matching" in reason


def test_no_pick_min_odds_too_tight():
    # Sport+market match, but every candidate is shorter than min_odds.
    promo = {"book": "FanDuel", "sport": "nba", "market": "h2h",
             "min_odds": +300}
    combined = [
        _edge("FanDuel", "NBA", "h2h", odds_decimal=1.5, outcome="Lakers"),
        _edge("FanDuel", "NBA", "h2h", odds_decimal=1.9, outcome="Knicks"),
    ]
    reason = _promo_no_pick_reason(promo, combined, matched_all=[], pool=[])
    assert "or longer" in reason
    # Longest line surfaced so the user sees the gap.
    assert "Knicks" in reason


def test_no_pick_market_missing_but_sport_present():
    # Sport (MLB) has plenty of game-line data but no HR props today.
    promo = {"book": "FanDuel", "sport": "mlb", "market": "hr"}
    combined = [
        _edge("FanDuel", "MLB", "h2h"),
        _edge("FanDuel", "MLB", "totals"),
        _edge("FanDuel", "MLB", "spreads"),
    ]
    reason = _promo_no_pick_reason(promo, combined, matched_all=[], pool=[])
    assert "MLB" in reason
    assert "HR" in reason
    # Names the markets actually present so the user knows the gap.
    assert "H2H" in reason or "TOTALS" in reason


def test_no_pick_sport_missing_other_sports_loaded():
    # PGA promo but slate has NBA / MLB only — distinct from market mismatch.
    promo = {"book": "DraftKings", "sport": "pga", "market": "golf_top5"}
    combined = [
        _edge("DraftKings", "NBA", "h2h"),
        _edge("DraftKings", "MLB", "totals"),
    ]
    reason = _promo_no_pick_reason(promo, combined, matched_all=[], pool=[])
    assert "PGA" in reason
    assert "fetch" in reason.lower()
    # Other-sport context shown so user can tell the book itself is alive.
    assert "NBA" in reason or "MLB" in reason


def test_no_pick_book_has_no_data():
    # The whole DK fetch failed — no DK outcomes anywhere in combined.
    promo = {"book": "DraftKings", "sport": "nba", "market": "h2h"}
    combined = [
        _edge("FanDuel", "NBA", "h2h"),
        _edge("FanDuel", "MLB", "totals"),
    ]
    reason = _promo_no_pick_reason(promo, combined, matched_all=[], pool=[])
    assert "DraftKings" in reason
    assert "failed" in reason.lower() or "no" in reason.lower()


# ── find_golf_edges / find_player_prop_edges promo_pool side-effect ────
# The parallel pool feeds compute_promo_picks WITHOUT the MIN_EV_PERCENT
# pre-filter — so a 25-50% boost can rank candidates the pure +EV view
# discards. Skipping these gates by accident would silently re-empty PGA
# and prop-boost promos for the user.

from odds_compare import find_golf_edges, find_player_prop_edges


def test_find_golf_edges_promo_pool_captures_sub_min_ev_candidates():
    # Build a long-shot outright where every book agrees on roughly the
    # same implied prob → fair_prob ≈ implied, so EV is near 0 — below
    # MIN_EV_PERCENT=0.5, but the kind of pick a boost would lift.
    # Four canonical books required by find_golf_edges for a reliable
    # fair price; all IDs are real AN_BOOK_IDS entries.
    golf_props = [{
        "player":      "Tommy Fleetwood",
        "tournament":  "PGA Championship",
        "commence":    "2099-01-01T12:00:00Z",
        "sport":       "PGA",
        "market":      "golf_top5",
        "market_disp": "Top 5 Finish",
        # 68=DK, 69=FD, 75=BetMGM, 49=Caesars, 71=BetRivers — 5 canonical
        # books, satisfies the 4+ minimum.
        "book_odds":   {68: 500, 69: 500, 75: 500, 49: 500, 71: 500},
    }]
    promo_pool: list[dict] = []
    edges = find_golf_edges(golf_props, promo_pool=promo_pool)
    # Pure +EV view excludes a fair-priced line; pool still captures it
    # so the boost path can evaluate.
    assert edges == []
    assert len(promo_pool) >= 1
    assert all(e["market"] == "golf_top5" for e in promo_pool)
    # Both DK and FD priced it, so both must appear so the right
    # promo's book can pick the candidate.
    books = {e["book"] for e in promo_pool}
    assert books == {"DraftKings", "FanDuel"}


def test_find_player_prop_edges_promo_pool_captures_sub_min_ev_candidates():
    # Five books all priced an HR yes at +500 (~16.7% implied); after
    # the one-sided fallback in _compute_prop_fair_prob the fair_prob
    # ≈ implied so the line lands at roughly 0% EV — below MIN_EV_PERCENT
    # but exactly the kind of candidate a 30% HR boost should surface.
    props = []
    for book in ("draftkings", "fanduel", "betmgm", "caesars", "betrivers"):
        props.append({
            "player":      "Aaron Judge",
            "game":        "NYY @ BOS",
            "commence":    "2099-01-01T19:00:00Z",
            "sport":       "MLB",
            "market":      "hr",
            "market_disp": "Anytime HR",
            "line":        0.5,
            "side":        "over",
            "book_odds":   {book: 500},
        })
    promo_pool: list[dict] = []
    edges = find_player_prop_edges(props, promo_pool=promo_pool)
    # Should not reach the +EV-only return…
    assert edges == []
    # …but the parallel pool must hold the boost-eligible candidates.
    assert len(promo_pool) >= 1
    assert all(e["market"] == "hr" for e in promo_pool)
    books = {e["book"] for e in promo_pool}
    assert books == {"DraftKings", "FanDuel"}


def test_find_player_prop_edges_default_promo_pool_none_no_side_effect():
    # Default behaviour (no promo_pool kwarg) is unchanged — callers like
    # run_headless that don't compute promo picks shouldn't pay any cost.
    props = []
    for book in ("draftkings", "fanduel", "betmgm", "caesars", "betrivers"):
        props.append({
            "player":      "Aaron Judge",
            "game":        "NYY @ BOS",
            "commence":    "2099-01-01T19:00:00Z",
            "sport":       "MLB",
            "market":      "hr",
            "market_disp": "Anytime HR",
            "line":        0.5,
            "side":        "over",
            "book_odds":   {book: 500},
        })
    # Passing nothing should not raise and should not crash on a missing
    # parallel pool.
    assert find_player_prop_edges(props) == []
