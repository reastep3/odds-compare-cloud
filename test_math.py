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


def test_promos_want_hr_mlb_all_market_promo():
    # An "fd 30% mlb" boost with market=all could land on HR via the
    # broader pool selection — must fetch HR so the picker can see it.
    assert _promos_want_hr([
        {"book": "FanDuel", "sport": "mlb", "market": "all"}
    ]) is True


def test_promos_want_hr_all_sport_all_market_promo():
    # Catch-all bonus_bet — picker selects longest-odds line from any
    # market, and HR is a typical long-odds winner. Must fetch HR.
    assert _promos_want_hr([
        {"book": "FanDuel", "sport": "all", "market": "all",
         "promo_type": "bonus_bet"}
    ]) is True


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


def test_qualifies_tennis_h2h_non_pinn_floor_2pct():
    # Was fully blocked; broad pool +8.3% on 266 picks.
    assert not _qualifies(_q("TENNIS", "h2h", 1.9))
    assert _qualifies(_q("TENNIS", "h2h", 2.0))
    assert _qualifies(_q("TENNIS", "h2h", 4.9))
    # Above the universal cap.
    assert not _qualifies(_q("TENNIS", "h2h", 5.0))


def test_qualifies_tennis_h2h_pinn_qualifies_at_1pct():
    assert _qualifies(_q("TENNIS", "h2h", 1.0, pinn=True))
    assert not _qualifies(_q("TENNIS", "h2h", 0.9, pinn=True))


def test_qualifies_boxing_h2h_non_pinn_floor_1pct():
    # Broad 1–5% +11.7% on 14 picks; ≥5% drops to -2.5% — universal cap
    # handles the upper bound.
    assert not _qualifies(_q("BOXING", "h2h", 0.9))
    assert _qualifies(_q("BOXING", "h2h", 1.0))
    assert _qualifies(_q("BOXING", "h2h", 4.9))
    assert not _qualifies(_q("BOXING", "h2h", 5.0))


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


def test_qualifies_prop_pts_floor_2pct():
    # pts threshold 3.0% → 2.0%; broad 2–3% pts +35.3%.
    e_in  = {"market": "pts", "ev_pct": 2.0}
    e_out = {"market": "pts", "ev_pct": 1.9}
    assert _qualifies_prop(e_in)
    assert not _qualifies_prop(e_out)


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
