"""
Odds Compare — +EV Edge Finder (Scraper Edition)
=================================================
No paid API required. Three data sources, zero cost:

  1. Action Network (internal API) — aggregates up to 13 US books
     (DraftKings, FanDuel, BetMGM, Caesars, BetRivers, ESPN Bet,
     Bet365, Fanatics, Hard Rock, PointsBet, BetParx, Fliff, WynnBet).

  2. Pinnacle (public guest API) — the sharpest book on the planet
     (~2% vig). Injected as the primary fair-line anchor, weighting
     65% of the fair-probability estimate when available.

  3. Bovada (public JSON API) — large offshore book, adds an extra
     sharp consensus line for games Pinnacle doesn't cover.

All sports are fetched in parallel (Action Network + Pinnacle +
Bovada simultaneously per sport). Fair-probability logic, Kelly
sizing, and Excel output are unchanged from the API version.

Usage:
    python odds_compare.py

Troubleshooting:
  • 403 from Action Network — open actionnetwork.com/odds in Chrome,
    find a /web/v1/games request in DevTools → Network, copy the
    headers/cookies and paste them into SCRAPER_HEADERS below.
  • 401 from Pinnacle — their guest key rotates occasionally. Open
    pinnacle.com in Chrome, DevTools → Network, grab the X-Api-Key
    header from any request to guest.api.arcadia.pinnacle.com and
    update PINNACLE_GUEST_KEY below.
  • Sport returns 0 games — league may be off-season, or AN/Pinnacle
    uses a different code. See AN_SPORTS / PINNACLE_LEAGUES below.
  • Bovada returns nothing — they block certain IPs/regions. The
    tool degrades gracefully; AN + Pinnacle still run.
"""

import glob
import os
import re
import shutil
import sys
import time
try:
    import tkinter as tk
    from tkinter import simpledialog
    _TK_AVAILABLE = True
except ImportError:
    # Headless environment (e.g. Alpine/slim Python images) — tkinter not
    # installed. The module still needs to be importable so run_headless_fetch()
    # and run_headless_finalize() work for the Telegram bot / cron jobs.
    # The GUI dialog classes below reference tk.Toplevel as a base class, so
    # we provide a stub that satisfies class definition but raises at instantiation.
    _TK_AVAILABLE = False

    class _TkUnavailable:
        def __init__(self, *args, **kwargs):
            raise RuntimeError(
                "tkinter is unavailable on this system — use run_headless_fetch() "
                "/ run_headless_finalize() or the Telegram bot instead."
            )

    class _TkStub:
        Tk = Toplevel = Frame = Label = Button = Entry = OptionMenu = \
            Checkbutton = StringVar = BooleanVar = _TkUnavailable
        LEFT = "left"; END = "end"

    class _SimpleDialogStub:
        @staticmethod
        def askfloat(*a, **kw): raise RuntimeError("tkinter unavailable")
        @staticmethod
        def askstring(*a, **kw): raise RuntimeError("tkinter unavailable")

    tk = _TkStub()
    simpledialog = _SimpleDialogStub()
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from difflib import SequenceMatcher
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

import json

import requests
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # dotenv optional; env vars may be set by the OS

import credit_tracker

EASTERN = ZoneInfo("America/New_York")

# ── Runtime paths (cloud-aware) ──────────────────────────────────────
# On cloud, set OUTPUT_DIR and DATA_DIR to a persistent volume mount.
# Locally (unset), both default to the script's directory.
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
# Treat empty-string env values the same as unset — a `.env` file with
# `OUTPUT_DIR=` (no value) would otherwise pass through as "" and break
# os.makedirs("").
OUTPUT_DIR  = os.environ.get("OUTPUT_DIR") or _SCRIPT_DIR
DATA_DIR    = os.environ.get("DATA_DIR")   or _SCRIPT_DIR
os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs(DATA_DIR,   exist_ok=True)


# ──────────────────────────────────────────────────────────────────────
# Action Network config
# ──────────────────────────────────────────────────────────────────────

# Internal book IDs used by Action Network's v2 scoreboard API.
# These are the "parent" book IDs returned in the scoreboard response.
# To discover new IDs: load actionnetwork.com in Chrome, open DevTools →
# Network, find any /web/v2/scoreboard/ call and look at the markets keys.
AN_BOOK_IDS: dict[int, str] = {
    # ── US legal sportsbooks (v2 scoreboard parent IDs) ──────────────
    68:   "DraftKings",      # parent_id=10, confirmed 2026-03-29
    69:   "FanDuel",         # parent_id=3,  confirmed 2026-03-29
    75:   "BetMGM",          # parent_id=1,  confirmed 2026-03-29
    49:   "Caesars",         # parent_id=2,  confirmed 2026-03-29
    71:   "BetRivers",       # parent_id=5,  confirmed 2026-03-29
    15:   "ESPN Bet",        # parent_id=18, confirmed 2026-03-29
    30:   "Bet365",          # confirmed 2026-03-29
    79:   "Bet365 NJ",       # alternate bet365 entity
    1005: "Caesars NY",      # state-specific Caesars
    1006: "FanDuel NY",      # state-specific FanDuel
    972:  "BetRivers NY",    # state-specific BetRivers
}

# Normalised book keys we actually bet on (must match _book_key() output)
TARGET_BOOKS = {"draftkings", "fanduel"}

# Master sport iteration list. Drives _fetch_sport_all() across the full
# scan. A sport in this list MUST have an ODDS_API_SPORTS entry; the
# legacy AN/Pinnacle/Bovada/DK/FD-direct lookups below are optional and
# only used if the Odds API fetch returns empty for that sport.
#
# Slug naming: lowercase, internal codes (not API keys). Legacy original
# 8 (nba, ncaab, nfl, ncaaf, mlb, nhl, ufc, soccer) kept for AN-fallback
# slug compatibility. New entries below them are API-only.
AN_SPORTS = [
    "nba",
    "ncaab",
    "nfl",
    "ncaaf",
    "mlb",
    "nhl",
    "ufc",       # was "MMA" — v2 uses "ufc"
    "soccer",    # multi-league: see ODDS_API_SPORTS["soccer"]
    # ── API-only additions (no AN fallback configured) ────────────────
    "wnba",      # WNBA — season starts mid-May
    "afl",       # Australian Rules Football
    "boxing",    # Major boxing matches
    "cricket",   # IPL + other major cricket tournaments
    "tennis",    # ATP/WTA — sport key resolved dynamically per-tournament
]

ACTION_NETWORK_BASE = "https://api.actionnetwork.com/web/v2"

# ──────────────────────────────────────────────────────────────────────
# The Odds API config
# ──────────────────────────────────────────────────────────────────────

ODDS_API_BASE = "https://api.the-odds-api.com/v4"

# Map internal sport codes → The Odds API sport keys.
#
# Special values:
#   "_dynamic_active:<prefix>"  → at fetch time, hit /sports?all=true and
#                                 use every active sport key starting with
#                                 <prefix>. Used for sports with rotating
#                                 per-tournament keys (tennis, golf majors).
ODDS_API_SPORTS: dict[str, str | list[str]] = {
    # US team sports
    "nba":     "basketball_nba",
    "ncaab":   "basketball_ncaab",
    "wnba":    "basketball_wnba",
    "nfl":     "americanfootball_nfl",
    "ncaaf":   "americanfootball_ncaaf",
    "mlb":     "baseball_mlb",
    "nhl":     "icehockey_nhl",
    "ufc":     "mma_mixed_martial_arts",
    # Soccer — top European + American leagues. Add/remove as the
    # calendar dictates. A league with no in-season fixtures returns
    # an empty list at no credit cost.
    "soccer": [
        "soccer_usa_mls",
        "soccer_epl",
        "soccer_uefa_champs_league",
        "soccer_uefa_europa_league",
        "soccer_uefa_europa_conference_league",
        "soccer_spain_la_liga",
        "soccer_italy_serie_a",
        "soccer_germany_bundesliga",
        "soccer_france_ligue_one",
        "soccer_mexico_ligamx",
        # FIFA World Cup 2026 (Jun 11 – Jul 19, US/MEX/CAN). Key verified
        # active on the /sports endpoint 2026-06-10; returns [] at no
        # credit cost once the tournament ends. The _qualifiers_* /
        # _womens / club-WC / _winner (outrights) keys are intentionally
        # NOT listed — they are separate competitions.
        "soccer_fifa_world_cup",
    ],
    # World Cup prop-fetch handle. Game lines for the tournament ride
    # the "soccer" list above; this dedicated key exists because the
    # prop pipeline (fetch_odds_api_props) resolves one sport_key per
    # prop sport — resolving "soccer" would pick the first list entry
    # (MLS) and miss the tournament entirely.
    "world_cup": "soccer_fifa_world_cup",
    # Other sports
    "afl":     "aussierules_afl",
    "boxing":  "boxing_boxing",
    "cricket": ["cricket_ipl"],
    # Tennis: the Odds API uses one key per tournament (e.g.
    # tennis_atp_french_open). Resolved at fetch time so the active
    # ATP/WTA events on tour are picked up automatically.
    "tennis":  "_dynamic_active:tennis_",
}

# Books to request from The Odds API
ODDS_API_BOOKMAKERS = (
    "draftkings,fanduel,pinnacle,betmgm,bovada,"
    "caesars,betrivers,espnbet,bet365,lowvig,betonlineag"
)

# Player prop market keys by sport.
#
# Each per-event call costs `(unique markets returned) × regions` credits,
# so adding markets here multiplies prop-fetch cost. The picks below are
# the markets with historically softest pricing on DK/FD vs Pinnacle —
# i.e. the highest +EV per credit spent.
ODDS_API_PROP_MARKETS: dict[str, list[tuple[str, str, str]]] = {
    # (api_market_key, internal_market_key, display_name)
    "nba": [
        ("player_points",                    "pts",         "Points"),
        ("player_rebounds",                  "reb",         "Rebounds"),
        ("player_assists",                   "ast",         "Assists"),
        ("player_threes",                    "threes",      "3-Pointers"),
        ("player_points_rebounds_assists",   "pra",         "Pts+Reb+Ast"),
        ("player_double_double",             "dd",          "Double-Double"),
    ],
    "mlb": [
        ("pitcher_strikeouts",          "strikeouts",  "Strikeouts"),
        ("batter_total_bases",          "total_bases", "Total Bases"),
        # HR routes through the ALTERNATE market key because DraftKings
        # and FanDuel (the bet-target books) only expose home-run odds
        # via batter_home_runs_alternate — they don't surface the plain
        # batter_home_runs market to the Odds API at all. The standard
        # market is exposed by Caesars / Fliff / ESPN BET, but losing
        # them is acceptable: we still keep ESPN BET + Hard Rock as
        # reference books via the alternate, and the user's primary
        # action is on DK/FD. Parser filters to point=0.5 (= "1+ HR" =
        # anytime). Discovered 2026-05-26 after the user reported
        # FanDuel showing HR prices in-app that the bot couldn't fetch.
        ("batter_home_runs_alternate",  "hr",          "Anytime HR"),
        ("batter_hits",                 "hits",        "Hits"),
        ("batter_rbis",                 "rbis",        "RBIs"),
    ],
    "nhl": [
        ("player_shots_on_goal", "sog",     "Shots on Goal"),
        ("player_points",        "nhl_pts", "Points"),
    ],
    # FIFA World Cup 2026. Anytime goalscorer is Yes-only on every book
    # (verified 2026-06-10: DK 46 / FD 46 / Pinnacle 39 / BetRivers 40
    # outcomes, all name="Yes" with the player in description) — it
    # rides the same one-sided consensus path as anytime HR. The other
    # WC props the API exposes are skipped on purpose: shots /
    # shots-on-target are FanDuel+BetRivers only (fails the 2-reference
    # floor when pricing FD), first/last goalscorer is unpickable
    # variance, cards lack FD, and btts / draw_no_bet add little over
    # the h2h/totals lines the soccer fetch already pulls.
    "world_cup": [
        ("player_goal_scorer_anytime", "goal", "Anytime Goalscorer"),
    ],
}


# First-inning / partial-game markets per sport. Like props, these are
# alternate markets only available on the per-event endpoint (sport-level
# returns 422). Promo-only — bypass _qualifies(), flow into promo_pool so
# the FanDuel "First Inning Friday" 50% boost (and similar) can pick them.
ODDS_API_FIRST_INNING_MARKETS: dict[str, list[tuple[str, str, str]]] = {
    # (api_market_key, internal_market_key, display_name)
    "mlb": [
        ("totals_1st_1_innings", "totals_1st", "1st Inning Total"),
    ],
}


# ──────────────────────────────────────────────────────────────────────
# Pinnacle config
# ──────────────────────────────────────────────────────────────────────

# Pinnacle's public guest API — no account needed. The key is their own
# frontend's guest key (embedded in pinnacle.com) and rotates periodically.
# Set via PINNACLE_GUEST_KEY env var; see .env.example for how to grab it.
PINNACLE_BASE       = "https://guest.api.arcadia.pinnacle.com/0.1"
PINNACLE_GUEST_KEY  = os.environ.get("PINNACLE_GUEST_KEY", "")

# Pinnacle league IDs — map from AN sport code to Pinnacle league ID(s).
# To find an ID: open Pinnacle, pick the league, inspect any
# /0.1/leagues/{id}/... request in DevTools → Network.
# Soccer maps to multiple Pinnacle leagues (one per competition).
PINNACLE_LEAGUES: dict[str, int | list[int]] = {
    "nba":    487,
    "ncaab":  493,
    "nfl":    889,
    "ncaaf":  880,
    "mlb":    246,
    "nhl":    1456,
    "ufc":    238,    # UFC (primary MMA org on Pinnacle)
    "soccer": [2764,  # MLS
               1980,  # EPL (England - Premier League)
               2196,  # La Liga (Spain)
               2436,  # Serie A (Italy)
               1842,  # Bundesliga (Germany)
               2036,  # Ligue 1 (France)
               ],
}

# Weight given to Pinnacle's implied probability when blending with
# the Action Network consensus. Pinnacle is the most efficient market
# in the world (~2% vig), so we lean on it heavily.
PINNACLE_WEIGHT = 0.65   # 65% Pinnacle, 35% AN consensus


# ──────────────────────────────────────────────────────────────────────
# Bovada config  (offshore, no account needed, JSON API)
# ──────────────────────────────────────────────────────────────────────

# Bovada's public JSON coupon API — returns pre-match lines only.
# Their odds are used purely as an additional consensus data point
# (same weight as any other non-Pinnacle book). Bovada is a high-volume
# offshore book with competitive lines, useful when Pinnacle is unavailable.
BOVADA_BASE = (
    "https://www.bovada.lv/services/sports/event/coupon/events/A/description"
)

# Maps AN sport codes → Bovada URL path segments.
# Verify paths at bovada.lv if a sport returns nothing.
BOVADA_SPORT_PATHS: dict[str, str] = {
    "nba":    "basketball/nba",
    "ncaab":  "basketball/college-basketball",
    "nfl":    "football/nfl",
    "ncaaf":  "football/college-football",
    "mlb":    "baseball/mlb",
    "nhl":    "ice-hockey/nhl",
    "ufc":    "fighting/mma",
    "soccer": "soccer/mls",
}

BOVADA_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept":          "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer":         "https://www.bovada.lv/",
    "Origin":          "https://www.bovada.lv",
}

# Bovada market description substrings → internal market keys
BOVADA_MARKET_MAP = {
    "moneyline":   "h2h",
    "point spread": "spreads",
    "spread":      "spreads",
    "run line":    "spreads",   # MLB alternate term
    "puck line":   "spreads",   # NHL alternate term
    "total":       "totals",
    "game total":  "totals",
    "over/under":  "totals",
}


# ──────────────────────────────────────────────────────────────────────
# General config
# ──────────────────────────────────────────────────────────────────────

MIN_EV_PERCENT = 0.5   # minimum EV% to include in output
KELLY_FRACTION = 0.5   # half-Kelly (industry standard for +EV bettors)
MAX_WORKERS    = 6     # parallel sport-fetch threads

# Player-prop line-discrepancy guard.
# When Pinnacle is absent, require at least this many non-target books
# pricing the SAME line before trusting the consensus fair price.
# Prevents a single soft book pricing an unusual line from being used
# as the sole reference for EV calculation.
MIN_PROP_REFERENCE_BOOKS = 2

# Stale-target-line guard for one-sided / outright markets (golf, HR,
# player-prop yes/no like double-double). For markets where fair_prob is
# derived from a multi-book median without over/under devig, a single
# stale or erroneous target-book line shows up as a giant pseudo-EV
# instead of being smoothed out. Require the target book's implied
# probability to be at least this fraction of the consensus — anything
# below means the target line is >1.8x longer than fair, which in
# practice has always been bad data (e.g. DK at +10500 on a player most
# books have at +1500), never a real edge. Calibrated against the
# Maverick McNealy PGA Championship incident (ratio ~0.15) and the
# Kazuki Higa PGA Top-20 incident 2026-05-16 — DK at +7200 vs consensus
# implying ~+3000 (ratio ~0.43), which slipped past the prior 0.4
# threshold and surfaced a pathological +201.8% boosted EV pick.
MIN_TARGET_OUTLIER_PROB_RATIO = 0.55

# Golf outright markets (golf_win / golf_top5 / golf_top20) are
# disabled 2026-05-22 after a third stale-line incident in three weeks:
#
#   Si Woo Kim, CJ Cup Byron Nelson — system read DK at +1275 while the
#   actual DK app showed +600. The 20% profit boost on +1275 surfaced
#   a 115.3% boosted EV pick that was entirely fictional. The +1275
#   number passed the MIN_TARGET_OUTLIER_PROB_RATIO=0.55 guard at
#   exactly 0.55 (target implied 7.3% vs consensus 13.2%).
#
# The structural problem: PGA outright winners are futures markets on
# 150+ player fields. Books update long-shot odds infrequently — when
# one book leaves a week-old number while peers move, our consensus
# picks the stale book up as a +EV target. Each calibration cycle has
# had to push the outlier-ratio higher (0.4 → 0.55) and the boosted-EV
# asymmetry (small bet, looks-like-100%-EV) is exactly the wrong shape
# for promo optimization.
#
# When this flips back to False, also re-enable the three
# find_golf_edges tests guarded by GOLF_DISABLED in test_math.py and
# confirm fresh stale-line guards (per-market-class, time-decay-aware)
# are in place upstream of find_golf_edges.
GOLF_DISABLED = True

# Minimum bet amounts enforced by each book
MIN_BET = {
    "DraftKings": 0.10,
    "FanDuel":    0.09,
}

SCRAPER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept":          "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer":         "https://www.actionnetwork.com/",
    "Origin":          "https://www.actionnetwork.com",
}

# Override SCRAPER_HEADERS from env (JSON blob) for cloud/rotating creds.
_headers_env = os.environ.get("SCRAPER_HEADERS")
if _headers_env:
    try:
        SCRAPER_HEADERS = json.loads(_headers_env)
    except json.JSONDecodeError:
        print("[WARN] SCRAPER_HEADERS env var is not valid JSON; using default.")


# ──────────────────────────────────────────────────────────────────────
# Math helpers  (unchanged from API version)
# ──────────────────────────────────────────────────────────────────────

def american_to_decimal(american: int | float) -> float:
    if american > 0:
        return (american / 100) + 1
    else:
        return (100 / abs(american)) + 1


def decimal_to_implied_prob(decimal_odds: float) -> float:
    return 1 / decimal_odds


def calc_ev(fair_prob: float, decimal_odds: float) -> float:
    return (fair_prob * (decimal_odds - 1) - (1 - fair_prob)) * 100


def kelly_bet(fair_prob: float, decimal_odds: float,
              fraction: float = KELLY_FRACTION) -> float:
    b = decimal_odds - 1
    q = 1 - fair_prob
    if b <= 0:
        return 0
    kelly = (fair_prob * b - q) / b
    return max(0, kelly * fraction)


def clamp_bet(amount: float, book: str) -> float:
    """Enforce book minimum bet. Returns 0 if Kelly says bet nothing."""
    if amount <= 0:
        return 0.0
    minimum = MIN_BET.get(book, 0.10)
    return max(amount, minimum)


def format_american(decimal_odds: float) -> str:
    if decimal_odds >= 2.0:
        return f"+{round((decimal_odds - 1) * 100)}"
    else:
        return f"-{round(100 / (decimal_odds - 1))}"


def _fmt_am(odds_int) -> str:
    """Format an integer American odds value with +/- prefix."""
    if odds_int is None:
        return ""
    odds_int = int(odds_int)
    return f"+{odds_int}" if odds_int > 0 else str(odds_int)


def _fmt_pt(point, market: str = "") -> str:
    """Format a point/line value. Only add '+' prefix for spreads."""
    if point is None:
        return ""
    val = float(point)
    if market.lower() == "spreads" and val > 0:
        return f"+{point}"
    if market.lower() == "spreads" and val == 0:
        return "0"
    return str(point)


# ──────────────────────────────────────────────────────────────────────
# Session factory
# ──────────────────────────────────────────────────────────────────────

def make_session() -> requests.Session:
    """Requests session with browser-like headers. Not thread-safe — one per thread."""
    s = requests.Session()
    s.headers.update(SCRAPER_HEADERS)
    return s


# ──────────────────────────────────────────────────────────────────────
# The Odds API — primary data source
# ──────────────────────────────────────────────────────────────────────

_DYNAMIC_ACTIVE_CACHE: dict[str, tuple[float, list[str]]] = {}
_DYNAMIC_ACTIVE_TTL = 3600   # seconds — refresh active tournaments hourly


def _resolve_dynamic_active_keys(prefix: str) -> list[str]:
    """
    Resolve a `_dynamic_active:<prefix>` token to the currently-active
    Odds API sport keys whose `key` field starts with `prefix`.

    Used for sports where The Odds API exposes one key per tournament
    (tennis, golf majors). Cached for an hour to avoid hammering the
    free /sports endpoint on parallel sport-fetch threads.
    """
    from bot_config import ODDS_API_KEY

    cached = _DYNAMIC_ACTIVE_CACHE.get(prefix)
    if cached and (time.time() - cached[0]) < _DYNAMIC_ACTIVE_TTL:
        return cached[1]

    try:
        r = requests.get(
            f"{ODDS_API_BASE}/sports",
            params={"apiKey": ODDS_API_KEY, "all": "false"},
            timeout=15,
        )
        if r.status_code != 200:
            print(f"  [OddsAPI/dynamic:{prefix}] sports list HTTP {r.status_code}")
            return []
        sports_list = r.json()
    except Exception as e:
        print(f"  [OddsAPI/dynamic:{prefix}] sports list error: {e}")
        return []

    keys = [s.get("key") for s in sports_list
            if isinstance(s, dict)
            and isinstance(s.get("key"), str)
            and s["key"].startswith(prefix)
            and s.get("active", True)]
    keys = [k for k in keys if k]
    _DYNAMIC_ACTIVE_CACHE[prefix] = (time.time(), keys)
    if keys:
        print(f"  [OddsAPI/dynamic:{prefix}] resolved {len(keys)} active "
              f"key(s): {', '.join(keys)}")
    return keys


def _odds_api_get(url: str, params: dict, *, timeout: int = 30,
                  retries: int = 3, label: str = "") -> requests.Response | None:
    """GET against The Odds API with retry/backoff on *transient* failures
    (429 rate-limit, 5xx, timeouts/connection errors). Returns the final
    Response — even if non-200 — or None when every attempt raised.
    Permanent client errors (401/403/422) return immediately; retrying
    those only burns time.

    Added 2026-06-25: the per-league soccer loop fetched soccer_fifa_world_cup
    LAST of 11 keys with a bare get() and no retry. Under the full scan's
    13-sport parallel burst (MAX_WORKERS threads, soccer doing 11 sequential
    calls) a single 429 on that last call silently dropped the World Cup
    game lines, surfacing as a promo "no World Cup candidates" when the
    moneylines were in fact posted on DK/FD. Retrying on 429 lets the WC
    call ride out the rate-limit window instead of vanishing."""
    backoff = 1.0
    last: requests.Response | None = None
    for attempt in range(1, retries + 1):
        err: Exception | None = None
        try:
            r = requests.get(url, params=params, timeout=timeout)
        except requests.RequestException as e:
            r, err, last = None, e, None
        else:
            last = r
            # Success or a permanent client error — nothing to gain by retrying.
            if r.status_code not in (429, 500, 502, 503, 504):
                return r
        if attempt == retries:
            if r is None:
                print(f"  [OddsAPI{label}] request error after {retries} "
                      f"attempt(s): {err}")
            return last
        wait = backoff
        if r is not None:
            retry_after = r.headers.get("Retry-After")
            if retry_after:
                try:
                    wait = max(wait, float(retry_after))
                except ValueError:
                    pass
            print(f"  [OddsAPI{label}] HTTP {r.status_code} — retry "
                  f"{attempt}/{retries - 1} in {wait:.1f}s")
        else:
            print(f"  [OddsAPI{label}] {type(err).__name__} — retry "
                  f"{attempt}/{retries - 1} in {wait:.1f}s")
        time.sleep(wait)
        backoff *= 2
    return last


def fetch_odds_api(sport: str, session: requests.Session | None = None,
                   ) -> tuple[list[dict], dict]:
    """
    Fetch game-line odds from The Odds API.

    Returns (events, status_summary) matching the internal event format.
    The Odds API response format is natively compatible — we just tag
    each bookmaker with source='api'.
    """
    from bot_config import ODDS_API_KEY

    api_keys = ODDS_API_SPORTS.get(sport)
    if not api_keys:
        return [], {}

    if isinstance(api_keys, str):
        # Dynamic resolver: e.g. "_dynamic_active:tennis_"
        if api_keys.startswith("_dynamic_active:"):
            prefix = api_keys.split(":", 1)[1]
            api_keys = _resolve_dynamic_active_keys(prefix)
            if not api_keys:
                return [], {}
        else:
            api_keys = [api_keys]

    all_events: list[dict] = []
    status_summary: dict[str, int] = {}

    for sport_key in api_keys:
        try:
            r = _odds_api_get(
                f"{ODDS_API_BASE}/sports/{sport_key}/odds",
                {
                    "apiKey":      ODDS_API_KEY,
                    "regions":     "us",
                    "markets":     "h2h,spreads,totals",
                    "oddsFormat":  "american",
                    "bookmakers":  ODDS_API_BOOKMAKERS,
                },
                timeout=30,
                label=f"/{sport_key}",
            )
            if r is None:
                continue
            remaining = r.headers.get("x-requests-remaining", "?")
            credit_tracker.record(remaining)
            if r.status_code != 200:
                print(f"  [OddsAPI/{sport_key}] HTTP {r.status_code}")
                continue

            events = r.json()
            for ev in events:
                # Tag each bookmaker with source for stale-line logic
                for bm in ev.get("bookmakers", []):
                    bm["source"] = "api"
                all_events.append(ev)

            status_summary["fetched"] = status_summary.get("fetched", 0) + len(events)
            print(f"  [OddsAPI/{sport_key}] {len(events)} events, "
                  f"{len(events[0]['bookmakers']) if events else 0} books "
                  f"(quota: {remaining} left)")

        except Exception as e:
            print(f"  [OddsAPI/{sport_key}] Error: {e}")

    return all_events, status_summary


def fetch_odds_api_props(
    sport: str,
    markets: set[str] | None = None,
) -> list[dict]:
    """
    Fetch player props from The Odds API for a given sport.

    Props require per-event calls (sport-level endpoint returns 422 for
    prop markets). Flow:
      1. GET /sports/{sport}/events  (free — 0 credits)
      2. For each event: GET /events/{id}/odds?markets=...  (1 credit per call)

    If `markets` is given, the markets query is restricted to those internal
    market keys (e.g. {"strikeouts"}) — saves credits when the caller only
    cares about a subset (e.g. a /run_promos run targeted at one prop).
    Unknown keys are silently skipped; an empty set after filtering returns [].

    Returns a list of raw prop dicts compatible with _merge_raw_props().
    """
    from bot_config import ODDS_API_KEY

    prop_markets = ODDS_API_PROP_MARKETS.get(sport)
    if not prop_markets:
        return []

    if markets is not None:
        prop_markets = [m for m in prop_markets if m[1] in markets]
        if not prop_markets:
            return []

    api_sport = ODDS_API_SPORTS.get(sport)
    if not api_sport:
        return []
    if isinstance(api_sport, list):
        api_sport = api_sport[0]

    api_market_keys = ",".join(m[0] for m in prop_markets)
    market_map = {m[0]: (m[1], m[2]) for m in prop_markets}

    # Step 1: Get event IDs (free — 0 credits)
    r = _odds_api_get(
        f"{ODDS_API_BASE}/sports/{api_sport}/events",
        {"apiKey": ODDS_API_KEY},
        timeout=15, label=f" Props/{sport.upper()} events",
    )
    if r is None or r.status_code != 200:
        where = "no response" if r is None else f"HTTP {r.status_code}"
        print(f"  [OddsAPI Props/{sport.upper()}] Events {where}")
        return []
    try:
        event_list = r.json()
    except ValueError:
        print(f"  [OddsAPI Props/{sport.upper()}] Events: bad JSON")
        return []

    if not event_list:
        return []

    # Credit guard: cap the per-event loop to games starting within ~26h
    # (find_edges' standard window). The /events list spans the API's
    # full lookahead — for soccer_fifa_world_cup that's the ENTIRE
    # tournament (72 events) — and each event here costs credits to
    # price. Promo matching only admits games inside MAX_PROMO_DAYS
    # (1 day) anyway. No lower bound: in-progress games keep their
    # props for /run_live use.
    from datetime import timedelta as _td
    _horizon = datetime.now(timezone.utc) + _td(hours=26)
    _kept: list[dict] = []
    for ev_info in event_list:
        try:
            ct = datetime.fromisoformat(
                str(ev_info.get("commence_time", "")).replace("Z", "+00:00"))
            if ct > _horizon:
                continue
        except (ValueError, AttributeError, TypeError):
            pass  # unparseable timestamp — keep the event
        _kept.append(ev_info)
    if len(_kept) < len(event_list):
        print(f"  [OddsAPI Props/{sport.upper()}] skipped "
              f"{len(event_list) - len(_kept)} event(s) beyond 26h "
              f"(credit guard)")
    event_list = _kept
    if not event_list:
        return []

    # Step 2: Fetch props per event
    raw: list[dict] = []
    remaining = "?"
    for ev_info in event_list:
        eid = ev_info.get("id")
        away = ev_info.get("away_team", "")
        home = ev_info.get("home_team", "")
        commence = ev_info.get("commence_time", "")
        if not eid:
            continue

        r2 = _odds_api_get(
            f"{ODDS_API_BASE}/sports/{api_sport}/events/{eid}/odds",
            {
                "apiKey":      ODDS_API_KEY,
                "regions":     "us",
                "markets":     api_market_keys,
                "oddsFormat":  "american",
                "bookmakers":  ODDS_API_BOOKMAKERS,
            },
            timeout=15, label=f" Props/{sport.upper()}",
        )
        if r2 is None:
            continue
        remaining = r2.headers.get("x-requests-remaining", remaining)
        credit_tracker.record(remaining)
        if r2.status_code != 200:
            continue
        try:
            ev_data = r2.json()
        except ValueError:
            continue

        for bm in ev_data.get("bookmakers", []):
            book_key = bm.get("key", "")
            for mkt in bm.get("markets", []):
                api_mkt_key = mkt.get("key", "")
                mapped = market_map.get(api_mkt_key)
                if not mapped:
                    continue
                internal_key, display_name = mapped

                for oc in mkt.get("outcomes", []):
                    player = oc.get("description", "")
                    raw_side = oc.get("name", "").lower()
                    # Yes/No markets (anytime HR, double-double, anytime TD)
                    # use the same vig-removal math as Over/Under, so we map
                    # Yes→over and No→under to flow through the existing
                    # pair-and-devig pipeline unchanged.
                    if raw_side in ("over", "yes"):
                        side_name = "over"
                    elif raw_side in ("under", "no"):
                        side_name = "under"
                    else:
                        continue
                    try:
                        odds = int(oc.get("price", 0))
                    except (ValueError, TypeError):
                        continue
                    line = oc.get("point")
                    try:
                        line = float(line) if line is not None else None
                    except (ValueError, TypeError):
                        line = None

                    # batter_home_runs_alternate exposes multiple lines per
                    # player (0.5 = anytime / 1+, 1.5 = 2+, etc.). Anytime
                    # HR is the 0.5 line; the higher lines are different
                    # markets — keep them out so the consensus stays clean
                    # and the HR Props tab doesn't list "2+ HR" as if it
                    # were anytime HR. Normalize line to None so the edge
                    # dict matches the schema of the legacy batter_home_runs
                    # market (no line displayed for binary anytime props).
                    if api_mkt_key == "batter_home_runs_alternate":
                        if line != 0.5:
                            continue
                        line = None

                    if not player:
                        continue

                    raw.append({
                        "player":      player,
                        "away_team":   away,
                        "home_team":   home,
                        "start_time":  commence,
                        "sport":       sport,
                        "market":      internal_key,
                        "market_disp": display_name,
                        "line":        line,
                        "side":        side_name,
                        "book":        book_key,
                        "odds":        odds,
                        # Odds API IDs for closing-line capture
                        "event_id":    eid,
                        "sport_key":   api_sport,
                    })

        time.sleep(0.05)  # gentle rate limiting

    print(f"  [OddsAPI Props/{sport.upper()}] {len(raw)} raw lines "
          f"from {len(event_list)} events (quota: {remaining} left)")
    return raw


def fetch_odds_api_first_inning(sport: str) -> list[dict]:
    """
    Fetch first-inning (and other partial-game) markets from The Odds API.

    Per-event call pattern (same as props): one /events list call (free),
    then one /events/{id}/odds call per event for the alternate market(s)
    listed in ODDS_API_FIRST_INNING_MARKETS[sport]. Pinnacle is included
    in the bookmaker request, so the sharp anchor comes through the same
    response — no separate Pinnacle period-1 fetch needed.

    Returns event dicts in the standard internal format (compatible with
    _inject_book_events), where each bookmaker's markets list keys the
    market with the internal key (e.g. "totals_1st") rather than the
    Odds API key, so find_edges treats them as a regular Over/Under market.
    """
    from bot_config import ODDS_API_KEY

    fi_markets = ODDS_API_FIRST_INNING_MARKETS.get(sport)
    if not fi_markets:
        return []

    api_sport = ODDS_API_SPORTS.get(sport)
    if not api_sport:
        return []
    if isinstance(api_sport, list):
        api_sport = api_sport[0]

    api_market_keys = ",".join(m[0] for m in fi_markets)
    market_map = {m[0]: m[1] for m in fi_markets}  # api_key -> internal_key

    r = _odds_api_get(
        f"{ODDS_API_BASE}/sports/{api_sport}/events",
        {"apiKey": ODDS_API_KEY},
        timeout=15, label=f" 1st-Inn/{sport.upper()} events",
    )
    if r is None or r.status_code != 200:
        where = "no response" if r is None else f"HTTP {r.status_code}"
        print(f"  [OddsAPI 1st-Inn/{sport.upper()}] Events {where}")
        return []
    try:
        event_list = r.json()
    except ValueError:
        print(f"  [OddsAPI 1st-Inn/{sport.upper()}] Events: bad JSON")
        return []

    if not event_list:
        return []

    events_out: list[dict] = []
    remaining = "?"
    for ev_info in event_list:
        eid = ev_info.get("id")
        away = ev_info.get("away_team", "")
        home = ev_info.get("home_team", "")
        commence = ev_info.get("commence_time", "")
        if not (eid and away and home):
            continue

        r2 = _odds_api_get(
            f"{ODDS_API_BASE}/sports/{api_sport}/events/{eid}/odds",
            {
                "apiKey":      ODDS_API_KEY,
                "regions":     "us",
                "markets":     api_market_keys,
                "oddsFormat":  "american",
                "bookmakers":  ODDS_API_BOOKMAKERS,
            },
            timeout=15, label=f" 1st-Inn/{sport.upper()}",
        )
        if r2 is None:
            continue
        remaining = r2.headers.get("x-requests-remaining", remaining)
        credit_tracker.record(remaining)
        if r2.status_code != 200:
            continue
        try:
            ev_data = r2.json()
        except ValueError:
            continue

        bookmakers_out: list[dict] = []
        for bm in ev_data.get("bookmakers", []):
            book_key = bm.get("key", "")
            markets_out: list[dict] = []
            for mkt in bm.get("markets", []):
                internal_key = market_map.get(mkt.get("key", ""))
                if not internal_key:
                    continue
                outcomes_out = []
                for oc in mkt.get("outcomes", []):
                    raw_name = (oc.get("name") or "").lower()
                    if raw_name not in ("over", "under"):
                        continue
                    try:
                        price = int(oc.get("price"))
                    except (ValueError, TypeError):
                        continue
                    point = oc.get("point")
                    try:
                        point = float(point) if point is not None else None
                    except (ValueError, TypeError):
                        point = None
                    outcomes_out.append({
                        "name":  "Over" if raw_name == "over" else "Under",
                        "price": price,
                        "point": point,
                    })
                if len(outcomes_out) == 2:
                    markets_out.append({
                        "key":      internal_key,
                        "outcomes": outcomes_out,
                    })
            if markets_out:
                bookmakers_out.append({
                    "key":     book_key,
                    "markets": markets_out,
                    "source":  "api",
                })

        if bookmakers_out:
            events_out.append({
                "commence_time": commence,
                "home_team":     home,
                "away_team":     away,
                "sport_title":   sport.upper(),
                "bookmakers":    bookmakers_out,
                "id":            eid,
                "sport_key":     api_sport,
            })
        time.sleep(0.05)

    print(f"  [OddsAPI 1st-Inn/{sport.upper()}] {len(events_out)}/{len(event_list)} "
          f"events with first-inning markets (quota: {remaining} left)")
    return events_out


# ──────────────────────────────────────────────────────────────────────
# Scraping — Action Network (legacy, kept for reference)
# ──────────────────────────────────────────────────────────────────────

# Maps state-specific book names to their canonical parent name for matching.
_BOOK_CANONICAL = {
    "fanduelny": "fanduel",
    "caesarsny": "caesars",
    "betriversny": "betrivers",
    "bet365nj": "bet365",
}


def _book_key(book_id: int) -> str:
    """Normalised internal key for a book (e.g. 'draftkings', 'espnbet')."""
    raw = AN_BOOK_IDS[book_id].lower().replace(" ", "")
    return _BOOK_CANONICAL.get(raw, raw)


def fetch_action_network(sport: str, session: requests.Session,
                          date: str | None = None,
                          verbose: bool = True) -> tuple[list[dict], dict]:
    """
    Pull full-game odds for one sport from Action Network's v2 scoreboard API.
    Returns (events, status_summary) where status_summary maps status→count
    for any raw games that were filtered out.

    date:    optional YYYYMMDD string — fetch next-day slate when today is done.
    verbose: if False, suppress "games found but none bettable" print (used on
             retry to avoid duplicating the message already shown for today).
    """
    book_ids_str = ",".join(str(b) for b in AN_BOOK_IDS)
    url    = f"{ACTION_NETWORK_BASE}/scoreboard/{sport}"
    params: dict = {"bookIds": book_ids_str}
    if date:
        params["date"] = date
    resp = session.get(url, params=params, timeout=30)
    if resp.status_code == 404:
        return [], {}
    if resp.status_code in (400, 403):
        print(f"  [{sport}] AN {resp.status_code} — see module docstring for fix")
        return [], {}
    resp.raise_for_status()

    raw_games = resp.json().get("games", [])
    events = [ev for game in raw_games
              if (ev := _normalize_an_game(game)) is not None]

    status_summary: dict[str, int] = {}
    if raw_games and not events:
        for g in raw_games:
            st = g.get("status", "unknown")
            status_summary[st] = status_summary.get(st, 0) + 1
        if verbose:
            status_str = ", ".join(f"{v} {k}"
                                   for k, v in status_summary.items())
            print(f"  [{sport.upper():<6}] {len(raw_games)} games found but "
                  f"none bettable ({status_str})")

    return events, status_summary


def _normalize_an_game(game: dict) -> Optional[dict]:
    """Convert one Action Network v2 scoreboard game dict into the internal event format."""
    try:
        status = game.get("status", "scheduled")
        if status not in ("scheduled", "pregame", "delayed", "postponed",
                          "not_started", "pre_game", ""):
            return None
        if game.get("type") not in (None, "game", "match", "event", "reg", "post"):
            return None

        start_time = game.get("start_time") or game.get("event_date", "")
        away_id    = game.get("away_team_id")
        home_id    = game.get("home_team_id")
        teams      = game.get("teams", [])

        # Build team lookup by ID
        team_map = {t["id"]: t for t in teams}
        away_obj = team_map.get(away_id, {})
        home_obj = team_map.get(home_id, {})
        away_team = away_obj.get("full_name") or away_obj.get("display_name", "Away")
        home_team = home_obj.get("full_name") or home_obj.get("display_name", "Home")
        league    = game.get("league_name", "Unknown")

        # v2 markets: dict keyed by book_id string → {event: {market_type: [entries]}}
        all_markets = game.get("markets", {})
        bookmakers = []

        for book_id_str, book_data in all_markets.items():
            book_id = int(book_id_str)
            if book_id not in AN_BOOK_IDS:
                continue

            key = _book_key(book_id)
            # AN uses "event" as the full-game nesting key; some responses use "game"
            evt = book_data.get("event") or book_data.get("game") or {}
            markets = []

            # Periods accepted as full-game lines (AN uses "event"; some responses
            # use "full_game", "game", "match", or omit the field entirely)
            _FULL_GAME_PERIODS = {"event", "full_game", "game", "match", None}

            # Moneyline
            ml_entries = evt.get("moneyline", [])
            if len(ml_entries) >= 2:
                ml_by_team = {}
                for e in ml_entries:
                    if e.get("period") not in _FULL_GAME_PERIODS:
                        continue
                    tid = e.get("team_id")
                    if tid:
                        ml_by_team[tid] = e
                ml_away = ml_by_team.get(away_id)
                ml_home = ml_by_team.get(home_id)
                if ml_away and ml_home:
                    markets.append({
                        "key": "h2h",
                        "outcomes": [
                            {"name": away_team, "price": int(ml_away["odds"]), "point": None},
                            {"name": home_team, "price": int(ml_home["odds"]), "point": None},
                        ],
                    })

            # Spread
            spd_entries = evt.get("spread", [])
            if len(spd_entries) >= 2:
                spd_by_team = {}
                for e in spd_entries:
                    if e.get("period") not in _FULL_GAME_PERIODS:
                        continue
                    tid = e.get("team_id")
                    if tid:
                        spd_by_team[tid] = e
                spd_away = spd_by_team.get(away_id)
                spd_home = spd_by_team.get(home_id)
                if spd_away and spd_home:
                    markets.append({
                        "key": "spreads",
                        "outcomes": [
                            {"name": away_team, "price": int(spd_away["odds"]),
                             "point": float(spd_away["value"])},
                            {"name": home_team, "price": int(spd_home["odds"]),
                             "point": float(spd_home["value"])},
                        ],
                    })

            # Total
            tot_entries = evt.get("total", [])
            if len(tot_entries) >= 2:
                over_e = under_e = None
                for e in tot_entries:
                    if e.get("period") not in _FULL_GAME_PERIODS:
                        continue
                    if e.get("side") == "over":
                        over_e = e
                    elif e.get("side") == "under":
                        under_e = e
                if over_e and under_e:
                    markets.append({
                        "key": "totals",
                        "outcomes": [
                            {"name": "Over",  "price": int(over_e["odds"]),
                             "point": float(over_e["value"])},
                            {"name": "Under", "price": int(under_e["odds"]),
                             "point": float(under_e["value"])},
                        ],
                    })

            if markets:
                bookmakers.append({"key": key, "markets": markets,
                                   "source": "an"})

        if not bookmakers:
            return None

        return {
            "commence_time": start_time,
            "home_team":     home_team,
            "away_team":     away_team,
            "sport_title":   league,
            "bookmakers":    bookmakers,
        }
    except Exception:
        return None


# ──────────────────────────────────────────────────────────────────────
# Scraping — Pinnacle (sharp-line anchor)
# ──────────────────────────────────────────────────────────────────────

def fetch_pinnacle(sport: str, session: requests.Session) -> list[dict]:
    """
    Fetch full-game lines from Pinnacle's public guest API for one sport.
    Returns normalised event dicts (same format as AN), keyed as 'pinnacle'.

    Pinnacle has the sharpest lines on the market (~2% vig vs 5-10% at
    US books), making their implied probability the closest thing to a
    true fair probability freely available.

    For sports that map to multiple Pinnacle leagues (e.g. soccer →
    EPL + La Liga + Serie A + …), fetches each league and merges events.
    """
    raw = PINNACLE_LEAGUES.get(sport)
    if not raw:
        return []

    league_ids = raw if isinstance(raw, list) else [raw]

    hdrs = {**SCRAPER_HEADERS,
            "X-Api-Key": PINNACLE_GUEST_KEY,
            "Referer":   "https://www.pinnacle.com/",
            "Origin":    "https://www.pinnacle.com"}

    all_events: list[dict] = []
    for league_id in league_ids:
        try:
            matchups_resp = session.get(
                f"{PINNACLE_BASE}/leagues/{league_id}/matchups",
                headers=hdrs, timeout=30)
            markets_resp  = session.get(
                f"{PINNACLE_BASE}/leagues/{league_id}/markets/straight",
                headers=hdrs, timeout=30)

            if matchups_resp.status_code in (401, 403):
                print(f"  [Pinnacle/{sport}] Auth error — update PINNACLE_GUEST_KEY")
                return []          # key is bad — no point trying more leagues
            if matchups_resp.status_code == 404:
                continue

            matchups_resp.raise_for_status()
            markets_resp.raise_for_status()

            matchups = matchups_resp.json()
            markets  = markets_resp.json()
            all_events.extend(_normalize_pinnacle(matchups, markets))

        except requests.exceptions.RequestException as e:
            print(f"  [Pinnacle/{sport}/{league_id}] Request error: {e}")
            continue

    return all_events


def _normalize_pinnacle(matchups: list[dict], markets: list[dict]) -> list[dict]:
    """Convert Pinnacle matchup + market data into internal event dicts."""
    # Index markets by matchupId for quick lookup
    mkt_by_id: dict[int, list[dict]] = {}
    for mkt in markets:
        mid = mkt.get("matchupId")
        if mid:
            mkt_by_id.setdefault(mid, []).append(mkt)

    events = []
    for mu in matchups:
        try:
            # Skip live, special (props), and parent-less alternate lines
            if mu.get("type") not in ("pregame", "matchup", None):
                continue
            if mu.get("special"):       # proposition / non-standard matchup
                continue
            if mu.get("parentId"):      # alternate line; skip — use main
                continue

            mid        = mu["id"]
            start_time = mu.get("startTime", "")
            home_team  = next(
                (p["name"] for p in mu.get("participants", [])
                 if p.get("alignment") == "home"), "Home")
            away_team  = next(
                (p["name"] for p in mu.get("participants", [])
                 if p.get("alignment") == "away"), "Away")
            league     = mu.get("league", {}).get("name", "Pinnacle")

            game_markets = mkt_by_id.get(mid, [])
            markets_out  = []
            seen_types   = set()   # only take the primary line per type

            for mkt in game_markets:
                # Full-game only (period 0)
                if mkt.get("period", 0) != 0:
                    continue
                if mkt.get("status") not in ("open", None):
                    continue

                mkt_type = mkt.get("type", "")
                if mkt_type in seen_types:
                    continue          # skip alternate lines
                seen_types.add(mkt_type)
                prices   = mkt.get("prices", [])

                if mkt_type == "moneyline":
                    h = next((p for p in prices if p["designation"] == "home"), None)
                    a = next((p for p in prices if p["designation"] == "away"), None)
                    if h and a:
                        markets_out.append({
                            "key": "h2h",
                            "outcomes": [
                                {"name": away_team, "price": int(a["price"]), "point": None},
                                {"name": home_team, "price": int(h["price"]), "point": None},
                            ],
                        })

                elif mkt_type == "spread":
                    h = next((p for p in prices if p["designation"] == "home"), None)
                    a = next((p for p in prices if p["designation"] == "away"), None)
                    if h and a:
                        markets_out.append({
                            "key": "spreads",
                            "outcomes": [
                                {"name": away_team, "price": int(a["price"]),
                                 "point": float(a.get("points", 0))},
                                {"name": home_team, "price": int(h["price"]),
                                 "point": float(h.get("points", 0))},
                            ],
                        })

                elif mkt_type == "total":
                    ov = next((p for p in prices if p["designation"] == "over"),  None)
                    un = next((p for p in prices if p["designation"] == "under"), None)
                    if ov and un:
                        markets_out.append({
                            "key": "totals",
                            "outcomes": [
                                {"name": "Over",  "price": int(ov["price"]),
                                 "point": float(ov.get("points", 0))},
                                {"name": "Under", "price": int(un["price"]),
                                 "point": float(un.get("points", 0))},
                            ],
                        })

            if markets_out:
                events.append({
                    "commence_time": start_time,
                    "home_team":     home_team,
                    "away_team":     away_team,
                    "sport_title":   league,
                    "bookmakers":    [{"key": "pinnacle", "markets": markets_out,
                                  "source": "direct"}],
                })

        except Exception:
            continue

    return events


def fetch_pinnacle_props(sport: str, session: requests.Session) -> list[dict]:
    """
    Fetch player prop lines from Pinnacle's public guest API for one sport.

    Pinnacle's matchups endpoint includes "special" matchups — these are player
    props (e.g. "LeBron James - Points") linked to a parent game via parentId.
    The main fetch_pinnacle() skips them; this function processes them instead.

    For sports that map to multiple Pinnacle leagues (e.g. soccer), fetches
    each league and merges results.

    Returns raw prop records in the same dict format used by DK/FD/AN scrapers:
      { player, away_team, home_team, start_time, sport, market, market_disp,
        line, side, odds, book }
    The 'book' value is always "pinnacle".
    """
    raw_ids = PINNACLE_LEAGUES.get(sport)
    if not raw_ids:
        return []

    league_ids = raw_ids if isinstance(raw_ids, list) else [raw_ids]

    hdrs = {**SCRAPER_HEADERS,
            "X-Api-Key": PINNACLE_GUEST_KEY,
            "Referer":   "https://www.pinnacle.com/",
            "Origin":    "https://www.pinnacle.com"}

    all_matchups: list[dict] = []
    all_markets:  list[dict] = []

    for league_id in league_ids:
        try:
            matchups_resp = session.get(
                f"{PINNACLE_BASE}/leagues/{league_id}/matchups",
                headers=hdrs, timeout=30)
            markets_resp  = session.get(
                f"{PINNACLE_BASE}/leagues/{league_id}/markets/straight",
                headers=hdrs, timeout=30)

            if matchups_resp.status_code in (401, 403):
                print(f"  [Pinnacle Props/{sport}] Auth error — update PINNACLE_GUEST_KEY")
                return []          # key is bad — no point trying more leagues
            if matchups_resp.status_code == 404:
                continue

            matchups_resp.raise_for_status()
            markets_resp.raise_for_status()

            all_matchups.extend(matchups_resp.json())
            all_markets.extend(markets_resp.json())

        except requests.exceptions.RequestException as e:
            print(f"  [Pinnacle Props/{sport}/{league_id}] Request error: {e}")
            continue

    matchups = all_matchups
    markets  = all_markets

    # Index markets by matchupId
    mkt_by_id: dict[int, list[dict]] = {}
    for mkt in markets:
        mid = mkt.get("matchupId")
        if mid:
            mkt_by_id.setdefault(mid, []).append(mkt)

    # Build parent game map: matchup_id -> {home_team, away_team, start_time}
    # Only non-special, non-parentId matchups are true parent games.
    parent_games: dict[int, dict] = {}
    for mu in matchups:
        if mu.get("special") or mu.get("parentId"):
            continue
        mid = mu.get("id")
        if not mid:
            continue
        home = next(
            (p["name"] for p in mu.get("participants", [])
             if p.get("alignment") == "home"), "Home")
        away = next(
            (p["name"] for p in mu.get("participants", [])
             if p.get("alignment") == "away"), "Away")
        parent_games[mid] = {
            "home_team":  home,
            "away_team":  away,
            "start_time": mu.get("startTime", ""),
        }

    raw: list[dict] = []

    for mu in matchups:
        special = mu.get("special")
        if not special:
            continue  # only process prop/special matchups

        description = special.get("description", "")

        # Pinnacle uses TWO description formats:
        #   "Player Name (Market)"      — e.g. "LeBron James (Points)"
        #   "Player Name - Market"      — e.g. "LeBron James - Points"
        # Try parentheses format first, then dash format.
        player_name = market_desc_l = ""
        paren_match = re.match(r"^(.+?)\s*\(([^)]+)\)\s*$", description)
        if paren_match:
            player_name   = paren_match.group(1).strip()
            market_desc_l = paren_match.group(2).strip().lower()
        elif " - " in description:
            player_name, md = description.split(" - ", 1)
            player_name   = player_name.strip()
            market_desc_l = md.strip().lower()
        else:
            continue

        # Classify market
        matched_market = None
        for substr, mkt_key, mkt_disp in _PINNACLE_PROP_DESC_PATTERNS:
            if substr in market_desc_l:
                expected_sport = _PINNACLE_PROP_SPORT.get(mkt_key)
                if expected_sport and expected_sport != sport:
                    continue  # this market belongs to a different sport
                matched_market = (mkt_key, mkt_disp)
                break

        if not matched_market:
            continue

        mkt_key, mkt_disp = matched_market
        mid         = mu.get("id")
        parent_id   = mu.get("parentId")

        # parentId can be in the matchup directly or in "parent" object
        if not parent_id:
            parent_id = (mu.get("parent") or {}).get("id")
        parent_info = parent_games.get(parent_id, {})
        home_team   = parent_info.get("home_team", "Home")
        away_team   = parent_info.get("away_team", "Away")
        start_time  = parent_info.get("start_time", mu.get("startTime", ""))

        # Build participantId → side ("over"/"under") map from matchup participants
        part_side: dict[int, str] = {}
        for part in mu.get("participants", []):
            pname = (part.get("name") or "").lower()
            pid   = part.get("id")
            if pid and "over" in pname:
                part_side[pid] = "over"
            elif pid and "under" in pname:
                part_side[pid] = "under"

        # Find the over/under market for this matchup
        game_markets = mkt_by_id.get(mid, [])
        seen_line: set[str] = set()

        for game_mkt in game_markets:
            if game_mkt.get("status") not in ("open", None):
                continue
            # For specials (player props), accept any market that has
            # over/under prices — don't filter on period or type name,
            # since Pinnacle labels vary across sports and can change.

            prices = game_mkt.get("prices", [])

            # Pinnacle props use two formats for over/under identification:
            #   A) "designation" field: "over" / "under"  (game-level totals)
            #   B) "participantId" linking to matchup participants named Over/Under
            ov = un = None
            for p in prices:
                desig = p.get("designation")
                if desig:
                    # Format A
                    if desig == "over":
                        ov = p
                    elif desig == "under":
                        un = p
                else:
                    # Format B: resolve via participantId
                    pid = p.get("participantId")
                    side = part_side.get(pid)
                    if side == "over":
                        ov = p
                    elif side == "under":
                        un = p

            if not (ov and un):
                continue

            line = ov.get("points") or un.get("points")
            if line is None:
                continue
            line = float(line)

            # Deduplicate same player + same line
            dedup_key = f"{player_name}|{mkt_key}|{line}"
            if dedup_key in seen_line:
                continue
            seen_line.add(dedup_key)

            for designation, price_data in (("over", ov), ("under", un)):
                try:
                    am_price = int(price_data["price"])
                except (KeyError, TypeError, ValueError):
                    continue
                raw.append({
                    "player":      player_name,
                    "away_team":   away_team,
                    "home_team":   home_team,
                    "start_time":  start_time,
                    "sport":       sport,
                    "market":      mkt_key,
                    "market_disp": mkt_disp,
                    "line":        line,
                    "side":        designation,
                    "odds":        am_price,
                    "book":        "pinnacle",
                })

    return raw


# ──────────────────────────────────────────────────────────────────────
# Scraping — Bovada  (offshore consensus book)
# ──────────────────────────────────────────────────────────────────────

def fetch_bovada(sport: str, session: requests.Session) -> list[dict]:
    """
    Fetch pre-match game lines from Bovada's public JSON API.
    Returns normalised event dicts (bookmaker key = 'bovada').
    Used as an additional consensus data point alongside Pinnacle + AN books.
    """
    path = BOVADA_SPORT_PATHS.get(sport)
    if not path:
        return []

    params = {
        "lang":          "en",
        "eventsLimit":   "50",
        "marketFilterId": "def",
        "sorted":        "true",
        "preMatchOnly":  "true",
    }
    try:
        resp = session.get(
            f"{BOVADA_BASE}/{path}",
            params=params,
            headers=BOVADA_HEADERS,
            timeout=30,
        )
        if resp.status_code in (403, 451):   # geo-block or legal restriction
            return []
        if resp.status_code == 404:
            return []
        resp.raise_for_status()
        raw = resp.json()
    except requests.exceptions.RequestException:
        return []
    except Exception:
        return []

    # Response may be a list of events directly, or wrapped in a list of groups
    events_raw = raw if isinstance(raw, list) else raw.get("events", [])

    return [ev for item in events_raw
            if (ev := _normalize_bovada_event(item)) is not None]


def _normalize_bovada_event(event: dict) -> Optional[dict]:
    """Convert one Bovada event dict into the internal event format."""
    try:
        # Skip live events
        if event.get("live") or event.get("type") == "LIVE":
            return None

        # Teams — prefer structured competitors over description string
        competitors = event.get("competitors", [])
        home_team = next(
            (c["name"] for c in competitors if c.get("home")), "")
        away_team = next(
            (c["name"] for c in competitors if not c.get("home")), "")

        if not home_team or not away_team:
            desc = event.get("description", "")
            if " @ " in desc:
                away_team, home_team = [s.strip() for s in desc.split(" @ ", 1)]
            else:
                return None

        # Start time — Bovada uses milliseconds
        start_ms   = event.get("startTime", 0)
        start_dt   = datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc)
        start_time = start_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

        markets_out = []
        for group in event.get("displayGroups", []):
            for mkt in group.get("markets", []):
                mkt_desc = mkt.get("description", "").lower()

                # Resolve to our internal market key
                mkt_key = None
                for substring, key in BOVADA_MARKET_MAP.items():
                    if substring in mkt_desc:
                        mkt_key = key
                        break
                if not mkt_key:
                    continue

                outcomes_raw = [
                    o for o in mkt.get("outcomes", [])
                    if o.get("status", {}).get("active", True)
                ]
                if len(outcomes_raw) < 2:
                    continue

                outcomes_out = []
                for o in outcomes_raw:
                    price_obj  = o.get("price", {})
                    am_str     = price_obj.get("american", "")
                    handicap   = price_obj.get("handicap", "0") or "0"
                    if not am_str or am_str in ("EVEN", ""):
                        if am_str == "EVEN":
                            am_price = 100
                        else:
                            continue
                    else:
                        try:
                            am_price = int(float(am_str))
                        except ValueError:
                            continue

                    point = None
                    if mkt_key in ("spreads", "totals"):
                        try:
                            point = float(handicap)
                        except ValueError:
                            point = None

                    outcomes_out.append({
                        "name":  o.get("description", ""),
                        "price": am_price,
                        "point": point,
                    })

                if len(outcomes_out) >= 2:
                    markets_out.append({"key": mkt_key, "outcomes": outcomes_out})

        if not markets_out:
            return None

        return {
            "commence_time": start_time,
            "home_team":     home_team,
            "away_team":     away_team,
            "sport_title":   "Bovada",
            "bookmakers":    [{"key": "bovada", "markets": markets_out,
                                  "source": "direct"}],
        }

    except Exception:
        return None


# ──────────────────────────────────────────────────────────────────────
# Game matching — inject external book lines into AN events
# ──────────────────────────────────────────────────────────────────────

_TEAM_ALIASES: dict[str, str] = {
    # City abbreviations → full names (Pinnacle/Bovada often abbreviate)
    "la clippers":     "los angeles clippers",
    "la lakers":       "los angeles lakers",
    "la dodgers":      "los angeles dodgers",
    "la rams":         "los angeles rams",
    "la chargers":     "los angeles chargers",
    "la angels":       "los angeles angels",
    "la galaxy":       "los angeles galaxy",
    "la fc":           "los angeles fc",
    "sf giants":       "san francisco giants",
    "sf 49ers":        "san francisco 49ers",
    "ny yankees":      "new york yankees",
    "ny mets":         "new york mets",
    "ny knicks":       "new york knicks",
    "ny jets":         "new york jets",
    "ny giants":       "new york giants",
    "ny rangers":      "new york rangers",
    "ny islanders":    "new york islanders",
    "nyc fc":          "new york city fc",
    "okc thunder":     "oklahoma city thunder",
    "okc":             "oklahoma city thunder",
    "phx suns":        "phoenix suns",
    "nola pelicans":   "new orleans pelicans",
    "gs warriors":     "golden state warriors",
    "gsw":             "golden state warriors",
    "tb rays":         "tampa bay rays",
    "tb lightning":    "tampa bay lightning",
    "tb buccaneers":   "tampa bay buccaneers",
    "kc royals":       "kansas city royals",
    "kc chiefs":       "kansas city chiefs",
    # Common short forms
    "d backs":         "diamondbacks",
    "dbacks":          "diamondbacks",
    "t wolves":        "timberwolves",
    "twolves":         "timberwolves",
    "sixers":          "76ers",
    "niners":          "49ers",
    "white sox":       "white sox",
    "red sox":         "red sox",
    "trail blazers":   "trail blazers",
    "blazers":         "trail blazers",
}


def _normalize_team_name(name: str) -> str:
    """Lowercase + strip punctuation + resolve known abbreviation aliases."""
    name = name.lower()
    name = re.sub(r"[^a-z0-9 ]", "", name)
    name = re.sub(r"\s+", " ", name).strip()
    # Check full name first, then try stripping "the" and other common words
    if name in _TEAM_ALIASES:
        return _TEAM_ALIASES[name]
    # Also try substring alias match for partial names (e.g., just "la clippers")
    for alias, canonical in _TEAM_ALIASES.items():
        if name == alias or name.endswith(f" {alias}"):
            return name.replace(alias, canonical) if " " in name else canonical
    return name


def _fuzzy(a: str, b: str) -> float:
    """Return 0-1 similarity ratio between two strings."""
    return SequenceMatcher(None, a, b).ratio()


def _teams_match(a: str, b: str, threshold: float = 0.72) -> bool:
    """True if two team name strings are similar enough to be the same team."""
    return SequenceMatcher(
        None,
        _normalize_team_name(a),
        _normalize_team_name(b),
    ).ratio() >= threshold


def _inject_book_events(an_events: list[dict],
                        book_events: list[dict],
                        replace: bool = False) -> tuple[list[dict], int]:
    """
    Generic injector: for each AN event, find the matching game in
    book_events (by date + fuzzy team names) and append all bookmaker
    entries from the external source into the AN event's bookmakers list.

    Works for Pinnacle, Bovada, or any future source that returns events
    in the standard internal format.

    replace: when True, remove any existing AN bookmaker entry for the same
             book key(s) before adding the new data.  Use this for direct
             scrapers (DK, FD) so they always override potentially-stale AN
             lines rather than losing to them in the best-odds dedup pass.

    Returns (updated_an_events, number_of_games_matched).
    """
    matched = 0
    for an_ev in an_events:
        an_date = an_ev["commence_time"][:10]   # YYYY-MM-DD
        an_home = an_ev["home_team"]
        an_away = an_ev["away_team"]

        for ext_ev in book_events:
            if ext_ev["commence_time"][:10] != an_date:
                continue
            if (_teams_match(an_home, ext_ev["home_team"]) and
                    _teams_match(an_away, ext_ev["away_team"])):
                if replace:
                    new_keys = {bm["key"] for bm in ext_ev["bookmakers"]}
                    an_ev["bookmakers"] = [
                        bm for bm in an_ev["bookmakers"]
                        if bm["key"] not in new_keys
                    ]
                an_ev["bookmakers"].extend(ext_ev["bookmakers"])
                # Propagate sportsbook event IDs for deep linking
                for _id_key in ("dk_event_id", "fd_event_id"):
                    if _id_key in ext_ev:
                        an_ev[_id_key] = ext_ev[_id_key]
                matched += 1
                break   # one match per AN event is enough

    return an_events, matched


# ──────────────────────────────────────────────────────────────────────
# Combined per-sport fetch (runs in its own thread)
# ──────────────────────────────────────────────────────────────────────

def _fetch_sport_all(sport: str) -> tuple[str, list[dict], int, int, str]:
    """
    Fetch odds for one sport via The Odds API, falling back to Action
    Network if the API returns nothing (quota exhausted, 429, error).

    Returns (sport_name, events, pinnacle_count, total_books, source) where
    source is "api" (Odds API), "fallback_an" (Action Network), or "none"
    (both empty). The Odds API normally returns DK + FD + Pinnacle + Bovada
    + BetMGM + more in a single call; AN covers DK + FD + a few others but
    no Pinnacle for most sports, so fair-prob blending degrades on fallback.
    """
    events, _ = fetch_odds_api(sport)
    source = "api"

    if not events:
        # Empty result — quota exhausted, 429, or transient error. Try AN.
        print(f"  [OddsAPI/{sport}] empty result — trying Action Network fallback")
        try:
            an_session = make_session()
            an_events, _ = fetch_action_network(sport, an_session, verbose=False)
            if an_events:
                events = an_events
                source = "fallback_an"
                print(f"  [AN-fallback/{sport}] {len(events)} events")
            else:
                source = "none"
        except Exception as e:
            print(f"  [AN-fallback/{sport}] error: {e}")
            source = "none"

    if not events:
        return sport.upper(), [], 0, 0, source

    # ── First-inning / partial-game markets ──────────────────────────────
    # Promo-only feed (FanDuel "First Inning Friday" etc.). MLB only today.
    # Skipped on AN fallback — these markets are Odds-API-exclusive.
    if source == "api" and sport in ODDS_API_FIRST_INNING_MARKETS:
        try:
            fi_events = fetch_odds_api_first_inning(sport)
            if fi_events:
                events, _ = _inject_book_events(events, fi_events)
        except Exception as e:
            print(f"  [OddsAPI 1st-Inn/{sport}] error: {e}")

    # Count Pinnacle coverage for diagnostics
    pinn_ct = sum(1 for ev in events
                  if any(bm["key"] == "pinnacle" for bm in ev.get("bookmakers", [])))
    total_books = max(
        (len(ev.get("bookmakers", [])) for ev in events), default=0)

    return sport.upper(), events, pinn_ct, total_books, source


# ──────────────────────────────────────────────────────────────────────
# Core analysis
# ──────────────────────────────────────────────────────────────────────

def find_edges(events: list[dict], parlay_pool: list | None = None,
               live_only: bool = False,
               promo_pool: list | None = None) -> list[dict]:
    """
    For each market outcome offered by DraftKings or FanDuel:
      1. Separate Pinnacle lines from other consensus lines.
      2. If Pinnacle is present: fair_prob = Pinnacle * 0.65 + AN_consensus * 0.35
         (Pinnacle is the sharpest book; its implied prob ≈ true probability)
      3. If Pinnacle is absent: fair_prob = average(median_consensus, sharpest_line)
         (original method — still valid when Pinnacle has no line)
      4. Flag outcomes where EV ≥ MIN_EV_PERCENT and calculate Kelly size.

    live_only=True flips the time window to currently-in-progress games only
    (started in the past 6 hours, none scheduled in the future) — used by /run_live.

    promo_pool: when provided, every evaluated DK/FD outcome (regardless of
    pre-boost EV) is appended. Lets promo picking consider mildly -EV edges
    that become +EV after a profit boost — e.g. long-shots that only clear
    the bar thanks to the boost.
    """
    from datetime import timedelta
    edges   = []
    now     = datetime.now(timezone.utc)
    if live_only:
        # Live mode: games already in progress. Lower bound caps how far back
        # we'll look (covers most US sports' max game length).
        earliest = now - timedelta(hours=6)
        latest   = now
    else:
        # Standard mode: games starting within the next ~26 hours.
        # 15-min lower grace handles minor source data-lag without admitting
        # live lines. 26-hour upper bound covers tomorrow's full slate even
        # on an early-morning run while excluding multi-day-out games.
        earliest = now - timedelta(minutes=15)
        latest   = now + timedelta(hours=26)

    for event in events:
        commence = event.get("commence_time", "")
        sport      = event.get("sport_title", "")
        # Per-sport upper bound: weekly combat cards (UFC/boxing) get a
        # multi-day reach so a planned promo surfaces ahead of the card;
        # daily sports stay at the standard ~26h window. max() never
        # shrinks the default, and live mode keeps its [past 6h, now]
        # window untouched.
        event_latest = latest
        if not live_only:
            _hd = _promo_horizon_days(sport)
            if _hd > MAX_PROMO_DAYS:
                event_latest = max(latest, now + timedelta(days=_hd))
        try:
            commence_dt = datetime.fromisoformat(commence.replace("Z", "+00:00"))
            if not (earliest <= commence_dt <= event_latest):
                continue
        except (ValueError, AttributeError):
            continue

        home       = event["home_team"]
        away       = event["away_team"]
        bookmakers = event.get("bookmakers", [])
        if not bookmakers:
            continue

        # Build: market_key -> outcome_key -> [(book, decimal, american, point, source)]
        market_data: dict[str, dict[str, list]] = {}
        for bm in bookmakers:
            bk  = bm["key"]
            src = bm.get("source", "an")
            for market in bm.get("markets", []):
                mkt = market["key"]
                market_data.setdefault(mkt, {})
                for outcome in market["outcomes"]:
                    oname = outcome["name"]
                    price = outcome["price"]
                    point = outcome.get("point")
                    dec   = american_to_decimal(price)
                    key   = f"{oname}|{point}" if point is not None else oname
                    market_data[mkt].setdefault(key, [])
                    market_data[mkt][key].append((bk, dec, price, point, src))

        # Collect this event's moneyline edges so the 3-way draw
        # probability can be stamped onto them after the market loop
        # (see the money-back-on-draw stamping below).
        _h2h_edges_this_event: list[dict] = []
        for mkt, outcomes in market_data.items():
            for outcome_key, book_lines in outcomes.items():
                target_lines   = [(b, d, am, pt, src) for b, d, am, pt, src in book_lines
                                  if b in TARGET_BOOKS]
                other_lines    = [(b, d, am, pt, src) for b, d, am, pt, src in book_lines
                                  if b not in TARGET_BOOKS]
                pinnacle_lines = [(b, d, am, pt, src) for b, d, am, pt, src in other_lines
                                  if b == "pinnacle"]
                non_pinn_lines = [(b, d, am, pt, src) for b, d, am, pt, src in other_lines
                                  if b != "pinnacle"]

                # Need at least one target book AND enough consensus data
                has_pinnacle = bool(pinnacle_lines)
                if not target_lines:
                    continue
                if not has_pinnacle and len(non_pinn_lines) < 2:
                    continue

                # ── Fair probability ──────────────────────────────────────
                if has_pinnacle:
                    pinn_prob = decimal_to_implied_prob(pinnacle_lines[0][1])

                    if non_pinn_lines:
                        # Blend Pinnacle (sharp) with AN consensus (broad)
                        other_probs = sorted(
                            [decimal_to_implied_prob(d) for _, d, _, _, _ in non_pinn_lines])
                        mid = len(other_probs) // 2
                        consensus_prob = (
                            (other_probs[mid - 1] + other_probs[mid]) / 2
                            if len(other_probs) % 2 == 0
                            else other_probs[mid]
                        )
                        fair_prob = (pinn_prob * PINNACLE_WEIGHT +
                                     consensus_prob * (1 - PINNACLE_WEIGHT))
                    else:
                        # Pinnacle only — use it directly (lowest vig available)
                        fair_prob = pinn_prob

                    best_other_dec = max(d for _, d, _, _, _ in other_lines)

                else:
                    # Original method — no Pinnacle line for this outcome
                    other_probs = sorted(
                        [decimal_to_implied_prob(d) for _, d, _, _, _ in other_lines])
                    mid = len(other_probs) // 2
                    consensus_prob = (
                        (other_probs[mid - 1] + other_probs[mid]) / 2
                        if len(other_probs) % 2 == 0
                        else other_probs[mid]
                    )
                    best_other_dec = max(d for _, d, _, _, _ in other_lines)
                    sharp_prob     = decimal_to_implied_prob(best_other_dec)
                    fair_prob      = (consensus_prob + sharp_prob) / 2

                fair_prob = max(0.05, min(0.95, fair_prob))

                # ── Deduplicate target lines (state-specific books merge)
                best_target: dict[str, tuple] = {}
                for book, dec_odds, am_odds, point, src in target_lines:
                    if book not in best_target or dec_odds > best_target[book][1]:
                        best_target[book] = (book, dec_odds, am_odds, point, src)
                target_lines = list(best_target.values())

                # ── Collect all book odds for comparison tab ────────────
                all_book_odds = {}
                # Dedup lines by canonical book key (keep best odds)
                _best_by_book: dict[str, tuple] = {}
                for b, d, am, pt, _src in book_lines:
                    if b not in _best_by_book or d > _best_by_book[b][1]:
                        _best_by_book[b] = (b, d, am, pt)

                # Map canonical key → display name
                _KEY_DISPLAY = {
                    "draftkings": "DraftKings", "fanduel": "FanDuel",
                    "betmgm": "BetMGM", "caesars": "Caesars",
                    "betrivers": "BetRivers", "espnbet": "ESPN Bet",
                    "bet365": "Bet365", "pinnacle": "Pinnacle",
                    "bovada": "Bovada", "fanatics": "Fanatics",
                    "hardrockbet": "Hard Rock Bet", "pointsbet": "PointsBet",
                    "wynnbet": "WynnBet",
                }
                for b, d, am, pt in _best_by_book.values():
                    display = _KEY_DISPLAY.get(b, b.title())
                    all_book_odds[display] = am

                # ── EV check ─────────────────────────────────────────────
                outcome_name = outcome_key.split("|")[0]
                for book, dec_odds, am_odds, point, _src in target_lines:
                    ev = calc_ev(fair_prob, dec_odds)
                    edge_dict = {
                        "sport":           sport,
                        "game":            f"{away} @ {home}",
                        "commence":        commence,
                        "book":            "DraftKings" if book == "draftkings" else "FanDuel",
                        "market":          mkt,
                        "outcome":         outcome_name,
                        "point":           point,
                        "odds_american":   am_odds,
                        "odds_decimal":    round(dec_odds, 3),
                        "fair_prob":       round(fair_prob * 100, 1),
                        "implied_prob":    round(decimal_to_implied_prob(dec_odds) * 100, 1),
                        "ev_pct":          round(ev, 2),
                        "kelly_pct":       round(kelly_bet(fair_prob, dec_odds) * 100, 2),
                        "num_other_books": len(other_lines),
                        "best_other_odds": format_american(best_other_dec),
                        "pinnacle_anchored": has_pinnacle,
                        "all_book_odds":   all_book_odds,
                        "dk_event_id":     event.get("dk_event_id"),
                        "fd_event_id":     event.get("fd_event_id"),
                        # Odds API canonical IDs — used by closing_lines.py
                        # to re-query the per-event endpoint near tip-off.
                        # None for Action Network fallback events.
                        "event_id":        event.get("id"),
                        "sport_key":       event.get("sport_key"),
                    }
                    if ev >= MIN_EV_PERCENT:
                        edges.append(edge_dict)
                    # ── Promo pool: every DK/FD outcome (for boost evaluation)
                    if promo_pool is not None:
                        promo_pool.append(edge_dict)
                    # Remember moneyline edges for draw-prob stamping.
                    if mkt == "h2h":
                        _h2h_edges_this_event.append(edge_dict)
                    # ── Parlay-pool collection (all fairly-priced favorites) ─
                    if parlay_pool is not None and fair_prob >= 0.30:
                        parlay_pool.append({
                            "sport":             sport,
                            "game":              f"{away} @ {home}",
                            "commence":          commence,
                            "book":              "DraftKings" if book == "draftkings" else "FanDuel",
                            "market":            mkt,
                            "outcome":           outcome_name,
                            "point":             point,
                            "odds_american":     am_odds,
                            "odds_decimal":      round(dec_odds, 3),
                            "fair_prob":         round(fair_prob * 100, 1),
                            "implied_prob":      round(decimal_to_implied_prob(dec_odds) * 100, 1),
                            "ev_pct":            round(ev, 2),
                            "kelly_pct":         round(kelly_bet(fair_prob, dec_odds) * 100, 2),
                            "num_other_books":   len(other_lines),
                            "pinnacle_anchored": has_pinnacle,
                        })

        # ── Money-back-on-draw support ───────────────────────────────
        # Stamp the 3-way draw probability onto each moneyline team edge
        # so a draw-insured promo (stake refunded on a draw) can value
        # the refund (= P(draw)). Uses the Draw outcome's own fair_prob —
        # the same Pinnacle-blend/consensus method as every other outcome,
        # so no separate math. Left unset for 2-way h2h (no Draw outcome),
        # so the insurance never applies to a sport that can't draw.
        _draw_e = next((e for e in _h2h_edges_this_event
                        if str(e.get("outcome", "")).strip().lower() == "draw"),
                       None)
        if _draw_e is not None:
            for _e in _h2h_edges_this_event:
                if _e is not _draw_e:
                    _e["draw_fair_prob"] = _draw_e["fair_prob"]

    edges.sort(key=lambda x: x["ev_pct"], reverse=True)
    return edges


# ──────────────────────────────────────────────────────────────────────
# Excel output
# ──────────────────────────────────────────────────────────────────────

def _make_pick_key(edge: dict) -> str:
    """
    Stable string key identifying a pick for seen-pick tracking.

    Intentionally excludes odds — only game identity matters.  A pick that
    reappears with different odds (e.g. -114 → -120) is still the same pick
    and should show as SEEN, not NEW.

    Components: game · book · market · outcome · line
    All normalised (lowercase, stripped, point rounded to nearest 0.5) so
    minor API formatting differences don't produce false NEW labels.
    """
    def _norm(s: str) -> str:
        return re.sub(r"\s+", " ", (s or "").lower().strip())

    pt = edge.get("point")
    if pt is not None:
        # Round to nearest 0.5 to absorb float precision drift; keep sign
        # so -2.0 and 2.0 remain distinct (home vs away spread)
        point_s = str(round(float(pt) * 2) / 2)
    else:
        point_s = ""

    return "||".join([
        _norm(edge.get("game", "")),
        _norm(edge.get("book", "")),
        edge.get("market", "").upper().strip(),
        _norm(edge.get("outcome", "")),
        point_s,
    ])


def _load_prev_play_keys(script_dir: str) -> set:
    """
    Load pick keys from the JSON sidecar written on the previous run.
    Returns an empty set if no prior file exists.
    """
    import json
    path = os.path.join(script_dir, "prev_play_keys.json")
    try:
        with open(path) as fh:
            data = json.load(fh)
        return set(data)
    except (FileNotFoundError, ValueError):
        return set()


def _save_play_keys(play_edges: list[dict], script_dir: str) -> None:
    """Persist this run's play keys so the next run can mark them as seen."""
    import json
    keys = [_make_pick_key(e) for e in play_edges]
    path = os.path.join(script_dir, "prev_play_keys.json")
    with open(path, "w") as fh:
        json.dump(keys, fh)


def write_book_event_ids_sidecar(edges: list[dict] | None,
                                   prop_edges: list[dict] | None,
                                   output_dir: str) -> str | None:
    """
    Write a single `latest_book_event_ids.json` sidecar holding the DK/FD
    event IDs for every pick in this scan, keyed by pick_key.

    The dashboard frontend reads this to wrap pick rows in a sportsbook
    deep link ("click → DK/FD event page"). xlsx round-trip strips the
    bookmaker IDs from edge_dict, so this sidecar is how we surface them
    back to the live UI without changing the xlsx schema.

    Always overwrites — the dashboard only ever shows the LATEST archive,
    so older scans' IDs are not needed and would just bloat the file.
    Picks without IDs (some sport coverage gaps) are still recorded with
    null values so the lookup always finds the pick (just no link).
    """
    path = os.path.join(output_dir, "latest_book_event_ids.json")

    out: dict[str, dict] = {}
    for edge in (edges or []) + (prop_edges or []):
        key = _make_pick_key(edge)
        out[key] = {
            "dk_event_id": edge.get("dk_event_id"),
            "fd_event_id": edge.get("fd_event_id"),
        }

    payload = {
        "scan_timestamp": datetime.now(timezone.utc).isoformat(),
        "picks":          out,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    has_id_count = sum(1 for v in out.values()
                       if v.get("dk_event_id") or v.get("fd_event_id"))
    print(f"  [book-links] {has_id_count}/{len(out)} picks have a DK/FD event ID "
          f"in {os.path.basename(path)}")
    return path


def write_pending_closes_sidecar(edges: list[dict] | None,
                                 prop_edges: list[dict] | None,
                                 output_dir: str,
                                 play_edges: list[dict] | None = None) -> str | None:
    """
    Append every +EV pick from this scan to the day's pending-closes JSON
    so the closing-line capture worker (closing_lines.py) can re-query the
    Odds API per-event endpoint near tip-off and record CLV.

    First-seen wins: if a pick already appears in the file (from an earlier
    scan the same day), its open odds and any captured close are preserved.
    Only brand-new picks get added. Picks without an Odds API event_id
    (Action Network fallback events) are skipped — we have no way to
    re-query their close.

    play_edges (the Play These list: qualified game lines + props) marks
    entries with play_these=True so /recheck can re-price just the picks
    the user was actually told to bet. The flag and scan-time ev_pct ARE
    updated on existing entries (a pick can qualify on a later scan than
    the one that first recorded it) — only open odds + close stay frozen.

    Filename: pending_closes_YYYY-MM-DD.json (date is the betting-day in ET).
    Returns the path written, or None if no eligible picks.
    """
    today_et = datetime.now(EASTERN).date().isoformat()
    path = os.path.join(output_dir, f"pending_closes_{today_et}.json")

    existing: dict = {}
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                existing = json.load(f).get("picks", {})
        except (json.JSONDecodeError, OSError):
            existing = {}

    play_keys = {_make_pick_key(e) for e in (play_edges or [])}

    added = 0
    skipped_no_id = 0
    for edge in (edges or []) + (prop_edges or []):
        if not edge.get("event_id") or not edge.get("sport_key"):
            skipped_no_id += 1
            continue
        key = _make_pick_key(edge)
        if key in existing:
            # Preserve first-seen open + any captured close, but let the
            # Play These flag and scan-time EV refresh (see docstring).
            entry = existing[key]
            if key in play_keys and not entry.get("play_these"):
                entry["play_these"] = True
            if entry.get("ev_pct") is None:
                entry["ev_pct"] = edge.get("ev_pct")
            continue
        existing[key] = {
            "sport":              edge.get("sport"),
            "game":               edge.get("game"),
            "commence":           edge.get("commence"),
            "book":               edge.get("book"),
            "market":             edge.get("market"),
            "outcome":            edge.get("outcome"),
            "point":              edge.get("point"),
            "event_id":           edge.get("event_id"),
            "sport_key":          edge.get("sport_key"),
            "open_odds_american": edge.get("odds_american"),
            "open_implied_pct":   edge.get("implied_prob"),
            "ev_pct":             edge.get("ev_pct"),
            "play_these":         key in play_keys,
            "close":              None,
        }
        added += 1

    payload = {
        "scan_timestamp": datetime.now(timezone.utc).isoformat(),
        "picks":          existing,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)

    print(f"  [closing-lines] {len(existing)} picks tracked "
          f"(+{added} new, {skipped_no_id} skipped without OddsAPI ID) "
          f"in {os.path.basename(path)}")
    return path


def _confidence(e):
    """Assign a confidence tier based on Pinnacle anchoring and consensus depth."""
    pinn  = e.get("pinnacle_anchored", False)
    books = e["num_other_books"]
    if pinn and e["ev_pct"] >= 3:
        return "STRONG"
    elif pinn:
        return "SHARP"
    elif e["market"] == "spreads" and books >= 7:
        return "SOLID"
    elif books >= 7:
        return "CONSENSUS"
    else:
        return "GOOD"


MAX_PLAY_DAYS = 1      # Play These: ignore games > 1 day out
MAX_PROMO_DAYS = 1     # Promos (singles + parlays): default within 1 day

# Per-sport promo horizon override (days). Most sports run daily and the
# user bets same-day, so a 1-day reach is right. Combat sports are weekly
# one-off cards whose books post lines a week+ ahead and which users plan
# promos around — a 1-day cap silently hid the user's FanDuel UFC promo
# ~40h before the 2026-06-14 White House card (find_edges discarded the
# bouts as >26h out, so the promo pool never saw them). Give combat
# sports a multi-day reach. Cost-free: the per-sport /odds fetch already
# returns the whole card in one call; the old window just dropped it
# client-side. Play These re-caps to MAX_PLAY_DAYS independently inside
# _qualifies, so the wider reach feeds ONLY the promo pool, never the
# curated cash list. Keyed by canonical sport (see _canonical_promo_sport).
_PROMO_HORIZON_DAYS_BY_SPORT: dict[str, float] = {
    "ufc":    4.0,
    "boxing": 4.0,
}
# NB: the FIFA World Cup is deliberately NOT here, even though it's a
# tournament. It has DAILY games and the user gets ONE promo/day, so a pick
# more than the 24h default out would waste that day's token (it can't be
# held for a match several days away — "24h max from run time"). Only
# genuinely weekly one-off cards (combat sports) get a multi-day reach.
# The real cause of the 2026-06-25 "no WC pick" was a dropped fetch, not the
# horizon — see _odds_api_get's retry (a 429 on the last soccer league
# silently ate the World Cup feed).

# ── Promo hit-rate ruleset ────────────────────────────────────────────
# Promos are use-it-or-lose-it and the goal is to CASH them, not to
# maximize theoretical EV (2026-06-11: two World Cup bonus bets went on
# goalscorer moonshots whose players never entered — bets voided, tokens
# dead). Pure EV-ranking monotonically rewards longshots, but bonus-bet
# conversion is concave in odds (+200 ≈ 67% of face value at fair
# prices, +450 ≈ 82%, +1000 ≈ 91%), so past ~+450 the marginal
# conversion is tiny while hit rate collapses — and the favorite-
# longshot bias (vig is worst on the tail) eats most of what's left.
#
# Each promo type gets a list of (floor, ceiling) American-odds windows,
# tried in order; the pick comes from the FIRST window holding a
# candidate, so the target band wins whenever it's populated. Off-band
# picks carry a band_stage tag so the bot / Excel can flag them.
# Bonus bets keep a high floor — the stake never returns, so a -200
# bonus bet cashes only a third of face value — but the last-resort
# window still allows short odds: a cashed -150 beats an expired token.
# Profit boosts ride cash stakes (favorites hit often and the boost
# does the lifting), so short odds are first-class there.
PROMO_ODDS_WINDOWS: dict[str, list[tuple[int, int]]] = {
    "bonus_bet": [(200, 450), (150, 500), (-200, 500)],
    "boost":     [(-200, 450), (-300, 700)],
}

# "Drawbreaker"-style promos: bet a team moneyline; if the match draws, the
# stake is refunded as a BONUS bet rather than cash. A bonus bet is worth only
# its winnings-only cash-equivalent, so the draw refund is valued at this
# retention fraction of face value (a free bet placed in the +200..+450 band
# retains ~0.67–0.82; 0.70 is a deliberately conservative central estimate, so
# the promo's value is never overstated). Plain cash money-back-on-draw keeps
# the full P(draw) — see _draw_refund_ev_pct.
DRAW_REFUND_BONUS_RETENTION = 0.70

# Voidable player props (goalscorer, HR, double-double, ...) carry
# participation risk: a DNP voids the bet and kills the token, and quoted
# prop odds are priced for cash bettors (void = refund), i.e. conditional
# on appearance, while a use-it-or-lose-it token collects unconditionally.
# This window is when lineups / injury reports are public and books have
# repriced — the best "will actually play" proxy available from odds data.
#
# It is NOT a hard gate (2026-06-13: it was, and it held an HR-only boost
# 12h before first pitch — a market="hr" promo has no game-line fallback,
# so holding it just wastes the token, the exact failure we set out to
# avoid). Behavior now (see _promo_picks_prop_early):
#   * A promo with no settle-always fallback always gets a pick — a promo
#     that TARGETS a voidable prop market (market="hr"/"dd"/"goal"/...).
#     Outside this window the pick is flagged participation_warn so the bot
#     tells the user to confirm the player is active before placing. The
#     odds bands already keep that pick to a likely starter, not a deep-
#     bench moonshot.
#   * Any other all-market / game-line promo still defers voidable props
#     until inside the window, so it prefers a settle-always line early and
#     only reaches for a prop once participation is essentially known.
PROMO_PROP_WINDOW_MIN = 75

# Parlay promos: cap each leg and the combined price so the parlay
# stays cashable. Legs at or under +150 keep every leg ≥ ~40% fair;
# +700 combined ≈ 12% to cash at fair prices — long enough to satisfy
# any normal min-odds term, short enough to actually hit sometimes.
PROMO_PARLAY_LEG_MAX_ODDS   = 150   # American, per leg
PROMO_PARLAY_TOTAL_MAX_ODDS = 700   # American, combined


# ── Profit-boost staking band ─────────────────────────────────────────
# Fully-graded promo P&L (Apr–Jun 2026; see the promo_oddsband_pnl_analysis
# note) showed profit boosts only cash net-positive when the *boosted*
# price lands in a middle band. Below it, deep favorites pay too little to
# cover their upset rate (the −200..+150 band ran ≈ −40% ROI). Above it,
# the hit rate collapses (+300..+700 hit ~20–24%) while a large boost
# inflates the *boosted* EV — and therefore half-Kelly — into a real-money
# stake on a rare event; that is the band that drove the losing weeks, and
# the same place prop-targeted promos (HR / goalscorer / double-double,
# which have no game-line fallback so the picker always emits one) pile up.
# Outside the band the pick is still placed — these tokens are use-it-or-
# lose-it — but staked at the book minimum: a lottery ticket, not a Kelly
# position. The lower bound is deliberately +100 (not +150) so legitimate
# short totals/spreads boosts that have cashed (e.g. a first-inning over at
# +123, a WNBA total at +118) keep a full stake.
PROMO_FULL_KELLY_ODDS_LO = 100    # boosted American (decimal 2.00)
PROMO_FULL_KELLY_ODDS_HI = 350    # boosted American (decimal 4.50)


def _in_promo_kelly_band(boost_dec: float) -> bool:
    """True if a boosted decimal price sits in the full-Kelly staking band."""
    return (american_to_decimal(PROMO_FULL_KELLY_ODDS_LO)
            <= boost_dec
            <= american_to_decimal(PROMO_FULL_KELLY_ODDS_HI))


def promo_boost_stake(bankroll: float, kelly_frac: float, boost_dec: float,
                      book: str,
                      max_bet: float | None = None) -> tuple[float, bool]:
    """Stake for a profit-boost promo pick.

    Full (half-)Kelly, clamped to the book minimum and the promo's max
    bet, when the boosted price sits in the band that has historically
    cashed (PROMO_FULL_KELLY_ODDS_LO..HI). Outside the band the pick is
    still worth placing — a promo token is use-it-or-lose-it — but only as
    a book-minimum lottery ticket, never a Kelly-sized real-money position.

    Returns (bet, lottery_capped)."""
    bet = clamp_bet(bankroll * kelly_frac, book)
    lottery_capped = False
    if bet > 0 and not _in_promo_kelly_band(boost_dec):
        bet = min(bet, MIN_BET.get(book, 0.10))
        lottery_capped = True
    if max_bet and bet > max_bet:
        bet = max_bet
    return bet, lottery_capped


def _starts_within_days(e, max_days: int) -> bool:
    """Return True if the edge's game starts within max_days from now."""
    commence = e.get("commence")
    if not commence:
        return True  # no timestamp = assume it's soon
    try:
        ct = datetime.fromisoformat(commence.replace("Z", "+00:00"))
        return ct < datetime.now(ct.tzinfo) + timedelta(days=max_days)
    except Exception:
        return True


def _starts_within_minutes(e, max_minutes: int) -> bool:
    """Return True if the edge's game starts within max_minutes from now.

    Deliberately the OPPOSITE missing-timestamp default from
    _starts_within_days: this helper gates voidable props into promo
    picks, so "can't tell when it starts" must mean "not eligible",
    not "assume it's fine"."""
    commence = e.get("commence")
    if not commence:
        return False
    try:
        ct = datetime.fromisoformat(commence.replace("Z", "+00:00"))
        return ct < datetime.now(ct.tzinfo) + timedelta(minutes=max_minutes)
    except Exception:
        return False


def _promo_horizon_days(sport_label: str) -> float:
    """Promo reach in days for an edge/event's sport label.

    MAX_PROMO_DAYS for daily sports; a longer reach for weekly combat
    cards (see _PROMO_HORIZON_DAYS_BY_SPORT). Accepts any raw sport label
    (Odds API title, AN league, internal code) — collapses via
    _canonical_promo_sport first."""
    canonical = _canonical_promo_sport(sport_label or "")
    return _PROMO_HORIZON_DAYS_BY_SPORT.get(canonical, MAX_PROMO_DAYS)


def _starts_within_promo_horizon(e) -> bool:
    """Like _starts_within_days but using the edge's per-sport promo
    horizon, so a weekly UFC/boxing card surfaces days ahead while daily
    sports stay at the 1-day default."""
    return _starts_within_days(e, _promo_horizon_days(e.get("sport", "")))


# Soccer leagues with consistently negative ROI across backtests.
# 2026-05-06 (30d backtest, post grader-fix resolution lift):
#   PT cohort by league — EPL -86%, BUNDESLIGA -70%, LIGUE1 -59%,
#   EUROPA -100%, LALIGA -30%, SERIEA -17%, CHAMPIONS -14%. The two
#   newly added (LIGUE1, SERIEA) match the broad-pool pattern that
#   drove the original block list. MLS remains the only PT-eligible
#   soccer league.
# 2026-05-09 (30d backtest, broad-pool tail): LIGAMX 0/4 (-99% ROI),
#   CONFERENCE 1/2 (-46% ROI). Same pattern as the European leagues —
#   non-Pinnacle consensus pricing on second-tier soccer is unreliable.
# 2026-06-10: FIFA WORLD CUP blocked pre-emptively — zero backtest
# history (tournament starts 2026-06-11) and the bar is 30+ days of data
# before a new sport reaches Play These (see the tennis activation note
# in _qualifies). Without this entry, World Cup spreads would slip
# through the generic spreads gate on day one. Promo picks are
# unaffected: they draw from promo_pool, which bypasses _qualifies().
_BLOCKED_SOCCER = frozenset({
    "EPL", "BUNDESLIGA", "CHAMPIONS", "EUROPA", "LALIGA",
    "LIGUE1", "SERIEA", "LIGAMX", "CONFERENCE",
    "FIFA WORLD CUP",
})


def _qualifies_base(e):
    """Return True if an edge qualifies for the 'Play These' tab.

    2026-06-22 — BOXING H2H paused and TENNIS H2H non-Pinn floor raised
    2.0 → 2.5, from an analyst review over Mar–Jun (n=5,806 settled):
      - Boxing: the 2026-05-22 add (#3 below) was on n=14 (+11.7%). At n=54
        it inverted to -30.9% flat (95% CI [-49.9, -11.9]; D-L shrunk -14.9%).
        Boxing has no Pinnacle, so its +3.0% CLV is captured against the soft
        book we bet (circular) — the realized bleed is the trustworthy signal.
        Removed the BOXING h2h branch entirely (incl. the Pinn path).
      - Tennis: the 2.0–2.5% band was -40.6% flat on n=28 (95% CI [-76, -5]),
        the worst pocket inside the old ev≥2.0 floor; tennis also has the
        weakest CLV of any sport (+0.46%). Raised the non-Pinn floor to 2.5 to
        drop that band; the 2.5–5% pocket is retained.

    2026-06-02 — TENNIS activated. The 2026-05-22 add never fired live: the
    Odds API tags tennis edges with the tournament title ("ATP Italian
    Open"), so sport never equalled "TENNIS" (now collapsed below). A ~2-month
    broad-pool replay (2026-04→05, all non-Pinn since tennis has no Pinnacle)
    shows tennis h2h +8.0% ROI / +8.2% flat on n=124; the qualifying 2–5%
    slice is +22.7% ROI / +5.1% flat on n=32. Clears the "30+ day data before
    enabling a new sport" bar. No threshold change — the existing non-Pinn
    ev≥2.0 gate (capped <5.0) catches the positive bands.

    Recalibrated 2026-05-22 against the 12-day backtest (2026-05-05 → 05-16,
    922 broad +EV picks, 189 PT picks at -24.1% ROI). Five changes:

      1. EV ≥ 5% cap restored on ALL game lines. PT bucket ran 0-9
         ($-42.58, -100% ROI) — driven by Pinn-anchored MLB picks at
         55.7%/7.0%/6.6%/5.6% (all losses) and MLB spreads at 5–5.5%
         (5 losses post-stale-guard). Broad pool ≥5% was -47.9%
         across 83 picks. The 2026-04-16 lift of this cap inverted
         once the soft-line tail showed up; a book offering 5%+ over
         Pinnacle is almost always a stale-line trap.

      2. TENNIS H2H added (non-Pinn ≥ 2.0%, Pinn ≥ 1.0%). Broad pool
         was +8.3% on 266 picks; the 2–5% slice was +10.1% on 77.
         Biggest pick-volume add available. Cap at <5% catches the
         retire-out tail (≥5% had many NO_RESULT picks).

      3. BOXING H2H added (non-Pinn ≥ 1.0%, Pinn ≥ 1.0%). Broad 1–5%
         slice was +11.7% on 14 picks; ≥5% drops to -2.5% on 12, so
         the EV cap above handles the upper bound.

      4. WNBA spreads non-Pinn floor lowered to 1.0% (general floor
         stays at 2.0%). Broad WNBA spreads 1.0–2.5% was +13.6% on
         18 picks; the existing 2.5% floor was excluding the
         profitable lower band.

      5. Spreads general non-Pinn floor 2.5% → 2.0%. Broad 1.0–2.0%
         slice was +14.1% on 70; the 2.0–2.5% pocket (-17% on 17)
         is the dead zone — we accept that small drag in exchange
         for the larger 1.5–5% positive band.
    """
    pinn  = e.get("pinnacle_anchored", False)
    ev    = e["ev_pct"]
    books = e["num_other_books"]
    mkt   = e["market"]
    sport = e.get("sport", "").upper()
    # The Odds API tags tennis events with the tournament title
    # ("ATP Italian Open", "WTA Madrid Open"), never a flat "TENNIS", so
    # the TENNIS branch below never fired live — tennis silently never
    # reached Play These despite being added 2026-05-22. Collapse the
    # ATP/WTA prefixes (same rule as _canonical_promo_sport and
    # backtest.py's grader). Tennis has no Pinnacle coverage, so every
    # tennis edge is non-Pinn.
    if sport.startswith("ATP") or sport.startswith("WTA"):
        sport = "TENNIS"

    if not _starts_within_days(e, MAX_PLAY_DAYS):
        return False
    if books < 4:
        return False

    # Universal EV cap — books offering 5%+ over Pinnacle are nearly
    # always trap lines (stale on news, mismatched markets, model edge
    # cases). Even Pinn-anchored picks at ≥5% lost ~$43 over 12 days.
    if ev >= 5.0:
        return False

    # Block European soccer — negative ROI across the board without Pinnacle
    if sport in _BLOCKED_SOCCER:
        return False

    # NBA game lines: narrow re-enable for SPREADS only after 60-day
    # backtest (2026-03-26 → 2026-05-26, post-recalibration replay)
    # showed NBA spreads non-Pinn 1–2% at +24.6% on 52 picks and 2–3%
    # at +12.9% on 21 picks. H2H also positive (+5.9% on 189) but
    # smaller per-bucket samples + matchup variance — defer until
    # next season's data. Totals stay blocked (-6.8% on 167). Capped
    # at <3% because 3–5% had small-sample losses (4 picks, -100%).
    # NBA pts props remain handled by _qualifies_prop().
    if sport == "NBA":
        if mkt == "spreads":
            # <3% cap applies to both Pinn and non-Pinn — NBA-spreads
            # Pinn at higher EVs is too rare to have a reliable signal
            # in 60d, and the positive band is 1.0–3.0% non-Pinn.
            return 1.0 <= ev < 3.0
        return False

    # H2H: MLB / MLS / TENNIS (boxing paused 2026-06-22 — see docstring).
    # MLB non-Pinn keeps the 2.5–3.0% upper cap (lineup/weather stale-line
    # trap above 3% non-Pinn). All Pinn-anchored paths qualify ≥1%
    # (subject to the universal <5% cap above).
    if mkt == "h2h":
        if sport == "MLB":
            if pinn and ev >= 1.0:
                return True
            return 2.5 <= ev <= 3.0
        if sport == "MLS":
            if pinn and ev >= 1.0:
                return True
            return ev >= 2.5
        if sport == "TENNIS":
            if pinn and ev >= 1.0:
                return True
            return ev >= 2.5
        # BOXING h2h paused 2026-06-22 (see docstring) — falls through to skip.
        return False

    if mkt == "spreads":
        if pinn and ev >= 1.0:
            return True
        # WNBA spreads non-Pinn floor lowered to 1.0% — the 1.0–2.5%
        # slice is the highest-ROI WNBA-spreads pocket.
        if sport == "WNBA":
            return ev >= 1.0
        return ev >= 2.0

    # Totals: MLB-only, Pinnacle-anchored. The non-MLB totals tail
    # bleeds even with the Pinnacle gate — 2026-05-06 30d backtest had
    # PT TOTALS at -12.4% ROI overall ($-12.66 P&L) despite the Pinn
    # requirement; high-EV totals get *worse* (≥3% -26%, ≥5% -39%),
    # the classic stale-soft-line trap. MLB totals are the only sport
    # where the model's totals signal holds up.
    if mkt == "totals":
        return sport == "MLB" and pinn and ev >= 1.0

    return False


def _qualifies_prop_base(e):
    """Return True if a player prop edge qualifies for 'Play These'.

    2026-06-02 — pts raised 2.0 → 3.0. A ~2-month broad-pool replay
    (2026-04→05, n=247 NBA pts, all non-Pinn) refuted the 2026-05-22 read
    that the 2–3% band was the best pocket: 2–3% is now -6.8% ROI / -8.2%
    flat on n=54, while the positive signal sits at 3%+ (3–5% breakeven,
    5%+ thin-but-strong). Dropping the 2–3% band lifts the whole included
    prop set from +24.7% ROI / +11.1% flat to +33.9% / +20.7% (n 163→109).
    strikeouts (4.0) and total_bases (1.5) held up — left unchanged.

    Recalibrated 2026-05-22 against the 12-day backtest (2026-05-05 →
    05-16, 241 broad PP picks). Two threshold changes:

      pts:        12-day broad EV-tier slice was 1–2% +24.9% on 13 picks,
                  2–3% +35.3% on 4, 3–4% -15.2% on 2, 4–5% +92.1% on 2.
                  Total +27.6% on 25 picks. The 3% gate from 2026-05-08
                  was tuned on the NBA-only 30-day window where 1–3% ran
                  breakeven; the recent re-test shows 2–3% is the highest-
                  ROI pocket. Drop to 2.0%.

      strikeouts: 12-day broad slice 1–2% -10.4% on 15, 2–3% -38.0% on 6,
                  4–5% -4.1% on 2, ≥5% -19.8% on 2. Total -22.2% on 40.
                  The 2.0% floor (from 2026-05-08 against the +6.9% 2–3%
                  bucket in 30d) has inverted — current data shows the
                  1–3% strikeouts band is a -22% bleed. Raise to 4.0% to
                  drop the bleed; live volume falls but PnL improves.

      total_bases: 12-day broad 1–2% +47.6% on 3, 4–5% -100% on 2.
                  Sample too small to recalibrate. Keep 1.5% floor.

    Earlier history (kept for reference):

    2026-05-08 — per-market non-Pinn thresholds introduced (pts 3.0,
                 strikeouts 2.0, total_bases 1.5) replacing flat 3.0%
                 gate.
    2026-05-06 — added MLB total_bases + strikeouts after grader fixes.
    2026-04-12 — initial NBA-only pts calibration (3.0%, no Pinn req).
    2026-04-16 — EV cap removed for parity with game-line qualifier.
                 (Re-evaluating; props don't show the ≥5% stale-line
                 trap that game lines do.)
    """
    ev    = e["ev_pct"]
    mkt   = e["market"]
    pinn  = e.get("pinnacle_anchored", False)

    if not _starts_within_days(e, MAX_PLAY_DAYS):
        return False

    # Pinn ≥1% gate is symmetric defence — Pinnacle props aren't wired
    # in today, but if we ever blend them in the threshold's already
    # set. Active path is the per-market non-Pinn gate below.
    if pinn and ev >= 1.0 and mkt in ("pts", "total_bases", "strikeouts"):
        return True

    # Per-market non-Pinn thresholds — see EV-tier table in docstring.
    if mkt == "pts":
        return ev >= 3.0
    if mkt == "strikeouts":
        return ev >= 4.0
    if mkt == "total_bases":
        return ev >= 1.5

    return False


def _gate_override(e: dict, base: bool, is_prop: bool) -> bool:
    """Let a human-approved gate override (gate_overrides.py) flip the base
    _qualifies decision. With no approved override — the default — `base` is
    returned unchanged, so live behaviour is byte-identical to the hand-tuned
    gate until the admin approves a proposal via /gate_approve. Any error
    falls back to `base`, so overrides can never break the gate."""
    try:
        import logging
        import gate_overrides
        sport = (e.get("sport", "") or "").upper()
        if sport.startswith("ATP") or sport.startswith("WTA"):
            sport = "TENNIS"
        forced = gate_overrides.decision_override(
            is_prop, sport, e.get("market", ""), e.get("ev_pct"))
        if forced is None or forced == base:
            return base
        logging.getLogger(__name__).info(
            "[gate-override] %s %s ev=%.2f: base=%s -> %s",
            sport or "prop", e.get("market"), e.get("ev_pct") or 0.0,
            base, forced)
        return forced
    except Exception:
        return base


def _qualifies(e):
    """Public game-line gate: base rules plus any human-approved override."""
    return _gate_override(e, _qualifies_base(e), is_prop=False)


def _qualifies_prop(e):
    """Public prop gate: base rules plus any human-approved override."""
    return _gate_override(e, _qualifies_prop_base(e), is_prop=True)


def write_excel(edges: list[dict], dk_bankroll: float,
                fd_bankroll: float, output_path: str,
                promos: list[dict] = None,
                hr_edges: list[dict] = None,
                golf_edges: list[dict] = None,
                player_prop_edges: list[dict] = None,
                parlay_pool: list = None,
                script_dir: str = None,
                promo_pool: list = None,
                pp_promo_pool: list = None,
                golf_promo_pool: list = None,
                an_only: bool = False):
    wb = openpyxl.Workbook()

    # Load keys from previous run for "New Picks" seen-tracking
    _script_dir   = script_dir or os.path.dirname(os.path.abspath(output_path))
    _prev_keys    = _load_prev_play_keys(_script_dir)

    # ── Styles ────────────────────────────────────────────────────────
    header_font  = Font(bold=True, size=12, color="FFFFFF")
    header_fill  = PatternFill(start_color="2F5496", end_color="2F5496", fill_type="solid")
    thin_border  = Border(
        left=Side(style="thin"), right=Side(style="thin"),
        top=Side(style="thin"),  bottom=Side(style="thin"),
    )
    tier_strong  = PatternFill(start_color="C6EFCE", end_color="C6EFCE", fill_type="solid")
    tier_solid   = PatternFill(start_color="E2EFDA", end_color="E2EFDA", fill_type="solid")
    tier_caution = PatternFill(start_color="FFEB9C", end_color="FFEB9C", fill_type="solid")
    tier_weak    = PatternFill(start_color="FCE4EC", end_color="FCE4EC", fill_type="solid")
    alt_fill     = PatternFill(start_color="F2F2F2", end_color="F2F2F2", fill_type="solid")

    # Confidence helpers — moved to module scope (_confidence, _qualifies)

    conf_fill = {
        "STRONG":    PatternFill(start_color="C6EFCE", end_color="C6EFCE", fill_type="solid"),
        "SHARP":     PatternFill(start_color="D5E8D4", end_color="D5E8D4", fill_type="solid"),
        "SOLID":     PatternFill(start_color="E2EFDA", end_color="E2EFDA", fill_type="solid"),
        "CONSENSUS": PatternFill(start_color="DAEEF3", end_color="DAEEF3", fill_type="solid"),
        "GOOD":      PatternFill(start_color="FFEB9C", end_color="FFEB9C", fill_type="solid"),
    }
    conf_font = {
        "STRONG":    Font(bold=True, color="006100"),
        "SHARP":     Font(bold=True, color="006100"),
        "SOLID":     Font(bold=True, color="375623"),
        "CONSENSUS": Font(bold=True, color="1F4E79"),
        "GOOD":      Font(bold=True, color="9C5700"),
    }

    # ── Summary sheet ─────────────────────────────────────────────────
    ws = wb.active
    ws.title = "Summary"

    if an_only:
        ws["A1"] = ("⚠ ACTION NETWORK ONLY — Odds API quota exhausted. "
                    "Pinnacle blending degraded; consider sizing down.")
        ws["A1"].font = Font(bold=True, size=14, color="9C0006")
        ws["A1"].fill = PatternFill(
            start_color="FFEB9C", end_color="FFEB9C", fill_type="solid")
        ws["A1"].alignment = Alignment(horizontal="left", vertical="center",
                                       wrap_text=True)
        ws.merge_cells("A1:G1")
        ws.row_dimensions[1].height = 36
        ws["A2"] = "ODDS COMPARE — +EV EDGE FINDER"
        ws["A2"].font = Font(bold=True, size=16, color="2F5496")
        ws["A3"] = f"Generated: {datetime.now(EASTERN).strftime('%Y-%m-%d %H:%M %Z')}"
        ws["A3"].font = Font(italic=True, color="666666")
        ws["A4"] = "Source: Action Network (fallback — Pinnacle missing on most sports)"
        ws["A4"].font = Font(italic=True, bold=True, color="9C0006")
        _summary_row_offset = 1
    else:
        ws["A1"] = "ODDS COMPARE — +EV EDGE FINDER"
        ws["A1"].font = Font(bold=True, size=16, color="2F5496")
        ws["A2"] = f"Generated: {datetime.now(EASTERN).strftime('%Y-%m-%d %H:%M %Z')}"
        ws["A2"].font = Font(italic=True, color="666666")
        ws["A3"] = "Source: The Odds API (primary) + Pinnacle blending"
        ws["A3"].font = Font(italic=True, color="888888")
        _summary_row_offset = 0

    def _r(n: int) -> int:
        return n + _summary_row_offset

    ws[f"A{_r(4)}"] = "DraftKings Bankroll:"
    ws[f"A{_r(4)}"].font = Font(bold=True, size=13)
    ws[f"B{_r(4)}"] = dk_bankroll
    ws[f"B{_r(4)}"].number_format = '$#,##0.00'
    ws[f"B{_r(4)}"].font = Font(bold=True, size=13, color="2F5496")

    ws[f"A{_r(5)}"] = "FanDuel Bankroll:"
    ws[f"A{_r(5)}"].font = Font(bold=True, size=13)
    ws[f"B{_r(5)}"] = fd_bankroll
    ws[f"B{_r(5)}"].number_format = '$#,##0.00'
    ws[f"B{_r(5)}"].font = Font(bold=True, size=13, color="2F5496")

    ws[f"A{_r(6)}"] = "Kelly Fraction:"
    ws[f"A{_r(6)}"].font = Font(bold=True)
    ws[f"B{_r(6)}"] = f"{KELLY_FRACTION * 100}%"

    ws[f"A{_r(7)}"] = "Min EV Threshold:"
    ws[f"A{_r(7)}"].font = Font(bold=True)
    ws[f"B{_r(7)}"] = f"{MIN_EV_PERCENT}%"

    ws[f"A{_r(8)}"] = "Total +EV Bets Found:"
    ws[f"A{_r(8)}"].font = Font(bold=True)
    ws[f"B{_r(8)}"] = len(edges)

    dk_edges = [e for e in edges if e["book"] == "DraftKings"]
    fd_edges = [e for e in edges if e["book"] == "FanDuel"]

    total_wagered = sum(
        (dk_bankroll if e["book"] == "DraftKings" else fd_bankroll)
        * e["kelly_pct"] / 100
        for e in edges
    ) if edges else 0
    ws[f"A{_r(9)}"] = "Total Capital Deployed:"
    ws[f"A{_r(9)}"].font = Font(bold=True)
    ws[f"B{_r(9)}"] = total_wagered
    ws[f"B{_r(9)}"].number_format = '$#,##0.00'

    pinnacle_count = sum(1 for e in edges if e.get("pinnacle_anchored"))
    ws[f"A{_r(10)}"] = "Pinnacle-Anchored Edges:"
    ws[f"A{_r(10)}"].font = Font(bold=True)
    ws[f"B{_r(10)}"] = f"{pinnacle_count} / {len(edges)}"

    ws[f"A{_r(11)}"] = "DraftKings Edges:"
    ws[f"A{_r(11)}"].font = Font(bold=True)
    ws[f"B{_r(11)}"] = len(dk_edges)
    ws[f"A{_r(12)}"] = "FanDuel Edges:"
    ws[f"A{_r(12)}"].font = Font(bold=True)
    ws[f"B{_r(12)}"] = len(fd_edges)

    if edges:
        ws[f"A{_r(13)}"] = "Best EV:"
        ws[f"A{_r(13)}"].font = Font(bold=True)
        ws[f"B{_r(13)}"] = (
            f"{edges[0]['ev_pct']}% — {edges[0]['game']} ({edges[0]['book']})"
        )

    for col in range(1, 3):
        ws.column_dimensions[get_column_letter(col)].width = 28

    # ── EV Edges sheet ────────────────────────────────────────────────
    ws_ev = wb.create_sheet("EV Edges")

    columns = [
        ("Rank", 6), ("Sport", 14), ("Game", 32), ("Game Time", 18),
        ("Book", 13), ("Market", 10), ("Pick", 22), ("Line", 8),
        ("Odds", 10), ("Decimal", 9), ("Fair Prob %", 11),
        ("Implied Prob %", 14), ("EV %", 9), ("Edge %", 9),
        ("Kelly %", 9), ("Bet Amount", 13), ("Potential Profit", 15),
        ("# Books", 9), ("Best Other Line", 15), ("📌 Pinnacle", 10),
    ]

    for col_idx, (name, width) in enumerate(columns, 1):
        cell           = ws_ev.cell(row=1, column=col_idx, value=name)
        cell.font      = header_font
        cell.fill      = header_fill
        cell.alignment = Alignment(horizontal="center", wrap_text=True)
        cell.border    = thin_border
        ws_ev.column_dimensions[get_column_letter(col_idx)].width = width

    for i, edge in enumerate(edges):
        row           = i + 2
        edge_bankroll = dk_bankroll if edge["book"] == "DraftKings" else fd_bankroll
        bet_amount    = clamp_bet(edge_bankroll * edge["kelly_pct"] / 100, edge["book"])
        profit        = bet_amount * (edge["odds_decimal"] - 1)
        edge_pct      = edge["fair_prob"] - edge["implied_prob"]

        try:
            ct        = datetime.fromisoformat(edge["commence"].replace("Z", "+00:00"))
            game_time = ct.astimezone(EASTERN).strftime("%m/%d %I:%M %p %Z")
        except (ValueError, AttributeError):
            game_time = edge["commence"]

        point_display = _fmt_pt(edge["point"], edge["market"])

        if edge["ev_pct"] >= 5:
            row_fill = tier_strong;  ev_font = Font(bold=True, color="006100")
        elif edge["ev_pct"] >= 3:
            row_fill = tier_solid;   ev_font = Font(bold=True, color="006100")
        elif edge["ev_pct"] >= 1:
            row_fill = tier_caution; ev_font = Font(bold=True, color="9C5700")
        else:
            row_fill = tier_weak;    ev_font = Font(color="999999")

        values = [
            i + 1, edge["sport"], edge["game"], game_time,
            edge["book"], edge["market"].upper(), edge["outcome"], point_display,
            _fmt_am(edge["odds_american"]), edge["odds_decimal"],
            edge["fair_prob"], edge["implied_prob"],
            edge["ev_pct"], round(edge_pct, 1), edge["kelly_pct"],
            bet_amount, profit,
            edge["num_other_books"], edge["best_other_odds"],
            "✓" if edge.get("pinnacle_anchored") else "",
        ]

        for col_idx, val in enumerate(values, 1):
            cell           = ws_ev.cell(row=row, column=col_idx, value=val)
            cell.border    = thin_border
            cell.alignment = Alignment(horizontal="center")
            cell.fill      = row_fill

        ws_ev.cell(row=row, column=16).number_format = '$#,##0.00'
        ws_ev.cell(row=row, column=17).number_format = '$#,##0.00'
        ws_ev.cell(row=row, column=13).font          = ev_font

    ws_ev.freeze_panes = "A2"
    if edges:
        ws_ev.auto_filter.ref = (
            f"A1:{get_column_letter(len(columns))}{len(edges) + 1}")

    # ── Per-book sheets ───────────────────────────────────────────────
    if dk_edges:
        _write_book_sheet(wb.create_sheet("DraftKings"), dk_edges, dk_bankroll,
                          columns, header_font, header_fill, thin_border,
                          tier_strong, tier_solid, tier_caution, tier_weak)
    if fd_edges:
        _write_book_sheet(wb.create_sheet("FanDuel"), fd_edges, fd_bankroll,
                          columns, header_font, header_fill, thin_border,
                          tier_strong, tier_solid, tier_caution, tier_weak)

    # ── "Play These" sheet — filtered by backtest-validated rules ────
    #   Recalibrated 2026-04-12 against 1906 EV edges + 422 props (full history).
    #
    #   BLOCK: European soccer (EPL, Bundesliga, Ligue1, Serie A, Champions,
    #     Europa). Consensus-only fair prices unreliable: EPL totals -66.9%,
    #     Bundesliga totals -37.8%, Ligue1 totals -27.2%.
    #
    #   BLOCK: NBA game lines. -18.9% ROI lifetime. Exception: NBA PTS props
    #     (+9.8% ROI, 117 picks) via _qualifies_prop().
    #
    #   BLOCK: H2H — all sports EXCEPT MLB. MLB H2H: +26.4% ROI, 114 picks,
    #     54% WR — the best-performing combo in the dataset.
    #
    #   BLOCK: EV ≥ 5% — stale-data false positives.
    #   BLOCK: < 4 consensus books — insufficient data for fair line.
    #
    #   SPREADS: EV ≥ 2%, 4+ books. (Pinnacle bonus: EV ≥ 1%.)
    #   TOTALS:  EV ≥ 2%, 5+ books. (Pinnacle bonus: EV ≥ 1%, 4+ books.)
    #   MLB H2H: EV ≥ 2%, 4+ books. (Pinnacle bonus: EV ≥ 1%.)
    #   PTS PROPS: EV ≥ 2%, no book minimum. (Pinnacle bonus: EV ≥ 1%.)
    #
    #     without Pinnacle at the 2% floor.
    #
    #   PLAYER PROPS (pts): EV ≥ 2%, 3+ books.  (Pinnacle: EV ≥ 1%, 2+ books.)
    #     Calibrated 2026-04-10: NBA pts 48% WR, +9.1% ROI over 110 picks.
    #     reb/ast excluded — negative ROI.
    play_edges = [e for e in edges if _qualifies(e)]
    if player_prop_edges:
        play_edges.extend(e for e in player_prop_edges if _qualifies_prop(e))
    play_edges.sort(key=lambda e: e["ev_pct"], reverse=True)

    if play_edges:
        ws_play = wb.create_sheet("Play These")

        play_cols = [
            ("", 4), ("Sport", 12), ("Game", 32), ("Game Time", 18),
            ("Book", 13), ("Market", 10), ("Pick", 22), ("Line", 8),
            ("Odds", 10), ("EV %", 9), ("Confidence", 12),
            ("Kelly %", 9), ("Bet Amount", 13), ("Potential Profit", 15),
            ("Best Other Line", 15),
        ]
        num_play_cols = len(play_cols)
        BET_AMT_COL   = 13  # 1-based column index for "Bet Amount"

        # Gold fill for Bet Amount cells — stands out against all row fills
        bet_amt_fill = PatternFill(start_color="FFF2CC", end_color="FFF2CC", fill_type="solid")
        dk_sect_fill = PatternFill(start_color="1F4E79", end_color="1F4E79", fill_type="solid")
        fd_sect_fill = PatternFill(start_color="1B4332", end_color="1B4332", fill_type="solid")

        def _write_play_section(edges_subset, bankroll, label, sect_fill, start_row):
            """Write a per-book sub-table; returns the next free row."""
            # Banner
            banner      = ws_play.cell(row=start_row, column=1, value=label)
            banner.font      = Font(bold=True, size=14, color="FFFFFF")
            banner.fill      = sect_fill
            banner.alignment = Alignment(horizontal="left")
            banner.border    = thin_border
            ws_play.merge_cells(start_row=start_row, start_column=1,
                                end_row=start_row, end_column=num_play_cols)
            ws_play.row_dimensions[start_row].height = 24

            # Column headers
            hdr_row = start_row + 1
            for col_idx, (name, width) in enumerate(play_cols, 1):
                cell           = ws_play.cell(row=hdr_row, column=col_idx, value=name)
                cell.font      = header_font
                cell.fill      = sect_fill
                cell.alignment = Alignment(horizontal="center", wrap_text=True)
                cell.border    = thin_border
                ws_play.column_dimensions[get_column_letter(col_idx)].width = max(
                    ws_play.column_dimensions[get_column_letter(col_idx)].width or 0,
                    width)

            total_risked = 0
            total_profit = 0
            for i, edge in enumerate(edges_subset):
                row           = hdr_row + 1 + i
                bet_amount    = clamp_bet(bankroll * edge["kelly_pct"] / 100, edge["book"])
                profit        = bet_amount * (edge["odds_decimal"] - 1)
                total_risked += bet_amount
                total_profit += profit
                conf          = _confidence(edge)

                try:
                    ct        = datetime.fromisoformat(
                        edge["commence"].replace("Z", "+00:00"))
                    game_time = ct.astimezone(EASTERN).strftime("%m/%d %I:%M %p %Z")
                except (ValueError, AttributeError):
                    game_time = edge["commence"]

                point_display = _fmt_pt(edge["point"], edge["market"])

                values = [
                    i + 1, edge["sport"], edge["game"], game_time,
                    edge["book"], edge["market"].upper(), edge["outcome"],
                    point_display, _fmt_am(edge["odds_american"]), edge["ev_pct"],
                    conf, edge["kelly_pct"], bet_amount, profit,
                    edge["best_other_odds"],
                ]

                for col_idx, val in enumerate(values, 1):
                    cell           = ws_play.cell(row=row, column=col_idx, value=val)
                    cell.border    = thin_border
                    cell.alignment = Alignment(horizontal="center")
                    if i % 2 == 1:
                        cell.fill  = alt_fill

                # Confidence cell
                ws_play.cell(row=row, column=11).fill = conf_fill[conf]
                ws_play.cell(row=row, column=11).font = conf_font[conf]

                # EV cell
                ws_play.cell(row=row, column=10).font = Font(bold=True, color="006100")

                # Bet Amount — gold highlight so it's easy to spot
                bet_cell           = ws_play.cell(row=row, column=BET_AMT_COL)
                bet_cell.fill      = bet_amt_fill
                bet_cell.font      = Font(bold=True, size=12, color="7B4D00")
                bet_cell.number_format = '$#,##0.00'
                ws_play.cell(row=row, column=14).number_format = '$#,##0.00'

            # Totals row
            totals_row = hdr_row + 1 + len(edges_subset)
            ws_play.cell(row=totals_row, column=12,
                         value="TOTALS:").font = Font(bold=True, size=12)
            tot_bet                = ws_play.cell(row=totals_row, column=13,
                                                  value=total_risked)
            tot_bet.number_format  = '$#,##0.00'
            tot_bet.font           = Font(bold=True, size=12, color="7B4D00")
            tot_bet.fill           = bet_amt_fill
            tot_prof               = ws_play.cell(row=totals_row, column=14,
                                                  value=total_profit)
            tot_prof.number_format = '$#,##0.00'
            tot_prof.font          = Font(bold=True, size=12, color="006100")

            return totals_row + 3   # skip two blank rows before next section

        dk_play = [e for e in play_edges if e["book"] == "DraftKings"]
        fd_play = [e for e in play_edges if e["book"] == "FanDuel"]

        current_row = 1
        dk_hdr_row  = None   # track for freeze_panes / auto_filter
        if dk_play:
            dk_hdr_row  = current_row + 1
            current_row = _write_play_section(
                dk_play, dk_bankroll, "DraftKings", dk_sect_fill, current_row)
        if fd_play:
            current_row = _write_play_section(
                fd_play, fd_bankroll, "FanDuel", fd_sect_fill, current_row)

        # Filter rules summary at top-right
        note_col = num_play_cols + 2
        ws_play.cell(row=1, column=note_col,
                     value="FILTER RULES (backtest-validated):").font = Font(bold=True, size=11)
        ws_play.cell(row=2, column=note_col,
                     value="SHARP: Pinnacle-anchored + EV >= 1%").font = Font(size=10)
        ws_play.cell(row=3, column=note_col,
                     value="CONSENSUS: 7+ books + EV >= 0.5% (no totals)").font = Font(size=10)
        ws_play.cell(row=4, column=note_col,
                     value="SOLID: Spreads + 4+ books + EV >= 1%").font = Font(size=10)
        ws_play.cell(row=5, column=note_col,
                     value="EXCLUDED: Totals w/o Pinnacle, < 4 books").font = Font(
                         size=10, color="999999")
        ws_play.column_dimensions[get_column_letter(note_col)].width = 42

        # ── Promo Picks section — best unique edge per promo ─────────
        straight_promos = [p for p in (promos or [])
                           if p.get("promo_type", "straight") in ("straight", "bonus_bet")]
        if straight_promos:
            promo_section_start = current_row
            promo_header_fill   = PatternFill(start_color="7B2D8E",
                                              end_color="7B2D8E", fill_type="solid")
            promo_boost_fill    = PatternFill(start_color="E8D5F5",
                                              end_color="E8D5F5", fill_type="solid")
            bonus_bet_fill      = PatternFill(start_color="D5F5E3",
                                              end_color="D5F5E3", fill_type="solid")

            # Section title
            title_cell = ws_play.cell(row=promo_section_start, column=1,
                                      value="PROMO PICKS — Best Unique Play Per Promo")
            title_cell.font = Font(bold=True, size=13, color="7B2D8E")
            ws_play.cell(row=promo_section_start + 1, column=1,
                         value=("Hit-rate ruleset: bonus bets target +200..+450, "
                                "boosts -200..+450 — first populated band wins, EV "
                                "ranks within it. Player props only within 75 min "
                                "of start (post-lineup). Each edge assigned to at "
                                "most one promo. Bonus Bet: you keep profit only "
                                "(stake is free).")).font = Font(
                                    size=9, italic=True, color="666666")

            # Sub-header
            sub_hdr_row = promo_section_start + 2
            sub_headers = ["Promo", "Game", "Book", "Market", "Pick",
                           "Line", "Orig Odds", "Boosted Odds",
                           "Orig EV %", "Boosted EV %", "EV Gain",
                           "Bet Amount", "Potential Profit", "Min Odds"]
            sub_widths  = [38, 32, 13, 10, 22, 8, 12, 12, 10, 11, 9, 12, 14, 10]
            for c, (hdr, w) in enumerate(zip(sub_headers, sub_widths), 1):
                cell           = ws_play.cell(row=sub_hdr_row, column=c, value=hdr)
                cell.font      = Font(bold=True, size=11, color="FFFFFF")
                cell.fill      = promo_header_fill
                cell.alignment = Alignment(horizontal="center", wrap_text=True)
                cell.border    = thin_border
                ws_play.column_dimensions[get_column_letter(c)].width = max(
                    ws_play.column_dimensions[get_column_letter(c)].width or 0, w)

            # Greedy assignment: best promo first, no edge reused.
            # Sort: profit-boosts by boost_pct desc; bonus bets by bet_amount desc.
            used_edge_ids: set[int] = set()
            sorted_promos = sorted(
                straight_promos,
                key=lambda p: (p["promo_type"] != "bonus_bet",   # boosts first
                               -p.get("boost_pct", 0)
                               if p["promo_type"] != "bonus_bet"
                               else -p.get("bet_amount", 0)))
            data_row = sub_hdr_row + 1
            for promo in sorted_promos:
                is_bonus_bet = promo.get("promo_type") == "bonus_bet"

                # Selection runs through select_promo_edge — the hit-rate
                # ruleset: staged odds windows (target band first), EV-
                # ranked within a window; voidable props lineup-window-
                # gated in _promo_matches.
                # Use the broader promo_pool (all DK/FD outcomes) so boosts
                # can turn mildly -EV edges into +EV picks. HR edges are
                # included; _promo_matches gates them to HR-targeted promos only.
                # pp_promo_pool / golf_promo_pool are the unfiltered versions
                # of player_prop_edges / golf_edges (populated by find_X_edges
                # via the side-effect param) so PGA + prop-boost promos can
                # see candidates that are sub-MIN_EV pre-boost; fall back to
                # the gated lists when an older caller hasn't supplied them.
                # pp_promo_pool already includes HR (find_player_prop_edges
                # appends every constructed edge to promo_pool, then skips
                # HR before the gated edges list). hr_edges is a separate
                # view used by HR Props rendering but redundant here.
                # player_prop_edges is the gated fallback for legacy callers
                # that didn't pass pp_promo_pool — HR is intentionally absent
                # from that list, so HR-boost lifts only work when the
                # caller provides pp_promo_pool (always true in the current
                # run_headless flow).
                _play_promo_pool = (list(promo_pool or edges)
                                    + list(pp_promo_pool
                                           or player_prop_edges or [])
                                    + list(golf_promo_pool
                                           or golf_edges or []))
                candidates = [e for e in _play_promo_pool
                              if id(e) not in used_edge_ids
                              and _promo_matches(promo, e)]
                best_edge, band_stage = select_promo_edge(promo, candidates)

                if best_edge is None:
                    # No match — show a placeholder row
                    cell = ws_play.cell(row=data_row, column=1,
                                        value=promo["label"])
                    cell.border    = thin_border
                    cell.alignment = Alignment(horizontal="left")
                    ws_play.cell(row=data_row, column=2,
                                 value="No qualifying pick found").font = Font(
                                     italic=True, color="999999")
                    data_row += 1
                    continue

                used_edge_ids.add(id(best_edge))

                # Flag off-band picks in the Promo column so a glance at
                # the sheet shows whether the target band was honored.
                n_windows = len(PROMO_ODDS_WINDOWS[
                    "bonus_bet" if is_bonus_bet else "boost"])
                if band_stage and band_stage >= n_windows:
                    band_note = " [outside odds bands]"
                elif band_stage:
                    band_note = " [relaxed band]"
                else:
                    band_note = ""
                # Prop picked before lineups are out — flag participation
                # risk (the pick voids and burns the promo on a DNP).
                if _pick_needs_participation_warn(best_edge):
                    band_note += " [confirm player active]"
                promo_label_disp = promo["label"] + band_note

                orig_dec    = best_edge["odds_decimal"]
                fair_prob_v = best_edge["fair_prob"] / 100
                mo_display  = ""
                if promo.get("min_odds") is not None:
                    mo = promo["min_odds"]
                    mo_display = f"{'+' if mo >= 0 else ''}{mo}"
                point_disp  = _fmt_pt(best_edge["point"], best_edge["market"])

                if is_bonus_bet:
                    # ── Bonus Bet math ────────────────────────────────
                    # Risk = $0 (stake is free). Win = profit only.
                    bet_v      = promo["bet_amount"]
                    profit_v   = bet_v * (orig_dec - 1)
                    exp_profit = fair_prob_v * profit_v          # expected $ gain
                    boosted_ev = (exp_profit / bet_v) * 100      # as % of bet
                    ev_gain    = round(boosted_ev - best_edge["ev_pct"], 2)
                    values = [
                        promo_label_disp,
                        best_edge["game"],
                        best_edge["book"],
                        best_edge["market"].upper(),
                        best_edge["outcome"],
                        point_disp,
                        _fmt_am(best_edge["odds_american"]),
                        "FREE BET",
                        best_edge["ev_pct"],
                        round(boosted_ev, 2),
                        ev_gain,
                        bet_v,
                        profit_v,
                        mo_display,
                    ]
                else:
                    # ── Profit Boost / draw-insurance math ────────────
                    boost_dec_v = apply_profit_boost(orig_dec,
                                                     promo.get("boost_pct") or 0)
                    # Money-back-on-draw adds P(draw) to EV (stake refunded,
                    # not lost, on a draw) and lifts Kelly accordingly; both
                    # add 0 for a normal boost.
                    boosted_ev  = (calc_ev(fair_prob_v, boost_dec_v)
                                   + _draw_refund_ev_pct(promo, best_edge))
                    boosted_k   = _promo_kelly(promo, best_edge,
                                               boost_dec_v, fair_prob_v)
                    bankroll_v  = (dk_bankroll if best_edge["book"] == "DraftKings"
                                   else fd_bankroll)
                    bet_v, lottery_capped = promo_boost_stake(
                        bankroll_v, boosted_k, boost_dec_v,
                        best_edge["book"], promo.get("max_bet"))
                    profit_v    = bet_v * (boost_dec_v - 1)
                    if lottery_capped:
                        promo_label_disp += " [lottery — capped stake]"
                    values = [
                        promo_label_disp,
                        best_edge["game"],
                        best_edge["book"],
                        best_edge["market"].upper(),
                        best_edge["outcome"],
                        point_disp,
                        _fmt_am(best_edge["odds_american"]),
                        format_american(boost_dec_v),
                        best_edge["ev_pct"],
                        round(boosted_ev, 2),
                        round(boosted_ev - best_edge["ev_pct"], 2),
                        bet_v,
                        profit_v,
                        mo_display,
                    ]
                promo_row_idx = data_row - sub_hdr_row  # 1-based offset from sub-header
                for c, val in enumerate(values, 1):
                    cell           = ws_play.cell(row=data_row, column=c, value=val)
                    cell.border    = thin_border
                    cell.alignment = Alignment(horizontal="center")
                    if promo_row_idx % 2 == 0:
                        cell.fill  = alt_fill

                # Highlight value columns — green tint for bonus bets, purple for boosts
                accent_fill = bonus_bet_fill if is_bonus_bet else promo_boost_fill
                col8_font   = Font(bold=True, italic=is_bonus_bet,
                                   color="006100" if is_bonus_bet else "000000")
                ws_play.cell(row=data_row, column=8).fill = accent_fill
                ws_play.cell(row=data_row, column=8).font = col8_font
                ws_play.cell(row=data_row, column=10).fill = accent_fill
                ws_play.cell(row=data_row, column=10).font = Font(
                    bold=True, color="006100" if is_bonus_bet else "7B2D8E")
                ws_play.cell(row=data_row, column=11).fill = accent_fill

                # Bet Amount — gold highlight matches the main tables
                bet_cell           = ws_play.cell(row=data_row, column=12)
                bet_cell.fill      = bet_amt_fill
                bet_cell.font      = Font(bold=True, size=12, color="7B4D00")
                bet_cell.number_format = '$#,##0.00'
                ws_play.cell(row=data_row, column=13).number_format = '$#,##0.00'
                ws_play.cell(row=data_row, column=1).alignment = Alignment(
                    horizontal="left", wrap_text=True)

                # EV gain color
                gain = boosted_ev - best_edge["ev_pct"]
                ws_play.cell(row=data_row, column=11).font = Font(
                    bold=True, color="006100" if gain > 0 else "9C0006")

                data_row += 1

        # Freeze below the first section's column headers
        freeze_at = (dk_hdr_row + 1) if dk_hdr_row else 3
        ws_play.freeze_panes = f"A{freeze_at}"
        if dk_play and dk_hdr_row:
            ws_play.auto_filter.ref = (
                f"A{dk_hdr_row}:{get_column_letter(num_play_cols)}"
                f"{dk_hdr_row + len(dk_play)}")

    # ── New Picks tab (Play These + seen-pick markers) ───────────────
    if play_edges:
        ws_np = wb.create_sheet("New Picks")

        np_cols = [
            ("Status", 10), ("Sport", 12), ("Game", 32), ("Game Time", 18),
            ("Book", 13), ("Market", 10), ("Pick", 22), ("Line", 8),
            ("Odds", 10), ("EV %", 9), ("Confidence", 12),
            ("Kelly %", 9), ("Bet Amount", 13), ("Potential Profit", 15),
            ("Best Other Line", 15),
        ]
        num_np_cols  = len(np_cols)
        NP_BET_COL   = 13   # Bet Amount column (1-based)
        NP_STATUS_COL = 1

        np_new_fill   = PatternFill(start_color="C6EFCE", end_color="C6EFCE", fill_type="solid")
        np_seen_fill  = PatternFill(start_color="D9D9D9", end_color="D9D9D9", fill_type="solid")
        np_seen_row   = PatternFill(start_color="F2F2F2", end_color="F2F2F2", fill_type="solid")
        np_bet_fill   = PatternFill(start_color="FFF2CC", end_color="FFF2CC", fill_type="solid")

        # Sort: new picks first, seen picks at bottom
        def _is_seen(e):
            return _make_pick_key(e) in _prev_keys

        new_edges  = [e for e in play_edges if not _is_seen(e)]
        seen_edges = [e for e in play_edges if     _is_seen(e)]

        def _write_np_section(edges_subset, bankroll_fn, label, sect_fill,
                              status_label, status_fill, status_font,
                              row_fill_fn, start_row):
            """Write one section of the New Picks sheet; return next free row."""
            # Banner
            banner           = ws_np.cell(row=start_row, column=1, value=label)
            banner.font      = Font(bold=True, size=14, color="FFFFFF")
            banner.fill      = sect_fill
            banner.alignment = Alignment(horizontal="left")
            banner.border    = thin_border
            ws_np.merge_cells(start_row=start_row, start_column=1,
                              end_row=start_row, end_column=num_np_cols)
            ws_np.row_dimensions[start_row].height = 24

            # Column headers
            hdr_row = start_row + 1
            for col_idx, (name, width) in enumerate(np_cols, 1):
                cell           = ws_np.cell(row=hdr_row, column=col_idx, value=name)
                cell.font      = Font(bold=True, size=11, color="FFFFFF")
                cell.fill      = sect_fill
                cell.alignment = Alignment(horizontal="center", wrap_text=True)
                cell.border    = thin_border
                ws_np.column_dimensions[get_column_letter(col_idx)].width = max(
                    ws_np.column_dimensions[get_column_letter(col_idx)].width or 0,
                    width)

            total_risked = 0
            total_profit = 0
            for i, edge in enumerate(edges_subset):
                row           = hdr_row + 1 + i
                bankroll      = bankroll_fn(edge)
                bet_amount    = clamp_bet(bankroll * edge["kelly_pct"] / 100, edge["book"])
                profit        = bet_amount * (edge["odds_decimal"] - 1)
                total_risked += bet_amount
                total_profit += profit
                conf          = _confidence(edge)

                try:
                    ct        = datetime.fromisoformat(
                        edge["commence"].replace("Z", "+00:00"))
                    game_time = ct.astimezone(EASTERN).strftime("%m/%d %I:%M %p %Z")
                except (ValueError, AttributeError):
                    game_time = edge["commence"]

                point_display = _fmt_pt(edge["point"], edge["market"])
                row_fill      = row_fill_fn(i, edge, conf)

                values = [
                    status_label, edge["sport"], edge["game"], game_time,
                    edge["book"], edge["market"].upper(), edge["outcome"],
                    point_display, _fmt_am(edge["odds_american"]), edge["ev_pct"],
                    conf, edge["kelly_pct"], bet_amount, profit,
                    edge["best_other_odds"],
                ]

                for col_idx, val in enumerate(values, 1):
                    cell           = ws_np.cell(row=row, column=col_idx, value=val)
                    cell.border    = thin_border
                    cell.alignment = Alignment(horizontal="center")
                    if row_fill:
                        cell.fill  = row_fill

                # Status badge
                st_cell       = ws_np.cell(row=row, column=NP_STATUS_COL)
                st_cell.fill  = status_fill
                st_cell.font  = status_font

                # Confidence badge (only style if NEW — seen rows are greyed)
                if status_label == "NEW":
                    ws_np.cell(row=row, column=11).fill = conf_fill[conf]
                    ws_np.cell(row=row, column=11).font = conf_font[conf]
                    ws_np.cell(row=row, column=10).font = Font(bold=True, color="006100")
                else:
                    # Dim text on seen rows
                    for col_idx in range(2, num_np_cols + 1):
                        ws_np.cell(row=row, column=col_idx).font = Font(color="888888")

                # Bet amount highlight
                bet_cell              = ws_np.cell(row=row, column=NP_BET_COL)
                bet_cell.number_format = '$#,##0.00'
                ws_np.cell(row=row, column=14).number_format = '$#,##0.00'
                if status_label == "NEW":
                    bet_cell.fill = np_bet_fill
                    bet_cell.font = Font(bold=True, size=12, color="7B4D00")

            # Totals row
            if edges_subset:
                tot_row = hdr_row + 1 + len(edges_subset)
                ws_np.cell(row=tot_row, column=12,
                           value="TOTALS:").font = Font(bold=True, size=12)
                t_bet              = ws_np.cell(row=tot_row, column=13, value=total_risked)
                t_bet.number_format = '$#,##0.00'
                t_bet.font         = Font(bold=True, size=12, color="7B4D00")
                t_prof             = ws_np.cell(row=tot_row, column=14, value=total_profit)
                t_prof.number_format = '$#,##0.00'
                t_prof.font         = Font(bold=True, size=12, color="006100")
                return tot_row + 3
            return start_row + 2

        # For bankroll lookup in NP tab we need book → bankroll
        def _np_bankroll(edge):
            return dk_bankroll if edge["book"] == "DraftKings" else fd_bankroll

        new_sect_fill  = PatternFill(start_color="1B5E20", end_color="1B5E20", fill_type="solid")
        seen_sect_fill = PatternFill(start_color="616161", end_color="616161", fill_type="solid")

        def _new_row_fill(i, edge, conf):
            return conf_fill.get(conf) or (alt_fill if i % 2 == 1 else None)

        def _seen_row_fill(i, edge, conf):
            return np_seen_row

        np_hdr_row    = None
        current_row_np = 1

        if new_edges:
            np_hdr_row     = current_row_np + 1
            current_row_np = _write_np_section(
                new_edges, _np_bankroll,
                f"✦ NEW PICKS  ({len(new_edges)} pick{'s' if len(new_edges) != 1 else ''})",
                new_sect_fill,
                "NEW",
                np_new_fill,
                Font(bold=True, color="006100", size=10),
                _new_row_fill,
                current_row_np,
            )
        else:
            # No new picks — placeholder message
            ws_np.cell(row=1, column=1,
                       value="No new picks this run — all current plays were already on the previous sheet.").font = Font(
                           italic=True, color="666666", size=11)
            current_row_np = 4

        if seen_edges:
            current_row_np = _write_np_section(
                seen_edges, _np_bankroll,
                f"ALREADY SEEN  ({len(seen_edges)} pick{'s' if len(seen_edges) != 1 else ''}) — shown on previous sheet, likely already bet",
                seen_sect_fill,
                "SEEN ✓",
                np_seen_fill,
                Font(bold=True, color="555555", size=10),
                _seen_row_fill,
                current_row_np,
            )

        # Freeze + filter
        freeze_np = (np_hdr_row + 1) if np_hdr_row else 3
        ws_np.freeze_panes = f"A{freeze_np}"
        if new_edges and np_hdr_row:
            ws_np.auto_filter.ref = (
                f"A{np_hdr_row}:{get_column_letter(num_np_cols)}"
                f"{np_hdr_row + len(new_edges)}")

    # ── Odds Comparison sheet (game lines + player props combined) ────
    _cmp_all = (list(edges) + list(player_prop_edges or []))
    _cmp_all.sort(key=lambda x: x["ev_pct"], reverse=True)
    if _cmp_all:
        ws_cmp = wb.create_sheet("Odds Comparison")

        # Collect every book name that appears across ALL edges (game + prop)
        all_books_set: set[str] = set()
        for edge in _cmp_all:
            all_books_set.update(edge.get("all_book_odds", {}).keys())

        # Fixed order: DraftKings, FanDuel first, then Pinnacle, then rest alpha
        priority = ["DraftKings", "FanDuel", "Pinnacle", "Bovada",
                    "BetMGM", "Caesars", "PointsBet", "ESPN Bet"]
        rest = sorted(all_books_set - set(priority))
        book_order = [b for b in priority if b in all_books_set] + rest

        # Header: Type column added so game lines vs props are instantly filterable.
        # Commence column (ET date) lets the backtester resolve to the real game
        # date instead of relying on the file-write date as a proxy — critical
        # during playoffs when books post props several days in advance.
        info_cols = [
            ("Rank", 6), ("Type", 13), ("Sport", 10), ("Game", 32),
            ("Market", 14), ("Pick", 24), ("Line", 8), ("EV %", 8),
            ("Fair Odds", 10), ("Commence", 12),
        ]
        # "Best Book" trails the per-book odds columns so the backtester can
        # attribute each prop row to the book the edge was actually found at
        # (highlight fills don't survive a values-only read; without this,
        # FanDuel-only edges get mis-keyed as DraftKings whenever DK also
        # lists the line — which breaks the closing-line CLV lookup).
        cmp_columns = (info_cols + [(bk, 13) for bk in book_order]
                       + [("Best Book", 13)])

        for col_idx, (name, width) in enumerate(cmp_columns, 1):
            cell           = ws_cmp.cell(row=1, column=col_idx, value=name)
            cell.font      = header_font
            cell.fill      = header_fill
            cell.alignment = Alignment(horizontal="center", wrap_text=True)
            cell.border    = thin_border
            ws_cmp.column_dimensions[get_column_letter(col_idx)].width = width

        # Highlight fills
        best_fill   = PatternFill(start_color="C6EFCE", end_color="C6EFCE", fill_type="solid")
        target_fill = PatternFill(start_color="DAEEF3", end_color="DAEEF3", fill_type="solid")
        worst_fill  = PatternFill(start_color="FCE4EC", end_color="FCE4EC", fill_type="solid")
        # Subtle tint for prop rows so they're visually distinct from game lines
        prop_row_fill  = PatternFill(start_color="FFF8E7", end_color="FFF8E7", fill_type="solid")
        prop_row_alt   = PatternFill(start_color="FFF0CC", end_color="FFF0CC", fill_type="solid")

        # Type badge fills
        game_type_fill = PatternFill(start_color="1F4E79", end_color="1F4E79", fill_type="solid")
        prop_type_fills = {
            "pts":        PatternFill(start_color="E65100", end_color="E65100", fill_type="solid"),
            "reb":        PatternFill(start_color="1B5E20", end_color="1B5E20", fill_type="solid"),
            "ast":        PatternFill(start_color="4A148C", end_color="4A148C", fill_type="solid"),
            "strikeouts": PatternFill(start_color="880E4F", end_color="880E4F", fill_type="solid"),
            "total_bases":PatternFill(start_color="BF360C", end_color="BF360C", fill_type="solid"),
        }
        prop_type_default = PatternFill(start_color="37474F", end_color="37474F", fill_type="solid")

        info_count = len(info_cols)
        EV_COL     = 8   # 1-based column index of "EV %"

        for i, edge in enumerate(_cmp_all):
            row   = i + 2
            point_display = _fmt_pt(edge["point"], edge["market"])
            fair_dec = 1 / (edge["fair_prob"] / 100) if edge["fair_prob"] > 0 else 0
            fair_am  = format_american(fair_dec) if fair_dec > 1 else ""
            is_prop  = "prop_side" in edge

            if is_prop:
                market_disp = edge.get("market_disp", edge["market"])
                type_label  = f"{market_disp} O/U"
                row_fill    = prop_row_fill if i % 2 == 0 else prop_row_alt
            else:
                market_disp = edge["market"].upper()
                type_label  = "GAME LINE"
                row_fill    = None if i % 2 == 0 else alt_fill

            commence_date = ""
            c_raw = edge.get("commence", "")
            if c_raw:
                try:
                    ct_et = datetime.fromisoformat(
                        c_raw.replace("Z", "+00:00")).astimezone(EASTERN)
                    commence_date = ct_et.strftime("%Y-%m-%d")
                except Exception:
                    commence_date = ""

            info_values = [
                i + 1, type_label, edge["sport"], edge["game"],
                market_disp, edge["outcome"], point_display,
                edge["ev_pct"], fair_am, commence_date,
            ]

            for col_idx, val in enumerate(info_values, 1):
                cell           = ws_cmp.cell(row=row, column=col_idx, value=val)
                cell.border    = thin_border
                cell.alignment = Alignment(horizontal="center")
                if row_fill:
                    cell.fill  = row_fill

            # Type badge
            type_cell      = ws_cmp.cell(row=row, column=2)
            if is_prop:
                mkt_key        = edge.get("market", "")
                type_cell.fill = prop_type_fills.get(mkt_key, prop_type_default)
            else:
                type_cell.fill = game_type_fill
            type_cell.font      = Font(bold=True, color="FFFFFF", size=9)
            type_cell.alignment = Alignment(horizontal="center")

            # EV% colouring
            ev_cell = ws_cmp.cell(row=row, column=EV_COL)
            if edge["ev_pct"] >= 3:
                ev_cell.font = Font(bold=True, color="006100")
            elif edge["ev_pct"] >= 1:
                ev_cell.font = Font(bold=True, color="9C5700")

            # Book odds columns
            book_odds   = edge.get("all_book_odds", {})
            odds_values = [book_odds.get(bk) for bk in book_order]

            valid_odds = [(j, am) for j, am in enumerate(odds_values) if am is not None]
            if valid_odds:
                best_idx  = max(valid_odds, key=lambda x: american_to_decimal(x[1]))[0]
                worst_idx = min(valid_odds, key=lambda x: american_to_decimal(x[1]))[0]
            else:
                best_idx = worst_idx = -1

            for j, am in enumerate(odds_values):
                col_idx        = info_count + j + 1
                cell           = ws_cmp.cell(row=row, column=col_idx)
                cell.border    = thin_border
                cell.alignment = Alignment(horizontal="center")
                if row_fill:
                    cell.fill  = row_fill

                if am is not None:
                    cell.value = f"+{am}" if am > 0 else str(am)
                    if j == best_idx:
                        cell.fill = best_fill
                        cell.font = Font(bold=True, color="006100")
                    elif j == worst_idx and best_idx != worst_idx:
                        cell.fill = worst_fill
                        cell.font = Font(color="9C0006")
                    elif book_order[j] in ("DraftKings", "FanDuel"):
                        cell.fill = target_fill
                        cell.font = Font(bold=True)
                else:
                    cell.value = "—"
                    cell.font  = Font(color="BBBBBB")

            # Best Book (the book this edge was priced at)
            bb_cell           = ws_cmp.cell(
                row=row, column=info_count + len(book_order) + 1,
                value=edge.get("book", ""))
            bb_cell.border    = thin_border
            bb_cell.alignment = Alignment(horizontal="center")
            bb_cell.font      = Font(bold=True)
            if row_fill:
                bb_cell.fill  = row_fill

        ws_cmp.freeze_panes = "A2"
        ws_cmp.auto_filter.ref = (
            f"A1:{get_column_letter(len(cmp_columns))}{len(_cmp_all) + 1}")

    # ── Promos / Profit Boost sheet ─────────────────────────────────
    if promos and edges:
        # For EVERY edge (not just +EV — a boost can turn a neutral line
        # into a +EV play), find matching promos and calculate boosted odds.
        # We also consider edges that didn't pass MIN_EV — those are in
        # `edges` only if they were already +EV. To catch boosts that CREATE
        # value, we re-scan all events. For now, boost the edges we have.

        ws_promo = wb.create_sheet("Promos")

        promo_header_fill = PatternFill(start_color="7B2D8E",
                                        end_color="7B2D8E", fill_type="solid")
        boost_fill = PatternFill(start_color="E8D5F5", end_color="E8D5F5",
                                 fill_type="solid")  # light purple

        promo_cols = [
            ("Rank", 6), ("Promo", 38), ("Sport", 10), ("Game", 32),
            ("Game Time", 16), ("Book", 13), ("Market", 10), ("Pick", 22),
            ("Line", 8), ("Original Odds", 13), ("Boosted Odds", 13),
            ("Original EV %", 12), ("Boosted EV %", 12), ("EV Gain", 9),
            ("Fair Prob %", 10), ("Kelly %", 9), ("Bet Amount", 12),
            ("Max Bet", 10), ("Potential Profit", 14), ("📌 Pinnacle", 10),
        ]

        for col_idx, (name, width) in enumerate(promo_cols, 1):
            cell           = ws_promo.cell(row=1, column=col_idx, value=name)
            cell.font      = Font(bold=True, size=11, color="FFFFFF")
            cell.fill      = promo_header_fill
            cell.alignment = Alignment(horizontal="center", wrap_text=True)
            cell.border    = thin_border
            ws_promo.column_dimensions[get_column_letter(col_idx)].width = width

        promo_rows = []
        # Use broader promo_pool (all DK/FD outcomes) so the Promos sheet
        # can include picks that only become +EV once the boost is applied.
        # player_prop_edges has no HR (find_player_prop_edges keeps HR out
        # of the returned list); hr_edges carries the ungated HR pool so a
        # 30% HR boost can still lift sub-MIN_EV picks. _promo_matches
        # gates HR edges to HR-targeted promos only.
        _all_for_promos = (list(promo_pool or edges)
                           + list(player_prop_edges or [])
                           + list(hr_edges or []))
        for promo in promos:
            if promo.get("promo_type") == "bonus_bet":
                continue   # handled in Play These PROMO PICKS section
            for edge in _all_for_promos:
                if not _promo_matches(promo, edge):
                    continue

                orig_dec  = edge["odds_decimal"]
                boost_dec = apply_profit_boost(orig_dec,
                                               promo.get("boost_pct") or 0)
                fair_prob = edge["fair_prob"] / 100

                boosted_ev    = (calc_ev(fair_prob, boost_dec)
                                 + _draw_refund_ev_pct(promo, edge))
                boosted_kelly = _promo_kelly(promo, edge, boost_dec, fair_prob)

                # Only show if boosted EV is positive
                if boosted_ev < 0.5:
                    continue

                bankroll = (dk_bankroll if edge["book"] == "DraftKings"
                            else fd_bankroll)
                bet, _ = promo_boost_stake(bankroll, boosted_kelly, boost_dec,
                                           edge["book"], promo.get("max_bet"))

                profit = bet * (boost_dec - 1)

                promo_rows.append({
                    "promo_label":    promo["label"],
                    "edge":           edge,
                    "boost_dec":      boost_dec,
                    "boosted_ev":     round(boosted_ev, 2),
                    "boosted_kelly":  round(boosted_kelly * 100, 2),
                    "ev_gain":        round(boosted_ev - edge["ev_pct"], 2),
                    "bet":            bet,
                    "max_bet":        promo.get("max_bet"),
                    "profit":         profit,
                })

        # Sort by boosted EV descending
        promo_rows.sort(key=lambda x: x["boosted_ev"], reverse=True)

        total_boosted_risk   = 0
        total_boosted_profit = 0
        for i, pr in enumerate(promo_rows):
            row  = i + 2
            edge = pr["edge"]
            total_boosted_risk   += pr["bet"]
            total_boosted_profit += pr["profit"]

            try:
                ct        = datetime.fromisoformat(
                    edge["commence"].replace("Z", "+00:00"))
                game_time = ct.astimezone(EASTERN).strftime("%m/%d %I:%M %p %Z")
            except (ValueError, AttributeError):
                game_time = edge["commence"]

            point_display = _fmt_pt(edge["point"], edge["market"])

            values = [
                i + 1,
                pr["promo_label"],
                edge["sport"],
                edge["game"],
                game_time,
                edge["book"],
                edge["market"].upper(),
                edge["outcome"],
                point_display,
                _fmt_am(edge["odds_american"]),
                format_american(pr["boost_dec"]),
                edge["ev_pct"],
                pr["boosted_ev"],
                pr["ev_gain"],
                edge["fair_prob"],
                pr["boosted_kelly"],
                pr["bet"],
                pr["max_bet"] if pr["max_bet"] else "",
                pr["profit"],
                "✓" if edge.get("pinnacle_anchored") else "",
            ]

            for col_idx, val in enumerate(values, 1):
                cell           = ws_promo.cell(row=row, column=col_idx, value=val)
                cell.border    = thin_border
                cell.alignment = Alignment(horizontal="center")
                if i % 2 == 1:
                    cell.fill  = alt_fill

            # Highlight boost columns
            ws_promo.cell(row=row, column=11).fill = boost_fill   # Boosted Odds
            ws_promo.cell(row=row, column=11).font = Font(bold=True)
            ws_promo.cell(row=row, column=13).fill = boost_fill   # Boosted EV
            ws_promo.cell(row=row, column=13).font = Font(bold=True, color="7B2D8E")
            ws_promo.cell(row=row, column=14).fill = boost_fill   # EV Gain
            ws_promo.cell(row=row, column=14).font = Font(bold=True, color="006100")
            ws_promo.cell(row=row, column=17).number_format = '$#,##0.00'
            ws_promo.cell(row=row, column=19).number_format = '$#,##0.00'

        # Totals row
        if promo_rows:
            tot_row = len(promo_rows) + 3
            ws_promo.cell(row=tot_row, column=16,
                          value="TOTALS:").font = Font(bold=True, size=12)
            ws_promo.cell(row=tot_row, column=17,
                          value=total_boosted_risk).number_format = '$#,##0.00'
            ws_promo.cell(row=tot_row, column=17).font = Font(bold=True, size=12)
            ws_promo.cell(row=tot_row, column=19,
                          value=total_boosted_profit).number_format = '$#,##0.00'
            ws_promo.cell(row=tot_row, column=19).font = Font(
                bold=True, size=12, color="006100")

        # Active promos summary at bottom
        sum_row = (len(promo_rows) + 5) if promo_rows else 3
        ws_promo.cell(row=sum_row, column=1,
                      value="ACTIVE PROMOS:").font = Font(bold=True, size=12)
        for j, promo in enumerate(promos):
            ws_promo.cell(row=sum_row + j + 1, column=1,
                          value=promo["label"]).font = Font(size=11)

        ws_promo.freeze_panes = "A2"
        if promo_rows:
            ws_promo.auto_filter.ref = (
                f"A1:{get_column_letter(len(promo_cols))}{len(promo_rows) + 1}")

    # ── HR Props sheet — anytime home run picks with boost ──────────
    if hr_edges and promos:
        hr_boost_rows = _build_hr_boost_rows(
            hr_edges, promos, dk_bankroll, fd_bankroll)

        if hr_boost_rows:
            ws_hr = wb.create_sheet("HR Props")

            hr_header_fill = PatternFill(
                start_color="8B0000", end_color="8B0000", fill_type="solid")
            boost_fill_hr = PatternFill(
                start_color="FFE0E0", end_color="FFE0E0", fill_type="solid")

            hr_cols = [
                ("Rank", 6), ("Player", 22), ("Game", 28), ("Game Time", 16),
                ("Book", 13), ("Original Odds", 13), ("Boosted Odds", 13),
                ("Fair Prob %", 10), ("Original EV %", 12), ("Boosted EV %", 12),
                ("EV Gain", 9), ("Kelly %", 9), ("Bet Amount", 12),
                ("Max Bet", 10), ("Potential Profit", 14),
                ("# Books", 8), ("Promo", 36),
            ]

            for col_idx, (name, width) in enumerate(hr_cols, 1):
                cell           = ws_hr.cell(row=1, column=col_idx, value=name)
                cell.font      = Font(bold=True, size=11, color="FFFFFF")
                cell.fill      = hr_header_fill
                cell.alignment = Alignment(horizontal="center", wrap_text=True)
                cell.border    = thin_border
                ws_hr.column_dimensions[get_column_letter(col_idx)].width = width

            total_hr_risk   = 0
            total_hr_profit = 0
            for i, pr in enumerate(hr_boost_rows):
                row  = i + 2
                edge = pr["edge"]
                total_hr_risk   += pr["bet"]
                total_hr_profit += pr["profit"]

                try:
                    ct        = datetime.fromisoformat(
                        edge["commence"].replace("Z", "+00:00"))
                    game_time = ct.astimezone(EASTERN).strftime("%m/%d %I:%M %p %Z")
                except (ValueError, AttributeError):
                    game_time = edge["commence"]

                values = [
                    i + 1,
                    edge["outcome"],          # player name
                    edge["game"],
                    game_time,
                    edge["book"],
                    _fmt_am(edge["odds_american"]),
                    format_american(pr["boost_dec"]),
                    edge["fair_prob"],
                    edge["ev_pct"],
                    pr["boosted_ev"],
                    pr["ev_gain"],
                    pr["boosted_kelly"],
                    pr["bet"],
                    pr["max_bet"] if pr["max_bet"] else "",
                    pr["profit"],
                    edge["num_other_books"],
                    pr["promo_label"],
                ]

                for col_idx, val in enumerate(values, 1):
                    cell           = ws_hr.cell(row=row, column=col_idx, value=val)
                    cell.border    = thin_border
                    cell.alignment = Alignment(horizontal="center")
                    if i % 2 == 1:
                        cell.fill  = alt_fill

                # Highlight boost columns
                ws_hr.cell(row=row, column=7).fill = boost_fill_hr   # Boosted Odds
                ws_hr.cell(row=row, column=7).font = Font(bold=True, color="8B0000")
                ws_hr.cell(row=row, column=10).fill = boost_fill_hr  # Boosted EV
                ws_hr.cell(row=row, column=10).font = Font(bold=True, color="006100")
                ws_hr.cell(row=row, column=11).fill = boost_fill_hr  # EV Gain
                ws_hr.cell(row=row, column=11).font = Font(bold=True, color="006100")
                ws_hr.cell(row=row, column=13).number_format = '$#,##0.00'
                ws_hr.cell(row=row, column=15).number_format = '$#,##0.00'

                # Color original EV based on sign
                orig_ev_cell = ws_hr.cell(row=row, column=9)
                if edge["ev_pct"] >= 0.5:
                    orig_ev_cell.font = Font(bold=True, color="006100")
                elif edge["ev_pct"] >= 0:
                    orig_ev_cell.font = Font(color="9C5700")
                else:
                    orig_ev_cell.font = Font(color="9C0006")

            # Totals row
            if hr_boost_rows:
                tot_row = len(hr_boost_rows) + 3
                ws_hr.cell(row=tot_row, column=12,
                           value="TOTALS:").font = Font(bold=True, size=12)
                ws_hr.cell(row=tot_row, column=13,
                           value=total_hr_risk).number_format = '$#,##0.00'
                ws_hr.cell(row=tot_row, column=13).font = Font(
                    bold=True, size=12)
                ws_hr.cell(row=tot_row, column=15,
                           value=total_hr_profit).number_format = '$#,##0.00'
                ws_hr.cell(row=tot_row, column=15).font = Font(
                    bold=True, size=12, color="006100")

            # Note about boost count
            note_row = (len(hr_boost_rows) + 5) if hr_boost_rows else 3
            ws_hr.cell(row=note_row, column=1,
                       value="Anytime HR Profit Boost Picks").font = Font(
                           bold=True, size=13, color="8B0000")
            ws_hr.cell(row=note_row + 1, column=1,
                       value=("Fair probability estimated from median consensus "
                              "of available books (no Pinnacle for props).")).font = Font(
                                  size=10, color="666666", italic=True)
            ws_hr.cell(row=note_row + 2, column=1,
                       value=("Picks ranked by boosted EV — use your best "
                              "boosts on the top rows.")).font = Font(
                                  size=10, color="666666", italic=True)

            ws_hr.freeze_panes = "A2"
            if hr_boost_rows:
                ws_hr.auto_filter.ref = (
                    f"A1:{get_column_letter(len(hr_cols))}"
                    f"{len(hr_boost_rows) + 1}")

    # ── Parlay Promos sheet ─────────────────────────────────────────
    if promos and edges:
        parlay_rows = _build_parlay_rows(
            edges, promos, dk_bankroll, fd_bankroll,
            extra_edges=(list(golf_edges or [])
                         + list(player_prop_edges or [])),
            parlay_pool=parlay_pool, promo_pool=promo_pool)
        if parlay_rows:
            ws_par = wb.create_sheet("Parlay Promos")

            par_header_fill = PatternFill(start_color="1F4E79",
                                          end_color="1F4E79", fill_type="solid")
            leg_fill        = PatternFill(start_color="DDEEFF",
                                          end_color="DDEEFF", fill_type="solid")
            leg_fill_alt    = PatternFill(start_color="EEF5FF",
                                          end_color="EEF5FF", fill_type="solid")
            boost_fill_par  = PatternFill(start_color="D5E8D4",
                                          end_color="D5E8D4", fill_type="solid")

            current_row = 1
            for pr in parlay_rows:
                # ── Parlay header row ─────────────────────────────────
                title_val = (f"{pr['promo_label']}   │   "
                             f"Parlay: {pr['parlay_american']}  →  "
                             f"Boosted: {pr['boost_american']}   │   "
                             f"Fair Prob: {pr['parlay_fair_prob']}%   │   "
                             f"Unboosted EV: {pr['unboosted_ev']}%   │   "
                             f"Boosted EV: {pr['boosted_ev']}%   │   "
                             f"Bet: ${pr['bet']:.2f}   │   "
                             f"Profit: ${pr['profit']:.2f}")
                cell = ws_par.cell(row=current_row, column=1, value=title_val)
                cell.font      = Font(bold=True, size=12, color="FFFFFF")
                cell.fill      = par_header_fill
                cell.alignment = Alignment(horizontal="left", wrap_text=False)
                cell.border    = thin_border
                ws_par.merge_cells(
                    start_row=current_row, start_column=1,
                    end_row=current_row, end_column=10)
                ws_par.row_dimensions[current_row].height = 22
                current_row += 1

                # ── Leg column headers ────────────────────────────────
                leg_hdrs = ["Leg", "Game", "Book", "Market", "Pick",
                            "Line", "Odds", "Fair Prob %", "Indiv EV %", "Pinnacle"]
                leg_widths = [5, 32, 13, 10, 22, 8, 10, 11, 10, 10]
                for c, (h, w) in enumerate(zip(leg_hdrs, leg_widths), 1):
                    cell           = ws_par.cell(row=current_row, column=c, value=h)
                    cell.font      = Font(bold=True, size=10, color="FFFFFF")
                    cell.fill      = par_header_fill
                    cell.alignment = Alignment(horizontal="center")
                    cell.border    = thin_border
                    ws_par.column_dimensions[get_column_letter(c)].width = max(
                        ws_par.column_dimensions[get_column_letter(c)].width or 0, w)
                current_row += 1

                # ── Leg detail rows ───────────────────────────────────
                for leg_num, leg in enumerate(pr["legs"], 1):
                    point_disp = _fmt_pt(leg["point"], leg["market"])
                    leg_values = [
                        leg_num,
                        leg["game"],
                        leg["book"],
                        leg["market"].upper(),
                        leg["outcome"],
                        point_disp,
                        _fmt_am(leg["odds_american"]),
                        leg["fair_prob"],
                        leg["ev_pct"],
                        "✓" if leg.get("pinnacle_anchored") else "",
                    ]
                    this_leg_fill = leg_fill if leg_num % 2 == 1 else leg_fill_alt
                    for c, val in enumerate(leg_values, 1):
                        cell           = ws_par.cell(row=current_row, column=c, value=val)
                        cell.fill      = this_leg_fill
                        cell.border    = thin_border
                        cell.alignment = Alignment(horizontal="center")

                    # Highlight EV cell
                    ev_cell = ws_par.cell(row=current_row, column=9)
                    if leg["ev_pct"] >= 1:
                        ev_cell.font = Font(bold=True, color="006100")
                    elif leg["ev_pct"] >= 0:
                        ev_cell.font = Font(color="9C5700")
                    else:
                        ev_cell.font = Font(color="9C0006")
                    current_row += 1

                # ── Summary row ───────────────────────────────────────
                summary_vals = [
                    f"{pr['num_legs']}-Leg Total",
                    "", "", "", "", "",
                    pr["parlay_american"],
                    f"{pr['parlay_fair_prob']}%",
                    f"Boosted: {pr['boosted_ev']}%",
                    "",
                ]
                for c, val in enumerate(summary_vals, 1):
                    cell           = ws_par.cell(row=current_row, column=c, value=val)
                    cell.fill      = boost_fill_par
                    cell.border    = thin_border
                    cell.alignment = Alignment(horizontal="center")
                ws_par.cell(row=current_row, column=1).font = Font(bold=True, size=11)
                ws_par.cell(row=current_row, column=7).font = Font(bold=True)
                ws_par.cell(row=current_row, column=9).font = Font(
                    bold=True, color="006100")
                current_row += 3   # blank gap between parlays

            # Note
            ws_par.cell(row=current_row, column=1,
                        value="Parlay EV assumes legs are independent (different games).").font = Font(
                            size=9, italic=True, color="888888")
            ws_par.cell(row=current_row + 1, column=1,
                        value=("Optimal combo found by brute-force (≤4 legs / ≤20 candidates) "
                               "or greedy top-N otherwise.")).font = Font(
                                   size=9, italic=True, color="888888")

    # ── Golf Props sheet ────────────────────────────────────────────
    if golf_edges:
        ws_golf = wb.create_sheet("Golf Props")

        golf_header_fill = PatternFill(start_color="2E7D32",
                                       end_color="2E7D32", fill_type="solid")

        # Group by market type for readability
        golf_by_market: dict[str, list] = {}
        for e in golf_edges:
            golf_by_market.setdefault(e["market"], []).append(e)

        golf_cols = [
            ("Rank", 6), ("Player", 22), ("Tournament", 28), ("Book", 13),
            ("Market", 16), ("Odds", 10), ("Fair Prob %", 10),
            ("EV %", 9), ("Kelly %", 9), ("Bet Amount", 12),
            ("Potential Profit", 14), ("# Books", 8), ("Best Other Line", 14),
        ]
        for col_idx, (name, width) in enumerate(golf_cols, 1):
            cell           = ws_golf.cell(row=1, column=col_idx, value=name)
            cell.font      = Font(bold=True, size=11, color="FFFFFF")
            cell.fill      = golf_header_fill
            cell.alignment = Alignment(horizontal="center", wrap_text=True)
            cell.border    = thin_border
            ws_golf.column_dimensions[get_column_letter(col_idx)].width = width

        # Market display order
        market_order = ["golf_top5", "golf_top10", "golf_top20", "golf_win"]
        market_labels = {
            "golf_top5":  "Top 5 Finish",
            "golf_top10": "Top 10 Finish",
            "golf_top20": "Top 20 Finish",
            "golf_win":   "Tournament Winner",
        }
        sorted_golf = []
        for mk in market_order:
            # Within each market group sort by kelly_pct desc — highest
            # bet-size (most actionable) rises to top; raw EV% misleads
            # for extreme longshots where the min-bet floor dominates
            group = sorted(golf_by_market.get(mk, []),
                           key=lambda e: e["kelly_pct"], reverse=True)
            sorted_golf.extend(group)
        for mk, group in golf_by_market.items():
            if mk not in market_order:
                sorted_golf.extend(
                    sorted(group, key=lambda e: e["kelly_pct"], reverse=True))

        total_golf_risk   = 0
        total_golf_profit = 0
        for i, edge in enumerate(sorted_golf):
            row      = i + 2
            bankroll = dk_bankroll if edge["book"] == "DraftKings" else fd_bankroll
            bet_amt  = clamp_bet(bankroll * edge["kelly_pct"] / 100, edge["book"])
            profit_v = bet_amt * (edge["odds_decimal"] - 1)
            total_golf_risk   += bet_amt
            total_golf_profit += profit_v

            try:
                ct = datetime.fromisoformat(
                    edge["commence"].replace("Z", "+00:00"))
                commence_str = ct.astimezone(EASTERN).strftime("%m/%d")
            except (ValueError, AttributeError):
                commence_str = ""

            values = [
                i + 1,
                edge["outcome"],
                f"{edge['game']} ({commence_str})" if commence_str else edge["game"],
                edge["book"],
                market_labels.get(edge["market"], edge["market"]),
                _fmt_am(edge["odds_american"]),
                edge["fair_prob"],
                edge["ev_pct"],
                edge["kelly_pct"],
                bet_amt,
                profit_v,
                edge["num_other_books"],
                edge["best_other_odds"],
            ]
            for col_idx, val in enumerate(values, 1):
                cell           = ws_golf.cell(row=row, column=col_idx, value=val)
                cell.border    = thin_border
                cell.alignment = Alignment(horizontal="center")
                if i % 2 == 1:
                    cell.fill  = alt_fill

            ws_golf.cell(row=row, column=10).number_format = '$#,##0.00'
            ws_golf.cell(row=row, column=11).number_format = '$#,##0.00'

            ev_cell = ws_golf.cell(row=row, column=8)
            if edge["ev_pct"] >= 3:
                ev_cell.font = Font(bold=True, color="006100")
            elif edge["ev_pct"] >= 1:
                ev_cell.font = Font(bold=True, color="9C5700")

        # Totals row
        tot_row = len(sorted_golf) + 3
        ws_golf.cell(row=tot_row, column=9, value="TOTALS:").font = Font(bold=True, size=12)
        ws_golf.cell(row=tot_row, column=10, value=total_golf_risk).number_format = '$#,##0.00'
        ws_golf.cell(row=tot_row, column=10).font = Font(bold=True, size=12)
        ws_golf.cell(row=tot_row, column=11, value=total_golf_profit).number_format = '$#,##0.00'
        ws_golf.cell(row=tot_row, column=11).font = Font(bold=True, size=12, color="006100")

        ws_golf.freeze_panes = "A2"
        if sorted_golf:
            ws_golf.auto_filter.ref = (
                f"A1:{get_column_letter(len(golf_cols))}{len(sorted_golf) + 1}")

    elif hr_edges:
        # No promos but still show raw HR edges for reference
        hr_ev_positive = [e for e in hr_edges if e["ev_pct"] >= 0.5]
        if hr_ev_positive:
            ws_hr = wb.create_sheet("HR Props (No Boost)")

            hr_header_fill = PatternFill(
                start_color="8B0000", end_color="8B0000", fill_type="solid")

            hr_cols_raw = [
                ("Rank", 6), ("Player", 22), ("Game", 28), ("Game Time", 16),
                ("Book", 13), ("Odds", 12), ("Fair Prob %", 10),
                ("EV %", 9), ("Kelly %", 9), ("# Books", 8),
                ("Best Other Line", 14),
            ]

            for col_idx, (name, width) in enumerate(hr_cols_raw, 1):
                cell           = ws_hr.cell(row=1, column=col_idx, value=name)
                cell.font      = Font(bold=True, size=11, color="FFFFFF")
                cell.fill      = hr_header_fill
                cell.alignment = Alignment(horizontal="center", wrap_text=True)
                cell.border    = thin_border
                ws_hr.column_dimensions[get_column_letter(col_idx)].width = width

            for i, edge in enumerate(hr_ev_positive):
                row = i + 2
                try:
                    ct        = datetime.fromisoformat(
                        edge["commence"].replace("Z", "+00:00"))
                    game_time = ct.astimezone(EASTERN).strftime("%m/%d %I:%M %p %Z")
                except (ValueError, AttributeError):
                    game_time = edge["commence"]

                values = [
                    i + 1, edge["outcome"], edge["game"], game_time,
                    edge["book"], _fmt_am(edge["odds_american"]), edge["fair_prob"],
                    edge["ev_pct"], edge["kelly_pct"],
                    edge["num_other_books"], edge["best_other_odds"],
                ]

                for col_idx, val in enumerate(values, 1):
                    cell           = ws_hr.cell(row=row, column=col_idx, value=val)
                    cell.border    = thin_border
                    cell.alignment = Alignment(horizontal="center")
                    if i % 2 == 1:
                        cell.fill  = alt_fill

                ev_cell = ws_hr.cell(row=row, column=8)
                if edge["ev_pct"] >= 3:
                    ev_cell.font = Font(bold=True, color="006100")
                elif edge["ev_pct"] >= 1:
                    ev_cell.font = Font(bold=True, color="9C5700")

            ws_hr.freeze_panes = "A2"

    # ── Player Props sheet (Strikeouts / Total Bases / Pts / Reb / Ast) ─
    if player_prop_edges:
        ws_pp = wb.create_sheet("Player Props")

        # Sport-keyed header colours
        _pp_sport_fill = {
            "NBA": PatternFill(start_color="1F4E79", end_color="1F4E79",
                               fill_type="solid"),
            "MLB": PatternFill(start_color="8B0000", end_color="8B0000",
                               fill_type="solid"),
        }
        _pp_default_fill = PatternFill(start_color="2F5496", end_color="2F5496",
                                       fill_type="solid")

        pp_cols = [
            ("Rank", 6), ("Sport", 8), ("Player", 22), ("Game", 28),
            ("Game Time", 16), ("Market", 13), ("Side", 6), ("Line", 7),
            ("Book", 13), ("Odds", 10), ("Fair Prob %", 11),
            ("EV %", 9), ("Kelly %", 9), ("Bet Amount", 13),
            ("Pot. Profit", 13), ("# Books", 8), ("Best Other Line", 15),
        ]

        # Write headers
        for col_idx, (name, width) in enumerate(pp_cols, 1):
            cell           = ws_pp.cell(row=1, column=col_idx, value=name)
            cell.font      = header_font
            cell.fill      = header_fill
            cell.alignment = Alignment(horizontal="center", wrap_text=True)
            cell.border    = thin_border
            ws_pp.column_dimensions[get_column_letter(col_idx)].width = width

        total_pp_risk   = 0
        total_pp_profit = 0
        for i, edge in enumerate(player_prop_edges):
            row      = i + 2
            bankroll = (dk_bankroll if edge["book"] == "DraftKings"
                        else fd_bankroll)
            bet_amt  = clamp_bet(bankroll * edge["kelly_pct"] / 100, edge["book"])
            profit_v = bet_amt * (edge["odds_decimal"] - 1)
            total_pp_risk   += bet_amt
            total_pp_profit += profit_v

            try:
                ct = datetime.fromisoformat(
                    edge["commence"].replace("Z", "+00:00"))
                game_time = ct.astimezone(EASTERN).strftime("%m/%d %I:%M %p %Z")
            except (ValueError, AttributeError):
                game_time = edge["commence"]

            line_disp = _fmt_pt(edge["point"], edge["market"])
            side_disp = "Over" if edge.get("prop_side") == "over" else "Under"

            values = [
                i + 1,
                edge["sport"],
                edge.get("player", edge["outcome"]),
                edge["game"],
                game_time,
                edge.get("market_disp", edge["market"]),
                side_disp,
                line_disp,
                edge["book"],
                _fmt_am(edge["odds_american"]),
                edge["fair_prob"],
                edge["ev_pct"],
                edge["kelly_pct"],
                bet_amt,
                profit_v,
                edge["num_other_books"],
                edge["best_other_odds"],
            ]

            sport_fill_row = _pp_sport_fill.get(edge["sport"], _pp_default_fill)
            if edge["ev_pct"] >= 5:
                row_fill = tier_strong
            elif edge["ev_pct"] >= 3:
                row_fill = tier_solid
            elif edge["ev_pct"] >= 1:
                row_fill = tier_caution
            else:
                row_fill = alt_fill if i % 2 == 1 else None

            for col_idx, val in enumerate(values, 1):
                cell           = ws_pp.cell(row=row, column=col_idx, value=val)
                cell.border    = thin_border
                cell.alignment = Alignment(horizontal="center")
                if row_fill:
                    cell.fill  = row_fill

            # Sport column — tiny coloured badge
            sp_cell = ws_pp.cell(row=row, column=2)
            sp_cell.fill = sport_fill_row
            sp_cell.font = Font(bold=True, color="FFFFFF", size=10)

            # EV% coloring
            ev_cell = ws_pp.cell(row=row, column=12)
            if edge["ev_pct"] >= 3:
                ev_cell.font = Font(bold=True, color="006100")
            elif edge["ev_pct"] >= 1:
                ev_cell.font = Font(bold=True, color="9C5700")

            # Side column — color Over/Under
            sd_cell = ws_pp.cell(row=row, column=7)
            if edge.get("prop_side") == "over":
                sd_cell.font = Font(bold=True, color="006100")
            else:
                sd_cell.font = Font(bold=True, color="9C0006")

            ws_pp.cell(row=row, column=14).number_format = '$#,##0.00'
            ws_pp.cell(row=row, column=15).number_format = '$#,##0.00'

        # Totals row
        tot_row = len(player_prop_edges) + 3
        ws_pp.cell(row=tot_row, column=13,
                   value="TOTALS:").font = Font(bold=True, size=12)
        ws_pp.cell(row=tot_row, column=14,
                   value=total_pp_risk).number_format = '$#,##0.00'
        ws_pp.cell(row=tot_row, column=14).font = Font(bold=True, size=12)
        ws_pp.cell(row=tot_row, column=15,
                   value=total_pp_profit).number_format = '$#,##0.00'
        ws_pp.cell(row=tot_row, column=15).font = Font(bold=True, size=12,
                                                       color="006100")
        # Notes
        note_row = tot_row + 2
        ws_pp.cell(row=note_row, column=1,
                   value="Player Props — Over/Under").font = Font(
                       bold=True, size=13)
        ws_pp.cell(row=note_row + 1, column=1,
                   value=("Fair probability via vig-removed median consensus "
                          "(no Pinnacle for player props).")).font = Font(
                              size=10, color="666666", italic=True)

        ws_pp.freeze_panes = "A2"
        if player_prop_edges:
            ws_pp.auto_filter.ref = (
                f"A1:{get_column_letter(len(pp_cols))}"
                f"{len(player_prop_edges) + 1}")

    # ── Persist play keys for next run's seen-pick detection ─────────
    if play_edges:
        _save_play_keys(play_edges, _script_dir)

    # ── Sidecar for closing-line capture ──────────────────────────────
    # Tracks every +EV pick (Play These + All Plays + props) so the
    # closing_lines worker can fetch the close near tip-off and compute
    # CLV. Output dir is OUTPUT_DIR — the same dir holding the xlsx.
    try:
        write_pending_closes_sidecar(
            edges, player_prop_edges, os.path.dirname(output_path),
            play_edges=play_edges,
        )
    except Exception as e:
        # Never let sidecar failure block the xlsx save; just log it.
        print(f"  [closing-lines] sidecar write failed: {e}")

    # ── Sidecar for sportsbook deep links ─────────────────────────────
    # Restores DK/FD event IDs that the xlsx round-trip drops, so the
    # dashboard can render "click pick → sportsbook event page" links.
    try:
        write_book_event_ids_sidecar(
            edges, player_prop_edges, os.path.dirname(output_path),
        )
    except Exception as e:
        print(f"  [book-links] sidecar write failed: {e}")

    wb.save(output_path)
    print(f"\nSaved to: {output_path}")


def _write_book_sheet(ws, edges, bankroll, columns, header_font, header_fill,
                      thin_border, tier_strong, tier_solid, tier_caution, tier_weak):
    for col_idx, (name, width) in enumerate(columns, 1):
        cell           = ws.cell(row=1, column=col_idx, value=name)
        cell.font      = header_font
        cell.fill      = header_fill
        cell.alignment = Alignment(horizontal="center", wrap_text=True)
        cell.border    = thin_border
        ws.column_dimensions[get_column_letter(col_idx)].width = width

    for i, edge in enumerate(edges):
        row        = i + 2
        bet_amount = clamp_bet(bankroll * edge["kelly_pct"] / 100, edge["book"])
        profit     = bet_amount * (edge["odds_decimal"] - 1)
        edge_pct   = edge["fair_prob"] - edge["implied_prob"]

        try:
            ct        = datetime.fromisoformat(edge["commence"].replace("Z", "+00:00"))
            game_time = ct.astimezone(EASTERN).strftime("%m/%d %I:%M %p %Z")
        except (ValueError, AttributeError):
            game_time = edge["commence"]

        point_display = _fmt_pt(edge["point"], edge["market"])

        if edge["ev_pct"] >= 5:
            row_fill = tier_strong;  ev_font = Font(bold=True, color="006100")
        elif edge["ev_pct"] >= 3:
            row_fill = tier_solid;   ev_font = Font(bold=True, color="006100")
        elif edge["ev_pct"] >= 1:
            row_fill = tier_caution; ev_font = Font(bold=True, color="9C5700")
        else:
            row_fill = tier_weak;    ev_font = Font(color="999999")

        values = [
            i + 1, edge["sport"], edge["game"], game_time,
            edge["book"], edge["market"].upper(), edge["outcome"], point_display,
            _fmt_am(edge["odds_american"]), edge["odds_decimal"],
            edge["fair_prob"], edge["implied_prob"],
            edge["ev_pct"], round(edge_pct, 1), edge["kelly_pct"],
            bet_amount, profit,
            edge["num_other_books"], edge["best_other_odds"],
            "✓" if edge.get("pinnacle_anchored") else "",
        ]

        for col_idx, val in enumerate(values, 1):
            cell           = ws.cell(row=row, column=col_idx, value=val)
            cell.border    = thin_border
            cell.alignment = Alignment(horizontal="center")
            cell.fill      = row_fill

        ws.cell(row=row, column=16).number_format = '$#,##0.00'
        ws.cell(row=row, column=17).number_format = '$#,##0.00'
        ws.cell(row=row, column=13).font          = ev_font

    ws.freeze_panes = "A2"
    if edges:
        ws.auto_filter.ref = (
            f"A1:{get_column_letter(len(columns))}{len(edges) + 1}")


# ──────────────────────────────────────────────────────────────────────
# PGA Golf Props
# ──────────────────────────────────────────────────────────────────────

# AN market keys for golf tournament props → internal market key + display name
AN_GOLF_MARKETS: dict[str, tuple[str, str]] = {
    "core_bet_type_3_top_5":   ("golf_top5",  "Top 5 Finish"),
    "core_bet_type_4_top_10":  ("golf_top10", "Top 10 Finish"),
    "core_bet_type_44_top_20": ("golf_top20", "Top 20 Finish"),
    "moneyline":               ("golf_win",   "Tournament Winner"),
    # matchup (H2H) skipped — pairs need special handling
}


def fetch_golf_props(session: requests.Session) -> list[dict]:
    """
    Fetch PGA Tour tournament finish props from Action Network.

    Returns a flat list of dicts, one per (player, market, tournament):
      {
        "player":       "Tommy Fleetwood",
        "tournament":   "Valero Texas Open",
        "commence":     "2026-04-02T12:00:00Z",
        "sport":        "PGA",
        "market":       "golf_top5",
        "market_disp":  "Top 5 Finish",
        "book_odds":    {68: 345, 69: 340, ...},   # book_id → american
      }

    When GOLF_DISABLED is set (2026-05-22 stale-line shutdown), returns
    an empty list without making the AN scoreboard request — both saves
    a scraper hit and guarantees no golf data ever enters downstream
    pools, even if find_golf_edges's own guard were bypassed.
    """
    if GOLF_DISABLED:
        return []
    try:
        resp = session.get(
            f"{ACTION_NETWORK_BASE}/scoreboard/pga",
            params={"bookIds": ",".join(str(b) for b in AN_BOOK_IDS)},
            timeout=30,
        )
        if resp.status_code != 200:
            print(f"  [Golf Props] PGA scoreboard returned {resp.status_code}")
            return []
    except Exception as e:
        print(f"  [Golf Props] Fetch error: {e}")
        return []

    data = resp.json()
    competitions = data.get("competitions", [])
    if not competitions:
        print("  [Golf Props] No PGA competitions found")
        return []

    results = []
    for comp in competitions:
        status = comp.get("status", "scheduled")
        if status in ("complete", "post", "final"):
            continue

        tournament = comp.get("meta", {}).get("title", "PGA Tour")
        commence   = comp.get("start_time", "")

        # Build player name map from competitors list
        comp_map: dict[int, str] = {}
        for c in comp.get("competitors", []):
            pid  = c.get("player_id")
            name = c.get("full_name") or c.get("player", {}).get("full_name")
            if pid and name:
                comp_map[pid] = name

        markets = comp.get("markets", {})   # {book_id_str: {event: {mkt_key: [entries]}}}

        # Aggregate: (player_id, mkt_key) → {book_id: odds}
        agg: dict[tuple, dict[int, int]] = {}
        for book_id_str, book_data in markets.items():
            try:
                bid = int(book_id_str)
            except ValueError:
                continue
            if bid not in AN_BOOK_IDS:
                continue

            for an_key in AN_GOLF_MARKETS:
                entries = book_data.get("event", {}).get(an_key, [])
                for entry in entries:
                    pid  = entry.get("player_id") or entry.get("competitor_id")
                    odds = entry.get("odds")
                    if pid and odds:
                        key = (pid, an_key)
                        agg.setdefault(key, {})[bid] = int(odds)

        # Emit one result per (player, market) pair
        for (pid, an_key), book_odds in agg.items():
            if len(book_odds) < 3:
                continue  # too few books for reliable consensus
            internal_key, display = AN_GOLF_MARKETS[an_key]
            player_name = comp_map.get(pid, f"Player_{pid}")
            results.append({
                "player":      player_name,
                "tournament":  tournament,
                "commence":    commence,
                "sport":       "PGA",
                "market":      internal_key,
                "market_disp": display,
                "book_odds":   book_odds,
            })

    print(f"  [Golf Props] {len(results)} player-market lines "
          f"across {len(competitions)} tournament(s)")
    return results


def find_golf_edges(
    golf_props: list[dict],
    promo_pool: list[dict] | None = None,
) -> list[dict]:
    """
    Find +EV golf props on DraftKings / FanDuel using median multi-book consensus.

    Key considerations vs. game markets:
      - State-specific books (FanDuel NY, BetRivers NY, etc.) must be
        canonicalized so they don't double-count the same line.
      - Require 4+ unique canonical books for a reliable fair line.
      - Outright winner odds for fringe players vary wildly across books;
        canonical dedup prevents phantom edges from state-variant pricing.

    If `promo_pool` is provided, every constructed edge (regardless of EV) is
    also appended there — mirrors the find_edges pattern so PGA-boost promos
    can rank candidates by *boosted* EV without the MIN_EV_PERCENT pre-filter
    discarding outcomes the boost would lift into +EV territory.

    GOLF_DISABLED short-circuit: if the module-level flag is set, the
    function returns [] immediately and never touches promo_pool — even
    if a caller (e.g. backtest replay, a test, or a stale code path)
    constructed golf_props by hand. See the GOLF_DISABLED constant for
    the stale-line history that motivated the kill-switch.
    """
    if GOLF_DISABLED:
        return []

    edges: list[dict] = []

    for prop in golf_props:
        raw_book_odds = prop["book_odds"]   # {book_id: american_odds}

        # ── Step 1: canonicalize books, keep best odds per canonical key ──
        # e.g. FanDuel NY (1006) → "fanduel", keep whichever has best odds
        canonical_best: dict[str, tuple[int, int]] = {}   # ck → (bid, am)
        for bid, am in raw_book_odds.items():
            ck = _book_key(bid)
            dec = american_to_decimal(am)
            if (ck not in canonical_best
                    or dec > american_to_decimal(canonical_best[ck][1])):
                canonical_best[ck] = (bid, am)

        # Need at least 4 distinct canonical books for golf consensus
        if len(canonical_best) < 4:
            continue

        # ── Step 2: split into target (DK/FD) vs. other canonical books ──
        target_entries: list[tuple] = []   # (display, dec, am, bid)
        other_entries:  list[tuple] = []

        for ck, (bid, am) in canonical_best.items():
            dec = american_to_decimal(am)
            if ck in TARGET_BOOKS:
                display = "DraftKings" if ck == "draftkings" else "FanDuel"
                target_entries.append((display, dec, am, bid))
            else:
                name = AN_BOOK_IDS.get(bid, ck.title())
                other_entries.append((name, dec, am, bid))

        if not target_entries or not other_entries:
            continue

        # ── Step 3: fair probability = median of all canonical books ──
        all_probs = sorted(
            decimal_to_implied_prob(american_to_decimal(canonical_best[ck][1]))
            for ck in canonical_best
        )
        mid = len(all_probs) // 2
        fair_prob = (
            (all_probs[mid - 1] + all_probs[mid]) / 2
            if len(all_probs) % 2 == 0
            else all_probs[mid]
        )
        fair_prob = max(0.001, min(0.85, fair_prob))

        best_other_dec = max(d for _, d, _, _ in other_entries)

        # ── Step 4: comparison display (canonical names, best odds each) ──
        _GOLF_DISPLAY = {
            "draftkings": "DraftKings", "fanduel": "FanDuel",
            "betmgm": "BetMGM", "caesars": "Caesars",
            "betrivers": "BetRivers", "espnbet": "ESPN Bet",
            "bet365": "Bet365",
        }
        all_book_display: dict[str, int] = {}
        for ck, (bid, am) in canonical_best.items():
            disp_name = _GOLF_DISPLAY.get(ck, ck.title())
            all_book_display[disp_name] = am

        # ── Step 5: EV check for each target book ────────────────────────
        # Dedup target by display name (already canonical, but guard anyway)
        best_target: dict[str, tuple] = {}
        for display, dec, am, bid in target_entries:
            if display not in best_target or dec > best_target[display][1]:
                best_target[display] = (display, dec, am, bid)

        for display, dec, am, bid in best_target.values():
            ev = calc_ev(fair_prob, dec)
            # For outright winner, skip if fair probability is below 0.2%
            # (round-number consensus on obscure 150+ player fields is unreliable).
            # This clamp applies to the promo pool too — a boost can't rescue
            # a 0.1%-prob long-shot's fair-price signal.
            if prop["market"] == "golf_win" and fair_prob < 0.002:
                continue
            # Stale-target-line guard: if the target book's implied prob is
            # well below the consensus median, the target line is almost
            # certainly stale/erroneous (see the McNealy +10500 vs ~+1500
            # incident). Skip — never present such a pick to the user.
            target_implied = 1.0 / dec
            if target_implied < MIN_TARGET_OUTLIER_PROB_RATIO * fair_prob:
                print(f"  [Golf] Outlier rejected: {prop['player']} "
                      f"{prop['market']} on {display} at "
                      f"{format_american(dec)} — consensus implies "
                      f"~{format_american(1.0 / fair_prob)} "
                      f"(ratio {target_implied / fair_prob:.2f}, likely stale)")
                continue
            kelly = kelly_bet(fair_prob, dec)
            edge_dict = {
                "sport":             prop["sport"],
                "game":              prop["tournament"],
                "commence":          prop["commence"],
                "book":              display,
                "market":            prop["market"],
                "outcome":           prop["player"],
                "point":             None,
                "odds_american":     am,
                "odds_decimal":      round(dec, 3),
                "fair_prob":         round(fair_prob * 100, 2),
                "implied_prob":      round(decimal_to_implied_prob(dec) * 100, 2),
                "ev_pct":            round(ev, 2),
                "kelly_pct":         round(kelly * 100, 2),
                "num_other_books":   len(other_entries),
                "best_other_odds":   format_american(best_other_dec),
                "pinnacle_anchored": False,
                "all_book_odds":     all_book_display,
                "market_disp":       prop["market_disp"],
            }
            if promo_pool is not None:
                promo_pool.append(edge_dict)
            if ev >= MIN_EV_PERCENT:
                edges.append(edge_dict)

    edges.sort(key=lambda x: x["ev_pct"], reverse=True)
    return edges


# ──────────────────────────────────────────────────────────────────────
# MLB Anytime HR Player Props
# ──────────────────────────────────────────────────────────────────────
#
# 2026-05-17: HR props moved from a dedicated Action Network pipeline
# (fetch_hr_props + find_hr_edges) to the Odds API batter_home_runs
# market. The AN scoreboard endpoint flaked near midnight ET, returning
# empty slates that made HR-targeted promos look broken; the Odds API
# path also offers Pinnacle-anchored devigged consensus when available,
# which is methodologically cleaner than AN's "median of all books incl
# the target" approach. find_player_prop_edges now filters HR to the
# Yes side, normalizes outcome/point, and routes the result into the
# hr_edges view derived from pp_promo_pool — see run_headless_fetch.


def _build_hr_boost_rows(hr_edges: list[dict], promos: list[dict],
                         dk_bankroll: float, fd_bankroll: float) -> list[dict]:
    """
    Apply HR-applicable profit boosts to HR prop edges and return
    ranked rows ready for the HR Props Excel tab.

    Each row includes original odds, boosted odds, and boosted EV.
    """
    # HR-applicable = explicit market="hr". Tightened 2026-05-26 to match
    # _promos_want_hr's fetch gate — _promo_matches already requires the
    # same explicit market for HR picks, so promos with market="all"
    # would render an empty HR Props tab anyway.
    hr_promos = []
    for promo in promos:
        if promo.get("promo_type") == "bonus_bet":
            continue
        if promo["market"] != "hr":
            continue
        hr_promos.append(promo)

    if not hr_promos:
        return []

    rows = []
    for promo in hr_promos:
        for edge in hr_edges:
            if promo["book"] != edge["book"]:
                continue

            orig_dec = edge["odds_decimal"]
            boost_dec = apply_profit_boost(orig_dec, promo["boost_pct"])
            fair_prob = edge["fair_prob"] / 100

            boosted_ev = calc_ev(fair_prob, boost_dec)
            if boosted_ev < 0.5:
                continue  # skip if even boosted is not worthwhile

            boosted_kelly = kelly_bet(fair_prob, boost_dec)
            bankroll = (dk_bankroll if edge["book"] == "DraftKings"
                        else fd_bankroll)
            bet, _ = promo_boost_stake(bankroll, boosted_kelly, boost_dec,
                                       edge["book"], promo.get("max_bet"))
            profit = bet * (boost_dec - 1)

            rows.append({
                "promo_label":    promo["label"],
                "edge":           edge,
                "boost_dec":      boost_dec,
                "boosted_ev":     round(boosted_ev, 2),
                "boosted_kelly":  round(boosted_kelly * 100, 2),
                "ev_gain":        round(boosted_ev - edge["ev_pct"], 2),
                "bet":            round(bet, 2),
                "max_bet":        promo.get("max_bet"),
                "profit":         round(profit, 2),
            })

    rows.sort(key=lambda x: x["boosted_ev"], reverse=True)
    return rows


# ──────────────────────────────────────────────────────────────────────
# Player Props — Strikeouts, Total Bases, NBA Pts / Reb / Ast  (O/U)
# ──────────────────────────────────────────────────────────────────────

# (substring_in_AN_key, internal_market_key, display_name, sport_code)
PLAYER_PROP_PATTERNS: list[tuple[str, str, str, str]] = [
    # MLB
    ("player_strikeouts",  "strikeouts",  "Strikeouts",  "mlb"),
    ("pitcher_strikeout",  "strikeouts",  "Strikeouts",  "mlb"),
    ("player_total_bases", "total_bases", "Total Bases", "mlb"),
    # NBA — AN v2 uses core_bet_type IDs, NOT "player_" prefixes.
    # core_bet_type_27 = pts O/U, 23 = reb O/U, 26 = ast O/U
    ("core_bet_type_27_",  "pts",         "Points",      "nba"),
    ("core_bet_type_23_",  "reb",         "Rebounds",    "nba"),
    ("core_bet_type_26_",  "ast",         "Assists",     "nba"),
    # Legacy keys kept for forward-compat if AN ever reverts
    ("player_points",      "pts",         "Points",      "nba"),
    ("player_rebounds",    "reb",         "Rebounds",    "nba"),
    ("player_assists",     "ast",         "Assists",     "nba"),
]

_PLAYER_PROP_MARKETS: frozenset[str] = frozenset(
    mkt for _, mkt, _, _ in PLAYER_PROP_PATTERNS)

# ── Direct-book scraper constants ─────────────────────────────────────

# DraftKings public sportsbook API (no auth required)
DK_SPORT_GROUPS: dict[str, int] = {
    "mlb":    84240,
    "nba":    42648,
    "nhl":    42133,
    "nfl":    88808,
    "ncaab":  92483,
    "ncaaf":  87637,
    "ufc":    9034,
    "soccer": 92893,   # MLS (update if wrong)
}
DK_PROPS_BASE = "https://sportsbook-nash.draftkings.com/sites/US-NJ-SB/api/v5"

# FanDuel public sportsbook guest key (rotate if 401s appear)
FD_API_KEY    = "FhMFpcPWXMeyZxOx"
FD_PROPS_BASE = "https://sbapi.fanduel.com"

# Pinnacle special-matchup description substrings → (market_key, market_disp, sport)
# Used by fetch_pinnacle_props() to classify "Player Name - Market" descriptions.
_PINNACLE_PROP_DESC_PATTERNS: list[tuple[str, str, str]] = [
    # (description substring lowercase, market_key, market_disp)
    # NBA
    ("points",      "pts",         "Points"),
    ("rebounds",    "reb",         "Rebounds"),
    ("assists",     "ast",         "Assists"),
    # MLB
    ("strikeout",   "strikeouts",  "Strikeouts"),
    ("total base",  "total_bases", "Total Bases"),
    ("pitcher k",   "strikeouts",  "Strikeouts"),
]

# Market keys that belong to NBA vs MLB (used for sport inference)
_PINNACLE_PROP_SPORT: dict[str, str] = {
    "pts":         "nba",
    "reb":         "nba",
    "ast":         "nba",
    "strikeouts":  "mlb",
    "total_bases": "mlb",
}

# Text fragments → (mkt_key, mkt_disp, sport_code) for DK/FD label matching
_PROP_LABEL_MAP: list[tuple[str, str, str, str]] = [
    # MLB
    ("strikeout",   "strikeouts",  "Strikeouts",  "mlb"),
    ("pitcher k",   "strikeouts",  "Strikeouts",  "mlb"),
    ("total base",  "total_bases", "Total Bases", "mlb"),
    ("batter tb",   "total_bases", "Total Bases", "mlb"),
    # NBA
    ("points",      "pts",         "Points",      "nba"),
    ("rebounds",    "reb",         "Rebounds",    "nba"),
    ("assists",     "ast",         "Assists",     "nba"),
    # Short-form aliases used by some books
    (" pts",        "pts",         "Points",      "nba"),
    (" reb",        "reb",         "Rebounds",    "nba"),
    (" ast",        "ast",         "Assists",     "nba"),
]


def _identify_player_prop_key(prop_key: str):
    """
    Return (market_key, display, sport_code, side) if prop_key matches a
    player O/U prop pattern, else None.
    side is "over", "under", or None (check entry-level data).
    Skips milestone/binary props (e.g. HR 1-or-more).
    """
    pk = prop_key.lower()
    if "milestone" in pk or "1_or_more" in pk:
        return None
    for substr, mkt, disp, sport in PLAYER_PROP_PATTERNS:
        if substr in pk:
            if "over" in pk and "under" not in pk:
                side = "over"
            elif "under" in pk and "over" not in pk:
                side = "under"
            else:
                side = None
            return mkt, disp, sport, side
    return None


def _resolve_player_name(player_id, players_map: dict, entry: dict) -> str:
    """Resolve player full name from AN players map or entry fallback fields."""
    if player_id is not None:
        for pid_key in (str(player_id), player_id):
            pdata = players_map.get(pid_key)
            if pdata:
                name = pdata.get("full_name") or pdata.get("display_name")
                if name:
                    return name
    return (entry.get("player_abbr") or entry.get("player_last_name") or "")


def _prop_label_match(text: str, sport: str) -> tuple[str, str] | None:
    """Return (mkt_key, mkt_disp) if text contains a known prop keyword for sport."""
    tl = text.lower()
    for frag, mkt_key, mkt_disp, s in _PROP_LABEL_MAP:
        if s == sport and frag in tl:
            return mkt_key, mkt_disp
    return None


def _parse_ou_runner(runner_name: str, runner: dict) -> tuple[str, str, float | None]:
    """
    Parse a sportsbook runner into (player_name, side, line).

    Handles formats like:
      "Gerrit Cole Over 6.5"        → ("Gerrit Cole", "over", 6.5)
      "Under 6.5" with player in runner metadata → (player, "under", 6.5)
    Returns ("", "", None) if parsing fails.
    """
    # Prefer explicit metadata player fields (FanDuel, some DK markets)
    player = ""
    for meta_key in ("metadata", "participants"):
        meta = runner.get(meta_key)
        if isinstance(meta, dict):
            player = (meta.get("player_name") or meta.get("name")
                      or meta.get("playerName") or "")
            if player:
                break

    tl   = runner_name.lower()
    side = ""
    line = None

    for s in ("over", "under"):
        idx = tl.find(s)
        if idx >= 0:
            side = s
            m = re.search(rf"{s}\s*([\d.]+)", tl[idx:])
            if m:
                try:
                    line = float(m.group(1))
                except ValueError:
                    pass
            if not player:
                player = runner_name[:idx].strip(" -/")
            break

    # Fall back to handicap field for line
    if line is None:
        hcap = runner.get("handicap")
        if hcap is not None:
            try:
                line = float(hcap)
            except (ValueError, TypeError):
                pass

    return player, side, line


def _display_book_name(book: str) -> str:
    """Normalise internal book key to a display name."""
    _MAP = {
        "draftkings": "DraftKings",
        "fanduel":    "FanDuel",
        "betmgm":     "BetMGM",
        "espnbet":    "ESPN Bet",
        "betrivers":  "BetRivers",
        "caesars":    "Caesars",
        "bet365":     "Bet365",
        "fanatics":   "Fanatics",
        "hardrock":   "Hard Rock",
    }
    return _MAP.get(book, book.title())


# ── DraftKings direct scraper ─────────────────────────────────────────

def _scrape_dk_game_lines(sport: str, session: requests.Session) -> list[dict]:
    """
    Scrape game lines (H2H, spread, total) from DraftKings' public API.

    Returns events in the standard internal format (same shape as fetch_pinnacle),
    so they can be injected via _inject_book_events. Bookmaker key = "draftkings".
    """
    group_id = DK_SPORT_GROUPS.get(sport)
    if not group_id:
        return []

    today = datetime.now(EASTERN).strftime("%Y-%m-%d")
    dk_headers = {
        "Origin":  "https://sportsbook.draftkings.com",
        "Referer": "https://sportsbook.draftkings.com/",
        "Accept":  "application/json",
    }

    try:
        r = session.get(
            f"{DK_PROPS_BASE}/eventgroups/{group_id}",
            params={"format": "json"},
            headers=dk_headers,
            timeout=30,
        )
        if r.status_code != 200:
            return []
        eg = r.json().get("eventGroup", {})
    except Exception:
        return []

    # Parse events
    ev_meta: dict[int, dict] = {}
    for ev in eg.get("events", []):
        eid   = ev.get("eventId")
        start = ev.get("startDate") or ev.get("startTime", "")
        if not eid or not str(start).startswith(today):
            continue
        name = ev.get("name", "")
        away = home = ""
        for sep in (" @ ", " vs ", " v "):
            if sep in name:
                parts = name.split(sep, 1)
                away, home = parts[0].strip(), parts[1].strip()
                break
        if not away or not home:
            continue
        ev_meta[eid] = {"away": away, "home": home, "start": start}

    if not ev_meta:
        return []

    # Find game-line offer categories — typically "Game Lines" or "Moneyline"
    _GL_KEYWORDS = {"game lines", "game", "moneyline", "point spread",
                    "total points", "total runs", "total goals", "puck line",
                    "run line"}
    gl_subs: list[tuple[int, int]] = []
    seen_pairs: set[tuple] = set()
    for cat in eg.get("offerCategories", []):
        cat_id   = cat.get("offerCategoryId")
        cat_name = (cat.get("name") or "").lower()
        # Only look at categories that are game-level (not player props)
        if any(kw in cat_name for kw in _GL_KEYWORDS):
            for sub in cat.get("offerSubcategoryDescriptors", []):
                sub_id = sub.get("subcategoryId")
                pair   = (cat_id, sub_id)
                if pair not in seen_pairs:
                    seen_pairs.add(pair)
                    gl_subs.append(pair)
                    # Also check if offers are already inline
                    inline = sub.get("offerSubcategory", {}).get("offers", [])
                    if inline:
                        # Mark inline so we don't re-fetch
                        pass

    # Build events from inline offers first (eventGroup sometimes has them)
    events_out: dict[int, dict] = {}  # eventId → internal event dict

    def _ensure_event(eid):
        if eid not in events_out and eid in ev_meta:
            m = ev_meta[eid]
            events_out[eid] = {
                "dk_event_id":   eid,
                "commence_time": m["start"],
                "home_team":     m["home"],
                "away_team":     m["away"],
                "sport_title":   sport.upper(),
                "bookmakers":    [{"key": "draftkings", "markets": [],
                                  "source": "direct"}],
            }
        return events_out.get(eid)

    def _parse_dk_offer(offer):
        """Parse one DK offer into market dicts and add to the event."""
        eid      = offer.get("eventId")
        ev_dict  = _ensure_event(eid)
        if not ev_dict:
            return
        outcomes = offer.get("outcomes", [])
        if len(outcomes) < 2:
            return

        # Determine market type from outcomes
        o0_label = (outcomes[0].get("label") or "").lower()
        o1_label = (outcomes[1].get("label") or "").lower()

        if o0_label in ("over", "under") or o1_label in ("over", "under"):
            # Totals
            ov = next((o for o in outcomes if (o.get("label") or "").lower() == "over"), None)
            un = next((o for o in outcomes if (o.get("label") or "").lower() == "under"), None)
            if not (ov and un):
                return
            try:
                ov_odds = int(ov.get("oddsAmerican") or ov.get("odds", ""))
                un_odds = int(un.get("oddsAmerican") or un.get("odds", ""))
                ov_line = float(ov.get("line") or ov.get("handicap") or 0)
            except (ValueError, TypeError):
                return
            if ov_line <= 0:
                return
            ev_dict["bookmakers"][0]["markets"].append({
                "key": "totals",
                "outcomes": [
                    {"name": "Over",  "price": ov_odds, "point": ov_line},
                    {"name": "Under", "price": un_odds, "point": ov_line},
                ],
            })
        elif any(o.get("line") is not None or o.get("handicap") is not None
                 for o in outcomes):
            # Spreads
            try:
                o0_odds = int(outcomes[0].get("oddsAmerican") or outcomes[0].get("odds", ""))
                o1_odds = int(outcomes[1].get("oddsAmerican") or outcomes[1].get("odds", ""))
                o0_line = float(outcomes[0].get("line") or outcomes[0].get("handicap") or 0)
                o1_line = float(outcomes[1].get("line") or outcomes[1].get("handicap") or 0)
            except (ValueError, TypeError):
                return
            o0_name = (outcomes[0].get("label") or outcomes[0].get("participant") or "").strip()
            o1_name = (outcomes[1].get("label") or outcomes[1].get("participant") or "").strip()
            ev_dict["bookmakers"][0]["markets"].append({
                "key": "spreads",
                "outcomes": [
                    {"name": o0_name, "price": o0_odds, "point": o0_line},
                    {"name": o1_name, "price": o1_odds, "point": o1_line},
                ],
            })
        else:
            # Moneyline (H2H)
            try:
                o0_odds = int(outcomes[0].get("oddsAmerican") or outcomes[0].get("odds", ""))
                o1_odds = int(outcomes[1].get("oddsAmerican") or outcomes[1].get("odds", ""))
            except (ValueError, TypeError):
                return
            o0_name = (outcomes[0].get("label") or outcomes[0].get("participant") or "").strip()
            o1_name = (outcomes[1].get("label") or outcomes[1].get("participant") or "").strip()
            ev_dict["bookmakers"][0]["markets"].append({
                "key": "h2h",
                "outcomes": [
                    {"name": o0_name, "price": o0_odds, "point": None},
                    {"name": o1_name, "price": o1_odds, "point": None},
                ],
            })

    # Check inline offers in the eventGroup
    for cat in eg.get("offerCategories", []):
        cat_name = (cat.get("name") or "").lower()
        if not any(kw in cat_name for kw in _GL_KEYWORDS):
            continue
        for sub in cat.get("offerSubcategoryDescriptors", []):
            for offer in sub.get("offerSubcategory", {}).get("offers", []):
                for nested in (offer if isinstance(offer, list) else [offer]):
                    _parse_dk_offer(nested)

    # Fetch subcategories that weren't inline
    event_ids_param = ",".join(str(i) for i in ev_meta)
    for cat_id, sub_id in gl_subs:
        try:
            r2 = session.get(
                f"{DK_PROPS_BASE}/eventgroups/{group_id}"
                f"/categories/{cat_id}/subcategories/{sub_id}",
                params={"format": "json", "eventIds": event_ids_param},
                headers=dk_headers,
                timeout=30,
            )
            if r2.status_code != 200:
                continue
            cat_resp = r2.json()
        except Exception:
            continue

        for ocat in cat_resp.get("eventGroup", {}).get("offerCategories", []):
            for sub_desc in ocat.get("offerSubcategoryDescriptors", []):
                offers = sub_desc.get("offerSubcategory", {}).get("offers", [])
                for offer in offers:
                    for nested in (offer if isinstance(offer, list) else [offer]):
                        _parse_dk_offer(nested)
        time.sleep(0.1)

    # Only return events that have at least one market
    return [ev for ev in events_out.values()
            if ev["bookmakers"][0]["markets"]]


def _scrape_dk_props(sport: str, session: requests.Session) -> list[dict]:
    """
    Scrape player props directly from DraftKings' public sportsbook API.

    Steps:
      1. Fetch the sport event-group to get today's events + offer categories.
      2. Find prop-related subcategories via keyword matching.
      3. Fetch each subcategory filtered to today's event IDs.
      4. Parse offers → outcomes → (player, over/under, line, odds).
    """
    group_id = DK_SPORT_GROUPS.get(sport)
    if not group_id:
        return []

    today = datetime.now(EASTERN).strftime("%Y-%m-%d")

    # DraftKings' sportsbook API requires its own Origin/Referer headers.
    # The session carries AN headers by default — patch them for DK calls.
    dk_headers = {
        "Origin":  "https://sportsbook.draftkings.com",
        "Referer": "https://sportsbook.draftkings.com/",
        "Accept":  "application/json",
    }

    try:
        r = session.get(
            f"{DK_PROPS_BASE}/eventgroups/{group_id}",
            params={"format": "json"},
            headers=dk_headers,
            timeout=30,
        )
        if r.status_code != 200:
            return []
        eg = r.json().get("eventGroup", {})
    except Exception:
        return []

    # Parse today's events
    ev_meta: dict[int, dict] = {}
    for ev in eg.get("events", []):
        eid   = ev.get("eventId")
        start = ev.get("startDate") or ev.get("startTime", "")
        if not eid or not str(start).startswith(today):
            continue
        name = ev.get("name", "")
        away = home = ""
        for sep in (" @ ", " vs ", " v "):
            if sep in name:
                parts = name.split(sep, 1)
                away, home = parts[0].strip(), parts[1].strip()
                break
        ev_meta[eid] = {"away_team": away, "home_team": home, "start_time": start}

    if not ev_meta:
        return []

    event_ids_param = ",".join(str(i) for i in ev_meta)

    # Find prop offer subcategories
    prop_subs: list[tuple[int, int, str, str]] = []
    seen_pairs: set[tuple] = set()
    for cat in eg.get("offerCategories", []):
        cat_id   = cat.get("offerCategoryId")
        cat_name = cat.get("name", "")
        for sub in cat.get("offerSubcategoryDescriptors", []):
            sub_id   = sub.get("subcategoryId")
            sub_name = sub.get("name", "")
            if (cat_id, sub_id) in seen_pairs:
                continue
            for label in (cat_name, sub_name):
                mapped = _prop_label_match(label, sport)
                if mapped:
                    prop_subs.append((cat_id, sub_id, *mapped))
                    seen_pairs.add((cat_id, sub_id))
                    break

    if not prop_subs:
        return []

    raw: list[dict] = []
    for cat_id, sub_id, mkt_key, mkt_disp in prop_subs:
        try:
            r2 = session.get(
                f"{DK_PROPS_BASE}/eventgroups/{group_id}"
                f"/categories/{cat_id}/subcategories/{sub_id}",
                params={"format": "json", "eventIds": event_ids_param},
                headers=dk_headers,
                timeout=30,
            )
            if r2.status_code != 200:
                continue
            cat_resp = r2.json()
        except Exception:
            continue

        for ocat in cat_resp.get("eventGroup", {}).get("offerCategories", []):
            for sub_desc in ocat.get("offerSubcategoryDescriptors", []):
                offers = sub_desc.get("offerSubcategory", {}).get("offers", [])
                for offer_item in offers:
                  for offer in (offer_item if isinstance(offer_item, list)
                                else [offer_item]):
                    eid    = offer.get("eventId")
                    meta   = ev_meta.get(eid, {})
                    player = (offer.get("label") or
                              offer.get("participant") or "").strip()
                    for oc in offer.get("outcomes", []):
                        side = (oc.get("label") or "").lower()
                        if side not in ("over", "under"):
                            continue
                        try:
                            odds = int(oc.get("oddsAmerican") or oc.get("odds", ""))
                        except (ValueError, TypeError):
                            continue
                        line = oc.get("line") or oc.get("handicap")
                        try:
                            line = float(line) if line is not None else None
                        except (ValueError, TypeError):
                            line = None
                        p = player or (oc.get("participant") or "").strip()
                        if not p:
                            continue
                        raw.append({
                            "player":      p,
                            "away_team":   meta.get("away_team", ""),
                            "home_team":   meta.get("home_team", ""),
                            "start_time":  meta.get("start_time", ""),
                            "sport":       sport,
                            "market":      mkt_key,
                            "market_disp": mkt_disp,
                            "line":        line,
                            "side":        side,
                            "book":        "draftkings",
                            "odds":        odds,
                        })
        time.sleep(0.1)

    return raw


# ── FanDuel direct scraper ────────────────────────────────────────────

def _make_fd_session() -> requests.Session:
    """
    Build a requests Session tuned for FanDuel's sportsbook API.

    sbapi.fanduel.com rejects Python's default TLS client hello (handshake
    failure) on some environments. Using a permissive cipher suite and
    disabling hostname verification for the SSL context fixes this without
    affecting other hosts.
    """
    import ssl
    from requests.adapters import HTTPAdapter
    try:
        from urllib3.util.ssl_ import create_urllib3_context  # urllib3 ≥ 1.26
        ctx = create_urllib3_context()
    except ImportError:
        ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode    = ssl.CERT_NONE
    try:
        ctx.set_ciphers("DEFAULT:@SECLEVEL=1")
    except ssl.SSLError:
        pass  # older OpenSSL — leave ciphers at default

    class _FDAdapter(HTTPAdapter):
        def init_poolmanager(self, *args, **kwargs):
            kwargs["ssl_context"] = ctx
            super().init_poolmanager(*args, **kwargs)
        def proxy_manager_for(self, proxy, **kwargs):
            kwargs["ssl_context"] = ctx
            return super().proxy_manager_for(proxy, **kwargs)

    fd_session = requests.Session()
    fd_session.headers.update(SCRAPER_HEADERS)
    fd_session.mount("https://", _FDAdapter())
    return fd_session


# ── FanDuel game-line constants ──────────────────────────────────────
FD_GL_SPORT_KEYWORDS: dict[str, list[str]] = {
    "mlb": ["mlb", "major league baseball"],
    "nba": ["nba", "national basketball"],
    "nhl": ["nhl", "national hockey"],
    "nfl": ["nfl", "national football"],
    "ncaab": ["ncaa basketball", "ncaab", "college basketball"],
    "ncaaf": ["ncaa football", "ncaaf", "college football"],
}

_FD_GL_MARKET_KEYWORDS: dict[str, list[str]] = {
    "h2h":     ["moneyline", "money line", "match result", "match winner",
                "to win"],
    "spreads": ["spread", "handicap", "run line", "puck line"],
    "totals":  ["total points", "total runs", "total goals",
                "over/under", "over / under", "game total", "match total"],
}


def _scrape_fd_game_lines(sport: str, session: requests.Session) -> list[dict]:
    """
    Scrape game lines (H2H, spread, total) from FanDuel's public API.

    Returns events in the standard internal format (same shape as fetch_pinnacle),
    so they can be injected via _inject_book_events. Bookmaker key = "fanduel".
    """
    keywords = FD_GL_SPORT_KEYWORDS.get(sport)
    if not keywords:
        return []

    FD_KEY = FD_API_KEY
    today  = datetime.now(EASTERN).strftime("%Y-%m-%d")
    fd_sess = _make_fd_session()

    # Step 1: find competition IDs
    try:
        r = fd_sess.get(
            f"{FD_PROPS_BASE}/api/competitions",
            params={"_ak": FD_KEY, "betexRegion": "GBR",
                    "jurisdiction": "USNJ", "locale": "en-US"},
            timeout=30,
        )
        if r.status_code != 200:
            return []
        comps_attach = r.json().get("attachments", {})
    except Exception:
        return []

    comp_ids: list = []
    for comp in comps_attach.get("competitions", {}).values():
        cname = comp.get("name", "").lower()
        if any(kw in cname for kw in keywords):
            cid = comp.get("competitionId") or comp.get("id")
            if cid:
                comp_ids.append(cid)

    if not comp_ids:
        return []

    # Step 2: get today's events per competition
    fd_events: list[dict] = []
    for comp_id in comp_ids:
        try:
            r2 = fd_sess.get(
                f"{FD_PROPS_BASE}/api/competitions/{comp_id}/events",
                params={"_ak": FD_KEY, "betexRegion": "GBR",
                        "jurisdiction": "USNJ", "locale": "en-US",
                        "startDate": today, "endDate": today,
                        "page": 0, "perPage": 50, "sortDir": "asc"},
                timeout=30,
            )
            if r2.status_code != 200:
                continue
            ev_attach = r2.json().get("attachments", {})
        except Exception:
            continue

        for eid, ev in ev_attach.get("events", {}).items():
            open_date = ev.get("openDate", "")
            if today not in str(open_date)[:10]:
                continue
            ev_name = ev.get("name", "")
            away = home = ""
            for sep in (" v ", " @ ", " vs "):
                if sep in ev_name:
                    parts = ev_name.split(sep, 1)
                    away, home = parts[0].strip(), parts[1].strip()
                    break
            if not away or not home:
                continue
            fd_events.append({
                "fd_event_id": eid,
                "away_team":   away,
                "home_team":   home,
                "start_time":  open_date,
            })

    if not fd_events:
        return []

    # Step 3: fetch event page for each event, extract game-line markets
    events_out: dict[str, dict] = {}  # fd_event_id → internal event dict

    for ev_info in fd_events:
        try:
            r3 = fd_sess.get(
                f"{FD_PROPS_BASE}/api/event-page",
                params={"eventId": ev_info["fd_event_id"],
                        "_ak": FD_KEY,
                        "betexRegion": "GBR",
                        "jurisdiction": "USNJ",
                        "locale": "en-US",
                        "timezone": "America/New_York"},
                timeout=30,
            )
            if r3.status_code != 200:
                continue
            ep_attach = r3.json().get("attachments", {})
        except Exception:
            continue

        eid = ev_info["fd_event_id"]
        ev_dict = {
            "commence_time": ev_info["start_time"],
            "home_team":     ev_info["home_team"],
            "away_team":     ev_info["away_team"],
            "sport_title":   sport.upper(),
            "bookmakers":    [{"key": "fanduel", "markets": [],
                              "source": "direct"}],
        }

        for mkt in ep_attach.get("markets", {}).values():
            mkt_name = (mkt.get("marketName") or mkt.get("name") or "").lower()
            runners  = mkt.get("runners", [])
            if len(runners) < 2:
                continue

            # Determine market type from name
            mkt_type = None
            for mt, kws in _FD_GL_MARKET_KEYWORDS.items():
                if any(kw in mkt_name for kw in kws):
                    mkt_type = mt
                    break
            if not mkt_type:
                continue

            # Skip alternate lines (keep main game lines only)
            if "alt" in mkt_name or "alternate" in mkt_name:
                continue

            def _fd_american_odds(runner):
                """Extract American odds from FD runner."""
                for odds_path in (
                    ["winRunnerOdds", "americanDisplayOdds", "americanOdds"],
                    ["winRunnerOdds", "americanOdds"],
                    ["currentPrices", "americanOdds"],
                ):
                    node = runner
                    for key in odds_path:
                        node = node.get(key, {}) if isinstance(node, dict) else {}
                    if isinstance(node, (str, int, float)) and node != {}:
                        try:
                            return int(float(str(node).replace("+", "")))
                        except (ValueError, TypeError):
                            pass
                return None

            if mkt_type == "totals":
                ov = next((r for r in runners
                           if (r.get("runnerName") or "").lower().startswith("over")), None)
                un = next((r for r in runners
                           if (r.get("runnerName") or "").lower().startswith("under")), None)
                if not (ov and un):
                    continue
                ov_odds = _fd_american_odds(ov)
                un_odds = _fd_american_odds(un)
                ov_hcap = ov.get("handicap") or un.get("handicap")
                if ov_odds is None or un_odds is None or not ov_hcap:
                    continue
                try:
                    ov_line = float(ov_hcap)
                except (ValueError, TypeError):
                    continue
                if ov_line <= 0:
                    continue
                ev_dict["bookmakers"][0]["markets"].append({
                    "key": "totals",
                    "outcomes": [
                        {"name": "Over",  "price": ov_odds, "point": ov_line},
                        {"name": "Under", "price": un_odds, "point": ov_line},
                    ],
                })

            elif mkt_type == "spreads":
                r0, r1 = runners[0], runners[1]
                r0_odds = _fd_american_odds(r0)
                r1_odds = _fd_american_odds(r1)
                if r0_odds is None or r1_odds is None:
                    continue
                try:
                    r0_hcap = float(r0.get("handicap", 0))
                    r1_hcap = float(r1.get("handicap", 0))
                except (ValueError, TypeError):
                    continue
                r0_name = (r0.get("runnerName") or "").strip()
                r1_name = (r1.get("runnerName") or "").strip()
                ev_dict["bookmakers"][0]["markets"].append({
                    "key": "spreads",
                    "outcomes": [
                        {"name": r0_name, "price": r0_odds, "point": r0_hcap},
                        {"name": r1_name, "price": r1_odds, "point": r1_hcap},
                    ],
                })

            else:  # h2h
                r0, r1 = runners[0], runners[1]
                r0_odds = _fd_american_odds(r0)
                r1_odds = _fd_american_odds(r1)
                if r0_odds is None or r1_odds is None:
                    continue
                r0_name = (r0.get("runnerName") or "").strip()
                r1_name = (r1.get("runnerName") or "").strip()
                ev_dict["bookmakers"][0]["markets"].append({
                    "key": "h2h",
                    "outcomes": [
                        {"name": r0_name, "price": r0_odds, "point": None},
                        {"name": r1_name, "price": r1_odds, "point": None},
                    ],
                })

        # Only keep events that got at least one market
        if ev_dict["bookmakers"][0]["markets"]:
            events_out[eid] = ev_dict

        time.sleep(0.1)

    return list(events_out.values())


def _scrape_fd_props(sport: str, session: requests.Session) -> list[dict]:
    """
    Scrape player props directly from FanDuel's public sportsbook API.

    Steps:
      1. Fetch competitions list and find the right MLB/NBA competition.
      2. Fetch today's events for that competition.
      3. For each event, fetch the event page and extract prop markets.
    """
    FD_KEY = FD_API_KEY
    today  = datetime.now(EASTERN).strftime("%Y-%m-%d")
    # Use a FanDuel-specific session with relaxed TLS (sbapi.fanduel.com
    # rejects Python's default TLS client hello in some environments).
    fd_sess = _make_fd_session()

    SPORT_KEYWORDS = {
        "mlb": ["mlb", "major league baseball"],
        "nba": ["nba", "national basketball"],
    }
    keywords = SPORT_KEYWORDS.get(sport, [sport])

    # Step 1: find competition IDs
    try:
        r = fd_sess.get(
            f"{FD_PROPS_BASE}/api/competitions",
            params={"_ak": FD_KEY, "betexRegion": "GBR",
                    "jurisdiction": "USNJ", "locale": "en-US"},
            timeout=30,
        )
        if r.status_code != 200:
            return []
        comps_attach = r.json().get("attachments", {})
    except Exception:
        return []

    comp_ids: list = []
    for comp in comps_attach.get("competitions", {}).values():
        cname = comp.get("name", "").lower()
        if any(kw in cname for kw in keywords):
            cid = comp.get("competitionId") or comp.get("id")
            if cid:
                comp_ids.append(cid)

    if not comp_ids:
        return []

    # Step 2: get today's events per competition
    fd_events: list[dict] = []
    for comp_id in comp_ids:
        try:
            r2 = fd_sess.get(
                f"{FD_PROPS_BASE}/api/competitions/{comp_id}/events",
                params={"_ak": FD_KEY, "betexRegion": "GBR",
                        "jurisdiction": "USNJ", "locale": "en-US",
                        "startDate": today, "endDate": today,
                        "page": 0, "perPage": 30, "sortDir": "asc"},
                timeout=30,
            )
            if r2.status_code != 200:
                continue
            ev_attach = r2.json().get("attachments", {})
        except Exception:
            continue

        for eid, ev in ev_attach.get("events", {}).items():
            open_date = ev.get("openDate", "")
            if today not in str(open_date)[:10]:
                continue
            ev_name = ev.get("name", "")
            away = home = ""
            for sep in (" v ", " @ ", " vs "):
                if sep in ev_name:
                    parts = ev_name.split(sep, 1)
                    away, home = parts[0].strip(), parts[1].strip()
                    break
            fd_events.append({
                "fd_event_id": eid,
                "away_team":   away,
                "home_team":   home,
                "start_time":  open_date,
            })

    if not fd_events:
        return []

    # Step 3: fetch event page for each event
    raw: list[dict] = []
    for ev_info in fd_events:
        try:
            r3 = fd_sess.get(
                f"{FD_PROPS_BASE}/api/event-page",
                params={"eventId": ev_info["fd_event_id"],
                        "_ak": FD_KEY,
                        "betexRegion": "GBR",
                        "jurisdiction": "USNJ",
                        "locale": "en-US",
                        "timezone": "America/New_York"},
                timeout=30,
            )
            if r3.status_code != 200:
                continue
            ep_attach = r3.json().get("attachments", {})
        except Exception:
            continue

        for mkt in ep_attach.get("markets", {}).values():
            mkt_name = mkt.get("marketName", "")
            mapped   = _prop_label_match(mkt_name, sport)
            if not mapped:
                continue
            mkt_key, mkt_disp = mapped

            for runner in mkt.get("runners", []):
                runner_name = runner.get("runnerName", "")
                player, side, line = _parse_ou_runner(runner_name, runner)
                if not player or side not in ("over", "under"):
                    continue

                # Extract American odds from various FD response shapes
                am_str = ""
                for odds_path in (
                    ["winRunnerOdds", "americanDisplayOdds", "americanOdds"],
                    ["winRunnerOdds", "americanOdds"],
                    ["currentPrices", "americanOdds"],
                ):
                    node = runner
                    for key in odds_path:
                        node = node.get(key, {}) if isinstance(node, dict) else {}
                    if isinstance(node, (str, int, float)) and node != {}:
                        am_str = str(node)
                        break

                if not am_str:
                    continue
                try:
                    odds = int(float(am_str.replace("+", "")))
                    if am_str.strip().startswith("+"):
                        odds = abs(odds)
                except (ValueError, TypeError):
                    continue

                raw.append({
                    "player":      player,
                    "away_team":   ev_info["away_team"],
                    "home_team":   ev_info["home_team"],
                    "start_time":  ev_info["start_time"],
                    "sport":       sport,
                    "market":      mkt_key,
                    "market_disp": mkt_disp,
                    "line":        line,
                    "side":        side,
                    "book":        "fanduel",
                    "odds":        odds,
                })
        time.sleep(0.1)

    return raw


# ── Action Network props scraper (supplement for BetMGM/Caesars/etc.) ─

def _scrape_an_props(sport: str, bettable_games: list[dict],
                     session: requests.Session) -> list[dict]:
    """
    Fetch player props from AN's /games/{id}/props endpoint.
    Used as a supplement to pull in books not scraped directly
    (BetMGM, Caesars, BetRivers, ESPN Bet, Bet365, Fanatics).
    Returns raw prop dicts with string book names.
    """
    raw: list[dict] = []
    for game in bettable_games:
        try:
            presp = session.get(
                f"{ACTION_NETWORK_BASE}/games/{game['id']}/props",
                timeout=30,
            )
            if presp.status_code != 200:
                continue
            props_data = presp.json()
        except Exception:
            continue

        player_props = props_data.get("player_props", {})
        players_map  = props_data.get("players", {})

        for prop_key, entries in player_props.items():
            result = _identify_player_prop_key(prop_key)
            if result is None:
                continue
            mkt_key, mkt_disp, prop_sport, key_side = result
            if prop_sport != sport:
                continue

            entries = entries if isinstance(entries, list) else [entries]
            for entry in entries:
                pid = entry.get("player_id") or entry.get("competitor_id")
                if not pid:
                    continue
                # Prefer "line" over "value" — AN's v2 API stores the integer
                # *threshold* in "value" (e.g. 8 = "player needs 8+") while
                # the actual O/U line (e.g. 7.5) lives in "line".  Reading
                # "value" first was causing a consistent +1 offset on whole-
                # number lines (rebounds, assists, etc.).
                entry_line = entry.get("line") or entry.get("value")
                lines = entry.get("lines", {})

                for bid_str, line_data in lines.items():
                    try:
                        bid = int(bid_str)
                    except ValueError:
                        continue
                    if bid not in AN_BOOK_IDS:
                        continue
                    book_name = _book_key(bid)
                    ld_list = (line_data if isinstance(line_data, list)
                               else [line_data])
                    for ld in ld_list:
                        odds = ld.get("odds")
                        if odds is None:
                            continue
                        # Same priority: "line" before "value"
                        line_val = (ld.get("line") or ld.get("value") or entry_line)
                        side = ld.get("side") or key_side or "over"
                        if side not in ("over", "under"):
                            side = "over"
                        try:
                            odds   = int(odds)
                            line_f = float(line_val) if line_val is not None else None
                        except (ValueError, TypeError):
                            continue
                        player_name = _resolve_player_name(pid, players_map, {})
                        if not player_name:
                            continue
                        raw.append({
                            "player":      player_name,
                            "away_team":   game.get("away_team", ""),
                            "home_team":   game.get("home_team", ""),
                            "start_time":  game.get("commence", ""),
                            "sport":       sport,
                            "market":      mkt_key,
                            "market_disp": mkt_disp,
                            "line":        line_f,
                            "side":        side,
                            "book":        book_name,
                            "odds":        odds,
                        })
        time.sleep(0.15)
    return raw


# ── Merger: combine raw lines from all sources ────────────────────────

def _line_bucket(line) -> str:
    if line is None:
        return "none"
    return str(round(float(line) * 2) / 2)


def _merge_raw_props(all_raw: list[dict]) -> list[dict]:
    """
    Merge raw prop records from multiple books into the final format.

    Groups by (player_norm, away_norm, home_norm, market, line_bucket, side),
    using fuzzy name matching to tolerate team/player name differences across
    sources. Returns list of dicts with book_odds={book_name: american_odds}.
    """
    def _norm(s: str) -> str:
        import unicodedata
        # Strip diacritics (Jokić → Jokic), drop periods (P.J. → PJ),
        # collapse whitespace.
        s = unicodedata.normalize("NFKD", s)
        s = "".join(c for c in s if not unicodedata.combining(c))
        s = s.replace(".", "").lower()
        return re.sub(r"\s+", " ", s).strip()

    def _norm_team(s: str) -> str:
        """Normalize team name using _norm + alias resolution."""
        n = _norm(s)
        if n in _TEAM_ALIASES:
            return _TEAM_ALIASES[n]
        for alias, canonical in _TEAM_ALIASES.items():
            if n == alias or n.endswith(f" {alias}"):
                return canonical
        return n

    acc: dict[tuple, dict] = {}

    for p in all_raw:
        player_n = _norm(p["player"])
        away_n   = _norm_team(p["away_team"])
        home_n   = _norm_team(p["home_team"])
        market   = p["market"]
        side     = p["side"]
        line_b   = _line_bucket(p.get("line"))

        found_key = None
        for key, grp in acc.items():
            kp, ka, kh, km, kl, ks = key
            if km != market or ks != side or kl != line_b:
                continue
            if _fuzzy(kp, player_n) < 0.85:
                continue
            away_ok = (ka == away_n or _fuzzy(ka, away_n) > 0.70
                       or ka in away_n or away_n in ka)
            home_ok = (kh == home_n or _fuzzy(kh, home_n) > 0.70
                       or kh in home_n or home_n in kh)
            if away_ok and home_ok:
                found_key = key
                break

        if found_key is None:
            found_key = (player_n, away_n, home_n, market, line_b, side)
            acc[found_key] = {
                "player":      p["player"],
                "away_team":   p["away_team"],
                "home_team":   p["home_team"],
                "start_time":  p["start_time"],
                "sport":       p["sport"].upper(),
                "market":      market,
                "market_disp": p["market_disp"],
                "line":        p.get("line"),
                "side":        side,
                "book_odds":   {},
                # Odds API IDs for closing-line capture; first non-None wins
                # (a prop may appear from multiple sources, only OddsAPI sets these)
                "event_id":    p.get("event_id"),
                "sport_key":   p.get("sport_key"),
            }

        grp  = acc[found_key]
        book = p["book"]
        odds = p["odds"]
        # Keep best (highest decimal) odds if same book appears from multiple sources
        if (book not in grp["book_odds"] or
                american_to_decimal(odds) > american_to_decimal(grp["book_odds"][book])):
            grp["book_odds"][book] = odds
        if grp["line"] is None and p.get("line") is not None:
            grp["line"] = p["line"]
        # Backfill OddsAPI IDs from any source that has them
        if grp.get("event_id") is None and p.get("event_id"):
            grp["event_id"] = p["event_id"]
        if grp.get("sport_key") is None and p.get("sport_key"):
            grp["sport_key"] = p["sport_key"]

    result = []
    for grp in acc.values():
        if len(grp["book_odds"]) < 2:
            continue
        game_label = f"{grp['away_team']} @ {grp['home_team']}"
        result.append({
            "player":      grp["player"],
            "game":        game_label,
            "commence":    grp["start_time"],
            "sport":       grp["sport"],
            "market":      grp["market"],
            "market_disp": grp["market_disp"],
            "line":        grp["line"],
            "side":        grp["side"],
            "book_odds":   grp["book_odds"],
            "event_id":    grp.get("event_id"),
            "sport_key":   grp.get("sport_key"),
        })
    return result


def fetch_player_props(
    session: requests.Session,
    sport_markets: dict[str, set[str]] | None = None,
) -> list[dict]:
    """
    Fetch player props from The Odds API (primary) + Pinnacle guest API
    (supplementary).

    The Odds API provides DK, FD, Pinnacle, BetMGM, and more in a single
    call per sport. Pinnacle's direct API is kept as a backup for additional
    prop coverage.

    All sources are merged by (player, game, market, line, side) using fuzzy
    name matching. Returns the combined list with book_odds keyed by book name
    strings (e.g. "draftkings", "fanduel", "pinnacle").

    If `sport_markets` is given, only fetches those sports and (for The Odds
    API path) restricts the markets query to the named internal market keys —
    used by /run_promos when the supplied promos target a specific prop only.
    """
    all_raw: list[dict] = []
    if sport_markets is not None:
        target_sports = sorted(sport_markets.keys())
    else:
        target_sports = sorted(ODDS_API_PROP_MARKETS.keys())

    for sport in target_sports:
        sport_raw: list[dict] = []
        wanted_markets = (sport_markets.get(sport)
                          if sport_markets is not None else None)

        # ── The Odds API (primary — DK, FD, Pinnacle, BetMGM, etc.) ──
        try:
            api_raw = fetch_odds_api_props(sport, markets=wanted_markets)
            sport_raw.extend(api_raw)
        except Exception as e:
            print(f"  [Props/OddsAPI/{sport.upper()}] Skipped: {e}")

        # ── Pinnacle direct (supplementary backup) ───────────────────
        if sport in PINNACLE_LEAGUES:
            try:
                pinn_raw = fetch_pinnacle_props(sport, session)
                if pinn_raw:
                    sport_raw.extend(pinn_raw)
            except Exception as e:
                print(f"  [Props/Pinnacle/{sport.upper()}] Skipped: {e}")

        all_raw.extend(sport_raw)

        book_counts: dict[str, int] = {}
        for p in sport_raw:
            b = p["book"]
            book_counts[b] = book_counts.get(b, 0) + 1
        if book_counts:
            counts_str = ", ".join(
                f"{_display_book_name(b)}={c}"
                for b, c in sorted(book_counts.items()))
            print(f"  [Player Props/{sport.upper()}] "
                  f"{len(sport_raw)} raw lines — {counts_str}")
        else:
            print(f"  [Player Props/{sport.upper()}] no lines found")

    return _merge_raw_props(all_raw)


def _compute_prop_fair_prob(
    ref_this:  dict,   # {book_name: am_odds} for THIS side, book-being-evaluated already removed
    ref_other: dict,   # {book_name: am_odds} for the OTHER side, same exclusion
) -> tuple[float | None, bool]:
    """
    Compute a fair probability from a pool of reference books.

    Because the book being evaluated has already been removed from ref_this /
    ref_other by the caller, the other target book (DK or FD) is present in
    the pool and used as a reference — it is no longer excluded globally.
    This lets FD price DK edges and DK price FD edges, which is correct since
    we only ever bet one of them per edge.

    Returns (fair_prob, pinnacle_anchored).
    Returns (None, False) when there is not enough reference data to build a
    reliable fair price (no Pinnacle AND fewer than MIN_PROP_REFERENCE_BOOKS
    non-Pinnacle reference books).
    """
    if not ref_this:
        return None, False

    pinn_has_both  = "pinnacle" in ref_this and "pinnacle" in ref_other
    has_other_side = bool(ref_other)

    if pinn_has_both:
        # ── Pinnacle-anchored ─────────────────────────────────────────────
        pinn_this_dec  = american_to_decimal(ref_this["pinnacle"])
        pinn_other_dec = american_to_decimal(ref_other["pinnacle"])
        pinn_this_imp  = decimal_to_implied_prob(pinn_this_dec)
        pinn_other_imp = decimal_to_implied_prob(pinn_other_dec)
        total_pinn_imp = pinn_this_imp + pinn_other_imp
        pinn_fair      = pinn_this_imp / total_pinn_imp if total_pinn_imp > 0 else 0.5

        # Consensus: all non-Pinnacle reference books (includes the other
        # target book, e.g. FD when evaluating DK)
        consensus_probs: list[float] = []
        for bk, am in ref_this.items():
            if bk == "pinnacle":
                continue
            dec = american_to_decimal(am)
            if has_other_side and bk in ref_other:
                o_dec = american_to_decimal(ref_other[bk])
                t_imp = decimal_to_implied_prob(dec)
                o_imp = decimal_to_implied_prob(o_dec)
                tot   = t_imp + o_imp
                if tot > 0:
                    consensus_probs.append(t_imp / tot)
            else:
                consensus_probs.append(decimal_to_implied_prob(dec))

        if consensus_probs:
            consensus_probs.sort()
            mid_c = len(consensus_probs) // 2
            consensus_fair = (
                (consensus_probs[mid_c - 1] + consensus_probs[mid_c]) / 2
                if len(consensus_probs) % 2 == 0
                else consensus_probs[mid_c])
            fair_prob = (pinn_fair * PINNACLE_WEIGHT +
                         consensus_fair * (1 - PINNACLE_WEIGHT))
        else:
            fair_prob = pinn_fair  # Pinnacle is the only reference — use directly

        return fair_prob, True

    else:
        # ── Consensus-only ────────────────────────────────────────────────
        fair_probs: list[float] = []
        for bk, am in ref_this.items():
            dec = american_to_decimal(am)
            if has_other_side and bk in ref_other:
                o_dec = american_to_decimal(ref_other[bk])
                t_imp = decimal_to_implied_prob(dec)
                o_imp = decimal_to_implied_prob(o_dec)
                tot   = t_imp + o_imp
                if tot > 0:
                    fair_probs.append(t_imp / tot)
            else:
                fair_probs.append(decimal_to_implied_prob(dec))

        # Require a minimum number of reference books.  The other target book
        # (DK or FD) counts toward this threshold since it is a sharp,
        # liquid market — a single soft book alone does not.
        if len(fair_probs) < MIN_PROP_REFERENCE_BOOKS:
            return None, False

        fair_probs.sort()
        mid       = len(fair_probs) // 2
        fair_prob = (
            (fair_probs[mid - 1] + fair_probs[mid]) / 2
            if len(fair_probs) % 2 == 0
            else fair_probs[mid])
        return fair_prob, False


# Binary Yes-only prop markets — books post a "Yes" price per player and
# no "No" side (anytime HR, WC anytime goalscorer). They share shaping
# rules in find_player_prop_edges: Yes side only, outcome is the bare
# player name, no line displayed.
_YES_ONLY_PROP_MARKETS = frozenset({"hr", "goal"})


def find_player_prop_edges(
    props: list[dict],
    promo_pool: list[dict] | None = None,
) -> list[dict]:
    """
    Find +EV player prop edges on DraftKings / FanDuel.

    If `promo_pool` is provided, every constructed prop edge (regardless of
    EV) is also appended there. Mirrors the find_edges pattern so prop-boost
    promos can rank candidates by boosted EV without the MIN_EV_PERCENT
    pre-filter discarding outcomes the boost would lift into +EV (notably
    long-shot binary markets like NBA DD, NBA PRA, and batter_home_runs
    where the boost is large enough to flip moderately -EV picks).

    book_odds keys are string book names (e.g. "draftkings", "fanduel",
    "betmgm", "pinnacle") — not AN integer IDs. TARGET_BOOKS is the set
    of books we bet on.

    Fair probability logic (mirrors game-line find_edges):
      • If Pinnacle has both over AND under for this prop:
          pinn_fair = vig-removed Pinnacle implied prob  (2% vig → very accurate)
          consensus_fair = median vig-removed prob from all other non-target books
          fair_prob = pinn_fair × 0.65 + consensus_fair × 0.35  (PINNACLE_WEIGHT)
          → pinnacle_anchored = True
      • Otherwise (no Pinnacle):
          fair_prob = median vig-removed prob across all non-target books
          → pinnacle_anchored = False
    """
    # Pair over + under for vig removal.
    # KEY INCLUDES line_bucket so that a DK Over 3.5 and an FD Over 2.5 are
    # never pooled into the same fair-price calculation — they are different
    # bets and mixing their odds would produce meaningless (and dangerously
    # inflated) EV signals.  _line_bucket() rounds to the nearest 0.5, so
    # lines within ±0.25 of each other still merge (e.g. 3.4 and 3.5 → "3.5"),
    # but 3.5 and 2.5 land in separate groups as intended.
    pairs: dict[tuple, dict] = {}
    for prop in props:
        line_b = _line_bucket(prop.get("line"))
        pk = (prop["player"], prop["game"], prop["market"], line_b)
        if pk not in pairs:
            pairs[pk] = {
                "player":      prop["player"],
                "game":        prop["game"],
                "commence":    prop["commence"],
                "sport":       prop["sport"],
                "market":      prop["market"],
                "market_disp": prop["market_disp"],
                "line":        prop["line"],
                "line_bucket": line_b,
                "over":        {},
                "under":       {},
                # Odds API IDs for closing-line capture (None for AN-fallback)
                "event_id":    prop.get("event_id"),
                "sport_key":   prop.get("sport_key"),
            }
        pairs[pk][prop["side"]].update(prop["book_odds"])

    edges: list[dict] = []

    for (player, game, market, line_b), pair in pairs.items():
        line = pair["line"]

        for side in ("over", "under"):
            # Yes-only binaries (anytime HR / anytime goalscorer) — a bet
            # on the "No" side isn't a market we surface (boost terms
            # always say "anytime ...", and the No side carries different
            # fair-prob math). Emit the Yes side only so these edges stay
            # shaped like the prior AN-flavored output the HR Props tab +
            # promo picker expect.
            if market in _YES_ONLY_PROP_MARKETS and side != "over":
                continue
            side_odds       = pair[side]
            other_side_odds = pair["under" if side == "over" else "over"]
            if not side_odds:
                continue

            target_entries: list[tuple] = []  # (display, dec, am, book_name)
            other_entries:  list[tuple] = []  # non-target books (display only)

            for book_name, am in side_odds.items():
                dec     = american_to_decimal(am)
                display = _display_book_name(book_name)
                if book_name in TARGET_BOOKS:
                    target_entries.append((display, dec, am, book_name))
                else:
                    other_entries.append((display, dec, am, book_name))

            if not target_entries:
                continue

            all_book_display: dict[str, int] = {
                _display_book_name(bk): am
                for bk, am in side_odds.items()
            }

            # best_other_dec / num_other_books are display fields using external
            # (non-target) books only — for context in the sheet output.
            best_other_dec = (max(american_to_decimal(am) for _, _, am, _ in other_entries)
                              if other_entries else None)

            # Dedup target books (keep best odds per canonical book name)
            best_target: dict[str, tuple] = {}
            for display, dec, am, book_name in target_entries:
                if display not in best_target or dec > best_target[display][1]:
                    best_target[display] = (display, dec, am, book_name)

            for display, dec, am, book_name in best_target.values():
                # ── Fair probability — computed PER target book ───────────────
                # Exclude only the book being evaluated so the OTHER target book
                # (FD when evaluating DK, DK when evaluating FD) is available as
                # a reference.  Previously both were excluded globally, which threw
                # away valid pricing information from a sharp, liquid market.
                ref_this  = {bk: od for bk, od in side_odds.items()
                             if bk != book_name}
                ref_other = {bk: od for bk, od in other_side_odds.items()
                             if bk != book_name}

                fair_prob, pinnacle_anchored = _compute_prop_fair_prob(
                    ref_this, ref_other)
                if fair_prob is None:
                    continue   # not enough reference data for a reliable fair price

                fair_prob = max(0.05, min(0.95, fair_prob))
                ev = calc_ev(fair_prob, dec)
                # Stale-target-line guard: if the target book's implied
                # prob is wildly below the consensus (computed without
                # this book), the target's price is almost certainly
                # stale or erroneous, not a real edge — protects against
                # pathological-EV picks slipping through the boost path.
                target_implied = 1.0 / dec
                if target_implied < MIN_TARGET_OUTLIER_PROB_RATIO * fair_prob:
                    print(f"  [PlayerProp] Outlier rejected: {player} "
                          f"{market} {side} on {display} at "
                          f"{format_american(dec)} — consensus implies "
                          f"~{format_american(1.0 / fair_prob)} "
                          f"(ratio {target_implied / fair_prob:.2f}, "
                          f"likely stale)")
                    continue
                kelly      = kelly_bet(fair_prob, dec)
                # Binary Yes/No markets (anytime HR, double-double) come
                # through with line=None — label them Y/N rather than O/U.
                # Yes-only markets (HR, goalscorer) are always binary
                # regardless of whether the Odds API included a 0.5 line:
                # outcome shows just the player name (Yes side only —
                # filtered above) and point is normalized to None so the
                # HR Props tab + telegram output read the same as the
                # prior AN-flavored shape.
                is_yes_only = market in _YES_ONLY_PROP_MARKETS
                is_binary   = is_yes_only or line is None
                side_label  = (("Y" if side == "over" else "N") if is_binary
                               else ("O" if side == "over" else "U"))
                outcome     = player if is_yes_only else f"{player} {side_label}"
                edge_point  = None if is_yes_only else line
                edge_dict = {
                    "sport":             pair["sport"],
                    "game":              game,
                    "commence":          pair["commence"],
                    "book":              display,
                    "market":            market,
                    "outcome":           outcome,
                    "point":             edge_point,
                    "odds_american":     am,
                    "odds_decimal":      round(dec, 3),
                    "fair_prob":         round(fair_prob * 100, 1),
                    "implied_prob":      round(decimal_to_implied_prob(dec) * 100, 1),
                    "ev_pct":            round(ev, 2),
                    "kelly_pct":         round(kelly * 100, 2),
                    "num_other_books":   len(other_entries),
                    "best_other_odds":   (format_american(best_other_dec)
                                          if best_other_dec else "—"),
                    "pinnacle_anchored": pinnacle_anchored,
                    "all_book_odds":     all_book_display,
                    "player":            player,
                    "market_disp":       pair["market_disp"],
                    "prop_side":         side,
                    # Odds API IDs for closing-line capture
                    "event_id":          pair.get("event_id"),
                    "sport_key":         pair.get("sport_key"),
                }
                if promo_pool is not None:
                    promo_pool.append(edge_dict)
                # HR stays out of the returned (gated) list — it has its own
                # hr_edges list derived from promo_pool downstream, and mixing
                # HR into player_prop_edges would force every caller that
                # concatenates the two (write_excel, eligible_games_for_promo)
                # to filter for dedup. Pre-migration `find_hr_edges` already
                # treated HR as its own pipeline; keep that boundary.
                # Goalscorer ("goal") deliberately STAYS in the returned
                # list: it has no legacy parallel tab, and inclusion gives
                # it the Player Props sheet + CLV registration for free
                # while _qualifies_prop keeps it out of Play These.
                if market == "hr":
                    continue
                if ev >= MIN_EV_PERCENT:
                    edges.append(edge_dict)

    edges.sort(key=lambda x: x["ev_pct"], reverse=True)

    # ── Line-split diagnostic ─────────────────────────────────────────────────
    # Detect props where multiple distinct lines exist across books for the same
    # (player, market).  These are the cases that used to silently produce false
    # EV flags.  Log them so you know which props have fragmented markets.
    from collections import defaultdict as _dd
    _line_map: dict[tuple, set] = _dd(set)
    for prop in props:
        _line_map[(prop["player"], prop["game"], prop["market"])].add(
            _line_bucket(prop.get("line")))
    split_props = {k: v for k, v in _line_map.items() if len(v) > 1}
    if split_props:
        print(f"\n  ⚠  {len(split_props)} prop(s) have split lines across books "
              f"(evaluated separately per line — no cross-line EV):")
        for (player, game, market), lines in sorted(split_props.items()):
            lines_str = " vs ".join(sorted(lines, key=lambda x: float(x) if x != "none" else -99))
            mkt_disp  = next((p["market_disp"] for p in props
                              if p["player"] == player and p["market"] == market),
                             market)
            print(f"     {player}  {mkt_disp}  [{lines_str}]  ({game})")

    return edges


# ── SGP correlation guardrails ────────────────────────────────────────
# Same-game legs are never truly independent, but some pairs are so
# tightly coupled that the parlay is one bet priced twice — and those
# are exactly the combos the book's SGP engine shaves hardest. Block
# the classic near-duplicates; keep weakly-coupled combos (props on
# different players, prop + game line) where the independence math
# stays a tolerable approximation of the real joint probability.
_SGP_GAME_LINE_MARKETS = frozenset({"h2h", "spreads"})
_SGP_TOTALS_MARKETS    = frozenset({"totals", "totals_1st"})


def _sgp_legs_compatible(a: dict, b: dict) -> bool:
    """True if two same-game legs are weakly correlated enough to parlay."""
    ma = str(a.get("market", "")).lower()
    mb = str(b.get("market", "")).lower()
    # Moneyline + spread track the same final margin (same side is a
    # near-duplicate; opposite sides are near-contradictory).
    if ma in _SGP_GAME_LINE_MARKETS and mb in _SGP_GAME_LINE_MARKETS:
        return False
    # Full-game and first-half totals ride the same scoring pace.
    if ma in _SGP_TOTALS_MARKETS and mb in _SGP_TOTALS_MARKETS:
        return False
    # Two props on the same player (outcome holds the player name).
    if (ma in _PLAYER_PROP_MARKETS and mb in _PLAYER_PROP_MARKETS
            and a.get("outcome") == b.get("outcome")):
        return False
    return True


def _sgp_combo_ok(legs) -> bool:
    """True if every pair of legs in the combo passes the guardrail."""
    legs = list(legs)
    return all(_sgp_legs_compatible(legs[i], legs[j])
               for i in range(len(legs)) for j in range(i + 1, len(legs)))


def _build_parlay_rows(edges: list[dict], promos: list[dict],
                       dk_bankroll: float, fd_bankroll: float,
                       extra_edges: list[dict] | None = None,
                       parlay_pool: list = None,
                       promo_pool: list = None) -> list[dict]:
    """
    For each parlay promo, find the optimal N-leg parlay from qualifying edges.

    For ≤4 legs with ≤20 candidates: try all combinations (brute-force optimal).
    Otherwise: greedy top-N by EV%.

    Assumes legs are independent for probability math. Non-SGP parlays take
    at most one leg per game (a second same-game leg makes the book reprice
    the ticket as an SGP); SGP combos are filtered by _sgp_combo_ok so only
    weakly-correlated same-game legs combine.

    promo_pool (broader — all DK/FD outcomes, no fair_prob floor) is preferred
    over parlay_pool (fair_prob ≥ 30% favorites) so SGPs can combine mid-range-
    probability legs like +250/+300 bets that were previously excluded.
    """
    from itertools import combinations as _combinations

    pool = promo_pool if promo_pool else (parlay_pool if parlay_pool else edges)

    parlay_promos = [p for p in promos if p.get("promo_type") == "parlay"]
    if not parlay_promos:
        return []

    all_edges = list(pool) + list(extra_edges or [])

    rows = []
    for promo in parlay_promos:
        # Boosted-EV math requires a boost %. If it's missing (e.g. the
        # vision extractor couldn't resolve a tiered/stepped boost),
        # skip rather than crash — the user can re-enter it manually.
        if promo.get("boost_pct") is None:
            print(f"  Skipping parlay promo — no boost_pct: {promo.get('label')}")
            continue
        num_legs = promo.get("num_legs", 3)

        # Collect qualifying legs — NOTE: min_odds is NOT checked here.
        # For parlays the minimum-odds restriction applies to the combined
        # parlay odds, not each individual leg.  A -200 leg is perfectly
        # valid as part of a 3-leg parlay that clears +300 overall.
        qualifying = []
        seen_keys: set[str] = set()
        game_filter = promo.get("game_filter")
        for edge in all_edges:
            if edge["book"] != promo["book"]:
                continue
            if not _starts_within_promo_horizon(edge):
                continue
            if edge.get("market") == "hr":
                continue  # HR props handled separately
            # Hit-rate ruleset: voidable props only inside the lineup
            # window (same gate as straight promos) — one DNP leg voids
            # or regrades the whole parlay.
            if (edge.get("market") in _VOIDABLE_PROP_MARKETS
                    and not _starts_within_minutes(edge,
                                                   PROMO_PROP_WINDOW_MIN)):
                continue
            # Per-leg odds cap: every leg ≥ ~40% fair keeps the parlay
            # cashable; a longshot leg re-creates the moonshot problem
            # one leg at a time.
            if edge["odds_decimal"] > american_to_decimal(
                    PROMO_PARLAY_LEG_MAX_ODDS):
                continue
            # Game-specific parlay: restrict all legs to the user-picked game
            if game_filter and edge.get("game") != game_filter:
                continue
            if promo["sport"] != "all":
                if _canonical_promo_sport(edge["sport"]) != promo["sport"]:
                    continue
            if promo["market"] != "all":
                if edge["market"].lower() != promo["market"]:
                    continue
            # Dedup: keep the highest-probability outcome per game+market
            mkt = edge.get("market", "")
            if mkt in _GOLF_MARKETS or mkt in _PLAYER_PROP_MARKETS:
                dedup_key = f"{edge['game']}|{mkt}|{edge['outcome']}"
            else:
                dedup_key = f"{edge['game']}|{mkt}"
            if dedup_key in seen_keys:
                # Replace if this one has higher fair_prob
                existing = next((q for q in qualifying
                                 if (f"{q['game']}|{q.get('market','')}" == dedup_key
                                     or f"{q['game']}|{q.get('market','')}|{q['outcome']}" == dedup_key)),
                                None)
                if existing and edge.get("fair_prob", 0) > existing.get("fair_prob", 0):
                    qualifying.remove(existing)
                    qualifying.append(edge)
                continue
            seen_keys.add(dedup_key)
            qualifying.append(edge)

        # Sort by fair probability descending (best parlay legs first)
        qualifying.sort(key=lambda e: e.get("fair_prob", 0), reverse=True)

        is_sgp = promo.get("sgp", False)

        # Pre-compute the parlay-level min-odds decimal once (None = no floor)
        parlay_min_dec = (american_to_decimal(promo["min_odds"])
                          if promo.get("min_odds") is not None else None)
        # Combined-odds ceiling (hit-rate ruleset), with 1.5× headroom
        # above the promo's own min-odds term so a "+700 min odds"
        # promo stays satisfiable instead of producing zero parlays.
        total_cap_dec = max(
            american_to_decimal(PROMO_PARLAY_TOTAL_MAX_ODDS),
            (parlay_min_dec or 1.0) * 1.5)
        bankroll = (dk_bankroll if promo["book"] == "DraftKings"
                    else fd_bankroll)

        # A promo with count=N means the user has N identical copies —
        # produce N distinct parlays by excluding each copy's legs from
        # the pool available to the next copy. Scoped per-promo so two
        # different parlay promos don't steal each other's legs.
        copies = max(1, int(promo.get("count", 1) or 1))
        used_leg_keys: set = set()
        rows_this_promo: list[dict] = []

        for copy_idx in range(copies):
            # Filter out legs already used by earlier copies of THIS promo.
            qf = [e for e in qualifying
                  if _make_pick_key(e) not in used_leg_keys]

            # Re-derive the pools to try (SGP groups each copy; non-SGP
            # just re-caps the filtered list).
            if is_sgp:
                game_groups: dict[str, list] = {}
                for edge in qf:
                    g = edge.get("game", "")
                    game_groups.setdefault(g, []).append(edge)
                for g in list(game_groups):
                    game_groups[g] = game_groups[g][:15]
                sgp_candidates = [(g, legs)
                                  for g, legs in game_groups.items()
                                  if len(legs) >= num_legs]
                if not sgp_candidates:
                    continue
                pools_to_try = [(legs, g) for g, legs in sgp_candidates]
            else:
                qf_capped = qf[:30]
                if len(qf_capped) < num_legs:
                    continue
                pools_to_try = [(qf_capped, None)]

            # Find the SINGLE best parlay for this copy — across every
            # candidate game (SGP) and every try_legs count.
            best_overall_combo = None
            best_overall_ev    = -float("inf")
            best_overall_legs  = None

            for candidate_pool, _game_name in pools_to_try:
                max_try_legs = min(num_legs + 3, len(candidate_pool))
                for try_legs in range(num_legs, max_try_legs + 1):
                    if len(candidate_pool) < try_legs:
                        break

                    combo_candidate = None
                    combo_ev = -float("inf")

                    if try_legs <= 4 and len(candidate_pool) <= 20:
                        for combo in _combinations(candidate_pool, try_legs):
                            if is_sgp and not _sgp_combo_ok(combo):
                                continue
                            # Non-SGP: one leg per game — a second same-game
                            # leg makes the book reprice the ticket as an SGP
                            # and the quoted payout stops matching the math.
                            if not is_sgp and len({l["game"] for l in combo}
                                                  ) < try_legs:
                                continue
                            parlay_dec = 1.0
                            parlay_fair = 1.0
                            for leg in combo:
                                parlay_dec  *= leg["odds_decimal"]
                                parlay_fair *= leg["fair_prob"] / 100
                            if (parlay_min_dec is not None
                                    and parlay_dec < parlay_min_dec):
                                continue
                            if parlay_dec > total_cap_dec:
                                continue
                            boost_dec = apply_profit_boost(
                                parlay_dec, promo["boost_pct"])
                            ev = calc_ev(parlay_fair, boost_dec)
                            if ev > combo_ev:
                                combo_ev    = ev
                                combo_candidate = combo
                    else:
                        # Greedy: top-N by fair probability. SGP skips
                        # legs that correlate with one already taken.
                        if is_sgp:
                            picked: list[dict] = []
                            for cand in candidate_pool:
                                if all(_sgp_legs_compatible(cand, c)
                                       for c in picked):
                                    picked.append(cand)
                                if len(picked) == try_legs:
                                    break
                            greedy = (tuple(picked)
                                      if len(picked) == try_legs else None)
                        else:
                            # One leg per game (same reprice reason as the
                            # brute-force path above).
                            picked = []
                            seen_games: set = set()
                            for cand in candidate_pool:
                                g = cand.get("game")
                                if g in seen_games:
                                    continue
                                picked.append(cand)
                                seen_games.add(g)
                                if len(picked) == try_legs:
                                    break
                            greedy = (tuple(picked)
                                      if len(picked) == try_legs else None)
                        if greedy is not None:
                            parlay_dec = 1.0
                            parlay_fair = 1.0
                            for leg in greedy:
                                parlay_dec  *= leg["odds_decimal"]
                                parlay_fair *= leg["fair_prob"] / 100
                            if ((parlay_min_dec is None
                                    or parlay_dec >= parlay_min_dec)
                                    and parlay_dec <= total_cap_dec):
                                boost_dec = apply_profit_boost(
                                    parlay_dec, promo["boost_pct"])
                                combo_ev = calc_ev(parlay_fair, boost_dec)
                                combo_candidate = greedy

                    if (combo_candidate is not None
                            and combo_ev > best_overall_ev):
                        best_overall_combo = combo_candidate
                        best_overall_ev    = combo_ev
                        best_overall_legs  = try_legs

            if best_overall_combo is None or best_overall_ev <= 0:
                continue

            # Mark these legs used so the next copy picks a different parlay
            for leg in best_overall_combo:
                used_leg_keys.add(_make_pick_key(leg))

            parlay_dec  = 1.0
            parlay_fair = 1.0
            for leg in best_overall_combo:
                parlay_dec  *= leg["odds_decimal"]
                parlay_fair *= leg["fair_prob"] / 100

            boost_dec    = apply_profit_boost(parlay_dec, promo["boost_pct"])
            boosted_ev   = calc_ev(parlay_fair, boost_dec)
            unboosted_ev = calc_ev(parlay_fair, parlay_dec)
            boosted_kelly = kelly_bet(parlay_fair, boost_dec)

            bet = clamp_bet(bankroll * boosted_kelly, promo["book"])
            if promo.get("max_bet") and bet > promo["max_bet"]:
                bet = promo["max_bet"]
            profit = bet * (boost_dec - 1)

            rows_this_promo.append({
                "promo_label":      promo["label"],
                "promo":            promo,
                "legs":             list(best_overall_combo),
                "num_legs":         best_overall_legs,
                "parlay_decimal":   round(parlay_dec, 3),
                "parlay_american":  format_american(parlay_dec),
                "parlay_fair_prob": round(parlay_fair * 100, 3),
                "boost_dec":        round(boost_dec, 3),
                "boost_american":   format_american(boost_dec),
                "unboosted_ev":     round(unboosted_ev, 2),
                "boosted_ev":       round(boosted_ev, 2),
                "boosted_kelly":    round(boosted_kelly * 100, 2),
                "bet":              round(bet, 2),
                "profit":           round(profit, 2),
                "copy_idx":         copy_idx,
                "copy_of":          copies,
            })

        if rows_this_promo:
            rows.extend(rows_this_promo)
        else:
            # Nothing qualifying for this parlay promo — emit a placeholder
            # so the user sees the promo was considered (and understands
            # why nothing came back: pool too small, not enough legs, or
            # even the boosted combined EV was negative).
            rows.append({
                "promo_label": promo["label"],
                "promo":       promo,
                "legs":        None,
                "num_legs":    0,
                "boosted_ev":  float("-inf"),
                "copy_idx":    0,
                "copy_of":     copies,
            })

    rows.sort(key=lambda x: x["boosted_ev"], reverse=True)
    return rows


# ──────────────────────────────────────────────────────────────────────
# Promo / Boost helpers
# ──────────────────────────────────────────────────────────────────────

def apply_profit_boost(decimal_odds: float, boost_pct: float) -> float:
    """
    Apply a profit boost to decimal odds.
    A 50% profit boost on decimal 2.50 (profit = 1.50):
        boosted profit = 1.50 * 1.50 = 2.25 → boosted decimal = 3.25
    """
    profit = decimal_odds - 1
    boosted_profit = profit * (1 + boost_pct / 100)
    return 1 + boosted_profit


# Valid sport labels that promos can target
PROMO_SPORTS = {
    "all":     "All Sports",
    "nba":     "NBA",
    "mlb":     "MLB",
    "nhl":     "NHL",
    "nfl":     "NFL",
    "ncaab":   "NCAAB",
    "ncaaf":   "NCAAF",
    "soccer":          "Soccer",
    "european_soccer": "European Soccer",
    "world_cup":       "World Cup",
    "ufc":     "UFC",
    "pga":     "PGA Tour",
    "wnba":    "WNBA",
    "afl":     "AFL",
    "boxing":  "Boxing",
    "cricket": "Cricket",
    "tennis":  "Tennis",
}

# Valid market labels
PROMO_MARKETS = {
    "all":         "All Markets",
    "h2h":         "Moneyline",
    "spreads":     "Spreads",
    "totals":      "Totals",
    # MLB props
    "hr":          "Anytime HR",
    "strikeouts":  "Strikeouts",
    "total_bases": "Total Bases",
    "hits":        "Hits",
    "rbis":        "RBIs",
    # NBA props
    "pts":         "NBA Points",
    "reb":         "NBA Rebounds",
    "ast":         "NBA Assists",
    "threes":      "NBA 3-Pointers",
    "pra":         "NBA Pts+Reb+Ast",
    "dd":          "NBA Double-Double",
    # NHL props
    "sog":         "NHL Shots on Goal",
    "nhl_pts":     "NHL Points",
    # Soccer props
    "goal":        "Anytime Goalscorer",
    # Golf props
    "golf_top5":   "Golf Top 5 Finish",
    "golf_top10":  "Golf Top 10 Finish",
    "golf_top20":  "Golf Top 20 Finish",
    "golf_win":    "Golf Outright Win",
}


class GamePickerDialog(tk.Toplevel):
    """
    Shown after odds are fetched.  For each promo flagged game_specific=True,
    lets the user choose which game on the slate the boost applies to.
    The selected game string is written back into each promo as 'game_filter'.
    """

    def __init__(self, parent, game_specific_promos: list[dict],
                 all_edges: list[dict], player_prop_edges: list[dict]):
        super().__init__(parent)
        self.title("Odds Compare — Select Game for Each Promo")
        self.attributes("-topmost", True)
        self.grab_set()
        self._selections: list[tuple[dict, tk.StringVar]] = []

        tk.Label(self, text="Game-Specific Promo — Pick Your Game",
                 font=("Helvetica", 14, "bold")).pack(pady=(14, 4))
        tk.Label(self,
                 text=("For each game-specific promo below, select the game "
                       "it applies to.  Only edges from that game will be "
                       "considered when choosing the best pick."),
                 font=("Helvetica", 10), fg="gray",
                 wraplength=620, justify="left").pack(padx=20, pady=(0, 12))

        body = tk.Frame(self)
        body.pack(padx=20, fill="x")

        combined_edges = list(all_edges) + list(player_prop_edges)

        for promo in game_specific_promos:
            # Build the game list: edges that pass every promo filter except
            # game_filter itself (which hasn't been set yet).
            eligible_games = sorted({
                e["game"] for e in combined_edges
                if _promo_matches(promo, e)
            })
            if not eligible_games:
                # Fallback: show all games (unusual but safe)
                eligible_games = sorted({e["game"] for e in combined_edges})
            if not eligible_games:
                continue

            # Promo label
            lbl_text = promo["label"]
            if len(lbl_text) > 70:
                lbl_text = lbl_text[:67] + "..."
            tk.Label(body, text=lbl_text,
                     font=("Helvetica", 10, "bold"),
                     anchor="w").pack(fill="x", pady=(10, 1))

            game_var = tk.StringVar(value=eligible_games[0])
            menu = tk.OptionMenu(body, game_var, *eligible_games)
            menu.config(width=62, anchor="w")
            menu.pack(fill="x", pady=(0, 4))

            self._selections.append((promo, game_var))

        tk.Button(self, text="Confirm", command=self._on_confirm,
                  width=14, font=("Helvetica", 12)).pack(pady=16)
        self.protocol("WM_DELETE_WINDOW", self._on_confirm)

    def _on_confirm(self):
        for promo, game_var in self._selections:
            selected = game_var.get().strip()
            if selected:
                promo["game_filter"] = selected
                # Append game name to the label so it shows in the sheet
                game_short = selected[:40] + ("…" if len(selected) > 40 else "")
                promo["label"] = promo["label"].rstrip(")") + f" [{game_short}]"
        self.destroy()


def ask_game_filters(parent, promos: list[dict],
                     all_edges: list[dict],
                     player_prop_edges: list[dict] | None = None) -> None:
    """
    If any promo is flagged game_specific=True, show the GamePickerDialog so
    the user can select which game each boost applies to.  Updates promos
    in-place with 'game_filter'.  No-op if none are game-specific.
    """
    targets = [p for p in promos if p.get("game_specific")]
    if not targets:
        return
    dlg = GamePickerDialog(parent, targets, all_edges, player_prop_edges or [])
    parent.wait_window(dlg)


class PromoDialog(tk.Toplevel):
    """Dialog for entering active promotional boosts."""

    def __init__(self, parent):
        super().__init__(parent)
        self.title("Odds Compare — Active Promos")
        self.attributes("-topmost", True)
        self.promos = []
        self._cancelled = False

        # ── Instructions ─────────────────────────────────────────────
        tk.Label(self, text="Enter Active Promos / Profit Boosts",
                 font=("Helvetica", 14, "bold")).grid(
                     row=0, column=0, columnspan=10, pady=(10, 2))
        tk.Label(self, text="(Leave blank and click Done if you have none)",
                 font=("Helvetica", 10), fg="gray").grid(
                     row=1, column=0, columnspan=10, pady=(0, 2))
        tk.Label(self,
                 text=("Min Odds: American odds floor for the promo to apply  "
                       "(e.g. -200 = must be -200 or better/longer).  "
                       "Leave blank for no restriction.  |  "
                       "Type: Straight = single bet,  Parlay = multi-leg combo,  "
                       "Bonus Bet = risk-free credit (enter $ amount, keep profit only).  |  "
                       "Game?: check to restrict this boost to one specific game "
                       "(you will pick from the slate after odds are fetched)."),
                 font=("Helvetica", 9), fg="gray").grid(
                     row=2, column=0, columnspan=10, pady=(0, 8))

        # ── Column headers ───────────────────────────────────────────
        headers = ["Book", "Sport", "Market", "Type", "Legs",
                   "Amount ($)", "Boost %", "Min Odds", "Max Bet ($)", "Game?"]
        for c, h in enumerate(headers):
            tk.Label(self, text=h, font=("Helvetica", 11, "bold")).grid(
                row=3, column=c, padx=5, pady=2)

        # ── Input rows ───────────────────────────────────────────────
        self._rows = []
        for i in range(8):  # 8 promo slots
            self._add_row(i + 4)

        # ── Buttons ──────────────────────────────────────────────────
        btn_frame = tk.Frame(self)
        btn_frame.grid(row=13, column=0, columnspan=10, pady=15)
        tk.Button(btn_frame, text="Done", command=self._on_done,
                  width=12, font=("Helvetica", 12)).pack(side=tk.LEFT, padx=10)
        tk.Button(btn_frame, text="Skip (no promos)", command=self._on_skip,
                  width=16, font=("Helvetica", 10)).pack(side=tk.LEFT, padx=10)

        self.protocol("WM_DELETE_WINDOW", self._on_skip)
        self.grab_set()

    def _add_row(self, grid_row):
        # Book dropdown
        book_var = tk.StringVar(value="DraftKings")
        book_menu = tk.OptionMenu(self, book_var, "DraftKings", "FanDuel")
        book_menu.config(width=10)
        book_menu.grid(row=grid_row, column=0, padx=3)

        # Sport dropdown
        sport_var = tk.StringVar(value="All Sports")
        sport_menu = tk.OptionMenu(self, sport_var, *PROMO_SPORTS.values())
        sport_menu.config(width=10)
        sport_menu.grid(row=grid_row, column=1, padx=3)

        # Market dropdown
        mkt_var = tk.StringVar(value="All Markets")
        mkt_menu = tk.OptionMenu(self, mkt_var, *PROMO_MARKETS.values())
        mkt_menu.config(width=10)
        mkt_menu.grid(row=grid_row, column=2, padx=3)

        # Type dropdown: Straight, Parlay, or Bonus Bet
        type_var = tk.StringVar(value="Straight")
        type_menu = tk.OptionMenu(self, type_var, "Straight", "Parlay",
                                  "Bonus Bet")
        type_menu.config(width=10)
        type_menu.grid(row=grid_row, column=3, padx=3)

        # Legs (Parlay only)
        legs_entry = tk.Entry(self, width=5, justify="center")
        legs_entry.insert(0, "3")

        # Amount $ (Bonus Bet only)
        amount_entry = tk.Entry(self, width=8, justify="center",
                                fg="darkgreen")

        # Boost %  (Straight / Parlay only)
        boost_entry = tk.Entry(self, width=8, justify="center")
        boost_entry.grid(row=grid_row, column=6, padx=3)

        # Min odds (American, optional — all types)
        # Created here so tab order goes Boost → Min Odds → Max Bet (left to right)
        min_odds_entry = tk.Entry(self, width=8, justify="center")
        min_odds_entry.grid(row=grid_row, column=7, padx=3)

        # Max bet  (Straight / Parlay only)
        max_entry = tk.Entry(self, width=10, justify="center")
        max_entry.grid(row=grid_row, column=8, padx=3)

        # Game specific (checked = restrict this promo to one game from the slate)
        game_specific_var = tk.BooleanVar(value=False)
        game_chk = tk.Checkbutton(self, variable=game_specific_var)
        game_chk.grid(row=grid_row, column=9, padx=3)

        def _on_type_change(*_args, _le=legs_entry, _ae=amount_entry,
                            _be=boost_entry, _me=max_entry,
                            _tv=type_var, _row=grid_row):
            t = _tv.get()
            if t == "Parlay":
                if not _le.get().strip():
                    _le.delete(0, tk.END); _le.insert(0, "3")
                _le.grid(row=_row, column=4, padx=3)
                _ae.grid_remove()
                _be.grid(row=_row, column=6, padx=3)
                _me.grid(row=_row, column=8, padx=3)
            elif t == "Bonus Bet":
                _le.grid_remove()
                _ae.grid(row=_row, column=5, padx=3)
                _be.grid_remove()
                _me.grid_remove()
            else:   # Straight
                _le.grid_remove()
                _ae.grid_remove()
                _be.grid(row=_row, column=6, padx=3)
                _me.grid(row=_row, column=8, padx=3)

        type_var.trace_add("write", _on_type_change)

        self._rows.append((book_var, sport_var, mkt_var, type_var,
                           legs_entry, amount_entry, boost_entry,
                           min_odds_entry, max_entry, game_specific_var))

    def _on_done(self):
        for (book_var, sport_var, mkt_var, type_var,
             legs_e, amount_e, boost_e, min_odds_e, max_e,
             game_specific_var) in self._rows:

            book          = book_var.get()
            promo_type    = type_var.get().lower().replace(" ", "_")  # "straight"/"parlay"/"bonus_bet"
            sport_display = sport_var.get()
            sport_key     = next((k for k, v in PROMO_SPORTS.items()
                                  if v == sport_display), "all")
            mkt_display   = mkt_var.get()
            mkt_key       = next((k for k, v in PROMO_MARKETS.items()
                                  if v == mkt_display), "all")

            min_odds_str = min_odds_e.get().strip()
            try:
                min_odds = int(float(min_odds_str)) if min_odds_str else None
            except ValueError:
                min_odds = None
            mo_lbl  = (f" (min {'+' if min_odds >= 0 else ''}{min_odds})"
                       if min_odds is not None else "")
            mkt_lbl = f" / {mkt_display}" if mkt_key != "all" else ""

            # ── Bonus Bet ─────────────────────────────────────────────
            if promo_type == "bonus_bet":
                amt_str = amount_e.get().strip()
                if not amt_str:
                    continue
                try:
                    bet_amount = float(amt_str)
                except ValueError:
                    continue
                if bet_amount <= 0:
                    continue
                label = (f"{book} ${bet_amount:.0f} bonus bet"
                         f" — {sport_display}{mkt_lbl}{mo_lbl}")
                self.promos.append({
                    "book":          book,
                    "sport":         sport_key,
                    "market":        mkt_key,
                    "promo_type":    "bonus_bet",
                    "bet_amount":    bet_amount,
                    "min_odds":      min_odds,
                    "max_bet":       None,
                    "game_specific": game_specific_var.get(),
                    "label":         label,
                })
                continue

            # ── Straight / Parlay ─────────────────────────────────────
            boost_str = boost_e.get().strip()
            if not boost_str:
                continue
            try:
                boost_pct = float(boost_str)
            except ValueError:
                continue
            if boost_pct <= 0:
                continue

            max_str = max_e.get().strip()
            max_bet = float(max_str) if max_str else None

            legs_str = legs_e.get().strip()
            try:
                num_legs = max(2, int(legs_str)) if legs_str else 3
            except ValueError:
                num_legs = 3

            mb_lbl = f" (max ${max_bet:.0f})" if max_bet else ""
            if promo_type == "parlay":
                label = (f"{book} {boost_pct:.0f}% {num_legs}-leg parlay boost"
                         f" — {sport_display}{mkt_lbl}{mo_lbl}{mb_lbl}")
            else:
                label = (f"{book} {boost_pct:.0f}% boost"
                         f" — {sport_display}{mkt_lbl}{mo_lbl}{mb_lbl}")

            self.promos.append({
                "book":          book,
                "sport":         sport_key,
                "market":        mkt_key,
                "promo_type":    promo_type,
                "num_legs":      num_legs,
                "boost_pct":     boost_pct,
                "min_odds":      min_odds,
                "max_bet":       max_bet,
                "game_specific": game_specific_var.get(),
                "label":         label,
            })

        self.destroy()

    def _on_skip(self):
        self.promos = []
        self.destroy()


def ask_promos(parent) -> list[dict]:
    """Show the promo entry dialog and return list of active promos."""
    dlg = PromoDialog(parent)
    parent.wait_window(dlg)
    return dlg.promos


# Odds API sport_keys for the European soccer leagues — used to filter
# promos with sport="european_soccer" (e.g. "fd 25% epl boost"). Edges
# get their sport_key from the Odds API event payload; Action Network
# fallback edges have sport_key=None and are excluded from this filter
# (we can't prove they're European leagues, so it's safer to skip).
EUROPEAN_SOCCER_SPORT_KEYS = frozenset({
    "soccer_epl",
    "soccer_uefa_champs_league",
    "soccer_uefa_europa_league",
    "soccer_uefa_europa_conference_league",
    "soccer_spain_la_liga",
    "soccer_italy_serie_a",
    "soccer_germany_bundesliga",
    "soccer_france_ligue_one",
})

# Odds API sport_keys eligible for sport="world_cup" promos (e.g.
# "dk 50% world cup" — tournament-wide boosts for the 2026 FIFA World
# Cup). Exact-match on purpose: the Odds API also exposes
# soccer_fifa_club_world_cup, soccer_fifa_world_cup_qualifiers_*,
# soccer_fifa_world_cup_womens and soccer_fifa_world_cup_winner
# (outrights) as SEPARATE keys, and a World Cup promo's terms cover none
# of them — so no prefix matching here.
WORLD_CUP_SPORT_KEYS = frozenset({
    "soccer_fifa_world_cup",
})

# Promo sports that narrow generic soccer edges to a subset of Odds API
# sport_keys. Maps promo-sport key → eligible sport_key set. Drives
# _promo_matches (edge filter), _derive_promo_scope (collapse to the
# "soccer" fetch) and _promo_no_pick_reason (subset-aware diagnostics).
_SOCCER_SUBSET_PROMOS: dict[str, frozenset[str]] = {
    "european_soccer": EUROPEAN_SOCCER_SPORT_KEYS,
    "world_cup":       WORLD_CUP_SPORT_KEYS,
}

# Diagnostic wording per subset promo-sport:
# (phrase for "none from …", short label for "All N … candidates").
_SOCCER_SUBSET_LABELS = {
    "european_soccer": ("European leagues", "European soccer"),
    "world_cup":       ("the FIFA World Cup", "World Cup"),
}


def _format_wc_window_msg(events: list[dict], book: str,
                          now: datetime | None = None) -> str | None:
    """Build the "World Cup odds are up but no match is inside the pick
    window yet" message from a soccer_fifa_world_cup /events payload.

    During a tournament the fixture list spans weeks, so a late-night
    run the day before a match finds 72 events fetched and zero edges —
    the generic "soccer fetch returned 0 events" diagnostic reads like
    a breakage when the real story is "come back within 24h of
    kickoff". Returns None when timing is NOT the explanation (no
    future fixtures, or the soonest game is already inside the promo
    window) so callers fall through to the generic subset diagnostics.
    Split from the network probe for unit-testability.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    soonest = None  # (commence_dt, away, home)
    for ev in events:
        try:
            ct = datetime.fromisoformat(
                str(ev.get("commence_time", "")).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            continue
        if ct <= now:
            continue
        if soonest is None or ct < soonest[0]:
            soonest = (ct, ev.get("away_team", "?"), ev.get("home_team", "?"))
    if soonest is None:
        return None
    ct, away, home = soonest
    window = timedelta(days=MAX_PROMO_DAYS)
    if ct - now <= window:
        return None   # in window — absence has some other cause
    hours_out  = (ct - now).total_seconds() / 3600.0
    when_str   = ct.astimezone(EASTERN).strftime("%m/%d %I:%M %p %Z")
    unlock_str = ((ct - window).astimezone(EASTERN)
                  .strftime("%m/%d %I:%M %p %Z"))
    return (f"World Cup odds are up ({len(events)} matches on the board) "
            f"but the next match ({away} @ {home}, {when_str}) is "
            f"{hours_out:.1f}h out — beyond the {MAX_PROMO_DAYS}-day promo "
            f"pick window. Re-run after {unlock_str} and the {book} "
            f"World Cup picks will populate.")


def _wc_next_game_diagnostic(book: str) -> str | None:
    """Probe upcoming World Cup fixtures (free /events call — 0 credits)
    when a world_cup promo found nothing to pick. Returns the timing
    message, or None when timing isn't the explanation (no fixtures,
    probe failure, or a game already inside the window) — callers fall
    through to the generic subset messages.
    """
    from bot_config import ODDS_API_KEY
    if not ODDS_API_KEY:
        return None   # offline / test environment — skip the probe
    try:
        r = requests.get(
            f"{ODDS_API_BASE}/sports/soccer_fifa_world_cup/events",
            params={"apiKey": ODDS_API_KEY},
            timeout=10,
        )
        if r.status_code != 200:
            return None
        events = r.json() or []
    except Exception:
        return None
    return _format_wc_window_msg(events, book)

# Maps league names from AN to our canonical sport keys for promo matching
_LEAGUE_TO_SPORT = {
    "nba": "nba", "mlb": "mlb", "nhl": "nhl", "nfl": "nfl",
    "ncaab": "ncaab", "ncaaf": "ncaaf", "ufc": "ufc",
    # The Odds API titles the mma_mixed_martial_arts key "MMA", so every
    # UFC edge from the primary feed canonicalises from "MMA" — without
    # this alias it stayed "mma" and never matched a ufc-scoped promo
    # (sport="ufc"), silently zeroing out UFC promo picks. Same migration
    # gap that broke tennis (see _canonical_promo_sport). The direct
    # DK/FD scrapers tag sport.upper()="UFC" and already resolved fine.
    "mma": "ufc",
    "mls": "soccer", "ligue1": "soccer", "epl": "soccer",
    "laliga": "soccer", "bundesliga": "soccer", "seriea": "soccer",
    "soccer": "soccer",
    "championsleague": "soccer", "uefachampions": "soccer",
    "europaleague": "soccer", "uefaeuropa": "soccer",
    "ligamx": "soccer",
    # WC goalscorer prop edges carry sport="WORLD_CUP" (the prop-sport
    # slug, uppercased by _merge_raw_props) rather than the Odds API
    # title "FIFA World Cup" the game-line edges get — canonicalise to
    # soccer so the subset filter's sport_key check takes over.
    "world_cup": "soccer",
    "pga": "pga",
    # New API-only sports
    "wnba": "wnba",
    "afl": "afl", "aussierules": "afl",
    "boxing": "boxing",
    "cricket": "cricket", "ipl": "cricket",
    "tennis": "tennis", "atp": "tennis", "wta": "tennis",
}


def _canonical_promo_sport(raw_sport: str) -> str:
    """Collapse an edge's raw sport label to a canonical promo-sport key.

    Most leagues map 1:1 via _LEAGUE_TO_SPORT, but tennis is special: The
    Odds API tags each event with the *tournament* title ("ATP Italian
    Open", "WTA Madrid Open") rather than a flat "Tennis". A literal
    _LEAGUE_TO_SPORT lookup ("atpitalianopen") therefore misses, the edge
    never canonicalises to "tennis", and every tennis-targeted promo
    silently matches nothing. Collapse any "atp…"/"wta…" title to "tennis"
    first (the same rule backtest.py uses when grading), then fall back to
    the league map. Returns the normalised key unchanged when nothing
    matches — mirroring the previous `.get(key, key)` default so non-tennis
    sports behave exactly as before.
    """
    key = raw_sport.lower().replace(" ", "")
    if key.startswith("atp") or key.startswith("wta"):
        return "tennis"
    # FIFA competition titles ("FIFA World Cup", "FIFA Club World Cup",
    # the qualifiers) are all soccer; collapse like the ATP/WTA rule
    # above so generic soccer promos match them. World-Cup-only
    # narrowing happens at the sport_key level in _promo_matches, not
    # here.
    if key.startswith("fifa"):
        return "soccer"
    return _LEAGUE_TO_SPORT.get(key, key)


# Golf market internal keys (for quick membership tests)
_GOLF_MARKETS = {"golf_top5", "golf_top10", "golf_top20", "golf_win"}


def _meets_min_odds(promo: dict, edge: dict) -> bool:
    """Return True if the edge's odds satisfy the promo's minimum odds requirement."""
    min_odds = promo.get("min_odds")
    if min_odds is None:
        return True
    # Convert the American floor to decimal and compare.
    # e.g. min_odds=-200 → decimal=1.5; edge must be at least that long.
    min_dec = american_to_decimal(min_odds)
    return edge["odds_decimal"] >= min_dec


def _is_draw_insurable(edge: dict) -> bool:
    """True if `edge` is a 3-way moneyline pick on a TEAM (not the Draw)
    that carries a draw probability — i.e. a pick a money-back-on-draw
    promo can insure. 2-way moneylines (no draw_fair_prob) and the Draw
    outcome itself are excluded."""
    return (edge.get("market") == "h2h"
            and edge.get("draw_fair_prob") is not None
            and str(edge.get("outcome", "")).strip().lower() != "draw")


def _draw_refund_ev_pct(promo: dict, edge: dict) -> float:
    """Extra EV% a money-back-on-draw promo adds to a moneyline pick.

    Refunding the stake on a draw (instead of losing it) is worth exactly
    P(draw) in stake units, so the boost to EV% is the draw's fair
    probability. Returns 0 when the promo isn't draw-insured or the edge
    isn't an insurable 3-way team moneyline."""
    if not promo.get("refund_on_draw"):
        return 0.0
    if not _is_draw_insurable(edge):
        return 0.0
    refund = edge["draw_fair_prob"]   # stored as a percent, like fair_prob
    # A "drawbreaker" refunds the stake as a BONUS bet (worth only its
    # winnings-only cash-equivalent), so discount the refund to its retention
    # value. Plain cash money-back-on-draw keeps the full P(draw).
    if promo.get("refund_as_bonus"):
        refund *= DRAW_REFUND_BONUS_RETENTION
    return refund


def _promo_kelly(promo: dict, edge: dict, dec: float, p_win: float,
                 fraction: float = KELLY_FRACTION) -> float:
    """Kelly stake fraction for a promo pick at decimal odds `dec`.

    DNB-aware: a money-back-on-draw pick has three outcomes — win (prob
    p_win, return b=dec-1), push/draw (prob p_draw, stake refunded), and
    loss (prob p_opp = 1 - p_win - p_draw). Maximising E[log] over those
    gives f* = (p_win*b - p_opp) / (b*(p_win + p_opp)); the push drops out
    of the growth term, which both lifts the stake (a draw can't lose) and
    shrinks the at-risk base (p_win + p_opp < 1). Reduces to the plain
    two-way Kelly when p_draw = 0. Falls back to kelly_bet otherwise."""
    if promo.get("refund_on_draw") and _is_draw_insurable(edge):
        b = dec - 1
        if b <= 0:
            return 0.0
        p_draw = edge["draw_fair_prob"] / 100
        p_opp  = max(0.0, 1 - p_win - p_draw)
        denom  = b * (p_win + p_opp)
        if denom <= 0:
            return 0.0
        return max(0.0, (p_win * b - p_opp) / denom) * fraction
    return kelly_bet(p_win, dec, fraction)


def _promo_matches(promo: dict, edge: dict,
                   ignore_prop_window: bool = False) -> bool:
    """Check if an edge qualifies for a straight promo.

    Parlay promos are handled by the dedicated Parlay Promos tab.
    HR props are handled by the dedicated HR Props tab.

    ignore_prop_window=True skips the voidable-prop lineup-window gate —
    used by _promo_no_pick_reason to tell "no candidates exist" apart
    from "candidates exist but it's too early to trust they'll play".
    """
    if promo["book"] != edge["book"]:
        return False
    # Per-sport horizon: 1 day for daily sports, longer for weekly combat
    # cards so a planned UFC/boxing promo surfaces ahead of the event.
    if not _starts_within_promo_horizon(edge):
        return False
    # Game-specific promos: restrict to the one game the user selected
    game_filter = promo.get("game_filter")
    if game_filter and edge.get("game") != game_filter:
        return False
    # Parlay promos use a separate optimisation path
    if promo.get("promo_type") == "parlay":
        return False
    # HR props only flow through to promos that *explicitly* target HR
    # (market="hr"). A generic all-market boost would otherwise silently
    # grab an HR prop when the user almost always meant a game line.
    # Explicit HR-targeted promos (e.g. "fd 50% hr") still also appear on
    # the dedicated HR Props tab via _build_hr_boost_rows.
    if edge.get("market") == "hr" and promo["market"] != "hr":
        return False
    # Anytime-goalscorer props flow ONLY to promos that explicitly name the
    # market ("dk 30% goals", "fd 25% anytime goalscorer" — a book token
    # usable on nothing else, which still finds the least-bad goalscorer).
    # Everything else is kept off them, including tournament-wide World Cup
    # boosts (market="all"): the user finds goalscorer props mispriced
    # regardless of cross-book EV and doesn't want the picker electing one
    # when it had a settle-always line to take. (A WC boost used to admit
    # them as its "canonical" play; that exception is gone.)
    if edge.get("market") == "goal" and promo["market"] != "goal":
        return False
    # Money-back-on-draw promos refund the stake only on a 3-way moneyline
    # draw, so they can ONLY land on a moneyline team pick that carries a
    # draw probability. Reject everything else: non-h2h markets, 2-way
    # moneylines (no draw to insure), and the Draw outcome itself (a draw
    # bet can't be "money back if it draws").
    if promo.get("refund_on_draw") and not _is_draw_insurable(edge):
        return False
    # Voidable-prop lineup window (see PROMO_PROP_WINDOW_MIN). Applies
    # ONLY when the promo has a settle-always fallback worth waiting for:
    # an all-market / game-line promo defers a voidable prop until lineups
    # are out so it prefers a settle-always line early. A promo with no
    # such fallback (see _promo_picks_prop_early: a prop-TARGETED promo)
    # stays eligible and the picker flags participation_warn instead —
    # gating it would just risk wasting the token.
    if not ignore_prop_window:
        if (edge.get("market") in _VOIDABLE_PROP_MARKETS
                and not _promo_picks_prop_early(promo, edge)
                and not _starts_within_minutes(edge, PROMO_PROP_WINDOW_MIN)):
            return False
    if promo["sport"] != "all":
        canonical = _canonical_promo_sport(edge["sport"])
        # Soccer-subset promos (european_soccer, world_cup) narrow
        # generic soccer edges to a fixed set of Odds API sport_keys.
        # Edges without sport_key (e.g. AN-fallback) are conservatively
        # excluded — without league metadata we can't prove eligibility.
        subset_keys = _SOCCER_SUBSET_PROMOS.get(promo["sport"])
        if subset_keys is not None:
            if canonical != "soccer":
                return False
            if edge.get("sport_key") not in subset_keys:
                return False
        elif canonical != promo["sport"]:
            return False
    if promo["market"] != "all":
        if edge["market"].lower() != promo["market"]:
            return False
    if not _meets_min_odds(promo, edge):
        return False
    return True


def _promo_window_stage(edge: dict,
                        windows: list[tuple[int, int]]) -> int | None:
    """Index of the first (tightest) odds window containing the edge.

    Windows are nested loosest-last, so the first hit is the stage the
    pick gets tagged with: 0 = target band, higher = relaxed."""
    dec = edge["odds_decimal"]
    for i, (lo, hi) in enumerate(windows):
        if american_to_decimal(lo) <= dec <= american_to_decimal(hi):
            return i
    return None


def select_promo_edge(promo: dict,
                      pool: list[dict]) -> tuple[dict | None, int | None]:
    """Pick the edge for a straight promo under the hit-rate ruleset.

    pool must already be filtered by _promo_matches (and deduped by the
    caller). Returns (edge, band_stage):

      0                  — target band (PROMO_ODDS_WINDOWS[...][0])
      1..len(windows)-1  — relaxed window; the target band was empty
      len(windows)       — outside every window (e.g. a promo whose
                           min-odds term sits above all ceilings);
                           takes the SHORTEST qualifying odds — the
                           most cashable thing available
      (None, None)       — empty pool

    Ranking inside a window keeps the +EV ethos: bonus bets by expected
    profit, boosts by boosted EV. The window caps variance; EV picks
    within it. For boosts a window only wins outright when its best
    pick clears MIN_EV_PERCENT post-boost — otherwise later windows get
    a shot, and if nothing clears anywhere the earliest non-empty
    window's best is returned (an in-band slightly-negative pick beats
    an off-band moonshot for cashing; the caller flags it is_fallback)."""
    if not pool:
        return None, None
    is_bonus = promo.get("promo_type") == "bonus_bet"
    windows = PROMO_ODDS_WINDOWS["bonus_bet" if is_bonus else "boost"]

    if is_bonus:
        amt = promo.get("bet_amount", 0) or 0

        def rank(e):
            # Expected $ profit of the free bet (stake never returns).
            return (e["fair_prob"] / 100) * amt * (e["odds_decimal"] - 1)
    else:
        bp = promo.get("boost_pct", 0) or 0

        def rank(e):
            # Post-boost EV% — comparable to MIN_EV_PERCENT directly.
            # _draw_refund_ev_pct adds P(draw) for a money-back-on-draw
            # promo (0 otherwise), so the picker ranks insured moneylines
            # by their true value, not the bare win EV.
            return (calc_ev(e["fair_prob"] / 100,
                            apply_profit_boost(e["odds_decimal"], bp))
                    + _draw_refund_ev_pct(promo, e))

    staged: dict[int, list[dict]] = {}
    for e in pool:
        s = _promo_window_stage(e, windows)
        if s is not None:
            staged.setdefault(s, []).append(e)

    first_nonempty: tuple[dict, int] | None = None
    for stage in range(len(windows)):
        cands = staged.get(stage)
        if not cands:
            continue
        best = max(cands, key=rank)
        if first_nonempty is None:
            first_nonempty = (best, stage)
        if is_bonus or rank(best) >= MIN_EV_PERCENT:
            return best, stage
    if first_nonempty is not None:
        return first_nonempty
    # Nothing inside any window — the promo's own terms force odds the
    # windows can't honor. Shortest odds = highest hit rate available.
    return min(pool, key=lambda e: e["odds_decimal"]), len(windows)


# ──────────────────────────────────────────────────────────────────────
# Headless entry point (for Telegram bot / automation)
# ──────────────────────────────────────────────────────────────────────

# Game-line markets handled by the per-sport fetch.
_GAME_LINE_MARKETS = frozenset({"h2h", "spreads", "totals"})
# Player-prop markets, grouped by which sport's prop fetch produces them.
# Drives both _derive_promo_scope() and the optional filter inside
# fetch_player_props().
_PROP_MARKETS_BY_SPORT: dict[str, frozenset[str]] = {
    "nba": frozenset({"pts", "reb", "ast", "threes", "pra", "dd"}),
    # HR routes through the Odds API batter_home_runs market alongside
    # the other MLB props — Action Network's parallel HR pipeline was
    # retired because its consensus math (median of all books including
    # the target) was strictly worse than the Pinnacle-anchored devigged
    # consensus from _compute_prop_fair_prob, and AN's scoreboard
    # endpoint flaked near midnight ET, surfacing the "No outcomes
    # match" diagnostic to users with an HR-targeted promo.
    "mlb": frozenset({"strikeouts", "total_bases", "hits", "rbis", "hr"}),
    "nhl": frozenset({"sog", "nhl_pts"}),
    # WC anytime goalscorer — fetched per-event from soccer_fifa_world_cup
    # (see ODDS_API_SPORTS["world_cup"]), so a scoped "dk 30% wc goals"
    # run costs only the handful of per-event calls inside 26h.
    "world_cup": frozenset({"goal"}),
}
# Golf markets that flow from fetch_golf_props.
_GOLF_PROMO_MARKETS = frozenset({"golf_top5", "golf_top10", "golf_top20",
                                  "golf_win"})

# Markets where the player not participating voids the bet — and a
# use-it-or-lose-it promo token dies with the void. Union of every prop
# pipeline's internal market keys (goal, hr, dd, sog, ...), plus the
# legacy AN pattern set in case it ever diverges. Golf top-N/win stays
# out: tournament-scale events with their own withdrawal rules, and the
# lineup-window concept doesn't map to a four-day event.
_VOIDABLE_PROP_MARKETS: frozenset[str] = frozenset().union(
    *_PROP_MARKETS_BY_SPORT.values()) | _PLAYER_PROP_MARKETS


def _promo_picks_prop_early(promo: dict, edge: dict) -> bool:
    """True if `promo` may take this voidable-prop `edge` before the
    lineup window — accepting a participation_warn — rather than holding
    the token until lineups confirm the player.

    Holds for a promo with no settle-always fallback it would rather take,
    where the window would just risk wasting the token: the promo TARGETS
    this prop market (market="hr"/"goal"/"dd"/...). Everything else (e.g. an
    all-market boost reaching a double-double) keeps deferring: it has a
    settle-always game line to take early."""
    return promo.get("market") in _VOIDABLE_PROP_MARKETS


def _pick_needs_participation_warn(edge: dict | None) -> bool:
    """True if a chosen promo edge is a voidable prop placed outside the
    lineup window — the formatters use this to tell the user to confirm
    the player is active before placing (a DNP voids the bet and burns
    the promo). Fires for any pick a promo was allowed to make early —
    i.e. a prop-targeted promo (see _promo_picks_prop_early and the gate
    in _promo_matches)."""
    if not edge:
        return False
    return (edge.get("market") in _VOIDABLE_PROP_MARKETS
            and not _starts_within_minutes(edge, PROMO_PROP_WINDOW_MIN))


def _derive_promo_scope(promos: list[dict] | None) -> dict | None:
    """Translate a list of promos into a fetch-scope hint.

    Returns None when no narrowing is possible (full fetch required):
      * No promos given
      * Any promo with sport="all" or market="all"
      * Any bonus_bet or parlay promo (these need broad pools to
        find candidates inside the target odds band / build leg combos)
      * Any market we don't recognise (be safe — fall back)

    Otherwise returns a dict:
      {
        "sports":             set[str],            # game-line sports to fetch
        "prop_sport_markets": dict[str, set[str]], # sport → internal market keys
        "need_golf":          bool,
      }
    Empty `sports`/`prop_sport_markets` mean those fetches can be skipped
    entirely. The caller is expected to short-circuit accordingly.

    Note: HR used to have its own `need_hr` flag routing to a parallel
    Action Network fetch. Post-migration HR is just another MLB prop in
    _PROP_MARKETS_BY_SPORT["mlb"], so an HR-targeted promo lands in
    prop_sport_markets={"mlb": {"hr"}} and rides the regular Odds API
    prop pipeline."""
    if not promos:
        return None

    sports: set[str] = set()
    prop_sport_markets: dict[str, set[str]] = {}
    need_golf = False

    for p in promos:
        if p.get("promo_type") in ("bonus_bet", "parlay"):
            return None
        sport = (p.get("sport") or "all").lower()
        market = (p.get("market") or "all").lower()
        if sport == "all" or market == "all":
            return None

        # Soccer-subset promos (european_soccer, world_cup) collapse to
        # the soccer fetch — the Odds API call pulls every soccer league;
        # the per-league filter is applied later inside _promo_matches
        # via edge["sport_key"].
        fetch_sport = "soccer" if sport in _SOCCER_SUBSET_PROMOS else sport

        if market in _GAME_LINE_MARKETS:
            sports.add(fetch_sport)
        elif market in _GOLF_PROMO_MARKETS:
            # GOLF_DISABLED: silently drop golf-market promos from the
            # scope. Without this, a golf-only promo would either set
            # need_golf=True (a no-op given the kill-switch but wastes
            # a fetch_golf_props call), or fall through to the "unknown
            # market" branch and trigger a full-slate fetch — which
            # would actually waste Odds API credits. The promo just
            # gets "no pick" downstream.
            continue
        else:
            # Player prop — figure out which sport's fetch produces it.
            for sp, mks in _PROP_MARKETS_BY_SPORT.items():
                if market in mks:
                    prop_sport_markets.setdefault(sp, set()).add(market)
                    break
            else:
                # Unknown market — be safe, fall back to full fetch.
                return None

    return {
        "sports":             sports,
        "prop_sport_markets": prop_sport_markets,
        "need_golf":          need_golf,
    }


def _promos_want_hr(promos: list[dict] | None) -> bool:
    """Return True if at least one promo *explicitly* targets HR (market="hr").

    Used to gate the batter_home_runs market on full /run fetches.
    Each per-event Odds API prop call costs (markets × regions) credits,
    so adding HR to the MLB prop list costs ~1 extra credit per MLB
    event. Skip unless the user asked for HR by name.

    Tightened 2026-05-26: previously any "all-market" promo (including
    bonus_bet and generic "fd 25%" boosts) unlocked the HR fetch on the
    theory that the picker might select an HR long-shot. In practice
    _promo_matches has *always* blocked HR edges from non-HR-market
    promos (see odds_compare.py around the "HR props only flow through
    to promos that explicitly target HR" comment) — so the data fetched
    for generic promos was never consumed. The looser gate was burning
    ~15 Odds API credits per scan for nothing. Now: only an explicit
    market="hr" promo unlocks the fetch.
    """
    if not promos:
        return False
    for p in promos:
        market = (p.get("market") or "all").lower()
        if market == "hr":
            return True
    return False


# Map from internal book name back to Odds API bookmaker key. Used by
# _hr_no_data_diagnostic to identify whether the user's specific book
# has posted HR data. Mirrors the book mapping in fetch_odds_api_props.
_USER_BOOK_TO_API_KEY = {
    "DraftKings": "draftkings",
    "FanDuel":    "fanduel",
}


def _hr_no_data_diagnostic(book: str) -> str:
    """When no HR edges made it through the merge phase, probe the Odds API
    directly (free /events call + one cheap /odds call) to tell the user
    *why* — and when to come back.

    Three distinct failure modes worth distinguishing:
      1. No MLB events visible at all (rare — usually means off-season
         or full slate already finished).
      2. Events exist but `book` hasn't posted HR yet (most common at
         night — DK/FD typically post HR 2–6h before first pitch,
         after lineups confirm).
      3. Events exist, `book` has HR, but only one book total — fails
         the MIN_PROP_REFERENCE_BOOKS=2 consensus floor.

    The /events call is free (0 credits). The probe /odds call costs
    1 credit but only runs once, only on the earliest event, and only
    when the user already paid for a full HR fetch that came back
    empty — so it's marginal vs the already-spent credits.
    """
    from datetime import datetime, timezone
    from bot_config import ODDS_API_KEY

    try:
        r = requests.get(
            f"{ODDS_API_BASE}/sports/baseball_mlb/events",
            params={"apiKey": ODDS_API_KEY},
            timeout=10,
        )
        if r.status_code != 200:
            return ("No MLB HR (batter_home_runs) data fetched and couldn't "
                    f"probe the Odds API (HTTP {r.status_code}). Try again "
                    f"closer to first pitch.")
        events = r.json() or []
    except Exception as e:
        return (f"No MLB HR (batter_home_runs) data fetched and the Odds "
                f"API probe failed ({type(e).__name__}). Try again closer "
                f"to first pitch.")

    now = datetime.now(timezone.utc)
    upcoming = []
    for ev in events:
        try:
            ct = datetime.fromisoformat(
                ev["commence_time"].replace("Z", "+00:00"))
        except Exception:
            continue
        if ct > now:
            upcoming.append((ct, ev))
    upcoming.sort(key=lambda t: t[0])

    if not upcoming:
        return ("No upcoming MLB games visible to the Odds API right now — "
                "off-day, schedule break, or all of today's games are "
                "already in progress / finished. No HR (batter_home_runs) "
                "data to fetch.")

    soonest_ct, soonest_ev = upcoming[0]
    hours_out = (soonest_ct - now).total_seconds() / 3600
    when_str = soonest_ct.strftime("%H:%M UTC")
    matchup = (f"{soonest_ev.get('away_team','?')} @ "
               f"{soonest_ev.get('home_team','?')}")

    # One probe call on the soonest event to see who actually has HR yet.
    # Ask for both the standard and alternate market keys because
    # different books expose HR under different keys (DK/FD use only
    # batter_home_runs_alternate; Caesars uses only batter_home_runs).
    try:
        r2 = requests.get(
            f"{ODDS_API_BASE}/sports/baseball_mlb/events/{soonest_ev['id']}/odds",
            params={
                "apiKey":     ODDS_API_KEY,
                "regions":    "us,us2",
                "markets":    "batter_home_runs,batter_home_runs_alternate",
                "oddsFormat": "american",
            },
            timeout=10,
        )
        if r2.status_code == 200:
            data = r2.json() or {}
            books_with_hr = sorted({
                bm.get("key", "")
                for bm in data.get("bookmakers", [])
                if any(m.get("key", "").startswith("batter_home_runs")
                       for m in bm.get("markets", []))
            })
        else:
            books_with_hr = []
    except Exception:
        books_with_hr = []

    user_book_key = _USER_BOOK_TO_API_KEY.get(book, book.lower())
    user_has_it = user_book_key in books_with_hr

    if not books_with_hr:
        return (f"No book has posted batter_home_runs yet — soonest game "
                f"({matchup}) is in {hours_out:.1f}h ({when_str}). DK/FD "
                f"usually post HR lines 2–6h before first pitch, after "
                f"lineups confirm. Try again then.")

    pretty_books = ", ".join(b.replace("williamhill_us", "Caesars")
                                .replace("draftkings", "DraftKings")
                                .replace("fanduel", "FanDuel")
                                .replace("betmgm", "BetMGM")
                                .replace("espnbet", "ESPN BET")
                                .title()
                             for b in books_with_hr)

    if not user_has_it:
        return (f"{book} hasn't posted batter_home_runs yet for the "
                f"soonest game ({matchup}, {hours_out:.1f}h out / "
                f"{when_str}). Books that have it now: {pretty_books}. "
                f"{book} usually fills in 2–6h before first pitch.")

    # User's book has HR but consensus failed → only 1-2 books total.
    return (f"{book} has batter_home_runs lines for the soonest game "
            f"({matchup}, {hours_out:.1f}h out) but only {len(books_with_hr)} "
            f"book(s) total ({pretty_books}) — needs 2+ books for a "
            f"reliable fair price. Wait for more books to post.")


def run_headless_fetch(dk_bankroll: float, fd_bankroll: float,
                       scope: dict | None = None,
                       promos: list[dict] | None = None) -> dict:
    """
    Phase 1 of the headless pipeline: fetch odds + props, compute edges.

    The slow phase (~60-90s). Independent of any user promos or game
    filters, so the caller can pause here to interactively prompt for
    game-specific promo selections before invoking run_headless_finalize.

    If `scope` is provided (see _derive_promo_scope), fetches are
    narrowed to only the sports/markets needed by the promos in scope —
    drops Odds API credit cost from ~30-50 to as little as ~1 for
    targeted promos like "fd 30% hr". scope=None preserves the full
    fetch behavior for /run, the auto-scan, and the webapp.

    Returns an intermediate state dict consumed by run_headless_finalize.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    script_dir  = DATA_DIR  # used for state files (prev_play_keys.json)
    output_path = os.path.join(
        OUTPUT_DIR,
        f"ev_edges_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
    )

    # Archive old sheets
    archive_dir = os.path.join(OUTPUT_DIR, "archive")
    old_sheets  = glob.glob(os.path.join(OUTPUT_DIR, "ev_edges_*.xlsx"))
    if old_sheets:
        os.makedirs(archive_dir, exist_ok=True)
        for old in old_sheets:
            dest = os.path.join(archive_dir, os.path.basename(old))
            shutil.move(old, dest)
        print(f"Archived {len(old_sheets)} old sheet(s) → archive/")

    print(f"DraftKings Bankroll: ${dk_bankroll:,.2f}")
    print(f"FanDuel Bankroll:    ${fd_bankroll:,.2f}")

    # Narrow per-sport game-line fetch to the scope (if any).
    if scope is None:
        sports_to_fetch = list(AN_SPORTS)
    else:
        sports_to_fetch = [s for s in AN_SPORTS if s in scope["sports"]]
        print(f"\n[Scoped run] game-line sports: "
              f"{sports_to_fetch or 'none'}; "
              f"player-props: {dict(scope['prop_sport_markets']) or 'none'}; "
              f"Golf: {scope['need_golf']}")
    if sports_to_fetch:
        print(f"\nFetching {len(sports_to_fetch)} sport(s) in parallel "
              f"(Odds API → AN fallback)...\n")
    else:
        print("\nSkipping per-sport game-line fetch (scope has no "
              "game-line markets).")

    # Parallel fetch
    all_edges      = []
    parlay_pool    = []
    promo_pool     = []
    total_games    = 0
    pinn_anchored  = 0
    bovada_matched = 0
    api_count      = 0
    an_count       = 0
    sport_sources: dict[str, str] = {}

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(_fetch_sport_all, sport): sport
                   for sport in sports_to_fetch}
        for future in as_completed(futures):
            sport = futures[future]
            try:
                sport_name, events, pinn_m, bov_m, source = future.result()
                sport_sources[sport_name] = source
                if events:
                    edges = find_edges(events, parlay_pool=parlay_pool,
                                       promo_pool=promo_pool)
                    all_edges.extend(edges)
                    total_games    += len(events)
                    pinn_anchored  += pinn_m
                    bovada_matched += bov_m
                    if source == "api":
                        api_count += 1
                    elif source == "fallback_an":
                        an_count += 1
                    extras = []
                    if pinn_m: extras.append(f"Pinnacle×{pinn_m}")
                    if bov_m:  extras.append(f"Bovada×{bov_m}")
                    if source == "fallback_an":
                        extras.append("AN-fallback")
                    note = f"  [{', '.join(extras)}]" if extras else ""
                    print(f"  [{sport_name:<6}] {len(events):>3} games"
                          f" → {len(edges):>3} +EV edges{note}")
            except Exception as e:
                print(f"  [{sport}] Error: {e}")

    all_edges.sort(key=lambda x: x["ev_pct"], reverse=True)

    # AN-only run: every sport with data fell back to Action Network.
    # Triggers the _ANONLY filename suffix and Summary-tab banner.
    sports_with_data = api_count + an_count
    an_only = (sports_with_data > 0 and api_count == 0 and an_count > 0)
    if an_only:
        output_path = output_path.replace(".xlsx", "_ANONLY.xlsx")
        print(f"\n⚠ AN-ONLY RUN — every sport fell back to Action Network "
              f"(Odds API quota likely exhausted)")

    # Fetch player props — each one can be scope-skipped independently.
    props_session     = make_session()
    # do_golf removed 2026-05-22: GOLF_DISABLED kills the path entirely
    # (see odds_compare.GOLF_DISABLED). golf_props_raw / golf_edges /
    # golf_promo_pool are kept as empty lists for compatibility with the
    # write_excel signature and downstream pool concatenations.
    do_pp   = (scope is None) or bool(scope["prop_sport_markets"])
    pp_filter = (None if scope is None
                 else scope["prop_sport_markets"] or None)

    # Full-fetch HR gating: when scope is None (the /run path) and the
    # caller passed an explicit promos list, drop batter_home_runs from
    # the MLB prop fetch unless at least one promo could apply to HR.
    # Saves ~1 Odds API credit per MLB event on the unscoped path —
    # adds up to ~15 credits a day on a typical slate when no HR promo
    # is configured. When promos=None (legacy callers, webapp without
    # a promos list), keep the original behavior of fetching all MLB
    # markets so we don't silently break those paths.
    hr_gated_out = False
    if scope is None and promos is not None and not _promos_want_hr(promos):
        pp_filter = {
            sp: set(mks) - {"hr"}
            for sp, mks in _PROP_MARKETS_BY_SPORT.items()
        }
        pp_filter = {sp: mks for sp, mks in pp_filter.items() if mks}
        do_pp = bool(pp_filter)
        hr_gated_out = True

    bits = []
    if do_pp:
        bits.append("O/U Props" if hr_gated_out else "O/U + HR Props")
    if bits:
        suffix = " (HR skipped — no HR-applicable promo)" if hr_gated_out else ""
        print(f"\nFetching player props ({' + '.join(bits)}){suffix}...")
    else:
        print("\nSkipping all player-prop fetches (scope doesn't need any).")

    # GOLF_DISABLED: empty placeholders. The fetch_golf_props /
    # find_golf_edges call-chain is removed from the live path; the
    # constants exist only so write_excel and pool-concatenation lines
    # downstream don't need to special-case golf's absence.
    golf_promo_pool: list[dict] = []
    golf_edges:      list[dict] = []

    # Player-prop pool mirrors the game-line promo_pool: find_X_edges
    # returns the +EV-only list used by sheet writers and /play_edges,
    # while pp_promo_pool captures every constructed edge so
    # compute_promo_picks can rank by boosted EV without losing
    # sub-MIN_EV candidates that a 25-50% boost would lift into +EV.

    pp_promo_pool: list[dict] = []
    pp_props_raw = (fetch_player_props(props_session, sport_markets=pp_filter)
                    if do_pp else [])
    player_prop_edges = (
        find_player_prop_edges(pp_props_raw, promo_pool=pp_promo_pool)
        if pp_props_raw else [])

    # HR edges are derived from the Odds API prop output now — find_player_prop_edges
    # filters batter_home_runs to the Yes side and normalizes outcome/point to
    # match the shape the HR Props tab + Telegram message expect. The HR list
    # is split out from the rest of the player_prop_edges so consumers that
    # only care about HR (HR Props sheet, /hr_picks) don't need to re-filter.
    # Use the *ungated* pp_promo_pool so sub-MIN_EV picks that a 30%+ HR boost
    # would lift into playable territory still appear; _build_hr_boost_rows
    # applies its own `boosted_ev >= 0.5` gate downstream.
    hr_edges = [e for e in pp_promo_pool if e.get("market") == "hr"]

    return {
        "all_edges":         all_edges,
        "hr_edges":          hr_edges,
        "golf_edges":        golf_edges,
        "golf_promo_pool":   golf_promo_pool,
        "player_prop_edges": player_prop_edges,
        "pp_promo_pool":     pp_promo_pool,
        "parlay_pool":       parlay_pool,
        "promo_pool":        promo_pool,
        "output_path":       output_path,
        "script_dir":        script_dir,
        "dk_bankroll":       dk_bankroll,
        "fd_bankroll":       fd_bankroll,
        "_counters": {
            "total_games":    total_games,
            "pinn_anchored":  pinn_anchored,
            "bovada_matched": bovada_matched,
            "an_only":        an_only,
            "api_count":      api_count,
            "an_count":       an_count,
            "sport_sources":  sport_sources,
        },
    }


def run_headless_live() -> list[dict]:
    """
    Live-only pipeline used by /run_live: fetch main lines for all sports,
    return +EV edges on currently in-progress games sorted by EV%.

    Skipped (vs run_headless_fetch): file archiving, Excel write, player
    props, prev_play_keys update. Nothing is persisted to disk.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    print(f"\n[LIVE] Fetching {len(AN_SPORTS)} sports for in-progress games...\n")
    all_edges: list[dict] = []

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(_fetch_sport_all, sport): sport
                   for sport in AN_SPORTS}
        for future in as_completed(futures):
            sport = futures[future]
            try:
                sport_name, events, _, _, _ = future.result()
                if not events:
                    continue
                edges = find_edges(events, parlay_pool=None, live_only=True)
                all_edges.extend(edges)
                if edges:
                    print(f"  [{sport_name:<6}] {len(edges):>2} live +EV edges")
            except Exception as e:
                print(f"  [{sport}] Error: {e}")

    all_edges.sort(key=lambda x: x["ev_pct"], reverse=True)
    return all_edges


def run_headless_finalize(fetched: dict,
                          promos: list[dict] | None = None,
                          game_filters: dict[str, str] | None = None,
                          skip_excel: bool = False) -> dict:
    """
    Phase 2 of the headless pipeline: apply game filters, write Excel,
    build promo picks + parlay rows. Fast (~1s).

    Parameters
    ----------
    fetched      : dict   — Output from run_headless_fetch.
    promos       : list   — Optional list of promo dicts (same schema as PromoDialog).
    game_filters : dict   — Optional {promo_label: "Away @ Home"} for game-specific promos.

    Returns
    -------
    dict with keys:
        play_edges, all_edges, promo_picks, parlay_rows, hr_edges,
        golf_edges, player_prop_edges, dk_bankroll, fd_bankroll,
        output_path, summary
    """
    promos       = promos or []
    game_filters = game_filters or {}

    all_edges         = fetched["all_edges"]
    hr_edges          = fetched["hr_edges"]
    golf_edges        = fetched["golf_edges"]
    player_prop_edges = fetched["player_prop_edges"]
    # Unfiltered pools (every constructed prop/golf edge, regardless of EV)
    # for boost-aware promo selection. Fall back to the gated lists when an
    # older fetched dict is passed in.
    golf_promo_pool   = fetched.get("golf_promo_pool", golf_edges)
    pp_promo_pool     = fetched.get("pp_promo_pool",   player_prop_edges)
    parlay_pool       = fetched["parlay_pool"]
    promo_pool        = fetched.get("promo_pool", all_edges)
    output_path       = fetched["output_path"]
    script_dir        = fetched["script_dir"]
    dk_bankroll       = fetched["dk_bankroll"]
    fd_bankroll       = fetched["fd_bankroll"]
    counters          = fetched["_counters"]

    # Apply game filters for game-specific promos
    for promo in promos:
        if promo.get("game_specific") and promo.get("label") in game_filters:
            promo["game_filter"] = game_filters[promo["label"]]

    # Write Excel (also handles promo picks, seen-tracking, etc.).
    # /run_promos passes skip_excel=True — the user doesn't want a
    # spreadsheet for a promo-only query.
    if not skip_excel:
        write_excel(all_edges, dk_bankroll, fd_bankroll, output_path,
                    promos, hr_edges, golf_edges, player_prop_edges,
                    parlay_pool=parlay_pool, script_dir=script_dir,
                    promo_pool=promo_pool,
                    pp_promo_pool=pp_promo_pool,
                    golf_promo_pool=golf_promo_pool,
                    an_only=counters.get("an_only", False))

    # Build play_edges for the bot
    play_edges = [e for e in all_edges if _qualifies(e)]

    # Include qualifying player props (e.g. NBA points)
    play_props = [e for e in player_prop_edges if _qualifies_prop(e)]
    if play_props:
        print(f"  {len(play_props)} player prop(s) qualify for Play These")
        play_edges.extend(play_props)
    play_edges.sort(key=lambda e: e["ev_pct"], reverse=True)

    # hr_edges is a *view* into pp_promo_pool (filtered to market="hr"), so
    # including both here would let the same HR pick collide with itself in
    # used_keys. Drop hr_edges from the union — HR picks ride pp_promo_pool.
    promo_picks = compute_promo_picks(
        promo_pool, promos,
        extra_edges=(list(pp_promo_pool or [])
                     + list(golf_promo_pool or [])))

    # Build parlay rows for the bot
    parlay_rows = _build_parlay_rows(
        all_edges, promos, dk_bankroll, fd_bankroll,
        extra_edges=player_prop_edges, parlay_pool=parlay_pool,
        promo_pool=promo_pool)

    return {
        "play_edges":        play_edges,
        "all_edges":         all_edges,
        "promo_picks":       promo_picks,
        "parlay_rows":       parlay_rows,
        "hr_edges":          hr_edges,
        "golf_edges":        golf_edges,
        "player_prop_edges": player_prop_edges,
        "dk_bankroll":       dk_bankroll,
        "fd_bankroll":       fd_bankroll,
        "output_path":       None if skip_excel else output_path,
        "summary": {
            "total_games":    counters["total_games"],
            "pinn_anchored":  counters["pinn_anchored"],
            "bovada_matched": counters["bovada_matched"],
            "total_edges":    len(all_edges),
            "an_only":        counters.get("an_only", False),
            "an_count":       counters.get("an_count", 0),
            "api_count":      counters.get("api_count", 0),
        },
    }


def compute_promo_picks(all_edges: list[dict],
                        promos: list[dict],
                        extra_edges: list[dict] | None = None) -> list[dict]:
    """Build the [{promo, edge}] picks list — best edge per straight promo,
    deduped so two promos don't pick the same edge.

    Pool is the broader promo_pool (all DK/FD outcomes, not just pre-boost
    +EV ones), combined with player_prop_edges. Selection runs through
    select_promo_edge — the hit-rate ruleset: staged odds windows
    (PROMO_ODDS_WINDOWS) with EV ranking inside a window, voidable props
    gated to the lineup window upstream in _promo_matches. Each pick
    carries band_stage (0 = target band) so formatters can flag off-band
    picks."""
    picks: list[dict] = []
    if not promos:
        return picks
    combined = list(all_edges) + list(extra_edges or [])
    used_keys: set = set()
    for promo in sorted(promos,
                        key=lambda p: (p.get("boost_pct") or 0), reverse=True):
        # Parlay promos are handled by _build_parlay_rows — skip here
        # so they don't pollute PROMO PICKS with "No qualifying pick
        # found" (the straight-pick _promo_matches always rejects them).
        if promo.get("promo_type") == "parlay":
            continue
        # A promo with count=N means the user has N identical copies —
        # produce N distinct picks, each on a different edge (via
        # used_keys deduplication).
        count = max(1, int(promo.get("count", 1) or 1))
        matched_all = [e for e in combined if _promo_matches(promo, e)]
        for copy_idx in range(count):
            pool = [e for e in matched_all
                    if _make_pick_key(e) not in used_keys]
            reason: str | None = None
            is_fallback = False
            best, band_stage = select_promo_edge(promo, pool)
            if best is not None:
                if promo.get("promo_type") != "bonus_bet":
                    # is_fallback keeps its historical meaning: the pick
                    # doesn't clear MIN_EV_PERCENT post-boost (burning a
                    # promo on a marginal line beats letting it expire).
                    # Off-band-ness is reported separately via band_stage.
                    boost_dec = apply_profit_boost(
                        best["odds_decimal"],
                        promo.get("boost_pct", 0) or 0)
                    is_fallback = (calc_ev(best["fair_prob"] / 100, boost_dec)
                                   + _draw_refund_ev_pct(promo, best)
                                   < MIN_EV_PERCENT)
                used_keys.add(_make_pick_key(best))
            else:
                # Diagnostic message when nothing was selected (empty pool).
                reason = _promo_no_pick_reason(
                    promo, combined, matched_all, pool)

            picks.append({
                "promo":       promo,
                "edge":        best,
                "copy_idx":    copy_idx,
                "copy_of":     count,
                "reason":      reason,
                "is_fallback": is_fallback,
                "band_stage":  band_stage,
                "participation_warn": _pick_needs_participation_warn(best),
            })
    return picks


def _promo_no_pick_reason(promo: dict,
                          combined: list[dict],
                          matched_all: list[dict],
                          pool: list[dict]) -> str:
    """Human-readable explanation of why no pick was selected. Walks the
    promo's filter chain from strictest (min_odds) to loosest (book only)
    to pinpoint which constraint emptied the pool, so a glance tells the
    user whether the issue is missing fetched data, a filter mismatch,
    too-short odds, or another promo claiming the only candidate."""
    book   = promo["book"]
    sport  = (promo.get("sport") or "all").lower()
    market = (promo.get("market") or "all").lower()

    # Pool empty but matches existed → another promo claimed them all.
    if matched_all and not pool:
        return (f"All {len(matched_all)} matching candidate(s) already "
                f"assigned to other promos.")

    # Voidable props held back by the lineup window get their own
    # message — the generic chain below would report "no candidates"
    # when the real story is "too early; re-run closer to start".
    window_held = [e for e in combined
                   if _promo_matches(promo, e, ignore_prop_window=True)
                   and not _promo_matches(promo, e)]
    if window_held:
        hours_out = []
        for e in window_held:
            try:
                ct = datetime.fromisoformat(
                    e["commence"].replace("Z", "+00:00"))
                hours_out.append(
                    (ct - datetime.now(ct.tzinfo)).total_seconds() / 3600)
            except Exception:
                continue
        timing = (f" Next eligible game starts in ~{min(hours_out):.1f}h —"
                  if hours_out else "")
        return (f"{len(window_held)} player-prop candidate(s) on hold: "
                f"props are only auto-picked within "
                f"{PROMO_PROP_WINDOW_MIN} min of start, once lineups / "
                f"injury reports are out (a DNP voids the bet and burns "
                f"the promo).{timing} re-run closer to kickoff.")

    # Soccer-subset promos (european_soccer, world_cup) need a dedicated
    # diagnostic so the user can tell "no soccer today" from "soccer
    # exists but none from the eligible competitions on this book".
    # Step 3 below would just say "no european_soccer" / "no world_cup"
    # and list soccer in the available sports — confusing because the
    # filter is at the sport_key level, not the sport level.
    subset_keys = _SOCCER_SUBSET_PROMOS.get(sport)
    if subset_keys is not None:
        in_slate_phrase, short_label = _SOCCER_SUBSET_LABELS[sport]
        book_soccer = [e for e in combined
                       if e.get("book") == book
                       and _canonical_promo_sport(e["sport"]) == "soccer"]
        if not book_soccer:
            # World Cup: distinguish "tournament fixtures exist but the
            # next match is beyond the pick window" (the 2 AM-the-day-
            # before case) from a genuinely dead fetch.
            if sport == "world_cup":
                timing = _wc_next_game_diagnostic(book)
                if timing:
                    return timing
            return (f"No {book} soccer edges loaded today — soccer fetch "
                    f"returned 0 events or pre-edge filters discarded them.")
        subset_book_soccer = [e for e in book_soccer
                              if e.get("sport_key") in subset_keys]
        if not subset_book_soccer:
            if sport == "world_cup":
                timing = _wc_next_game_diagnostic(book)
                if timing:
                    return timing
            outside = sorted({
                (e.get("sport_key") or "unknown")
                .replace("soccer_", "")
                .replace("_", " ")
                .title()
                for e in book_soccer
            })
            sample = ", ".join(outside[:5])
            more = f" (+{len(outside) - 5})" if len(outside) > 5 else ""
            return (f"{len(book_soccer)} {book} soccer edge(s) loaded but "
                    f"none from {in_slate_phrase}. Today's soccer slate: "
                    f"{sample}{more}.")
        # subset_book_soccer is non-empty: eligible-competition edges exist
        # on this book, yet none matched the promo. Genuine dedup (another
        # promo claimed them) is already handled at the top via
        # `matched_all and not pool`; this diagnostic only runs when best is
        # None (empty pool), so by here matched_all is empty — the edges
        # exist but every one failed _promo_matches. The old code blamed
        # dedup unconditionally, which read as "your boost lost to a better
        # promo" when the truth was "nothing here is eligible". Pin down the
        # subset-specific reasons the generic chain below can't see (start
        # horizon, goalscorer ban), then fall through to it for the rest
        # (min_odds / market / draw-insurable).

        # Per-sport reach for the messages below (the 24h MAX_PROMO_DAYS
        # default for soccer; see _promo_horizon_days).
        horizon_h = int(_promo_horizon_days(
            subset_book_soccer[0].get("sport", "")) * 24)

        # (a) Every eligible fixture is beyond the promo's start horizon.
        #     The goalscorer prop fetch reaches ~26h (per-event credit
        #     window, see fetch_odds_api_props) while game-line promo
        #     matching stops at this sport's horizon — so subset_book_soccer
        #     is non-empty with nothing inside the pick window. The "come
        #     back closer to kickoff" case the old dedup wording masked (and
        #     which the subset_book_soccer-empty timing check above never
        #     reached, because the 26h-window goal props keep the list
        #     non-empty).
        in_horizon = [e for e in subset_book_soccer
                      if _starts_within_promo_horizon(e)]
        if not in_horizon:
            if sport == "world_cup":
                timing = _wc_next_game_diagnostic(book)
                if timing:
                    return timing
            hours_out = []
            for e in subset_book_soccer:
                try:
                    ct = datetime.fromisoformat(
                        e["commence"].replace("Z", "+00:00"))
                    hours_out.append(
                        (ct - datetime.now(ct.tzinfo)).total_seconds() / 3600)
                except Exception:
                    continue
            soonest = (f" Next fixture starts in ~{min(hours_out):.1f}h —"
                       if hours_out else "")
            return (f"{len(subset_book_soccer)} {book} {short_label} edge(s) "
                    f"loaded but none start within {horizon_h}h.{soonest} "
                    f"re-run closer to kickoff.")

        # (b) Only anytime-goalscorer props sit inside the window and this
        #     isn't a goals-targeted token. The 2026-06-18 ban keeps generic
        #     boosts off market="goal" (see _promo_matches), so a settle-
        #     always boost has nothing here it is allowed to take — burning
        #     the boost on a goalscorer prop is exactly what the ban prevents.
        if market != "goal" and all(e.get("market") == "goal"
                                    for e in in_horizon):
            return (f"{len(in_horizon)} {book} {short_label} edge(s) within "
                    f"{horizon_h}h are all anytime-goalscorer props — a "
                    f"non-goals promo can't take those (goalscorer rule). Use "
                    f"a goals-targeted token (market=goal), or wait for game "
                    f"lines to post.")

        # (c) In-window game lines exist but failed min_odds / market /
        #     draw-insurable — fall through to the generic filter-chain
        #     diagnostics below, which name the exact constraint.

    # HR-targeted promos need a dedicated diagnostic. _promo_matches
    # rejects HR edges whenever promo.market != "hr" (intentional guard
    # against generic boosts silently grabbing a HR prop), which means
    # the relax-to-all path below would always under-count HR availability
    # and the "Available markets" sample would never include HR. Inspect
    # `combined` directly so the message reflects what was actually fetched.
    # NB: matched_all is always empty by the time we get here (the
    # "all claimed" branch above handles the non-empty case), so we only
    # report on the existence of HR data, not on dedup.
    if market == "hr":
        hr_in_combined = [e for e in combined if e.get("market") == "hr"]
        if not hr_in_combined:
            return _hr_no_data_diagnostic(book)
        hr_for_book = [e for e in hr_in_combined if e.get("book") == book]
        if not hr_for_book:
            books_with_hr = sorted({e.get("book") for e in hr_in_combined
                                    if e.get("book")})
            return (f"{len(hr_in_combined)} MLB HR edge(s) fetched but "
                    f"none on {book} ({book} HR coverage was empty for "
                    f"every game). Books with HR today: "
                    f"{', '.join(books_with_hr) or 'none'}.")
        # HR edges exist for this book but failed some other filter
        # (min_odds, start-window, stale-line guard upstream). Fall
        # through to the generic min_odds / start-window chain below.

    saved_min = promo.get("min_odds")
    saved_mkt = promo.get("market")
    try:
        # Step 1: relax min_odds. If anything matches now, the odds floor
        # was the culprit.
        promo["min_odds"] = None
        no_min = [e for e in combined if _promo_matches(promo, e)]
        if no_min and saved_min is not None:
            min_am = format_american(american_to_decimal(saved_min))
            longest = max(no_min, key=lambda e: e["odds_decimal"])
            longest_am = format_american(longest["odds_decimal"])
            outcome = longest.get("outcome", "?")
            filt = "/".join(s for s in (
                sport.upper() if sport != "all" else "",
                market.upper() if market != "all" else "",
            ) if s)
            filt_label = f"{filt} " if filt else ""
            return (f"0 {book} candidates at {min_am} or longer "
                    f"({len(no_min)} {filt_label}edge(s) matched but were too "
                    f"short). Longest today: {outcome} {longest_am}.")
        if no_min:
            return "0 candidates match this promo's filters."

        # Step 2: relax market too. If the sport has data but not on the
        # promo's market, that's a market-availability gap.
        if market != "all":
            promo["market"] = "all"
            no_market = [e for e in combined if _promo_matches(promo, e)]
            if no_market:
                markets_seen = sorted({e["market"].lower() for e in no_market})
                sample = ", ".join(m.upper() for m in markets_seen[:5])
                more = (f" (+{len(markets_seen) - 5})"
                        if len(markets_seen) > 5 else "")
                return (f"{len(no_market)} {book} {sport.upper()} edge(s) in "
                        f"slate but none on market={market.upper()}. "
                        f"Available {sport.upper()} markets today: "
                        f"{sample}{more}.")
    finally:
        promo["min_odds"] = saved_min
        promo["market"]   = saved_mkt

    # Step 3: book has data for other sports but not this one → the sport's
    # fetch returned no edges (off-season, no games today, or fetch failed).
    book_edges = [e for e in combined if e.get("book") == book]
    if book_edges and sport != "all":
        sports_seen = sorted({
            _canonical_promo_sport(e["sport"]).upper()
            for e in book_edges
        })
        sample = ", ".join(sports_seen[:6])
        more = (f" (+{len(sports_seen) - 6})"
                if len(sports_seen) > 6 else "")
        return (f"No {book} {sport.upper()} edges built today — "
                f"{len(book_edges)} {book} outcome(s) loaded on other "
                f"sports ({sample}{more}). {sport.upper()} fetch likely "
                f"returned 0 games or pre-edge filters discarded them.")
    if book_edges:
        return (f"No qualifying pick found among "
                f"{len(book_edges)} {book} outcome(s).")

    # Step 4: book has zero outcomes anywhere → fetch failed entirely.
    return (f"No {book} outcomes loaded today — the {book} fetch may have "
            f"failed entirely.")


def eligible_games_for_promo(promo: dict, all_edges: list[dict],
                             player_prop_edges: list[dict] | None = None,
                             hr_edges: list[dict] | None = None,
                             ) -> list[str]:
    """List of "Away @ Home" game strings this promo could apply to,
    using all promo filters except game_filter itself.

    Used by the Telegram bot to render a tap-keyboard for game selection.
    Accepts the broader promo_pool (all DK/FD outcomes) so games without a
    pre-boost +EV edge still appear — the boost may make them viable.
    For parlay promos, min_odds is not enforced here since the parlay-level
    odds (not per-leg) must clear the floor. HR edges are merged in so
    HR-targeted promos see MLB matchups; _promo_matches still gates them.
    Post-AN-migration: player_prop_edges no longer contains HR, so the
    hr_edges concat doesn't produce duplicate game entries.
    """
    combined = (list(all_edges)
                + list(player_prop_edges or [])
                + list(hr_edges or []))
    is_parlay = promo.get("promo_type") == "parlay"
    saved_filter = promo.get("game_filter")
    promo["game_filter"] = None
    try:
        if is_parlay:
            # Parlay path: skip min_odds + parlay-type skip inside _promo_matches
            games = sorted({
                e["game"] for e in combined
                if e["book"] == promo["book"]
                and _starts_within_promo_horizon(e)
                and e.get("market") != "hr"
                and (promo["sport"] == "all"
                     or _canonical_promo_sport(e["sport"]) == promo["sport"])
                and (promo["market"] == "all"
                     or e["market"].lower() == promo["market"])
            })
        else:
            games = sorted({e["game"] for e in combined
                            if _promo_matches(promo, e)})
    finally:
        promo["game_filter"] = saved_filter
    return games


def run_headless(dk_bankroll: float, fd_bankroll: float,
                 promos: list[dict] | None = None,
                 game_filters: dict[str, str] | None = None) -> dict:
    """Convenience wrapper for non-interactive consumers — runs both phases
    in sequence. Bot path uses fetch + finalize separately so it can prompt
    the user for game-specific promo selections in between.

    The promos list is forwarded to Phase 1 so the HR market is only
    fetched when at least one promo could apply to HR — saves Odds API
    credits on the unscoped /run path."""
    fetched = run_headless_fetch(dk_bankroll, fd_bankroll, promos=promos)
    return run_headless_finalize(fetched, promos, game_filters)


# ──────────────────────────────────────────────────────────────────────
# Main (GUI)
# ──────────────────────────────────────────────────────────────────────

def main():
    # ── Bankroll prompts ──────────────────────────────────────────────
    # Env-var defaults — used as GUI prefill value, OR as the actual value
    # in headless mode (cloud, no DISPLAY, or tkinter not installed).
    default_dk = float(os.environ.get("DEFAULT_DK_BANKROLL", 100))
    default_fd = float(os.environ.get("DEFAULT_FD_BANKROLL", 100))

    # Try to open a Tk root. Falls back to env-var-only headless mode if
    # tkinter is missing or there is no display (e.g. running on a server).
    root = None
    if _TK_AVAILABLE:
        try:
            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
        except Exception as e:
            print(f"[INFO] GUI unavailable ({e}); running in headless mode.")
            root = None

    if root is None:
        print(f"[INFO] Using env-var bankrolls "
              f"(DK=${default_dk}, FD=${default_fd}). "
              f"Set DEFAULT_DK_BANKROLL / DEFAULT_FD_BANKROLL to override.")
        dk_bankroll = default_dk
        fd_bankroll = default_fd
        promos = []
    else:
        dk_bankroll = simpledialog.askfloat(
            "Odds Compare", "Enter your DraftKings bankroll ($):",
            minvalue=1.0, initialvalue=default_dk, parent=root,
        )
        if dk_bankroll is None:
            root.destroy(); print("Cancelled."); sys.exit(0)

        fd_bankroll = simpledialog.askfloat(
            "Odds Compare", "Enter your FanDuel bankroll ($):",
            minvalue=1.0, initialvalue=default_fd, parent=root,
        )
        if fd_bankroll is None:
            root.destroy(); print("Cancelled."); sys.exit(0)

        # root is kept alive until after the optional game-picker dialog below.
        promos = ask_promos(root)

    script_dir  = DATA_DIR  # used for state files (prev_play_keys.json)
    output_path = os.path.join(
        OUTPUT_DIR,
        f"ev_edges_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
    )

    # ── Archive old sheets ───────────────────────────────────────────
    archive_dir = os.path.join(OUTPUT_DIR, "archive")
    old_sheets  = glob.glob(os.path.join(OUTPUT_DIR, "ev_edges_*.xlsx"))
    if old_sheets:
        os.makedirs(archive_dir, exist_ok=True)
        for old in old_sheets:
            dest = os.path.join(archive_dir, os.path.basename(old))
            shutil.move(old, dest)
        print(f"Archived {len(old_sheets)} old sheet(s) → archive/")

    print(f"DraftKings Bankroll: ${dk_bankroll:,.2f}")
    print(f"FanDuel Bankroll:    ${fd_bankroll:,.2f}")
    print(f"Kelly Fraction:      {KELLY_FRACTION * 100}%")
    print(f"Min EV:              {MIN_EV_PERCENT}%")
    print(f"Pinnacle weight:     {int(PINNACLE_WEIGHT * 100)}% (when available)")
    book_count = len(AN_BOOK_IDS) + 2   # AN books + Pinnacle + Bovada
    print(f"Consensus sources:   {book_count} books max per game")
    print(f"\nFetching {len(AN_SPORTS)} sports in parallel "
          f"(Odds API → AN fallback)...\n")

    # ── Parallel fetch ────────────────────────────────────────────────
    all_edges      = []
    parlay_pool    = []
    promo_pool     = []
    total_games    = 0
    pinn_anchored  = 0
    bovada_matched = 0
    api_count      = 0
    an_count       = 0

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(_fetch_sport_all, sport): sport
                   for sport in AN_SPORTS}
        for future in as_completed(futures):
            sport = futures[future]
            try:
                sport_name, events, pinn_m, bov_m, source = future.result()
                if events:
                    edges = find_edges(events, parlay_pool=parlay_pool,
                                       promo_pool=promo_pool)
                    all_edges.extend(edges)
                    total_games    += len(events)
                    pinn_anchored  += pinn_m
                    bovada_matched += bov_m
                    if source == "api":
                        api_count += 1
                    elif source == "fallback_an":
                        an_count += 1
                    extras = []
                    if pinn_m: extras.append(f"Pinnacle×{pinn_m}")
                    if bov_m:  extras.append(f"Bovada×{bov_m}")
                    if source == "fallback_an":
                        extras.append("AN-fallback")
                    note = f"  [{', '.join(extras)}]" if extras else ""
                    print(f"  [{sport_name:<6}] {len(events):>3} games"
                          f" → {len(edges):>3} +EV edges{note}")
                elif not events:
                    pass
            except Exception as e:
                print(f"  [{sport}] Error: {e}")

    all_edges.sort(key=lambda x: x["ev_pct"], reverse=True)

    sports_with_data = api_count + an_count
    an_only = (sports_with_data > 0 and api_count == 0 and an_count > 0)
    if an_only:
        output_path = output_path.replace(".xlsx", "_ANONLY.xlsx")
        print(f"\n⚠ AN-ONLY RUN — every sport fell back to Action Network")

    # ── Fetch player props: Strikeouts/TB/HR/Pts/Reb/Ast ──────────────
    # HR rides the Odds API batter_home_runs market via fetch_player_props
    # (was a separate AN pipeline pre-migration). pp_promo_pool captures
    # every constructed edge incl. HR so HR-boost promos can lift
    # sub-MIN_EV picks; hr_edges is the HR-only view for the HR Props tab.
    # Golf was removed 2026-05-22 (see GOLF_DISABLED).
    print(f"\nFetching player props (O/U + HR Props)...")
    props_session  = make_session()
    golf_edges:    list[dict] = []
    pp_promo_pool: list[dict] = []
    pp_props_raw       = fetch_player_props(props_session)
    player_prop_edges  = (
        find_player_prop_edges(pp_props_raw, promo_pool=pp_promo_pool)
        if pp_props_raw else [])
    hr_edges = [e for e in pp_promo_pool if e.get("market") == "hr"]

    # ── Game-specific promo: pick game from the now-fetched slate ─────
    if root is not None and promos and any(p.get("game_specific") for p in promos):
        ask_game_filters(root, promos, all_edges, player_prop_edges)

    # Done with GUI — clean up tkinter
    import gc
    gc.collect()
    if root is not None:
        root.destroy()

    print(f"\n{'='*56}")
    print(f"TOTAL GAMES SCANNED:   {total_games}")
    print(f"PINNACLE MATCHED:      {pinn_anchored} games")
    print(f"BOVADA MATCHED:        {bovada_matched} games")
    print(f"TOTAL +EV EDGES FOUND: {len(all_edges)}")
    if all_edges:
        dk_count = sum(1 for e in all_edges if e["book"] == "DraftKings")
        fd_count = sum(1 for e in all_edges if e["book"] == "FanDuel")
        pa_count = sum(1 for e in all_edges if e.get("pinnacle_anchored"))
        print(f"  DraftKings:          {dk_count}")
        print(f"  FanDuel:             {fd_count}")
        print(f"  Pinnacle-anchored:   {pa_count} / {len(all_edges)}")
        print(f"  Best edge:           {all_edges[0]['ev_pct']}% EV"
              f" — {all_edges[0]['game']}")

    if hr_edges:
        hr_positive = [e for e in hr_edges if e["ev_pct"] >= 0.5]
        print(f"\nHR PLAYER PROPS:")
        print(f"  Total HR lines:      {len(hr_edges)}")
        print(f"  +EV HR lines:        {len(hr_positive)}")
        if hr_positive:
            print(f"  Best HR edge:        {hr_positive[0]['ev_pct']}% EV"
                  f" — {hr_positive[0]['outcome']} ({hr_positive[0]['game']})")

    if golf_edges:
        mkt_counts = {}
        for e in golf_edges:
            mkt_counts[e["market"]] = mkt_counts.get(e["market"], 0) + 1
        print(f"\nGOLF PROPS ({golf_edges[0]['game'] if golf_edges else ''}):")
        for mkt, cnt in sorted(mkt_counts.items()):
            disp = {"golf_top5": "Top 5", "golf_top10": "Top 10",
                    "golf_top20": "Top 20", "golf_win": "Outright"}.get(mkt, mkt)
            print(f"  +EV {disp:<10}: {cnt}")
        print(f"  Best golf edge:      {golf_edges[0]['ev_pct']}% EV"
              f" — {golf_edges[0]['outcome']} "
              f"({golf_edges[0].get('market_disp', golf_edges[0]['market'])})")

    if player_prop_edges:
        mkt_pp_counts: dict[str, int] = {}
        for e in player_prop_edges:
            mkt_pp_counts[e.get("market_disp", e["market"])] = (
                mkt_pp_counts.get(e.get("market_disp", e["market"]), 0) + 1)
        print(f"\nPLAYER PROPS (+EV O/U): {len(player_prop_edges)} edges")
        for mkt, cnt in sorted(mkt_pp_counts.items()):
            print(f"  {mkt:<14}: {cnt}")
        print(f"  Best:            {player_prop_edges[0]['ev_pct']}% EV"
              f" — {player_prop_edges[0]['outcome']}"
              f" ({player_prop_edges[0]['game']})")

    write_excel(all_edges, dk_bankroll, fd_bankroll, output_path,
                promos, hr_edges, golf_edges, player_prop_edges,
                parlay_pool=parlay_pool, script_dir=script_dir,
                promo_pool=promo_pool, an_only=an_only)

    if all_edges:
        print(f"\nTop 5 edges:")
        print(f"{'Rank':<5} {'EV%':<8} {'Book':<12} {'Game':<30} "
              f"{'Pick':<20} {'Odds':<10} {'Bet':<10} {'📌'}")
        print("-" * 100)
        for i, e in enumerate(all_edges[:5]):
            br   = dk_bankroll if e["book"] == "DraftKings" else fd_bankroll
            bet  = clamp_bet(br * e["kelly_pct"] / 100, e["book"])
            pick = e["outcome"]
            if e["point"] is not None:
                pick += f" {_fmt_pt(e['point'], e['market'])}"
            pin  = "✓" if e.get("pinnacle_anchored") else ""
            print(f"{i+1:<5} {e['ev_pct']:<8} {e['book']:<12} {e['game']:<30} "
                  f"{pick:<20} {_fmt_am(e['odds_american']):<10} ${bet:<9.2f} {pin}")

    if promos:
        print(f"\n{'='*56}")
        print(f"ACTIVE PROMOS: {len(promos)}")
        for promo in promos:
            print(f"  • {promo['label']}")
            if promo.get("promo_type") == "bonus_bet":
                amt = promo.get("bet_amount", 0)
                matching = [e for e in all_edges if _promo_matches(promo, e)]
                if matching:
                    # pick maximising expected profit for a free bet
                    best = max(matching,
                               key=lambda e: (e["fair_prob"] / 100)
                                             * amt * (e["odds_decimal"] - 1))
                    exp_profit = (best["fair_prob"] / 100) * amt * (best["odds_decimal"] - 1)
                    print(f"    Best bonus-bet pick: {best['game']} — {best['outcome']}"
                          f" {_fmt_pt(best.get('point'), best.get('market', ''))}")
                    print(f"    {_fmt_am(best['odds_american'])}  expected profit ≈ ${exp_profit:.2f}")
                continue
            matching = [e for e in all_edges if _promo_matches(promo, e)]
            if matching:
                best = matching[0]
                boost_dec = apply_profit_boost(best["odds_decimal"],
                                               promo.get("boost_pct") or 0)
                boosted_ev = (calc_ev(best["fair_prob"] / 100, boost_dec)
                              + _draw_refund_ev_pct(promo, best))
                print(f"    Best boosted pick: {best['game']} — {best['outcome']}"
                      f" {_fmt_pt(best.get('point'), best.get('market', ''))}")
                print(f"    {_fmt_am(best['odds_american'])} → {format_american(boost_dec)}"
                      f"  ({best['ev_pct']}% → {boosted_ev:.1f}% EV)")
            else:
                print(f"    No qualifying picks found")

        # Show HR boost summary
        if hr_edges:
            hr_boost_rows = _build_hr_boost_rows(
                hr_edges, promos, dk_bankroll, fd_bankroll)
            if hr_boost_rows:
                print(f"\n  HR BOOST PICKS: {len(hr_boost_rows)} boosted HR plays")
                for j, pr in enumerate(hr_boost_rows[:5]):
                    e = pr["edge"]
                    print(f"    {j+1}. {e['outcome']:<20} "
                          f"{_fmt_am(e['odds_american']):>5} → "
                          f"{format_american(pr['boost_dec']):>5}  "
                          f"({pr['boosted_ev']:.1f}% EV)  "
                          f"${pr['bet']:.2f}")
                if len(hr_boost_rows) > 5:
                    print(f"    ... and {len(hr_boost_rows)-5} more "
                          f"(see 'HR Props' tab)")

        parlay_count = sum(1 for p in promos if p.get("promo_type") == "parlay")
        if parlay_count:
            print(f"  Parlay promos: {parlay_count} → see 'Parlay Promos' tab")
        print(f"\nSee 'Play These', 'Promos', 'Parlay Promos', and 'HR Props' tabs.")


if __name__ == "__main__":
    main()
