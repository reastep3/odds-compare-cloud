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

EASTERN = ZoneInfo("America/New_York")

# ── Runtime paths (cloud-aware) ──────────────────────────────────────
# On cloud, set OUTPUT_DIR and DATA_DIR to a persistent volume mount.
# Locally (unset), both default to the script's directory.
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR  = os.environ.get("OUTPUT_DIR", _SCRIPT_DIR)
DATA_DIR    = os.environ.get("DATA_DIR",   _SCRIPT_DIR)
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

# v2 scoreboard sport slugs (LOWERCASE required).
# If a sport returns nothing, check actionnetwork.com DevTools for the slug.
AN_SPORTS = [
    "nba",
    "ncaab",
    "nfl",
    "ncaaf",
    "mlb",
    "nhl",
    "ufc",       # was "MMA" — v2 uses "ufc"
    "soccer",    # MLS + international; filter by league name in Excel
]

ACTION_NETWORK_BASE = "https://api.actionnetwork.com/web/v2"

# ──────────────────────────────────────────────────────────────────────
# The Odds API config
# ──────────────────────────────────────────────────────────────────────

ODDS_API_BASE = "https://api.the-odds-api.com/v4"

# Map internal sport codes → The Odds API sport keys
ODDS_API_SPORTS: dict[str, str | list[str]] = {
    "nba":    "basketball_nba",
    "ncaab":  "basketball_ncaab",
    "nfl":    "americanfootball_nfl",
    "ncaaf":  "americanfootball_ncaaf",
    "mlb":    "baseball_mlb",
    "nhl":    "icehockey_nhl",
    "ufc":    "mma_mixed_martial_arts",
    "soccer": ["soccer_usa_mls"],
}

# Books to request from The Odds API
ODDS_API_BOOKMAKERS = (
    "draftkings,fanduel,pinnacle,betmgm,bovada,"
    "caesars,betrivers,espnbet,bet365,lowvig,betonlineag"
)

# Player prop market keys by sport
ODDS_API_PROP_MARKETS: dict[str, list[tuple[str, str, str]]] = {
    # (api_market_key, internal_market_key, display_name)
    "nba": [
        ("player_points",   "pts", "Points"),
        ("player_rebounds",  "reb", "Rebounds"),
        ("player_assists",   "ast", "Assists"),
    ],
    "mlb": [
        ("pitcher_strikeouts", "strikeouts", "Strikeouts"),
        ("batter_total_bases", "total_bases", "Total Bases"),
    ],
}


# ──────────────────────────────────────────────────────────────────────
# Pinnacle config
# ──────────────────────────────────────────────────────────────────────

# Pinnacle's public guest API — no account needed.
# The key below is their own frontend guest key (embedded in their site).
# If you get 401 errors, grab a fresh key from DevTools as described above.
PINNACLE_BASE       = "https://guest.api.arcadia.pinnacle.com/0.1"
PINNACLE_GUEST_KEY  = os.environ.get(
    "PINNACLE_GUEST_KEY", "CmX2KcMrXuFmNg6YFbmTxE0y9CIrOi0R"
)

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
        api_keys = [api_keys]

    all_events: list[dict] = []
    status_summary: dict[str, int] = {}

    for sport_key in api_keys:
        try:
            r = requests.get(
                f"{ODDS_API_BASE}/sports/{sport_key}/odds",
                params={
                    "apiKey":      ODDS_API_KEY,
                    "regions":     "us",
                    "markets":     "h2h,spreads,totals",
                    "oddsFormat":  "american",
                    "bookmakers":  ODDS_API_BOOKMAKERS,
                },
                timeout=30,
            )
            remaining = r.headers.get("x-requests-remaining", "?")
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


def fetch_odds_api_props(sport: str) -> list[dict]:
    """
    Fetch player props from The Odds API for a given sport.

    Props require per-event calls (sport-level endpoint returns 422 for
    prop markets). Flow:
      1. GET /sports/{sport}/events  (free — 0 credits)
      2. For each event: GET /events/{id}/odds?markets=...  (1 credit per call)

    Returns a list of raw prop dicts compatible with _merge_raw_props().
    """
    from bot_config import ODDS_API_KEY

    prop_markets = ODDS_API_PROP_MARKETS.get(sport)
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
    try:
        r = requests.get(
            f"{ODDS_API_BASE}/sports/{api_sport}/events",
            params={"apiKey": ODDS_API_KEY},
            timeout=15,
        )
        if r.status_code != 200:
            print(f"  [OddsAPI Props/{sport.upper()}] Events HTTP {r.status_code}")
            return []
        event_list = r.json()
    except Exception as e:
        print(f"  [OddsAPI Props/{sport.upper()}] Events error: {e}")
        return []

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

        try:
            r2 = requests.get(
                f"{ODDS_API_BASE}/sports/{api_sport}/events/{eid}/odds",
                params={
                    "apiKey":      ODDS_API_KEY,
                    "regions":     "us",
                    "markets":     api_market_keys,
                    "oddsFormat":  "american",
                    "bookmakers":  ODDS_API_BOOKMAKERS,
                },
                timeout=15,
            )
            remaining = r2.headers.get("x-requests-remaining", remaining)
            if r2.status_code != 200:
                continue
            ev_data = r2.json()
        except Exception:
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
                    side_name = oc.get("name", "").lower()
                    if side_name not in ("over", "under"):
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
                    })

        time.sleep(0.05)  # gentle rate limiting

    print(f"  [OddsAPI Props/{sport.upper()}] {len(raw)} raw lines "
          f"from {len(event_list)} events (quota: {remaining} left)")
    return raw


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

def _fetch_sport_all(sport: str) -> tuple[str, list[dict], int, int]:
    """
    Fetch odds for one sport via The Odds API.

    Returns (sport_name, events, pinnacle_count, total_books).
    The Odds API returns DK + FD + Pinnacle + Bovada + BetMGM + more in
    a single call, so no separate scraping or injection needed.
    """
    events, status = fetch_odds_api(sport)

    if not events:
        return sport.upper(), [], 0, 0

    # Count Pinnacle coverage for diagnostics
    pinn_ct = sum(1 for ev in events
                  if any(bm["key"] == "pinnacle" for bm in ev.get("bookmakers", [])))
    total_books = max(
        (len(ev.get("bookmakers", [])) for ev in events), default=0)

    return sport.upper(), events, pinn_ct, total_books


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
        try:
            commence_dt = datetime.fromisoformat(commence.replace("Z", "+00:00"))
            if not (earliest <= commence_dt <= latest):
                continue
        except (ValueError, AttributeError):
            continue

        home       = event["home_team"]
        away       = event["away_team"]
        sport      = event.get("sport_title", "")
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
                    }
                    if ev >= MIN_EV_PERCENT:
                        edges.append(edge_dict)
                    # ── Promo pool: every DK/FD outcome (for boost evaluation)
                    if promo_pool is not None:
                        promo_pool.append(edge_dict)
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
MAX_PROMO_DAYS = 1     # Promos (singles + parlays): within 1 day


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


# European soccer leagues with consistently negative ROI across backtests.
# Ligue1 and Serie A were previously blocked but proved profitable once
# the broader +EV pool was examined (Ligue1 +26% / +4%, Serie A +47% / +11%
# across month + week), so they are now allowed.
# LALIGA added provisionally 2026-04-21: 1W/4L with 4 pushes across 31d+7d PT
# windows (-72% / -31% ROI). Small sample — revisit after more data.
_BLOCKED_SOCCER = frozenset({
    "EPL", "BUNDESLIGA", "CHAMPIONS", "EUROPA", "LALIGA",
})


def _qualifies(e):
    """Return True if an edge qualifies for the 'Play These' tab.

    Recalibrated 2026-04-16 (v2). Non-Pinnacle EV threshold bumped from
    2.0% → 2.5% for H2H/spreads/totals. The EV 1–3% non-Pinnacle band
    produced -8.8% ROI over 235 picks (30-day) and -6.8% ROI over the
    78 dropped picks in the past-week simulation — consistent bleed
    across two independent windows. Pinnacle-anchored picks unchanged
    (still qualify at EV ≥ 1.0%).

    Recalibrated 2026-04-16. Heavy-mispricing EV cap removed — the ≥ 5% EV
    tier showed +32% ROI (month) / +28% ROI (week) across the raw +EV pool,
    and spreads/totals at EV ≥ 4% hit ~70%+ WR. These are now allowed
    through rather than filtered as suspected stale lines.
    """
    pinn  = e.get("pinnacle_anchored", False)
    ev    = e["ev_pct"]
    books = e["num_other_books"]
    mkt   = e["market"]
    sport = e.get("sport", "").upper()

    if not _starts_within_days(e, MAX_PLAY_DAYS):
        return False
    if books < 4:
        return False

    # Block European soccer — negative ROI across the board without Pinnacle
    if sport in _BLOCKED_SOCCER:
        return False

    # Block NBA game lines (spreads/totals/h2h) — -18.9% ROI lifetime.
    # NBA pts props handled by _qualifies_prop().
    if sport == "NBA":
        return False

    # H2H: block everything EXCEPT MLB (+26.4% ROI, 114 picks, 54% WR)
    if mkt == "h2h":
        if sport == "MLB":
            if pinn and ev >= 1.0:
                return True
            return ev >= 2.5
        return False

    if mkt == "spreads":
        if pinn and ev >= 1.0:
            return True
        return ev >= 2.5

    if mkt == "totals":
        if pinn and ev >= 1.0:
            return True
        return books >= 5 and ev >= 2.5

    return False


def _qualifies_prop(e):
    """Return True if a player prop edge qualifies for 'Play These'.

    Calibrated 2026-04-12 against 149 settled NBA points picks:
      pts: 56% WR, +22.5% ROI this week (+9.1% lifetime) — profitable
           even without Pinnacle and with low book counts.
      reb/ast: negative ROI — excluded.
    EV cap removed 2026-04-16 to stay consistent with game-line qualifier.
    """
    ev    = e["ev_pct"]
    mkt   = e["market"]
    pinn  = e.get("pinnacle_anchored", False)

    if not _starts_within_days(e, MAX_PLAY_DAYS):
        return False

    # NBA points — backtested +22.5% ROI (week), +9.1% (lifetime)
    # No book-count gate — props often have only 1-2 reference books
    # and are still profitable. Pinnacle bonus if available.
    if mkt == "pts":
        if pinn and ev >= 1.0:
            return True
        return ev >= 2.0

    return False


def write_excel(edges: list[dict], dk_bankroll: float,
                fd_bankroll: float, output_path: str,
                promos: list[dict] = None,
                hr_edges: list[dict] = None,
                golf_edges: list[dict] = None,
                player_prop_edges: list[dict] = None,
                parlay_pool: list = None,
                script_dir: str = None,
                promo_pool: list = None):
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

    ws["A1"] = "ODDS COMPARE — +EV EDGE FINDER"
    ws["A1"].font = Font(bold=True, size=16, color="2F5496")
    ws["A2"] = f"Generated: {datetime.now(EASTERN).strftime('%Y-%m-%d %H:%M %Z')}"
    ws["A2"].font = Font(italic=True, color="666666")
    ws["A3"] = "Source: Action Network + Pinnacle (scraped — no API key)"
    ws["A3"].font = Font(italic=True, color="888888")

    ws["A4"] = "DraftKings Bankroll:"
    ws["A4"].font = Font(bold=True, size=13)
    ws["B4"] = dk_bankroll
    ws["B4"].number_format = '$#,##0.00'
    ws["B4"].font = Font(bold=True, size=13, color="2F5496")

    ws["A5"] = "FanDuel Bankroll:"
    ws["A5"].font = Font(bold=True, size=13)
    ws["B5"] = fd_bankroll
    ws["B5"].number_format = '$#,##0.00'
    ws["B5"].font = Font(bold=True, size=13, color="2F5496")

    ws["A6"] = "Kelly Fraction:"
    ws["A6"].font = Font(bold=True)
    ws["B6"] = f"{KELLY_FRACTION * 100}%"

    ws["A7"] = "Min EV Threshold:"
    ws["A7"].font = Font(bold=True)
    ws["B7"] = f"{MIN_EV_PERCENT}%"

    ws["A8"] = "Total +EV Bets Found:"
    ws["A8"].font = Font(bold=True)
    ws["B8"] = len(edges)

    dk_edges = [e for e in edges if e["book"] == "DraftKings"]
    fd_edges = [e for e in edges if e["book"] == "FanDuel"]

    total_wagered = sum(
        (dk_bankroll if e["book"] == "DraftKings" else fd_bankroll)
        * e["kelly_pct"] / 100
        for e in edges
    ) if edges else 0
    ws["A9"] = "Total Capital Deployed:"
    ws["A9"].font = Font(bold=True)
    ws["B9"] = total_wagered
    ws["B9"].number_format = '$#,##0.00'

    pinnacle_count = sum(1 for e in edges if e.get("pinnacle_anchored"))
    ws["A10"] = "Pinnacle-Anchored Edges:"
    ws["A10"].font = Font(bold=True)
    ws["B10"] = f"{pinnacle_count} / {len(edges)}"

    ws["A11"] = "DraftKings Edges:"
    ws["A11"].font = Font(bold=True)
    ws["B11"] = len(dk_edges)
    ws["A12"] = "FanDuel Edges:"
    ws["A12"].font = Font(bold=True)
    ws["B12"] = len(fd_edges)

    if edges:
        ws["A13"] = "Best EV:"
        ws["A13"].font = Font(bold=True)
        ws["B13"] = (
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
                         value=("Promos ranked by boost % / bonus value. "
                                "Each edge assigned to at most one promo. "
                                "Bonus Bet: you keep profit only (stake is free).")).font = Font(
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

                # For bonus bets, pick the edge that maximises expected profit
                # (fair_prob × profit_if_win), not just highest EV%.
                def _bonus_sort_key(e):
                    fp   = e["fair_prob"] / 100
                    dec  = e["odds_decimal"]
                    amt  = promo.get("bet_amount", 0)
                    return fp * amt * (dec - 1)

                best_edge = None
                # Use the broader promo_pool (all DK/FD outcomes) so boosts
                # can turn mildly -EV edges into +EV picks.
                _play_promo_pool = (list(promo_pool or edges)
                                    + list(player_prop_edges or []))
                if is_bonus_bet:
                    _play_promo_pool = sorted(_play_promo_pool,
                                              key=_bonus_sort_key, reverse=True)
                elif promo.get("boost_pct"):
                    # Rank by BOOSTED EV so long-shots that only clear the
                    # bar thanks to the boost can still win the pick.
                    _bp = promo["boost_pct"]

                    def _boosted_ev_sort(e, _bp=_bp):
                        bd = apply_profit_boost(e["odds_decimal"], _bp)
                        return calc_ev(e["fair_prob"] / 100, bd)

                    _play_promo_pool = sorted(_play_promo_pool,
                                              key=_boosted_ev_sort,
                                              reverse=True)
                for edge in _play_promo_pool:
                    if id(edge) in used_edge_ids:
                        continue
                    if not _promo_matches(promo, edge):
                        continue
                    # Skip edges that still don't clear the bar after boost
                    if not is_bonus_bet and promo.get("boost_pct"):
                        bd = apply_profit_boost(edge["odds_decimal"],
                                                promo["boost_pct"])
                        if calc_ev(edge["fair_prob"] / 100, bd) < 0.5:
                            continue
                    best_edge = edge
                    break

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
                        promo["label"],
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
                    # ── Profit Boost math ─────────────────────────────
                    boost_dec_v = apply_profit_boost(orig_dec, promo["boost_pct"])
                    boosted_ev  = calc_ev(fair_prob_v, boost_dec_v)
                    boosted_k   = kelly_bet(fair_prob_v, boost_dec_v)
                    bankroll_v  = (dk_bankroll if best_edge["book"] == "DraftKings"
                                   else fd_bankroll)
                    bet_v       = clamp_bet(bankroll_v * boosted_k, best_edge["book"])
                    if promo.get("max_bet") and bet_v > promo["max_bet"]:
                        bet_v = promo["max_bet"]
                    profit_v    = bet_v * (boost_dec_v - 1)
                    values = [
                        promo["label"],
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
        cmp_columns = info_cols + [(bk, 13) for bk in book_order]

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
        _all_for_promos = (list(promo_pool or edges)
                           + list(player_prop_edges or []))
        for promo in promos:
            if promo.get("promo_type") == "bonus_bet":
                continue   # handled in Play These PROMO PICKS section
            for edge in _all_for_promos:
                if not _promo_matches(promo, edge):
                    continue

                orig_dec  = edge["odds_decimal"]
                boost_dec = apply_profit_boost(orig_dec, promo["boost_pct"])
                fair_prob = edge["fair_prob"] / 100

                boosted_ev   = calc_ev(fair_prob, boost_dec)
                boosted_kelly = kelly_bet(fair_prob, boost_dec)

                # Only show if boosted EV is positive
                if boosted_ev < 0.5:
                    continue

                bankroll = (dk_bankroll if edge["book"] == "DraftKings"
                            else fd_bankroll)
                bet = clamp_bet(bankroll * boosted_kelly, edge["book"])
                # Cap at max bet if promo has one
                if promo.get("max_bet") and bet > promo["max_bet"]:
                    bet = promo["max_bet"]

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
    """
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


def find_golf_edges(golf_props: list[dict]) -> list[dict]:
    """
    Find +EV golf props on DraftKings / FanDuel using median multi-book consensus.

    Key considerations vs. game markets:
      - State-specific books (FanDuel NY, BetRivers NY, etc.) must be
        canonicalized so they don't double-count the same line.
      - Require 4+ unique canonical books for a reliable fair line.
      - Outright winner odds for fringe players vary wildly across books;
        canonical dedup prevents phantom edges from state-variant pricing.
    """
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
            # (round-number consensus on obscure 150+ player fields is unreliable)
            if prop["market"] == "golf_win" and fair_prob < 0.002:
                continue
            if ev >= MIN_EV_PERCENT:
                kelly = kelly_bet(fair_prob, dec)
                edges.append({
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
                })

    edges.sort(key=lambda x: x["ev_pct"], reverse=True)
    return edges


# ──────────────────────────────────────────────────────────────────────
# MLB Anytime HR Player Props
# ──────────────────────────────────────────────────────────────────────

# Action Network prop key for "Anytime Home Run" (1+ HR milestones)
AN_HR_PROP_KEY = "core_bet_type_433_943_player_home_runs_milestones_1_or_more"


def fetch_hr_props(session: requests.Session) -> list[dict]:
    """
    Fetch anytime HR player prop odds for all today's MLB games.

    1. Hit AN v2 scoreboard for MLB to get game IDs.
    2. For each game, hit /web/v2/games/{id}/props to get HR odds.
    3. Return a list of HR prop dicts with multi-book odds.

    Returns list of:
      {
        "player":    "Mike Trout",
        "game":      "LAA @ NYY",
        "commence":  "2026-03-29T...",
        "sport":     "MLB",
        "book_odds": {68: 320, 69: 310, ...},   # book_id → american odds
      }
    """
    # Step 1: get MLB game IDs from scoreboard
    book_ids_str = ",".join(str(b) for b in AN_BOOK_IDS)
    url = f"{ACTION_NETWORK_BASE}/scoreboard/mlb"
    try:
        resp = session.get(url, params={"bookIds": book_ids_str}, timeout=30)
        if resp.status_code != 200:
            print(f"  [HR Props] MLB scoreboard returned {resp.status_code}")
            return []
    except Exception as e:
        print(f"  [HR Props] MLB scoreboard error: {e}")
        return []

    games = resp.json().get("games", [])
    if not games:
        print("  [HR Props] No MLB games today")
        return []

    # Filter to bettable (upcoming) games
    bettable = []
    skipped = 0
    for game in games:
        game_id = game.get("id")
        if not game_id:
            continue
        status = game.get("status", "scheduled")
        if status not in ("scheduled", "pregame", "delayed", "postponed", ""):
            skipped += 1
            continue
        bettable.append(game)

    if not bettable:
        print(f"  [HR Props] {len(games)} MLB games found but all are "
              f"complete/in-progress — run earlier for tomorrow's slate")
        return []

    print(f"  [HR Props] {len(bettable)} bettable MLB games "
          f"({skipped} already complete) — fetching props...")

    # Step 2: for each bettable game, fetch HR props
    hr_props = []
    for game in bettable:

        # Build game label
        teams = game.get("teams", [])
        away_name = home_name = "TBD"
        away_id = game.get("away_team_id")
        home_id = game.get("home_team_id")
        for t in teams:
            tid = t.get("id")
            if tid == away_id:
                away_name = t.get("abbr") or t.get("full_name", "Away")
            elif tid == home_id:
                home_name = t.get("abbr") or t.get("full_name", "Home")
        game_label = f"{away_name} @ {home_name}"
        commence = game.get("start_time") or game.get("event_date", "")

        # Fetch props
        props_url = f"{ACTION_NETWORK_BASE}/games/{game_id}/props"
        try:
            presp = session.get(props_url, timeout=30)
            if presp.status_code != 200:
                continue
            props_data = presp.json()
        except Exception:
            continue

        # Extract HR prop
        player_props = props_data.get("player_props", {})
        hr_data = player_props.get(AN_HR_PROP_KEY)
        if not hr_data:
            continue

        players_map = props_data.get("players", {})

        # hr_data is a list of player entries
        entries = hr_data if isinstance(hr_data, list) else [hr_data]
        for entry in entries:
            player_id = entry.get("player_id")
            player_name = "Unknown"
            if player_id and str(player_id) in players_map:
                player_name = players_map[str(player_id)].get(
                    "full_name", "Unknown")
            elif player_id and player_id in players_map:
                player_name = players_map[player_id].get(
                    "full_name", "Unknown")
            # Fallback to abbreviated name on the entry itself
            if player_name == "Unknown":
                player_name = (entry.get("player_abbr")
                               or entry.get("player_last_name")
                               or "Unknown")

            lines = entry.get("lines", {})
            if not lines:
                continue

            book_odds: dict[int, int] = {}
            for bid_str, line_data in lines.items():
                try:
                    bid = int(bid_str)
                except ValueError:
                    continue
                # line_data can be a list or dict
                if isinstance(line_data, list):
                    for ld in line_data:
                        odds = ld.get("odds")
                        if odds is not None:
                            book_odds[bid] = int(odds)
                            break
                elif isinstance(line_data, dict):
                    odds = line_data.get("odds")
                    if odds is not None:
                        book_odds[bid] = int(odds)

            if book_odds:
                hr_props.append({
                    "player":    player_name,
                    "game":      game_label,
                    "commence":  commence,
                    "sport":     "MLB",
                    "book_odds": book_odds,
                })

        # Small delay to avoid hammering the API
        time.sleep(0.3)

    print(f"  [HR Props] {len(hr_props)} player HR lines across "
          f"{len(games)} games")
    return hr_props


def find_hr_edges(hr_props: list[dict]) -> list[dict]:
    """
    For each HR prop, find +EV opportunities on DraftKings / FanDuel.

    Uses median consensus of all available books as fair probability
    (Pinnacle doesn't offer HR player props).

    Returns list of edge dicts compatible with the promo system:
      {
        "player", "game", "commence", "sport", "book", "market",
        "outcome", "point", "odds_american", "odds_decimal",
        "fair_prob", "implied_prob", "ev_pct", "kelly_pct",
        "num_other_books", "best_other_odds", "pinnacle_anchored",
        "all_book_odds",
      }
    """
    # Map AN book IDs to canonical keys
    target_book_ids = set()
    for bid, name in AN_BOOK_IDS.items():
        key = _book_key(bid)
        if key in TARGET_BOOKS:
            target_book_ids.add(bid)

    edges = []
    for prop in hr_props:
        book_odds = prop["book_odds"]
        if len(book_odds) < 2:
            continue  # need at least 2 books for consensus

        # Split into target (DK/FD) and other books
        target_entries = []
        other_entries = []
        for bid, am in book_odds.items():
            dec = american_to_decimal(am)
            if bid in target_book_ids:
                bk = _book_key(bid)
                display = "DraftKings" if bk == "draftkings" else "FanDuel"
                target_entries.append((display, dec, am, bid))
            elif bid in AN_BOOK_IDS:
                other_entries.append((AN_BOOK_IDS[bid], dec, am, bid))
            else:
                other_entries.append((f"Book_{bid}", dec, am, bid))

        if not target_entries:
            continue
        if not other_entries:
            continue  # need at least 1 other book

        # Fair probability from median of ALL books (including target)
        all_probs = sorted(
            [decimal_to_implied_prob(american_to_decimal(am))
             for am in book_odds.values()])
        mid = len(all_probs) // 2
        if len(all_probs) % 2 == 0:
            fair_prob = (all_probs[mid - 1] + all_probs[mid]) / 2
        else:
            fair_prob = all_probs[mid]

        fair_prob = max(0.02, min(0.60, fair_prob))  # HR probs are typically 5-25%

        best_other_dec = max(d for _, d, _, _ in other_entries)

        # Collect all book odds for comparison
        all_book_display: dict[str, int] = {}
        for bid, am in book_odds.items():
            if bid in AN_BOOK_IDS:
                name = AN_BOOK_IDS[bid]
            else:
                name = f"Book_{bid}"
            all_book_display[name] = am

        # Dedup target entries by canonical book (keep best odds)
        best_target: dict[str, tuple] = {}
        for display, dec, am, bid in target_entries:
            if display not in best_target or dec > best_target[display][1]:
                best_target[display] = (display, dec, am, bid)

        for display, dec, am, bid in best_target.values():
            ev = calc_ev(fair_prob, dec)
            # Lower EV threshold for HR props (boost makes them valuable)
            if ev >= -5.0:  # include slightly -EV since boost can flip them
                kelly = kelly_bet(fair_prob, dec)
                edges.append({
                    "sport":           prop["sport"],
                    "game":            prop["game"],
                    "commence":        prop["commence"],
                    "book":            display,
                    "market":          "hr",
                    "outcome":         prop["player"],
                    "point":           None,
                    "odds_american":   am,
                    "odds_decimal":    round(dec, 3),
                    "fair_prob":       round(fair_prob * 100, 1),
                    "implied_prob":    round(decimal_to_implied_prob(dec) * 100, 1),
                    "ev_pct":          round(ev, 2),
                    "kelly_pct":       round(kelly * 100, 2),
                    "num_other_books": len(other_entries),
                    "best_other_odds": format_american(best_other_dec),
                    "pinnacle_anchored": False,
                    "all_book_odds":   all_book_display,
                })

    edges.sort(key=lambda x: x["ev_pct"], reverse=True)
    return edges


def _build_hr_boost_rows(hr_edges: list[dict], promos: list[dict],
                         dk_bankroll: float, fd_bankroll: float) -> list[dict]:
    """
    Apply HR-applicable profit boosts to HR prop edges and return
    ranked rows ready for the HR Props Excel tab.

    Each row includes original odds, boosted odds, and boosted EV.
    """
    # Find HR-applicable promos (sport=mlb or all, market=hr or all)
    hr_promos = []
    for promo in promos:
        if promo.get("promo_type") == "bonus_bet":
            continue
        if promo["sport"] not in ("all", "mlb"):
            continue
        if promo["market"] not in ("all", "hr"):
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
            bet = clamp_bet(bankroll * boosted_kelly, edge["book"])
            if promo.get("max_bet") and bet > promo["max_bet"]:
                bet = promo["max_bet"]
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
        })
    return result


def fetch_player_props(session: requests.Session) -> list[dict]:
    """
    Fetch player props from The Odds API (primary) + Pinnacle guest API
    (supplementary).

    The Odds API provides DK, FD, Pinnacle, BetMGM, and more in a single
    call per sport. Pinnacle's direct API is kept as a backup for additional
    prop coverage.

    All sources are merged by (player, game, market, line, side) using fuzzy
    name matching. Returns the combined list with book_odds keyed by book name
    strings (e.g. "draftkings", "fanduel", "pinnacle").
    """
    all_raw: list[dict] = []
    target_sports = sorted(ODDS_API_PROP_MARKETS.keys())

    for sport in target_sports:
        sport_raw: list[dict] = []

        # ── The Odds API (primary — DK, FD, Pinnacle, BetMGM, etc.) ──
        try:
            api_raw = fetch_odds_api_props(sport)
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


def find_player_prop_edges(props: list[dict]) -> list[dict]:
    """
    Find +EV player prop edges on DraftKings / FanDuel.

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
            }
        pairs[pk][prop["side"]].update(prop["book_odds"])

    edges: list[dict] = []

    for (player, game, market, line_b), pair in pairs.items():
        line = pair["line"]

        for side in ("over", "under"):
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
                if ev >= MIN_EV_PERCENT:
                    kelly      = kelly_bet(fair_prob, dec)
                    side_label = "O" if side == "over" else "U"
                    edges.append({
                        "sport":             pair["sport"],
                        "game":              game,
                        "commence":          pair["commence"],
                        "book":              display,
                        "market":            market,
                        "outcome":           f"{player} {side_label}",
                        "point":             line,
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
                    })

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


def _build_parlay_rows(edges: list[dict], promos: list[dict],
                       dk_bankroll: float, fd_bankroll: float,
                       extra_edges: list[dict] | None = None,
                       parlay_pool: list = None,
                       promo_pool: list = None) -> list[dict]:
    """
    For each parlay promo, find the optimal N-leg parlay from qualifying edges.

    For ≤4 legs with ≤20 candidates: try all combinations (brute-force optimal).
    Otherwise: greedy top-N by EV%.

    Assumes legs are independent (different games) for probability math.
    Never puts two picks from the same game+market in one parlay (contradictory).

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
            if not _starts_within_days(edge, MAX_PROMO_DAYS):
                continue
            if edge.get("market") == "hr":
                continue  # HR props handled separately
            # Game-specific parlay: restrict all legs to the user-picked game
            if game_filter and edge.get("game") != game_filter:
                continue
            if promo["sport"] != "all":
                edge_sport = edge["sport"].lower().replace(" ", "")
                canonical = _LEAGUE_TO_SPORT.get(edge_sport, edge_sport)
                if canonical != promo["sport"]:
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
                            parlay_dec = 1.0
                            parlay_fair = 1.0
                            for leg in combo:
                                parlay_dec  *= leg["odds_decimal"]
                                parlay_fair *= leg["fair_prob"] / 100
                            if (parlay_min_dec is not None
                                    and parlay_dec < parlay_min_dec):
                                continue
                            boost_dec = apply_profit_boost(
                                parlay_dec, promo["boost_pct"])
                            ev = calc_ev(parlay_fair, boost_dec)
                            if ev > combo_ev:
                                combo_ev    = ev
                                combo_candidate = combo
                    else:
                        # Greedy: top-N by fair probability
                        greedy = tuple(candidate_pool[:try_legs])
                        parlay_dec = 1.0
                        parlay_fair = 1.0
                        for leg in greedy:
                            parlay_dec  *= leg["odds_decimal"]
                            parlay_fair *= leg["fair_prob"] / 100
                        if (parlay_min_dec is None
                                or parlay_dec >= parlay_min_dec):
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
    "all":    "All Sports",
    "nba":    "NBA",
    "mlb":    "MLB",
    "nhl":    "NHL",
    "nfl":    "NFL",
    "ncaab":  "NCAAB",
    "ncaaf":  "NCAAF",
    "soccer": "Soccer",
    "ufc":    "UFC",
    "pga":    "PGA Tour",
}

# Valid market labels
PROMO_MARKETS = {
    "all":         "All Markets",
    "h2h":         "Moneyline",
    "spreads":     "Spreads",
    "totals":      "Totals",
    "hr":          "Anytime HR",
    "strikeouts":  "Strikeouts",
    "total_bases": "Total Bases",
    "pts":         "NBA Points",
    "reb":         "NBA Rebounds",
    "ast":         "NBA Assists",
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


# Maps league names from AN to our canonical sport keys for promo matching
_LEAGUE_TO_SPORT = {
    "nba": "nba", "mlb": "mlb", "nhl": "nhl", "nfl": "nfl",
    "ncaab": "ncaab", "ncaaf": "ncaaf", "ufc": "ufc",
    "mls": "soccer", "ligue1": "soccer", "epl": "soccer",
    "laliga": "soccer", "bundesliga": "soccer", "seriea": "soccer",
    "soccer": "soccer",
    "pga": "pga",
}

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


def _promo_matches(promo: dict, edge: dict) -> bool:
    """Check if an edge qualifies for a straight promo.

    Parlay promos are handled by the dedicated Parlay Promos tab.
    HR props are handled by the dedicated HR Props tab.
    """
    if promo["book"] != edge["book"]:
        return False
    if not _starts_within_days(edge, MAX_PROMO_DAYS):
        return False
    # Game-specific promos: restrict to the one game the user selected
    game_filter = promo.get("game_filter")
    if game_filter and edge.get("game") != game_filter:
        return False
    # Parlay promos use a separate optimisation path
    if promo.get("promo_type") == "parlay":
        return False
    # HR props: not considered for all-purpose/straight promos. They live
    # in their own dedicated Excel tab (_build_hr_boost_rows) so the
    # all-market boost doesn't silently grab a HR prop when the user
    # meant to apply it to a game line.
    if edge.get("market") == "hr":
        return False
    if promo["sport"] != "all":
        edge_sport = edge["sport"].lower().replace(" ", "")
        canonical = _LEAGUE_TO_SPORT.get(edge_sport, edge_sport)
        if canonical != promo["sport"]:
            return False
    if promo["market"] != "all":
        if edge["market"].lower() != promo["market"]:
            return False
    if not _meets_min_odds(promo, edge):
        return False
    return True


# ──────────────────────────────────────────────────────────────────────
# Headless entry point (for Telegram bot / automation)
# ──────────────────────────────────────────────────────────────────────

def run_headless_fetch(dk_bankroll: float, fd_bankroll: float) -> dict:
    """
    Phase 1 of the headless pipeline: fetch odds + props, compute edges.

    The slow phase (~60-90s). Independent of any user promos or game
    filters, so the caller can pause here to interactively prompt for
    game-specific promo selections before invoking run_headless_finalize.

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
    print(f"\nFetching {len(AN_SPORTS)} sports in parallel "
          f"(Action Network + Pinnacle + Bovada)...\n")

    # Parallel fetch
    all_edges      = []
    parlay_pool    = []
    promo_pool     = []
    total_games    = 0
    pinn_anchored  = 0
    bovada_matched = 0

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(_fetch_sport_all, sport): sport
                   for sport in AN_SPORTS}
        for future in as_completed(futures):
            sport = futures[future]
            try:
                sport_name, events, pinn_m, bov_m = future.result()
                if events:
                    edges = find_edges(events, parlay_pool=parlay_pool,
                                       promo_pool=promo_pool)
                    all_edges.extend(edges)
                    total_games    += len(events)
                    pinn_anchored  += pinn_m
                    bovada_matched += bov_m
                    extras = []
                    if pinn_m: extras.append(f"Pinnacle×{pinn_m}")
                    if bov_m:  extras.append(f"Bovada×{bov_m}")
                    note = f"  [{', '.join(extras)}]" if extras else ""
                    print(f"  [{sport_name:<6}] {len(events):>3} games"
                          f" → {len(edges):>3} +EV edges{note}")
            except Exception as e:
                print(f"  [{sport}] Error: {e}")

    all_edges.sort(key=lambda x: x["ev_pct"], reverse=True)

    # Fetch player props
    print(f"\nFetching player props (MLB HR + PGA Golf + O/U Props)...")
    props_session     = make_session()
    hr_props_raw      = fetch_hr_props(props_session)
    hr_edges          = find_hr_edges(hr_props_raw) if hr_props_raw else []
    golf_props_raw    = fetch_golf_props(props_session)
    golf_edges        = find_golf_edges(golf_props_raw) if golf_props_raw else []
    pp_props_raw      = fetch_player_props(props_session)
    player_prop_edges = find_player_prop_edges(pp_props_raw) if pp_props_raw else []

    return {
        "all_edges":         all_edges,
        "hr_edges":          hr_edges,
        "golf_edges":        golf_edges,
        "player_prop_edges": player_prop_edges,
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
                sport_name, events, _, _ = future.result()
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
                    promo_pool=promo_pool)

    # Build play_edges for the bot
    play_edges = [e for e in all_edges if _qualifies(e)]

    # Include qualifying player props (e.g. NBA points)
    play_props = [e for e in player_prop_edges if _qualifies_prop(e)]
    if play_props:
        print(f"  {len(play_props)} player prop(s) qualify for Play These")
        play_edges.extend(play_props)

    promo_picks = compute_promo_picks(promo_pool, promos,
                                      extra_edges=player_prop_edges)

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
        },
    }


def compute_promo_picks(all_edges: list[dict],
                        promos: list[dict],
                        extra_edges: list[dict] | None = None) -> list[dict]:
    """Build the [{promo, edge}] picks list — best edge per straight promo,
    deduped so two promos don't pick the same edge.

    Pool is the broader promo_pool (all DK/FD outcomes, not just pre-boost
    +EV ones), combined with player_prop_edges. For profit boosts, rank by
    BOOSTED EV so a long-shot that's mildly -EV pre-boost but clearly +EV
    post-boost beats a modestly +EV pre-boost favorite. For bonus bets,
    rank by expected profit (fair_prob × profit_if_win)."""
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
            best = None
            reason: str | None = None
            is_fallback = False
            if pool:
                if promo.get("promo_type") == "bonus_bet":
                    amt = promo.get("bet_amount", 0)
                    best = max(pool,
                               key=lambda e: (e["fair_prob"] / 100)
                                             * amt * (e["odds_decimal"] - 1))
                else:
                    boost_pct = promo.get("boost_pct", 0) or 0

                    def _boosted_ev(e, _bp=boost_pct):
                        boost_dec = apply_profit_boost(
                            e["odds_decimal"], _bp)
                        return calc_ev(e["fair_prob"] / 100, boost_dec)

                    ranked = sorted(pool, key=_boosted_ev, reverse=True)
                    top_candidate = ranked[0]
                    top_ev = _boosted_ev(top_candidate)
                    # Preferred: post-boost EV clears MIN_EV_PERCENT.
                    # Fallback: no +EV option, so take least-bad — burning
                    # a promo on a marginal line beats letting it expire.
                    # Marked is_fallback so display can flag it clearly.
                    best = top_candidate
                    is_fallback = top_ev < MIN_EV_PERCENT
                if best is not None:
                    used_keys.add(_make_pick_key(best))

            # Diagnostic message when nothing was selected (empty pool).
            if best is None:
                reason = _promo_no_pick_reason(
                    promo, combined, matched_all, pool)

            picks.append({
                "promo":       promo,
                "edge":        best,
                "copy_idx":    copy_idx,
                "copy_of":     count,
                "reason":      reason,
                "is_fallback": is_fallback,
            })
    return picks


def _promo_no_pick_reason(promo: dict,
                          combined: list[dict],
                          matched_all: list[dict],
                          pool: list[dict]) -> str:
    """Human-readable explanation of why no pick was selected. Only reached
    when the pool was genuinely empty — with the fallback path in place,
    any non-empty pool will produce a pick (marked is_fallback if -EV)."""
    if not matched_all:
        # Re-check with min_odds relaxed to see whether the slate simply
        # lacks long-enough lines (common for small slates) versus a
        # broader filter (sport/market) blocking everything.
        saved_min = promo.get("min_odds")
        promo["min_odds"] = None
        try:
            no_min = [e for e in combined if _promo_matches(promo, e)]
        finally:
            promo["min_odds"] = saved_min
        if not no_min:
            return (f"No {promo['book']} outcomes match this promo's "
                    f"sport/market filters in today's slate.")
        min_odds = promo.get("min_odds")
        if min_odds is not None:
            min_am = format_american(american_to_decimal(min_odds))
            longest = max(no_min, key=lambda e: e["odds_decimal"])
            longest_am = format_american(longest["odds_decimal"])
            outcome = longest.get("outcome", "?")
            return (f"0 {promo['book']} candidates at {min_am} or longer. "
                    f"Longest {promo['book']} line today: "
                    f"{outcome} {longest_am}.")
        return "0 candidates match this promo's filters."
    if not pool:
        return (f"All {len(matched_all)} matching candidate(s) already "
                f"assigned to other promos.")
    return "No qualifying pick found."


def eligible_games_for_promo(promo: dict, all_edges: list[dict],
                             player_prop_edges: list[dict] | None = None,
                             ) -> list[str]:
    """List of "Away @ Home" game strings this promo could apply to,
    using all promo filters except game_filter itself.

    Used by the Telegram bot to render a tap-keyboard for game selection.
    Accepts the broader promo_pool (all DK/FD outcomes) so games without a
    pre-boost +EV edge still appear — the boost may make them viable.
    For parlay promos, min_odds is not enforced here since the parlay-level
    odds (not per-leg) must clear the floor.
    """
    combined = list(all_edges) + list(player_prop_edges or [])
    is_parlay = promo.get("promo_type") == "parlay"
    saved_filter = promo.get("game_filter")
    promo["game_filter"] = None
    try:
        if is_parlay:
            # Parlay path: skip min_odds + parlay-type skip inside _promo_matches
            games = sorted({
                e["game"] for e in combined
                if e["book"] == promo["book"]
                and _starts_within_days(e, MAX_PROMO_DAYS)
                and e.get("market") != "hr"
                and (promo["sport"] == "all"
                     or _LEAGUE_TO_SPORT.get(
                         e["sport"].lower().replace(" ", ""),
                         e["sport"].lower().replace(" ", "")) == promo["sport"])
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
    the user for game-specific promo selections in between."""
    fetched = run_headless_fetch(dk_bankroll, fd_bankroll)
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
          f"(Action Network + Pinnacle + Bovada)...\n")

    # ── Parallel fetch ────────────────────────────────────────────────
    all_edges      = []
    parlay_pool    = []
    promo_pool     = []
    total_games    = 0
    pinn_anchored  = 0
    bovada_matched = 0

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(_fetch_sport_all, sport): sport
                   for sport in AN_SPORTS}
        for future in as_completed(futures):
            sport = futures[future]
            try:
                sport_name, events, pinn_m, bov_m = future.result()
                if events:
                    edges = find_edges(events, parlay_pool=parlay_pool,
                                       promo_pool=promo_pool)
                    all_edges.extend(edges)
                    total_games    += len(events)
                    pinn_anchored  += pinn_m
                    bovada_matched += bov_m
                    extras = []
                    if pinn_m: extras.append(f"Pinnacle×{pinn_m}")
                    if bov_m:  extras.append(f"Bovada×{bov_m}")
                    note = f"  [{', '.join(extras)}]" if extras else ""
                    print(f"  [{sport_name:<6}] {len(events):>3} games"
                          f" → {len(edges):>3} +EV edges{note}")
                elif not events:
                    # fetch_action_network already prints why if games
                    # existed but were filtered — only print if truly 0
                    pass
            except Exception as e:
                print(f"  [{sport}] Error: {e}")

    all_edges.sort(key=lambda x: x["ev_pct"], reverse=True)

    # ── Fetch player props: HR, Golf, Strikeouts/TB/Pts/Reb/Ast ────────
    print(f"\nFetching player props (MLB HR + PGA Golf + O/U Props)...")
    props_session  = make_session()
    hr_props_raw   = fetch_hr_props(props_session)
    hr_edges       = find_hr_edges(hr_props_raw) if hr_props_raw else []
    golf_props_raw = fetch_golf_props(props_session)
    golf_edges     = find_golf_edges(golf_props_raw) if golf_props_raw else []
    pp_props_raw       = fetch_player_props(props_session)
    player_prop_edges  = find_player_prop_edges(pp_props_raw) if pp_props_raw else []

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
                promo_pool=promo_pool)

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
                                               promo["boost_pct"])
                boosted_ev = calc_ev(best["fair_prob"] / 100, boost_dec)
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
