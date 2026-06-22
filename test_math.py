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


def test_scope_hr_routes_to_odds_api_mlb_prop_fetch():
    # Post-2026-05-17 migration: HR is no longer a separate Action Network
    # pipeline. It rides the Odds API batter_home_runs market alongside
    # other MLB props, so an HR-targeted promo lands in prop_sport_markets
    # rather than the old need_hr flag (which no longer exists in scope).
    scope = _derive_promo_scope([
        {"book": "FanDuel", "sport": "mlb", "market": "hr"}
    ])
    assert scope is not None
    assert scope["prop_sport_markets"] == {"mlb": {"hr"}}
    assert scope["sports"] == set()
    assert scope["need_golf"] is False
    assert "need_hr" not in scope, "need_hr should be dropped post-AN-migration"


def test_scope_game_line():
    scope = _derive_promo_scope([
        {"book": "DraftKings", "sport": "nba", "market": "spreads"}
    ])
    assert scope is not None
    assert scope["sports"] == {"nba"}
    assert scope["prop_sport_markets"] == {}


def test_scope_player_prop_routed_to_correct_sport():
    scope = _derive_promo_scope([
        {"book": "FanDuel", "sport": "mlb", "market": "strikeouts"}
    ])
    assert scope is not None
    assert scope["prop_sport_markets"] == {"mlb": {"strikeouts"}}
    assert scope["sports"] == set()


from odds_compare import _promos_want_hr


def test_promos_want_hr_empty_list_returns_false():
    # /run with no promos and no scope should not trigger an HR fetch —
    # nothing downstream would consume the data.
    assert _promos_want_hr([]) is False
    assert _promos_want_hr(None) is False


def test_promos_want_hr_explicit_hr_promo():
    assert _promos_want_hr([
        {"book": "FanDuel", "sport": "mlb", "market": "hr"}
    ]) is True


def test_promos_want_hr_mlb_all_market_promo_no_longer_triggers():
    # Tightened 2026-05-26: a generic "fd 30% mlb" boost with market=all
    # does NOT trigger the HR fetch. _promo_matches has always blocked
    # HR edges from non-HR-market promos, so the data was never used.
    assert _promos_want_hr([
        {"book": "FanDuel", "sport": "mlb", "market": "all"}
    ]) is False


def test_promos_want_hr_all_sport_all_market_promo_no_longer_triggers():
    # Tightened 2026-05-26: catch-all bonus_bet does NOT trigger HR.
    # Picker's _promo_matches blocks HR edges for non-HR-market promos,
    # so fetching them was burning credits with zero downstream use.
    assert _promos_want_hr([
        {"book": "FanDuel", "sport": "all", "market": "all",
         "promo_type": "bonus_bet"}
    ]) is False


def test_promos_want_hr_unrelated_promo_returns_false():
    # An NBA-points promo can't possibly apply to MLB HR — gate fires.
    assert _promos_want_hr([
        {"book": "DraftKings", "sport": "nba", "market": "pts"}
    ]) is False


def test_promos_want_hr_mlb_strikeouts_returns_false():
    # MLB strikeouts is a pitcher prop — wrong market, can't apply to HR.
    assert _promos_want_hr([
        {"book": "FanDuel", "sport": "mlb", "market": "strikeouts"}
    ]) is False


def test_promos_want_hr_mixed_list_any_match_wins():
    # If even one promo applies, fetch HR — the gate is conservative.
    assert _promos_want_hr([
        {"book": "DraftKings", "sport": "nba", "market": "pts"},
        {"book": "FanDuel", "sport": "mlb", "market": "hr"},
    ]) is True


def test_scope_union_across_multiple_promos():
    scope = _derive_promo_scope([
        {"book": "FanDuel", "sport": "mlb", "market": "h2h"},
        {"book": "FanDuel", "sport": "mlb", "market": "hr"},
        {"book": "DraftKings", "sport": "nba", "market": "pts"},
    ])
    assert scope is not None
    assert scope["sports"] == {"mlb"}
    # HR is now an MLB prop alongside strikeouts/total_bases etc; the
    # union groups it with pts under prop_sport_markets.
    assert scope["prop_sport_markets"] == {"mlb": {"hr"}, "nba": {"pts"}}


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


def test_no_pick_hr_fetch_returned_empty():
    # HR-specific diagnostic: when no HR edges exist in combined at all,
    # the message names the fetch as the culprit rather than listing
    # game-line markets that wouldn't satisfy a HR promo anyway.
    # Post-2026-05-17: message references the Odds API batter_home_runs
    # market (the AN pipeline was retired).
    promo = {"book": "FanDuel", "sport": "mlb", "market": "hr"}
    combined = [
        _edge("FanDuel", "MLB", "h2h"),
        _edge("FanDuel", "MLB", "totals"),
        _edge("FanDuel", "MLB", "spreads"),
    ]
    reason = _promo_no_pick_reason(promo, combined, matched_all=[], pool=[])
    assert "HR" in reason
    # Root cause must point at the fetch, not at the game-line markets.
    assert "fetch" in reason.lower() or "no MLB HR data" in reason
    # New diagnostic references the Odds API by name so the user
    # doesn't try to debug the retired Action Network path.
    assert "Odds API" in reason or "batter_home_runs" in reason


def test_no_pick_hr_no_coverage_on_target_book():
    # HR fetch produced edges, but every line is on DK — none on FD.
    # The diagnostic must say so explicitly so the user knows the
    # other book has data; intuiting from "no HR data" would be wrong.
    promo = {"book": "FanDuel", "sport": "mlb", "market": "hr"}
    combined = [
        _edge("DraftKings", "MLB", "hr", outcome="Aaron Judge"),
        _edge("DraftKings", "MLB", "hr", outcome="Shohei Ohtani"),
    ]
    reason = _promo_no_pick_reason(promo, combined, matched_all=[], pool=[])
    assert "FanDuel" in reason
    assert "DraftKings" in reason
    assert "HR" in reason


def test_no_pick_hr_book_edges_exist_all_claimed():
    # FD has HR coverage AND matches the promo's filters — but every
    # HR candidate was already consumed by an earlier higher-boost promo.
    promo = {"book": "FanDuel", "sport": "mlb", "market": "hr"}
    fd_hr_edges = [
        _edge("FanDuel", "MLB", "hr", outcome="Aaron Judge"),
        _edge("FanDuel", "MLB", "hr", outcome="Shohei Ohtani"),
    ]
    reason = _promo_no_pick_reason(
        promo, combined=fd_hr_edges, matched_all=fd_hr_edges, pool=[])
    # The generic "All N matching candidate(s) already assigned" runs
    # first (matched_all non-empty); HR-specific path only activates
    # when matched_all is empty.
    assert "already" in reason.lower()


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

from odds_compare import find_golf_edges, find_player_prop_edges, GOLF_DISABLED


# The three find_golf_edges tests below assert internal behaviors (promo
# pool population, McNealy outlier rejection, Higa outlier rejection)
# that only matter when golf is active. The 2026-05-22 Si Woo Kim
# incident turned GOLF_DISABLED on; when it flips off, these tests must
# pass again as the safety net that re-enables golf relies on.
_skip_if_golf_off = pytest.mark.skipif(
    GOLF_DISABLED, reason="GOLF_DISABLED — golf pipeline is intentionally off."
)


def test_scope_golf_only_promo_returns_empty_not_full_fetch():
    # GOLF_DISABLED cleanup: a golf-only promo must NOT collapse the
    # scope to None (which would trigger a full-slate Odds API fetch
    # and cost real credits). Instead the golf market is silently
    # dropped — the scope ends up empty and run_headless_fetch
    # produces nothing, so the promo just reports "no pick".
    if not GOLF_DISABLED:
        pytest.skip("Only meaningful while GOLF_DISABLED is True.")
    from odds_compare import _derive_promo_scope
    scope = _derive_promo_scope([
        {"book": "DraftKings", "sport": "pga", "market": "golf_win"}
    ])
    assert scope is not None, (
        "Golf-only promo should produce an empty scope, NOT None "
        "(None triggers a full-slate Odds API fetch)."
    )
    assert scope["sports"] == set()
    assert scope["prop_sport_markets"] == {}
    assert scope["need_golf"] is False, (
        "GOLF_DISABLED means need_golf must stay False even when a "
        "golf-market promo is in the list."
    )


def test_find_golf_edges_returns_empty_when_disabled():
    # Kill-switch regression. Even if a caller constructs perfectly valid
    # golf props (and a promo pool), find_golf_edges must return [] and
    # leave the pool untouched. Without this guard, a stale +1275 line
    # could still surface as a 100%+ boosted EV promo pick.
    if not GOLF_DISABLED:
        pytest.skip("Only meaningful while GOLF_DISABLED is True.")
    golf_props = [{
        "player":      "Si Woo Kim",
        "tournament":  "THE CJ CUP Byron Nelson",
        "commence":    "2099-01-01T12:00:00Z",
        "sport":       "PGA",
        "market":      "golf_win",
        "market_disp": "Tournament Winner",
        "book_odds":   {68: 1275, 69: 600, 75: 600, 49: 600, 71: 600},
    }]
    promo_pool: list[dict] = []
    edges = find_golf_edges(golf_props, promo_pool=promo_pool)
    assert edges == []
    assert promo_pool == []


@_skip_if_golf_off
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


# ── parse_promo HR keyword/phrase coverage ─────────────────────────────
# Regression: "fd mlb 30% home run max:$10" parsed as market="all"
# because PROMO_MARKET_PHRASES only knew about "first inning" / "1st
# inning". The scanner then awarded the promo to a generic MLB spread
# instead of an HR prop, defeating the entire point of an HR-targeted
# boost.

from telegram_bot import parse_promo


def test_parse_promo_two_word_home_run():
    promo = parse_promo("fd mlb 30% home run max:$10")
    assert promo is not None
    assert promo["book"]      == "FanDuel"
    assert promo["sport"]     == "mlb"
    assert promo["market"]    == "hr"
    assert promo["boost_pct"] == 30


def test_parse_promo_two_word_home_runs_plural():
    promo = parse_promo("dk 25% mlb home runs")
    assert promo is not None
    assert promo["market"] == "hr"


def test_parse_promo_single_token_homer_slang():
    promo = parse_promo("fd 30% mlb homer")
    assert promo is not None
    assert promo["market"] == "hr"


# ── parse_promo total-bases keyword/phrase coverage ───────────────────
# Regression: "dk 50% total bases mlb" parsed as market="totals" (game
# over/under) because the bare "total" keyword fired before any phrase
# matched, and "bases" was dropped. A total-bases boost would then land
# on a game total instead of the batter prop. "total bases" must be a
# two-word phrase so the lookahead beats the single-token mapping.

def test_parse_promo_two_word_total_bases():
    promo = parse_promo("dk 50% total bases mlb")
    assert promo is not None
    assert promo["book"]      == "DraftKings"
    assert promo["sport"]     == "mlb"
    assert promo["market"]    == "total_bases"
    assert promo["boost_pct"] == 50


def test_parse_promo_total_base_singular():
    promo = parse_promo("dk 25% total base")
    assert promo is not None
    assert promo["market"] == "total_bases"


def test_parse_promo_batter_tb_phrase():
    promo = parse_promo("fd 50% batter tb")
    assert promo is not None
    assert promo["market"] == "total_bases"


def test_parse_promo_bare_total_still_game_totals():
    # The fix must NOT regress the existing bare "total" → game O/U mapping.
    promo = parse_promo("dk 20% mlb total")
    assert promo is not None
    assert promo["market"] == "totals"


# ── parse_promo player-points keyword/phrase coverage ─────────────────
# FanDuel sends "30% profit boost on a player points prop" — a recurring
# NBA-themed promo. The parser must recognize the two-word "player
# points" phrase, single-token "points"/"pts", and remap to "nhl_pts"
# when the user pinned the sport to NHL.

def test_parse_promo_two_word_player_points():
    promo = parse_promo("fd 30% player points")
    assert promo is not None
    assert promo["book"]      == "FanDuel"
    assert promo["market"]    == "pts"
    assert promo["boost_pct"] == 30


def test_parse_promo_single_token_points():
    promo = parse_promo("fd 30% nba points")
    assert promo is not None
    assert promo["sport"]  == "nba"
    assert promo["market"] == "pts"


def test_parse_promo_player_points_nhl_remaps_to_nhl_pts():
    promo = parse_promo("fd 30% nhl player points")
    assert promo is not None
    assert promo["sport"]  == "nhl"
    assert promo["market"] == "nhl_pts"


def test_parse_promo_player_points_with_filler():
    # Near-verbatim FanDuel wording: "30% profit boost on a player points prop"
    promo = parse_promo("fd 30% profit boost player points")
    assert promo is not None
    assert promo["market"]    == "pts"
    assert promo["boost_pct"] == 30


# ── Stale-target-line outlier guard ────────────────────────────────────
# Regression: a stale +10500 DK line on Maverick McNealy with the
# consensus around +1500 produced a 373% pseudo-EV pick. Caused real
# user-facing harm because the boosted promo path surfaced it. The
# outlier guard rejects any target line whose implied prob is below
# MIN_TARGET_OUTLIER_PROB_RATIO * consensus_fair_prob.

@_skip_if_golf_off
def test_find_golf_edges_rejects_outlier_target_book():
    # DK is at a stale +10500 while every other book agrees on +500.
    # The target's implied prob (~0.94%) is 5% of the consensus (~16.7%),
    # well below the 0.4 ratio threshold — must be discarded from BOTH
    # the returned edges and the promo_pool so it never reaches the
    # scanner via either path.
    golf_props = [{
        "player":      "Maverick McNealy",
        "tournament":  "PGA Championship",
        "commence":    "2099-01-01T12:00:00Z",
        "sport":       "PGA",
        "market":      "golf_win",
        "market_disp": "Tournament Winner",
        # 68=DK stale at +10500; 69=FD/75=BetMGM/49=Caesars/71=BetRivers at +500.
        "book_odds":   {68: 10500, 69: 500, 75: 500, 49: 500, 71: 500},
    }]
    promo_pool: list[dict] = []
    edges = find_golf_edges(golf_props, promo_pool=promo_pool)
    # DK must not appear anywhere — not in +EV edges, not in promo pool.
    assert not any(e["book"] == "DraftKings" for e in edges)
    assert not any(e["book"] == "DraftKings" for e in promo_pool)
    # FD's clean +500 line is fair-priced, so it lands in the promo pool
    # (for boost evaluation) but not in the +EV edges (EV ≈ 0).
    fd_in_pool = [e for e in promo_pool if e["book"] == "FanDuel"]
    assert len(fd_in_pool) == 1


def test_find_player_prop_edges_rejects_outlier_target_book():
    # DK stale at +10500 on an anytime HR while four other books at +500.
    # _compute_prop_fair_prob excludes the book being evaluated, so the
    # consensus seen when evaluating DK is the clean 4-book median.
    props = [{
        "player": "Aaron Judge", "game": "NYY @ BOS",
        "commence": "2099-01-01T19:00:00Z", "sport": "MLB",
        "market": "hr", "market_disp": "Anytime HR",
        "line": 0.5, "side": "over",
        "book_odds": {"draftkings": 10500},
    }]
    for book in ("fanduel", "betmgm", "caesars", "betrivers"):
        props.append({
            "player": "Aaron Judge", "game": "NYY @ BOS",
            "commence": "2099-01-01T19:00:00Z", "sport": "MLB",
            "market": "hr", "market_disp": "Anytime HR",
            "line": 0.5, "side": "over",
            "book_odds": {book: 500},
        })
    promo_pool: list[dict] = []
    edges = find_player_prop_edges(props, promo_pool=promo_pool)
    # DK must not appear — outlier-rejected before edge_dict construction.
    assert not any(e["book"] == "DraftKings" for e in promo_pool)
    assert not any(e["book"] == "DraftKings" for e in edges)
    # FD's fair-priced line still reaches the promo pool.
    assert any(e["book"] == "FanDuel" for e in promo_pool)


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


# ── Odds API HR migration regressions ──────────────────────────────────
# Post-2026-05-17: HR rides the Odds API batter_home_runs market instead
# of a dedicated Action Network pipeline. Two shape guarantees matter
# for downstream consumers (HR Props tab, Telegram message, promo
# matcher): outcome is just the player name (no "Y" suffix) and point
# is None (not 0.5). Also, the Yes-side-only filter prevents the system
# from surfacing an N-side "Aaron Judge will NOT homer" pick on an
# HR-boost promo.


def test_find_player_prop_edges_hr_emits_yes_side_only_with_clean_shape():
    # 5 books price both sides of a binary anytime HR; verify only the
    # Yes (over) side surfaces, and that the outcome/point are normalized
    # to the AN-style shape the HR Props tab expects.
    props = []
    for book in ("draftkings", "fanduel", "betmgm", "caesars", "betrivers"):
        props.append({
            "player": "Aaron Judge", "game": "NYY @ BOS",
            "commence": "2099-01-01T19:00:00Z", "sport": "MLB",
            "market": "hr", "market_disp": "Anytime HR",
            "line": 0.5, "side": "over",
            "book_odds": {book: 500},
        })
        # Each book also prices the No side. Without the HR-Yes-only
        # filter we'd emit "Aaron Judge N" picks too — undesirable
        # because HR-boost promos always target the Yes side.
        props.append({
            "player": "Aaron Judge", "game": "NYY @ BOS",
            "commence": "2099-01-01T19:00:00Z", "sport": "MLB",
            "market": "hr", "market_disp": "Anytime HR",
            "line": 0.5, "side": "under",
            "book_odds": {book: -650},
        })
    promo_pool: list[dict] = []
    find_player_prop_edges(props, promo_pool=promo_pool)
    hr_edges = [e for e in promo_pool if e["market"] == "hr"]
    # Should have exactly one entry per target book (DK, FD), Yes-side only.
    assert len(hr_edges) == 2
    books = {e["book"] for e in hr_edges}
    assert books == {"DraftKings", "FanDuel"}
    for edge in hr_edges:
        # No "Y"/"N"/"O"/"U" suffix on the outcome — just the player.
        assert edge["outcome"] == "Aaron Judge"
        # Point normalized to None for shape parity with the prior AN
        # pipeline; the binary nature is implicit in market="hr".
        assert edge["point"] is None


def test_find_player_prop_edges_hr_excluded_from_returned_gated_list():
    # HR edges live only in the side-effect promo_pool, not in the
    # returned (gated) `edges` list — that's how downstream code knows
    # to look at `hr_edges` separately from `player_prop_edges`.
    # Construct a clearly +EV HR scenario: DK at +700 while consensus
    # has Judge at ~+400 (consensus ~20% implied vs DK 12.5% — DK is
    # giving you a discount, EV is positive).
    props = [{
        "player": "Aaron Judge", "game": "NYY @ BOS",
        "commence": "2099-01-01T19:00:00Z", "sport": "MLB",
        "market": "hr", "market_disp": "Anytime HR",
        "line": 0.5, "side": "over",
        "book_odds": {"draftkings": 700},
    }]
    for book in ("fanduel", "betmgm", "caesars", "betrivers"):
        props.append({
            "player": "Aaron Judge", "game": "NYY @ BOS",
            "commence": "2099-01-01T19:00:00Z", "sport": "MLB",
            "market": "hr", "market_disp": "Anytime HR",
            "line": 0.5, "side": "over",
            "book_odds": {book: 400},
        })
    promo_pool: list[dict] = []
    edges = find_player_prop_edges(props, promo_pool=promo_pool)
    # Returned list (gated +EV) must contain zero HR entries.
    assert not any(e["market"] == "hr" for e in edges)
    # The promo_pool still has the HR entries for boost-aware ranking.
    assert any(e["market"] == "hr" for e in promo_pool)


@_skip_if_golf_off
def test_find_golf_edges_rejects_higa_style_outlier_at_tightened_ratio():
    # Regression for the 2026-05-16 Higa incident — DK at +7200 while the
    # rest of the market clustered around +500 (consensus implies ~16.7%).
    # The target's implied prob (~1.37%) is ~8% of the consensus, well
    # below the 0.55 ratio guard. Old 0.40 threshold let any target
    # priced ≥2.5x longer than fair through — the new threshold catches
    # anything ≥1.82x longer, closing the gap that produced a +201.8%
    # boosted EV pseudo-pick.
    golf_props = [{
        "player":      "Kazuki Higa",
        "tournament":  "PGA Championship",
        "commence":    "2099-01-01T12:00:00Z",
        "sport":       "PGA",
        "market":      "golf_top20",
        "market_disp": "Top 20 Finish",
        # 68=DK stale at +7200; 69/75/49/71 cluster at +500 (~16.7% implied).
        "book_odds":   {68: 7200, 69: 500, 75: 500, 49: 500, 71: 500},
    }]
    promo_pool: list[dict] = []
    edges = find_golf_edges(golf_props, promo_pool=promo_pool)
    # DK must not appear in either path — stale-line guard rejects it
    # before the edge dict is constructed.
    assert not any(e["book"] == "DraftKings" for e in edges)
    assert not any(e["book"] == "DraftKings" for e in promo_pool)


# ── European-soccer parsing + filtering ────────────────────────────────
# When a promo restricts to European leagues (e.g. "fd 25% any European
# soccer event"), the system has to (1) parse the phrase to a distinct
# canonical key, (2) match only edges whose Odds API sport_key is in the
# European-leagues set, and (3) avoid silently routing MLS / Liga MX
# games into a promo that wouldn't credit the boost.

from telegram_bot import parse_promo
from odds_compare import (
    EUROPEAN_SOCCER_SPORT_KEYS,
    _canonical_promo_sport,
    _derive_promo_scope,
    _promo_matches,
    _promo_no_pick_reason,
)


def test_parse_promo_two_word_european_soccer():
    promo = parse_promo("fd 25% european soccer max:$10")
    assert promo is not None
    assert promo["sport"] == "european_soccer"
    # Label renders cleanly even with the underscore in the key.
    assert "European Soccer" in promo["label"]


def test_parse_promo_single_token_epl_narrows_to_european():
    # Single-token league shortcuts must NOT silently widen to generic
    # "soccer" — that's the bug that let MLS games slip into EPL promos.
    promo = parse_promo("dk 30% epl")
    assert promo is not None
    assert promo["sport"] == "european_soccer"


def test_parse_promo_premier_league_two_word_narrows_to_european():
    promo = parse_promo("fd 25% premier league")
    assert promo is not None
    assert promo["sport"] == "european_soccer"


def test_promo_matches_european_soccer_accepts_epl_edge():
    # An EPL game (sport_key=soccer_epl) must satisfy a european_soccer promo.
    promo = {"book": "FanDuel", "sport": "european_soccer", "market": "h2h",
             "promo_type": "straight", "min_odds": None}
    edge = {
        "book":         "FanDuel",
        "sport":        "Soccer",
        "sport_key":    "soccer_epl",
        "market":       "h2h",
        "odds_decimal": 2.5,
        # No commence → _starts_within_days returns True (soon-by-default).
    }
    assert _promo_matches(promo, edge) is True


def test_promo_matches_european_soccer_rejects_mls_edge():
    # The bug we're closing — typing "european soccer" must NOT match an
    # MLS game (sport_key=soccer_usa_mls). Pre-fix, both collapsed to
    # the same "soccer" bucket and the MLS line silently won the promo.
    promo = {"book": "FanDuel", "sport": "european_soccer", "market": "h2h",
             "promo_type": "straight", "min_odds": None}
    edge = {
        "book":         "FanDuel",
        "sport":        "Soccer",
        "sport_key":    "soccer_usa_mls",
        "market":       "h2h",
        "odds_decimal": 2.5,
    }
    assert _promo_matches(promo, edge) is False


def test_promo_matches_european_soccer_rejects_missing_sport_key():
    # Action Network fallback edges have sport_key=None — we can't prove
    # they're European leagues, so the filter conservatively excludes them.
    # Better to surface "no European data" than to risk a non-European pick.
    promo = {"book": "FanDuel", "sport": "european_soccer", "market": "h2h",
             "promo_type": "straight", "min_odds": None}
    edge = {
        "book":         "FanDuel",
        "sport":        "Soccer",
        "sport_key":    None,
        "market":       "h2h",
        "odds_decimal": 2.5,
    }
    assert _promo_matches(promo, edge) is False


# ── Tennis promo matching ──────────────────────────────────────────────
# The Odds API tags tennis events with the tournament title ("ATP Italian
# Open", "WTA Madrid Open"), not a flat "Tennis". Before the
# _canonical_promo_sport fix, these never collapsed to "tennis", so a
# tennis-targeted promo silently matched nothing despite the data being
# fetched (tennis h2h/spreads/totals ride the regular game-line fetch).

@pytest.mark.parametrize("raw_sport,expected", [
    ("ATP Italian Open", "tennis"),
    ("WTA Madrid Open",  "tennis"),
    ("ATP French Open",  "tennis"),
    ("Tennis",           "tennis"),
    ("atp",              "tennis"),
    ("wta",              "tennis"),
    # Non-tennis sports are unaffected — same result as the old
    # _LEAGUE_TO_SPORT.get(key, key) lookup.
    ("MLB",              "mlb"),
    ("Soccer",           "soccer"),
    ("EPL",              "soccer"),
    ("NBA",              "nba"),
    ("Unknownsport",     "unknownsport"),
])
def test_canonical_promo_sport(raw_sport, expected):
    assert _canonical_promo_sport(raw_sport) == expected


def test_promo_matches_tennis_promo_accepts_atp_tournament_edge():
    # The bug we're closing: an ATP-tournament edge must satisfy a
    # tennis-targeted promo.
    promo = {"book": "DraftKings", "sport": "tennis", "market": "h2h",
             "promo_type": "straight", "min_odds": None}
    edge = {
        "book":         "DraftKings",
        "sport":        "ATP Italian Open",
        "sport_key":    "tennis_atp_italian_open",
        "market":       "h2h",
        "odds_decimal": 1.8,
    }
    assert _promo_matches(promo, edge) is True


def test_promo_matches_tennis_promo_accepts_wta_totals_edge():
    # Tennis totals (total games over/under) ride the game-line fetch and
    # must match a tennis totals promo.
    promo = {"book": "FanDuel", "sport": "tennis", "market": "totals",
             "promo_type": "straight", "min_odds": None}
    edge = {
        "book":         "FanDuel",
        "sport":        "WTA Madrid Open",
        "sport_key":    "tennis_wta_madrid_open",
        "market":       "totals",
        "odds_decimal": 1.95,
    }
    assert _promo_matches(promo, edge) is True


def test_promo_matches_tennis_promo_rejects_non_tennis_edge():
    # A tennis promo must not grab a baseball edge.
    promo = {"book": "DraftKings", "sport": "tennis", "market": "h2h",
             "promo_type": "straight", "min_odds": None}
    edge = {
        "book":         "DraftKings",
        "sport":        "MLB",
        "sport_key":    "baseball_mlb",
        "market":       "h2h",
        "odds_decimal": 1.8,
    }
    assert _promo_matches(promo, edge) is False


def test_european_soccer_keys_cover_known_european_leagues():
    # Guardrail: if someone later prunes or renames a league in the
    # Odds API map, this test will fail loudly rather than silently
    # excluding edges from the European-soccer filter.
    expected = {
        "soccer_epl",
        "soccer_uefa_champs_league",
        "soccer_uefa_europa_league",
        "soccer_uefa_europa_conference_league",
        "soccer_spain_la_liga",
        "soccer_italy_serie_a",
        "soccer_germany_bundesliga",
        "soccer_france_ligue_one",
    }
    assert EUROPEAN_SOCCER_SPORT_KEYS == expected
    # MLS and Liga MX must NOT be in the European set — they're the
    # whole reason the filter exists.
    assert "soccer_usa_mls" not in EUROPEAN_SOCCER_SPORT_KEYS
    assert "soccer_mexico_ligamx" not in EUROPEAN_SOCCER_SPORT_KEYS


def test_scope_european_soccer_collapses_to_soccer_fetch():
    # european_soccer is a sport_key-level filter, not a separate fetch —
    # the Odds API call is the same as plain "soccer" (pulls every league),
    # then _promo_matches narrows downstream. Scoping must reflect that
    # so /run_promos doesn't try to fetch a nonexistent "european_soccer"
    # game-line endpoint.
    scope = _derive_promo_scope([
        {"book": "FanDuel", "sport": "european_soccer", "market": "h2h"}
    ])
    assert scope is not None
    assert scope["sports"] == {"soccer"}


def test_no_pick_european_soccer_only_mls_loaded():
    # Distinct diagnostic: MLS edges exist but the promo restricted to
    # European leagues. The generic "no european_soccer edges" message
    # would be confusing because soccer DOES appear in the slate.
    promo = {"book": "FanDuel", "sport": "european_soccer", "market": "h2h"}
    combined = [
        {**_edge("FanDuel", "Soccer", "h2h", outcome="LAFC"),
         "sport_key": "soccer_usa_mls"},
        {**_edge("FanDuel", "Soccer", "h2h", outcome="Atlanta United"),
         "sport_key": "soccer_usa_mls"},
    ]
    reason = _promo_no_pick_reason(promo, combined, matched_all=[], pool=[])
    assert "European" in reason
    assert "MLS" in reason.upper() or "Usa Mls" in reason


def test_no_pick_european_soccer_no_soccer_at_all():
    # No soccer data of any kind on FD — the message should flag the
    # soccer fetch as the issue, not the European-leagues filter.
    promo = {"book": "FanDuel", "sport": "european_soccer", "market": "h2h"}
    combined = [
        _edge("FanDuel", "MLB", "h2h"),
        _edge("FanDuel", "NBA", "h2h"),
    ]
    reason = _promo_no_pick_reason(promo, combined, matched_all=[], pool=[])
    assert "FanDuel" in reason
    assert "soccer" in reason.lower()


# ── _qualifies / _qualifies_prop (2026-05-22 recalibration) ────────────
# Covers the changes from the 12-day backtest (2026-05-05 → 05-16):
#   - Universal EV ≥ 5% cap on game lines
#   - Tennis/Boxing H2H added
#   - WNBA spreads non-Pinn floor 1.0%
#   - Spreads general non-Pinn floor 2.0%
#   - pts non-Pinn 2.0%, strikeouts non-Pinn 4.0%

from odds_compare import _qualifies, _qualifies_prop


def _q(sport, market, ev, *, pinn=False, books=8):
    """Build a minimal edge dict that exercises the qualifier."""
    return {
        "sport": sport,
        "market": market,
        "ev_pct": ev,
        "pinnacle_anchored": pinn,
        "num_other_books": books,
        # no commence → _starts_within_days treats as soon
    }


def test_qualifies_ev_cap_blocks_pinn_mlb_h2h_at_or_above_5():
    # The headline 2026-05-22 fix: Pinn-anchored MLB H2H at ≥5% was the
    # single biggest bleed source ($-32 across 4 picks in 12 days).
    assert not _qualifies(_q("MLB", "h2h", 5.0, pinn=True))
    assert not _qualifies(_q("MLB", "h2h", 55.7, pinn=True))


def test_qualifies_ev_cap_blocks_pinn_mlb_spreads_at_or_above_5():
    # 5 MLB spreads picks at 5.0–5.5% (all Pinn-anchored) all lost
    # post-stale-guard tightening (5/13–5/16).
    assert not _qualifies(_q("MLB", "spreads", 5.0, pinn=True))
    assert not _qualifies(_q("MLB", "spreads", 7.5, pinn=True))


def test_qualifies_pinn_mlb_h2h_below_cap_still_qualifies():
    assert _qualifies(_q("MLB", "h2h", 4.9, pinn=True))
    assert _qualifies(_q("MLB", "h2h", 1.0, pinn=True))


def test_qualifies_tennis_h2h_non_pinn_floor_2pt5():
    # 2026-06-22: non-Pinn floor 2.0 → 2.5, dropping the 2.0–2.5% band
    # (-40.6% flat on n=28). The 2.5–5% pocket is retained.
    assert not _qualifies(_q("TENNIS", "h2h", 2.4))
    assert _qualifies(_q("TENNIS", "h2h", 2.5))
    assert _qualifies(_q("TENNIS", "h2h", 4.9))
    # Above the universal cap.
    assert not _qualifies(_q("TENNIS", "h2h", 5.0))


def test_qualifies_tennis_h2h_pinn_qualifies_at_1pct():
    assert _qualifies(_q("TENNIS", "h2h", 1.0, pinn=True))
    assert not _qualifies(_q("TENNIS", "h2h", 0.9, pinn=True))


def test_qualifies_tennis_tournament_title_activates():
    # 2026-06-02 regression guard: live edges are tagged with the Odds API
    # tournament title ("ATP Italian Open"), NOT a flat "TENNIS". Before the
    # fix the TENNIS branch never fired live (the older tests above used the
    # already-canonical "TENNIS" string and so missed this). These assert the
    # real-world labels now qualify.
    assert _qualifies(_q("ATP Italian Open", "h2h", 2.5))
    assert _qualifies(_q("WTA Madrid Open", "h2h", 2.5))
    assert not _qualifies(_q("ATP Italian Open", "h2h", 1.9))   # below 2.0 gate
    assert not _qualifies(_q("WTA Madrid Open", "h2h", 5.0))    # universal cap


def test_qualifies_boxing_h2h_paused():
    # 2026-06-22: boxing h2h paused. The 2026-05-22 add (n=14, +11.7%)
    # inverted to -30.9% flat at n=54, so the whole branch was removed —
    # boxing h2h is now skipped at every EV, Pinn or not.
    assert not _qualifies(_q("BOXING", "h2h", 1.0))
    assert not _qualifies(_q("BOXING", "h2h", 2.5))
    assert not _qualifies(_q("BOXING", "h2h", 4.9))
    assert not _qualifies(_q("BOXING", "h2h", 2.0, pinn=True))


def test_qualifies_wnba_spreads_non_pinn_floor_1pct():
    # WNBA spreads non-Pinn floor 2.5% → 1.0%. Broad 1.0–2.5% slice
    # was +13.6% on 18 picks — the existing 2.5% gate was dropping
    # the highest-ROI band.
    assert _qualifies(_q("WNBA", "spreads", 1.0))
    assert _qualifies(_q("WNBA", "spreads", 2.4))
    assert not _qualifies(_q("WNBA", "spreads", 0.9))


def test_qualifies_spreads_general_non_pinn_floor_2pct():
    # General spreads non-Pinn 2.5% → 2.0% (anything except WNBA).
    assert _qualifies(_q("MLB", "spreads", 2.0))
    assert _qualifies(_q("NHL", "spreads", 2.5))
    assert not _qualifies(_q("MLB", "spreads", 1.9))


def test_qualifies_mlb_h2h_non_pinn_upper_cap_preserved():
    # Existing 2.5–3.0% cap on non-Pinn MLB H2H must still hold —
    # the >3% non-Pinn band was 1W/12L (-79%) in the 30d backtest.
    assert _qualifies(_q("MLB", "h2h", 2.5))
    assert _qualifies(_q("MLB", "h2h", 3.0))
    assert not _qualifies(_q("MLB", "h2h", 3.1))
    assert not _qualifies(_q("MLB", "h2h", 2.4))


def test_qualifies_blocked_sports_still_blocked():
    # NBA H2H + totals remain blocked. NBA spreads has its own narrow
    # re-enable — see test_qualifies_nba_spreads_narrow_reenable.
    # European soccer remains fully blocked.
    assert not _qualifies(_q("NBA", "h2h", 3.0, pinn=True))
    assert not _qualifies(_q("NBA", "totals", 2.0, pinn=True))
    assert not _qualifies(_q("EPL", "h2h", 3.0, pinn=True))
    assert not _qualifies(_q("BUNDESLIGA", "h2h", 4.0, pinn=True))


def test_qualifies_nba_spreads_narrow_reenable():
    # 60d backtest (2026-03-26 → 2026-05-26): NBA spreads non-Pinn
    # 1–2% +24.6% on 52 picks, 2–3% +12.9% on 21 picks. Re-enabled
    # at 1.0–3.0% only; 3%+ stays blocked (small-sample losses in
    # 3–5% band).
    assert _qualifies(_q("NBA", "spreads", 1.0))
    assert _qualifies(_q("NBA", "spreads", 2.9))
    assert _qualifies(_q("NBA", "spreads", 1.5, pinn=True))
    # Below floor and at/above 3% upper cap both blocked.
    assert not _qualifies(_q("NBA", "spreads", 0.9))
    assert not _qualifies(_q("NBA", "spreads", 3.0))
    assert not _qualifies(_q("NBA", "spreads", 4.0))
    # Pinn at 3%+ also blocked (matches non-Pinn upper cap).
    assert not _qualifies(_q("NBA", "spreads", 3.5, pinn=True))


def test_qualifies_totals_still_mlb_pinn_only():
    # Totals path unchanged: MLB + Pinn ≥ 1% only.
    assert _qualifies(_q("MLB", "totals", 1.0, pinn=True))
    assert not _qualifies(_q("MLB", "totals", 1.0, pinn=False))
    assert not _qualifies(_q("WNBA", "totals", 3.0, pinn=True))
    # Universal cap also catches Pinn MLB totals.
    assert not _qualifies(_q("MLB", "totals", 5.0, pinn=True))


def test_qualifies_book_count_floor_unchanged():
    # books < 4 still blocks.
    assert not _qualifies(_q("MLB", "h2h", 2.0, pinn=True, books=3))


def test_qualifies_prop_pts_floor_3pct():
    # 2026-06-02: pts threshold 2.0% → 3.0%. The ~2-month replay showed the
    # 2–3% band at -6.8% ROI / -8.2% flat (n=54); positive signal is 3%+.
    assert _qualifies_prop({"market": "pts", "ev_pct": 3.0})
    assert not _qualifies_prop({"market": "pts", "ev_pct": 2.9})
    # The old 2.0–3.0 inclusion band is now excluded.
    assert not _qualifies_prop({"market": "pts", "ev_pct": 2.0})


def test_qualifies_prop_strikeouts_floor_4pct():
    # strikeouts threshold 2.0% → 4.0%; the 1–3% band was -22% in 12 days.
    assert not _qualifies_prop({"market": "strikeouts", "ev_pct": 3.9})
    assert _qualifies_prop({"market": "strikeouts", "ev_pct": 4.0})


def test_qualifies_prop_total_bases_unchanged():
    # total_bases still 1.5%.
    assert _qualifies_prop({"market": "total_bases", "ev_pct": 1.5})
    assert not _qualifies_prop({"market": "total_bases", "ev_pct": 1.4})


def test_qualifies_prop_other_markets_blocked():
    # reb/ast remain excluded; unknown markets blocked.
    assert not _qualifies_prop({"market": "rebounds", "ev_pct": 10.0})
    assert not _qualifies_prop({"market": "assists", "ev_pct": 10.0})
    assert not _qualifies_prop({"market": "made_threes", "ev_pct": 10.0})


# ── World Cup promo support (2026 FIFA World Cup) ──────────────────────
# Tournament-wide boosts ("50% profit boost on all World Cup bets") need
# sport="world_cup" to narrow to the exact soccer_fifa_world_cup
# sport_key. The Odds API also exposes soccer_fifa_club_world_cup,
# _qualifiers_*, _womens and _winner as SEPARATE keys — none of them
# qualify under a World Cup promo's terms, so exact key matching (not
# prefix matching) is load-bearing here.

from odds_compare import (_canonical_promo_sport, _promo_matches,
                          ODDS_API_SPORTS, WORLD_CUP_SPORT_KEYS)


def _wc_edge(**overrides):
    """Edge as the soccer fetch produces it for a World Cup game:
    sport is the Odds API sport_title verbatim, sport_key carries the
    competition identity used for subset narrowing."""
    edge = {
        "book":         "DraftKings",
        "sport":        "FIFA World Cup",
        "sport_key":    "soccer_fifa_world_cup",
        "market":       "h2h",
        "odds_decimal": 2.4,
        "outcome":      "Mexico",
        "game":         "Mexico @ South Africa",
    }
    edge.update(overrides)
    return edge


def _wc_promo(**overrides):
    promo = {
        "book":        "DraftKings",
        "sport":       "world_cup",
        "market":      "all",
        "promo_type":  "straight",
        "min_odds":    None,
        "game_filter": None,
    }
    promo.update(overrides)
    return promo


def test_parse_promo_world_cup_phrase():
    promo = parse_promo("dk 50% world cup")
    assert promo is not None
    assert promo["book"]      == "DraftKings"
    assert promo["sport"]     == "world_cup"
    assert promo["market"]    == "all"
    assert promo["boost_pct"] == 50
    assert "World Cup" in promo["label"]


def test_parse_promo_world_cup_near_verbatim_slip():
    # What the user would paste from the books' promo copy. "fifa world"
    # is the two-token phrase; the trailing "cup" and the filler words
    # must fall away silently.
    promo = parse_promo("fd 50% profit boost on all fifa world cup bets")
    assert promo is not None
    assert promo["book"]      == "FanDuel"
    assert promo["sport"]     == "world_cup"
    assert promo["boost_pct"] == 50


def test_parse_promo_wc_keyword_with_market():
    promo = parse_promo("dk 25% wc ml")
    assert promo is not None
    assert promo["sport"]  == "world_cup"
    assert promo["market"] == "h2h"


def test_promo_matches_world_cup_edge():
    assert _promo_matches(_wc_promo(), _wc_edge())


def test_promo_matches_world_cup_rejects_other_fifa_competitions():
    # Same "FIFA …" title family, different competitions — prefix
    # matching on either title or sport_key would wrongly admit these.
    club = _wc_edge(sport="FIFA Club World Cup",
                    sport_key="soccer_fifa_club_world_cup")
    qual = _wc_edge(sport="FIFA World Cup Qualifiers - Europe",
                    sport_key="soccer_fifa_world_cup_qualifiers_europe")
    assert not _promo_matches(_wc_promo(), club)
    assert not _promo_matches(_wc_promo(), qual)


def test_promo_matches_world_cup_rejects_mls_and_keyless_edges():
    mls = _wc_edge(sport="MLS", sport_key="soccer_usa_mls")
    assert not _promo_matches(_wc_promo(), mls)
    # AN-fallback edges carry no sport_key — without competition
    # metadata we can't prove eligibility, so they're excluded.
    keyless = _wc_edge(sport_key=None)
    assert not _promo_matches(_wc_promo(), keyless)


def test_promo_matches_subset_promos_dont_cross():
    eur_promo = _wc_promo(sport="european_soccer")
    assert not _promo_matches(eur_promo, _wc_edge())
    epl = _wc_edge(sport="EPL", sport_key="soccer_epl")
    assert not _promo_matches(_wc_promo(), epl)
    assert _promo_matches(eur_promo, epl)


def test_generic_soccer_promo_covers_world_cup():
    # FIFA titles collapse to canonical "soccer", so a plain soccer
    # boost (no subset narrowing) covers World Cup games too.
    assert _canonical_promo_sport("FIFA World Cup") == "soccer"
    assert _promo_matches(_wc_promo(sport="soccer"), _wc_edge())


def test_scope_world_cup_collapses_to_soccer_fetch():
    scope = _derive_promo_scope([
        {"book": "DraftKings", "sport": "world_cup", "market": "h2h"},
    ])
    assert scope is not None
    assert scope["sports"] == {"soccer"}
    assert scope["prop_sport_markets"] == {}


def test_world_cup_key_wired_into_soccer_fetch():
    # The subset filter is meaningless if the fetch never pulls the
    # tournament; removing the key from the fetch list must fail loudly.
    assert "soccer_fifa_world_cup" in ODDS_API_SPORTS["soccer"]
    assert WORLD_CUP_SPORT_KEYS == {"soccer_fifa_world_cup"}


def test_qualifies_blocks_world_cup_from_play_these():
    # 2026-06-10: no backtest history yet (30+ day bar, see tennis
    # precedent) — World Cup is promo-only. Spreads would otherwise
    # pass the generic gate on day one.
    assert not _qualifies(_q("FIFA World Cup", "spreads", 2.5, pinn=True))
    assert not _qualifies(_q("FIFA World Cup", "spreads", 2.5))
    assert not _qualifies(_q("FIFA World Cup", "h2h", 3.0, pinn=True))


def test_no_pick_world_cup_names_the_slate():
    # Soccer loaded but no World Cup games on the slate: the diagnostic
    # must say which competitions DID load, not just "no world_cup".
    promo = {"book": "DraftKings", "sport": "world_cup", "market": "all"}
    mls = _edge("DraftKings", "MLS", "h2h")
    mls["sport_key"] = "soccer_usa_mls"
    reason = _promo_no_pick_reason(promo, [mls], matched_all=[], pool=[])
    assert "FIFA World Cup" in reason
    assert "Usa Mls" in reason


def test_backtest_grades_world_cup_via_espn():
    # Without these mappings, World Cup picks fall through to the "soccer"
    # default (MLS scoreboard), never match, and silently skip grading
    # for the whole tournament.
    from backtest import ESPN_SPORT_MAP, _normalise_sport
    assert _normalise_sport("FIFA World Cup") == "world_cup"
    assert _normalise_sport("world cup") == "world_cup"
    assert ESPN_SPORT_MAP["world_cup"] == "soccer/fifa.world"


# ── World Cup anytime-goalscorer props ─────────────────────────────────
# Yes-only market (books post a "Yes" price per player, never a "No"),
# riding the same one-sided consensus path as anytime HR. Promo-only:
# _qualifies_prop keeps it out of Play These; the broad pool + CLV
# capture collect the calibration data.

def _goal_edge(**overrides):
    """Goalscorer prop edge as find_player_prop_edges constructs it:
    sport is the prop-sport slug uppercased by _merge_raw_props."""
    edge = {
        "book":         "DraftKings",
        "sport":        "WORLD_CUP",
        "sport_key":    "soccer_fifa_world_cup",
        "market":       "goal",
        "odds_decimal": 2.5,
        "outcome":      "Raul Jimenez",
        "game":         "South Africa @ Mexico",
    }
    edge.update(overrides)
    return edge


def test_parse_promo_goalscorer_market_forms():
    promo = parse_promo("dk 30% world cup goals")
    assert promo["sport"]  == "world_cup"
    assert promo["market"] == "goal"
    assert parse_promo("fd 25% anytime goalscorer")["market"] == "goal"
    p3 = parse_promo("fd 25% wc goal scorer")
    assert p3["sport"] == "world_cup" and p3["market"] == "goal"
    assert "Anytime Goalscorer" in promo["label"]


def test_goalscorer_flows_only_to_explicit_goal_promos():
    # Market/sport routing: a goalscorer prop flows ONLY to promos that
    # explicitly target the goal market (a book token usable on nothing
    # else). A tournament-wide WC boost (market="all") no longer grabs it —
    # the user finds goalscorer props mispriced regardless of cross-book EV
    # and doesn't want the picker electing them. Whether an explicit goal
    # promo picks early or only inside the lineup window is covered by
    # test_explicit_goal_promo_picks_goalscorer_early_with_warn; here we pin
    # a concrete kickoff time and just check the routing.
    from datetime import datetime, timedelta, timezone
    soon = (datetime.now(timezone.utc) + timedelta(minutes=30)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    # A WC all-market boost is kept off goalscorer props.
    assert not _promo_matches(_wc_promo(), _goal_edge(commence=soon))
    # Explicit market targeting works regardless of sport pin.
    assert _promo_matches(_wc_promo(market="goal"), _goal_edge(commence=soon))
    assert _promo_matches(_wc_promo(sport="all", market="goal"),
                          _goal_edge(commence=soon))


def test_explicit_goal_promo_picks_goalscorer_early_with_warn():
    # A goalscorer-TARGETED promo (market="goal" — e.g. "fd 25% anytime
    # goalscorer", a book token usable on nothing else) picks the prop early
    # rather than holding the token until 75 min before kickoff: it has no
    # settle-always line it would rather take, and the staged odds bands
    # already keep the pick to a likely starter. The early pick is flagged
    # for the user to confirm the XI (a DNP still voids the token). A WC
    # all-market boost, by contrast, no longer reaches a goalscorer at all
    # (see test_goalscorer_blocked_from_generic_boosts).
    from datetime import datetime, timedelta, timezone
    from odds_compare import _pick_needs_participation_warn
    far = (datetime.now(timezone.utc) + timedelta(hours=6)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    e_far = _goal_edge(commence=far)
    # A goal-TARGETED promo admits the goalscorer early, flagged to confirm.
    assert _promo_matches(_wc_promo(market="goal"), e_far)
    assert _pick_needs_participation_warn(e_far) is True
    # Inside the lineup window the pick stands and the warn clears.
    soon = (datetime.now(timezone.utc) + timedelta(minutes=30)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    e_soon = _goal_edge(commence=soon)
    assert _promo_matches(_wc_promo(market="goal"), e_soon)
    assert _pick_needs_participation_warn(e_soon) is False


def test_goalscorer_blocked_from_generic_boosts():
    # "fd 25%" (all sports) and "dk 25% soccer" must not silently grab a
    # goalscorer prop — same reasoning as the HR guard.
    assert not _promo_matches(_wc_promo(sport="all"), _goal_edge())
    assert not _promo_matches(_wc_promo(sport="soccer"), _goal_edge())
    # A tournament-wide WC boost (market="all") is no longer an exception:
    # the user wants goalscorer props off every promo except an explicit
    # goal-targeted token, so even the WC all-market boost is blocked.
    assert not _promo_matches(_wc_promo(), _goal_edge())
    # The HR guard itself is unchanged: a WC all-market promo still
    # never takes an HR prop.
    hr_edge = _goal_edge(sport="MLB", sport_key="baseball_mlb", market="hr")
    assert not _promo_matches(_wc_promo(), hr_edge)


def test_scope_world_cup_goals_is_prop_only_fetch():
    scope = _derive_promo_scope([
        {"book": "DraftKings", "sport": "world_cup", "market": "goal"},
    ])
    assert scope is not None
    assert scope["sports"] == set()
    assert scope["prop_sport_markets"] == {"world_cup": {"goal"}}


def test_world_cup_prop_fetch_wiring():
    from odds_compare import (ODDS_API_PROP_MARKETS, _PROP_MARKETS_BY_SPORT,
                              _YES_ONLY_PROP_MARKETS)
    assert ODDS_API_SPORTS["world_cup"] == "soccer_fifa_world_cup"
    assert [(a, i) for a, i, _ in ODDS_API_PROP_MARKETS["world_cup"]] == [
        ("player_goal_scorer_anytime", "goal")]
    assert _PROP_MARKETS_BY_SPORT["world_cup"] == {"goal"}
    assert "goal" in _YES_ONLY_PROP_MARKETS
    # Closing-line capture derives its market map from the prop config —
    # the goal entry must resolve for CLV re-queries to work.
    from closing_lines import INTERNAL_TO_API_MARKET
    assert INTERNAL_TO_API_MARKET["goal"] == "player_goal_scorer_anytime"


def test_goalscorer_edge_construction_one_sided():
    from odds_compare import find_player_prop_edges
    merged = [{
        "player": "Raul Jimenez", "game": "South Africa @ Mexico",
        "commence": "2026-06-11T19:00:00Z", "sport": "WORLD_CUP",
        "market": "goal", "market_disp": "Anytime Goalscorer",
        "line": None, "side": "over",
        "book_odds": {"draftkings": 150, "fanduel": 135,
                      "pinnacle": 122, "betrivers": 140},
        "event_id": "ev1", "sport_key": "soccer_fifa_world_cup",
    }]
    pool: list = []
    edges = find_player_prop_edges(merged, promo_pool=pool)
    dk = [e for e in pool if e["book"] == "DraftKings"]
    assert dk, "DK goalscorer edge should be constructed"
    e = dk[0]
    # Binary Yes-only shape: bare player name, no line, no Pinn anchor
    # (Pinnacle has no No side to devig against — it's a consensus ref).
    assert e["outcome"] == "Raul Jimenez"
    assert e["point"] is None
    assert e["pinnacle_anchored"] is False
    assert e["sport_key"] == "soccer_fifa_world_cup"
    # DK at +150 vs refs (FD 135 / Pinn 122 / BR 140) is +EV → unlike HR,
    # goal edges stay in the returned list (prop sheet + CLV); Play These
    # exclusion happens via _qualifies_prop instead.
    assert any(x["market"] == "goal" for x in edges)
    assert not _qualifies_prop({"market": "goal", "ev_pct": 10.0})


def test_backtest_promo_goalscorer_grades_manual():
    # Binary anytime markets can't auto-grade without DNP detection —
    # GOAL joins HR in the MANUAL bucket (CLV still captures via
    # closing_lines).
    from backtest import run_backtest_promo
    pick = {"market": "GOAL", "sport": "unknown", "game_date": None,
            "decimal": 3.0, "book": "DraftKings", "game": "x @ y",
            "pick": "Raul Jimenez", "bet_amount": 5.0}
    run_backtest_promo([pick])
    assert pick["result"] == "MANUAL"


def test_wc_window_message_when_next_game_beyond_promo_window():
    # The exact 2:22 AM ET scenario from launch eve: 72 fixtures fetched,
    # zero in the 1-day promo window. The diagnostic must say WHEN to
    # re-run, not imply the fetch broke.
    from datetime import datetime, timezone
    from odds_compare import _format_wc_window_msg
    now = datetime(2026, 6, 10, 6, 22, tzinfo=timezone.utc)
    events = [
        {"commence_time": "2026-06-11T19:00:00Z",
         "away_team": "South Africa", "home_team": "Mexico"},
        {"commence_time": "2026-06-12T02:00:00Z",
         "away_team": "Czech Republic", "home_team": "South Korea"},
    ]
    msg = _format_wc_window_msg(events, "DraftKings", now=now)
    assert msg is not None
    assert "South Africa @ Mexico" in msg
    assert "36.6h" in msg
    # Window opens 24h before kickoff: 06/10 3:00 PM EDT.
    assert "Re-run after 06/10 03:00 PM" in msg
    assert "DraftKings" in msg


def test_wc_window_message_silent_when_explanation_is_elsewhere():
    from datetime import datetime, timezone
    from odds_compare import _format_wc_window_msg
    now = datetime(2026, 6, 10, 6, 22, tzinfo=timezone.utc)
    # Soonest game inside the window → timing isn't the story; fall
    # through to the generic subset diagnostics.
    in_window = [{"commence_time": "2026-06-10T19:00:00Z",
                  "away_team": "A", "home_team": "B"}]
    assert _format_wc_window_msg(in_window, "DraftKings", now=now) is None
    # No future fixtures at all (tournament over) → same.
    assert _format_wc_window_msg([], "DraftKings", now=now) is None
    past = [{"commence_time": "2026-06-09T19:00:00Z",
             "away_team": "A", "home_team": "B"}]
    assert _format_wc_window_msg(past, "DraftKings", now=now) is None


# ── Promo hit-rate ruleset ─────────────────────────────────────────────
# Promos are use-it-or-lose-it; selection targets the cashable odds band
# instead of EV-maxing into moonshots (the two voided WC goalscorer
# tokens, 2026-06-11). These tests pin the band logic and the voidable-
# prop lineup-window gate.

from datetime import datetime as _dt, timedelta as _td, timezone as _tz

from odds_compare import (
    select_promo_edge,
    _promo_window_stage,
    _starts_within_minutes,
    PROMO_ODDS_WINDOWS,
    PROMO_PROP_WINDOW_MIN,
)


def _iso_in(minutes: float) -> str:
    return (_dt.now(_tz.utc) + _td(minutes=minutes)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def _promo_edge(american, fair_prob, market="h2h", commence=None,
                book="DraftKings", sport="MLB"):
    e = {
        "odds_decimal": american_to_decimal(american),
        "odds_american": american,
        "fair_prob":    fair_prob,   # percent, matching the pipeline
        "market":       market,
        "book":         book,
        "sport":        sport,
        "game":         "Away @ Home",
        "outcome":      "Home",
        "point":        None,
    }
    if commence is not None:
        e["commence"] = commence
    return e


def test_bonus_bet_prefers_band_over_higher_ev_moonshot():
    # The exact failure mode this ruleset exists for: the +2000 moonshot
    # has higher expected profit per token dollar (0.06 × 20 = 1.20)
    # than the +350 (0.25 × 3.5 = 0.875), but it sits outside every
    # window — the in-band +350 must win at stage 0.
    promo = {"book": "DraftKings", "sport": "all", "market": "all",
             "promo_type": "bonus_bet", "bet_amount": 50}
    moonshot = _promo_edge(2000, 6.0)
    band     = _promo_edge(350, 25.0)
    best, stage = select_promo_edge(promo, [moonshot, band])
    assert best is band
    assert stage == 0


def test_bonus_bet_relaxes_to_short_odds_when_band_empty():
    # Only a -150 favorite available: stages (200,450) and (150,500)
    # are empty, the last-resort (-200,500) window admits it. A cashed
    # -150 beats an expired token.
    promo = {"book": "DraftKings", "sport": "all", "market": "all",
             "promo_type": "bonus_bet", "bet_amount": 25}
    fav = _promo_edge(-150, 62.0)
    best, stage = select_promo_edge(promo, [fav])
    assert best is fav
    assert stage == 2


def test_promo_outside_all_windows_takes_shortest_odds():
    # Promo terms force odds above every ceiling (e.g. a +600 min-odds
    # term filtered the pool upstream): terminal stage == len(windows),
    # and the SHORTEST available odds win — most cashable thing left.
    promo = {"book": "DraftKings", "sport": "all", "market": "all",
             "promo_type": "bonus_bet", "bet_amount": 25}
    longer  = _promo_edge(1200, 6.0)
    shorter = _promo_edge(900, 9.0)
    best, stage = select_promo_edge(promo, [longer, shorter])
    assert best is shorter
    assert stage == len(PROMO_ODDS_WINDOWS["bonus_bet"])


def test_boost_keeps_in_band_favorite_over_off_band_moonshot():
    # Profit boosts ride cash stakes, so a -180 favorite is first-class
    # (stage-0 floor is -200). The +1200 moonshot has far higher boosted
    # EV but exceeds even the relaxed +700 ceiling — never auto-picked.
    promo = {"book": "DraftKings", "sport": "all", "market": "all",
             "promo_type": "straight", "boost_pct": 50}
    fav      = _promo_edge(-180, 60.0)   # boosted EV ≈ +10%
    moonshot = _promo_edge(1200, 8.0)    # boosted EV ≈ +52%, off-band
    best, stage = select_promo_edge(promo, [fav, moonshot])
    assert best is fav
    assert stage == 0


def test_boost_prefers_plus_ev_in_relaxed_window_over_neg_ev_in_band():
    # Band candidate is -EV even after the boost; the relaxed window
    # holds a clearly +EV pick — the +EV pick wins at stage 1.
    promo = {"book": "DraftKings", "sport": "all", "market": "all",
             "promo_type": "straight", "boost_pct": 20}
    neg_in_band = _promo_edge(-150, 55.0)   # boosted EV ≈ -1%
    pos_relaxed = _promo_edge(600, 16.0)    # boosted EV ≈ +31%
    best, stage = select_promo_edge(promo, [neg_in_band, pos_relaxed])
    assert best is pos_relaxed
    assert stage == 1


def test_boost_falls_back_to_earliest_band_when_nothing_clears_ev():
    # Nothing +EV anywhere: return the earliest non-empty window's best
    # (the caller marks it is_fallback) rather than leaving the band.
    promo = {"book": "DraftKings", "sport": "all", "market": "all",
             "promo_type": "straight", "boost_pct": 10}
    only = _promo_edge(-150, 50.0)   # boosted EV ≈ -13%
    best, stage = select_promo_edge(promo, [only])
    assert best is only
    assert stage == 0


def test_select_promo_edge_empty_pool():
    promo = {"book": "DraftKings", "sport": "all", "market": "all",
             "promo_type": "bonus_bet", "bet_amount": 25}
    assert select_promo_edge(promo, []) == (None, None)


def test_promo_window_stage_boundaries():
    windows = PROMO_ODDS_WINDOWS["bonus_bet"]
    assert _promo_window_stage(_promo_edge(200, 30.0), windows) == 0
    assert _promo_window_stage(_promo_edge(450, 17.0), windows) == 0
    assert _promo_window_stage(_promo_edge(175, 33.0), windows) == 1
    assert _promo_window_stage(_promo_edge(-150, 62.0), windows) == 2
    assert _promo_window_stage(_promo_edge(2000, 5.0), windows) is None


def test_starts_within_minutes():
    soon = {"commence": _iso_in(30)}
    far  = {"commence": _iso_in(6 * 60)}
    assert _starts_within_minutes(soon, PROMO_PROP_WINDOW_MIN) is True
    assert _starts_within_minutes(far, PROMO_PROP_WINDOW_MIN) is False
    # Opposite default from _starts_within_days: unknown start time must
    # mean NOT eligible, since this helper gates voidable props.
    assert _starts_within_minutes({}, PROMO_PROP_WINDOW_MIN) is False
    assert _starts_within_minutes({"commence": "garbage"},
                                  PROMO_PROP_WINDOW_MIN) is False


def test_prop_window_gates_all_market_promo_not_prop_targeted():
    # The lineup window defers voidable props ONLY for promos with a
    # game-line fallback. An all-market promo defers a double-double prop
    # until lineups are out (prefers a settle-always line early)...
    from odds_compare import _pick_needs_participation_warn
    allm = {"book": "DraftKings", "sport": "all", "market": "all",
            "promo_type": "bonus_bet", "bet_amount": 25, "min_odds": None}
    dd_far  = _promo_edge(300, 28.0, market="dd", sport="NBA",
                          commence=_iso_in(6 * 60))
    dd_soon = _promo_edge(300, 28.0, market="dd", sport="NBA",
                          commence=_iso_in(30))
    assert _promo_matches(allm, dd_far) is False
    assert _promo_matches(allm, dd_soon) is True
    # Diagnostics peek past the gate to explain "too early" for all-market.
    assert _promo_matches(allm, dd_far, ignore_prop_window=True) is True
    # ...but a dd-TARGETED promo has no fallback, so it admits the prop
    # early and the picker flags participation instead of holding.
    dd_promo = {"book": "DraftKings", "sport": "all", "market": "dd",
                "promo_type": "straight", "boost_pct": 25, "min_odds": None}
    assert _promo_matches(dd_promo, dd_far) is True
    assert _promo_matches(dd_promo, dd_soon) is True
    assert _pick_needs_participation_warn(dd_far) is True
    assert _pick_needs_participation_warn(dd_soon) is False


def test_promo_matches_game_lines_unaffected_by_lineup_window():
    # The void gate is about participation risk — game lines settle no
    # matter what and stay eligible all day.
    promo = {"book": "DraftKings", "sport": "all", "market": "all",
             "promo_type": "bonus_bet", "bet_amount": 25,
             "min_odds": None}
    h2h_far = _promo_edge(300, 28.0, market="h2h",
                          commence=_iso_in(6 * 60))
    assert _promo_matches(promo, h2h_far) is True


# ── UFC promo coverage ─────────────────────────────────────────────────
# Regression for the migration gap: The Odds API (primary feed) titles
# the mma_mixed_martial_arts key "MMA", but the internal sport code and
# parsed promos use "ufc". Without the "mma" -> "ufc" canonical alias,
# every ufc-scoped promo silently matched nothing. Same failure class as
# the tennis ATP/WTA-title break.

from odds_compare import _canonical_promo_sport, _derive_promo_scope


def test_mma_title_canonicalises_to_ufc():
    # The Odds API sport_title for mma_mixed_martial_arts is "MMA".
    assert _canonical_promo_sport("MMA") == "ufc"
    assert _canonical_promo_sport("mma") == "ufc"
    # Direct DK/FD scrapers tag sport.upper() = "UFC" — must also resolve.
    assert _canonical_promo_sport("UFC") == "ufc"
    assert _canonical_promo_sport("ufc") == "ufc"


def test_ufc_promo_parses_both_aliases():
    for text in ("dk 50% ufc", "dk 50% mma"):
        promo = parse_promo(text)
        assert promo is not None, text
        assert promo["sport"] == "ufc", text
        assert promo["boost_pct"] == 50
    # Moneyline keyword resolves to h2h for a UFC bout.
    ml = parse_promo("fd 30% ufc ml")
    assert ml["sport"] == "ufc"
    assert ml["market"] == "h2h"


def test_ufc_promo_matches_mma_labelled_edge():
    # The exact live break: an Odds-API UFC edge (sport_title "MMA")
    # against a ufc-scoped promo. Pre-fix this returned False.
    promo = {"book": "DraftKings", "sport": "ufc", "market": "all",
             "promo_type": "bonus_bet", "bet_amount": 25, "min_odds": None}
    edge = _promo_edge(280, 30.0, market="h2h", sport="MMA",
                       commence=_iso_in(8 * 60))
    assert _promo_matches(promo, edge) is True
    # Explicit moneyline targeting matches too.
    ml_promo = {"book": "DraftKings", "sport": "ufc", "market": "h2h",
                "promo_type": "straight", "boost_pct": 50, "min_odds": None}
    assert _promo_matches(ml_promo, edge) is True


def test_ufc_h2h_not_gated_by_lineup_window():
    # UFC moneylines are settle-always game lines, not voidable props —
    # a bout 8h out must stay eligible (no 75-min prop gate). Pins the
    # decision that the recent void-window change does not touch UFC.
    promo = {"book": "DraftKings", "sport": "ufc", "market": "all",
             "promo_type": "bonus_bet", "bet_amount": 25, "min_odds": None}
    far = _promo_edge(280, 30.0, market="h2h", sport="MMA",
                      commence=_iso_in(8 * 60))
    assert _promo_matches(promo, far) is True


def test_scope_ufc_h2h_routes_to_ufc_fetch():
    scope = _derive_promo_scope([
        {"book": "DraftKings", "sport": "ufc", "market": "h2h"},
    ])
    assert scope is not None
    assert scope["sports"] == {"ufc"}
    assert scope["prop_sport_markets"] == {}


# ── Combat-sport promo horizon ─────────────────────────────────────────
# 2026-06-13: a FanDuel UFC promo ~40h before the Sunday White House card
# returned nothing — find_edges/_promo_matches capped the promo reach at
# 1 day, so the weekly card (lines posted days ahead) never entered the
# pool. Combat sports now reach 4 days; daily sports stay at 1.

def test_promo_horizon_days_per_sport():
    from odds_compare import _promo_horizon_days, MAX_PROMO_DAYS
    # Combat sports — both Odds API title and internal code resolve.
    assert _promo_horizon_days("MMA") == 4.0
    assert _promo_horizon_days("ufc") == 4.0
    assert _promo_horizon_days("Boxing") == 4.0
    # Daily sports keep the 1-day default.
    assert _promo_horizon_days("MLB") == MAX_PROMO_DAYS
    assert _promo_horizon_days("NBA") == MAX_PROMO_DAYS
    assert _promo_horizon_days("") == MAX_PROMO_DAYS


def test_ufc_promo_surfaces_card_beyond_one_day():
    # The White House-card miss: a UFC bout ~40h out must now match a UFC
    # promo, while a daily-sport edge the same distance out still does not.
    ufc_promo = {"book": "FanDuel", "sport": "ufc", "market": "all",
                 "promo_type": "straight", "boost_pct": 50, "min_odds": -200}
    ufc_far = _promo_edge(-150, 62.0, market="h2h", sport="MMA",
                          book="FanDuel", commence=_iso_in(40 * 60))
    assert _promo_matches(ufc_promo, ufc_far) is True
    mlb_promo = {"book": "FanDuel", "sport": "mlb", "market": "all",
                 "promo_type": "straight", "boost_pct": 50, "min_odds": -200}
    mlb_far = _promo_edge(-150, 62.0, market="h2h", sport="MLB",
                          book="FanDuel", commence=_iso_in(40 * 60))
    assert _promo_matches(mlb_promo, mlb_far) is False


def test_starts_within_promo_horizon_uses_sport():
    from odds_compare import _starts_within_promo_horizon
    # 40h out: inside the UFC reach, outside MLB's.
    assert _starts_within_promo_horizon(
        {"sport": "MMA", "commence": _iso_in(40 * 60)}) is True
    assert _starts_within_promo_horizon(
        {"sport": "MLB", "commence": _iso_in(40 * 60)}) is False
    # 5 days out: beyond even the combat reach.
    assert _starts_within_promo_horizon(
        {"sport": "MMA", "commence": _iso_in(5 * 24 * 60)}) is False


# ── HR-targeted promo no longer held (end to end) ──────────────────────

from odds_compare import compute_promo_picks


def test_hr_targeted_promo_picks_early_with_participation_warn():
    # The 2026-06-13 complaint: a FanDuel "30% MLB HR" boost was HELD 12h
    # before first pitch and produced no pick — wasting a token that can
    # ONLY be spent on an HR prop. It must now produce a pick, flagged to
    # confirm the batter is in the lineup.
    hr_promo = {"book": "FanDuel", "sport": "mlb", "market": "hr",
                "promo_type": "straight", "boost_pct": 30,
                "min_odds": None, "label": "FD 30% MLB HR"}
    hr_edge = _promo_edge(350, 26.0, market="hr", sport="MLB",
                          book="FanDuel", commence=_iso_in(12 * 60))
    picks = compute_promo_picks([hr_edge], [hr_promo])
    assert len(picks) == 1
    p = picks[0]
    assert p["edge"] is hr_edge            # picked, not held
    assert p["reason"] is None
    assert p["participation_warn"] is True


def test_hr_targeted_promo_no_warn_inside_window():
    # Same promo, but first pitch is 30 min out (lineups posted) — pick
    # stands with no participation warning.
    hr_promo = {"book": "FanDuel", "sport": "mlb", "market": "hr",
                "promo_type": "straight", "boost_pct": 30,
                "min_odds": None, "label": "FD 30% MLB HR"}
    hr_edge = _promo_edge(350, 26.0, market="hr", sport="MLB",
                          book="FanDuel", commence=_iso_in(30))
    picks = compute_promo_picks([hr_edge], [hr_promo])
    assert len(picks) == 1
    assert picks[0]["edge"] is hr_edge
    assert picks[0]["participation_warn"] is False


# ── Money-back-on-draw (Draw No Bet) promo tests ──────────────────────
from odds_compare import (
    _draw_refund_ev_pct, _promo_kelly, _is_draw_insurable, KELLY_FRACTION,
)


def _ml_team_edge(**overrides):
    """A 3-way soccer moneyline TEAM edge as find_edges builds it for a
    World Cup game, carrying the draw probability draw insurance values."""
    edge = {
        "book":           "DraftKings",
        "sport":          "Soccer",
        "sport_key":      "soccer_fifa_world_cup",
        "market":         "h2h",
        "odds_decimal":   2.0,        # +100
        "outcome":        "Brazil",
        "game":           "Brazil @ Serbia",
        "fair_prob":      45.0,       # P(win) = 45%
        "draw_fair_prob": 26.0,       # P(draw) = 26%
    }
    edge.update(overrides)
    return edge


def test_parse_promo_money_back_on_draw_pins_moneyline():
    p = parse_promo("dk world cup money back on draw")
    assert p["refund_on_draw"] is True
    assert p["sport"]  == "world_cup"
    assert p["market"] == "h2h"          # pinned: insurance is moneyline-only
    assert p["boost_pct"] is None        # valid with no boost %
    assert "money back on draw" in p["label"]


def test_parse_promo_draw_no_bet_and_dnb_shorthand():
    assert parse_promo("fd draw no bet world cup")["refund_on_draw"] is True
    assert parse_promo("dk wc dnb")["refund_on_draw"] is True
    # A plain boost is unaffected.
    assert parse_promo("dk 50% world cup")["refund_on_draw"] is False


def test_parse_promo_boost_plus_draw_insurance():
    p = parse_promo("fd 50% world cup ml money back on draw")
    assert p["refund_on_draw"] is True
    assert p["boost_pct"] == 50
    assert p["market"] == "h2h"
    assert "50" in p["label"] and "money back on draw" in p["label"]


def test_draw_refund_ev_equals_draw_probability():
    promo = _wc_promo(market="h2h", refund_on_draw=True)
    # Refunding the stake on a draw is worth exactly P(draw) in EV%.
    assert _draw_refund_ev_pct(promo, _ml_team_edge(draw_fair_prob=26.0)) == 26.0
    # A non-insured promo gets nothing.
    assert _draw_refund_ev_pct(_wc_promo(market="h2h"), _ml_team_edge()) == 0.0


def test_draw_refund_only_applies_to_insurable_moneylines():
    promo = _wc_promo(market="h2h", refund_on_draw=True)
    # The Draw outcome itself can't be insured against a draw.
    assert _draw_refund_ev_pct(promo, _ml_team_edge(outcome="Draw")) == 0.0
    # A 2-way moneyline (no draw_fair_prob) gets no refund value.
    no_draw = _ml_team_edge(); no_draw.pop("draw_fair_prob")
    assert _draw_refund_ev_pct(promo, no_draw) == 0.0
    assert _is_draw_insurable(_ml_team_edge()) is True


def test_promo_matches_draw_insurance_team_ml_only():
    promo = _wc_promo(market="h2h", refund_on_draw=True)
    assert _promo_matches(promo, _ml_team_edge()) is True
    # Rejects the Draw outcome, 2-way moneylines, and non-h2h markets.
    assert _promo_matches(promo, _ml_team_edge(outcome="Draw")) is False
    no_draw = _ml_team_edge(); no_draw.pop("draw_fair_prob")
    assert _promo_matches(promo, no_draw) is False
    assert _promo_matches(promo, _ml_team_edge(market="totals")) is False


def test_dnb_kelly_lifts_stake_and_reduces_to_plain_when_no_draw():
    promo = _wc_promo(market="h2h", refund_on_draw=True)
    edge  = _ml_team_edge(odds_decimal=2.0, fair_prob=45.0, draw_fair_prob=26.0)
    p_win = 0.45
    dnb   = _promo_kelly(promo, edge, edge["odds_decimal"], p_win)
    # A draw can't lose, so DNB Kelly stakes strictly more than plain.
    assert dnb > kelly_bet(p_win, edge["odds_decimal"])
    # Exact push-aware formula: f = (p_win*b - p_opp)/(b*(p_win+p_opp)) * frac
    b, p_draw = 1.0, 0.26
    p_opp = 1 - p_win - p_draw
    expected = (p_win * b - p_opp) / (b * (p_win + p_opp)) * KELLY_FRACTION
    assert abs(dnb - expected) < 1e-9
    # With no draw it collapses to the standard two-way Kelly (use a +EV
    # price so both are positive, not clamped at 0).
    e0 = _ml_team_edge(draw_fair_prob=0.0, odds_decimal=3.0)
    assert abs(_promo_kelly(promo, e0, e0["odds_decimal"], p_win)
               - kelly_bet(p_win, e0["odds_decimal"])) < 1e-9


def test_draw_insurance_boosted_ev_is_winEV_plus_drawprob():
    # End-to-end valuation: boosted EV = base win EV + P(draw).
    promo = _wc_promo(market="h2h", refund_on_draw=True)
    edge  = _ml_team_edge(odds_decimal=2.0, fair_prob=45.0, draw_fair_prob=26.0)
    base  = calc_ev(0.45, 2.0)                 # (0.45*2 - 1)*100 = -10.0%
    boosted = base + _draw_refund_ev_pct(promo, edge)
    assert abs(boosted - 16.0) < 1e-6          # -10 + 26 = +16%
