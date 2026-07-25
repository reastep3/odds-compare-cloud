from __future__ import annotations

"""
backtest.py — Odds Compare: Play These pick backtester
=======================================================
Scans archive/ for ev_edges_*.xlsx files, reads ONLY the "Play These"
tab (backtest-validated picks — no promos), deduplicates every pick to
its FIRST ever appearance (earliest file date, so odds can't be
cherry-picked after a line move), fetches real game results from
ESPN's free scoreboard API, and writes a detailed P&L report.

Run:
    python3 backtest.py          ← tkinter dialog prompts for date range

Output:
    backtest_STARTDATE_ENDDATE.xlsx  (saved to same folder as this script)

Notes:
  • "Play These" is read by column position (col 6 = Market, col 13 = Bet Amt)
    so both the old single-table format and the new DK / FD section format
    are parsed correctly without needing to detect headers.
  • The Promo Picks section at the bottom of Play These uses a different column
    layout and is automatically excluded (col 6 ≠ a market string there).
  • HR props and Golf props cannot be auto-resolved from scores — marked MANUAL.
  • Edge durability: picks that appeared in 3+ sheets before the game are flagged
    as "persistent" — a signal the edge held as the line moved.
"""

import glob
import html
import json
import os
import re
import shutil
import time
from collections import defaultdict
from datetime import datetime, timedelta, date
from difflib import SequenceMatcher

import requests
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter
from openpyxl.chart import BarChart, LineChart, Reference


# tkinter is imported lazily inside prompt_date_range() — it's only used by
# the standalone CLI, never by the Telegram bot, and the Fly slim image
# doesn't ship Tcl/Tk system libs (libtk8.6.so).


# ── Config ─────────────────────────────────────────────────────────────────────

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # dotenv optional; env vars may be set by the OS

ESPN_BASE  = "https://site.api.espn.com/apis/site/v2/sports"
# SCRIPT_DIR historically means "where data lives" — reads from OUTPUT_DIR
# env var on cloud, falls back to the script's own directory locally.
_MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPT_DIR  = os.environ.get("OUTPUT_DIR", _MODULE_DIR)
ARCHIVE_DIR = os.path.join(SCRIPT_DIR, "archive")

ESPN_SPORT_MAP = {
    "nba":    "basketball/nba",
    "ncaab":  "basketball/mens-college-basketball",
    "wnba":   "basketball/wnba",
    "nfl":    "football/nfl",
    "ncaaf":  "football/college-football",
    "mlb":    "baseball/mlb",
    "nhl":    "hockey/nhl",
    "mma":    "mma/ufc",
    "ufc":    "mma/ufc",
    # Soccer — one key per league, each maps to its own ESPN slug
    "soccer":        "soccer/usa.1",   # MLS (default/fallback)
    "mls":           "soccer/usa.1",
    "laliga":        "soccer/esp.1",   # Spanish La Liga
    "ligue1":        "soccer/fra.1",   # French Ligue 1
    "epl":           "soccer/eng.1",   # English Premier League
    "premier_league":"soccer/eng.1",
    "seriea":        "soccer/ita.1",   # Italian Serie A
    "bundesliga":    "soccer/ger.1",   # German Bundesliga
    "champions":     "soccer/uefa.champions",
    "europa":        "soccer/uefa.europa",
    "conference":    "soccer/uefa.europa.conf",
    "ligamx":        "soccer/mex.1",
    # FIFA World Cup 2026 — slug verified 2026-06-10 (returns the Jun 11
    # openers). Promo-driven picks (tournament-wide boosts) grade here.
    "world_cup":     "soccer/fifa.world",
    # Tracking-only adds 2026-07-09
    "brazil_serie_a":    "soccer/bra.1",
    "argentina_primera": "soccer/arg.1",
    # CFL is NOT here: ESPN dropped CFL (football/cfl returns no events —
    # discovered 2026-07-09 when four Jul 3–6 picks graded NO_RESULT).
    # CFL grades via TheSportsDB instead (THESPORTSDB_SPORTS below).
    # AFL / Boxing / Cricket / Tennis are not auto-graded via ESPN — they
    # surface in the picks output but skip backtest grading. (ESPN's
    # AFL coverage is intermittent and tennis/cricket use different slug
    # conventions per tournament.)
}

# Normalise raw AN league names → ESPN_SPORT_MAP keys.
# AN stores the full league name in the Sport column (e.g. "National Hockey
# League", "College Basketball"), which must be mapped before ESPN lookup.
_SPORT_NORM: dict[str, str] = {
    # NBA
    "nba":                              "nba",
    "national basketball association":  "nba",
    # MLB
    "mlb":                              "mlb",
    "major league baseball":            "mlb",
    # NHL
    "nhl":                              "nhl",
    "national hockey league":           "nhl",
    # NFL
    "nfl":                              "nfl",
    "national football league":         "nfl",
    # NCAAB
    "ncaab":                            "ncaab",
    "college basketball":               "ncaab",
    "men's college basketball":         "ncaab",
    "mens college basketball":          "ncaab",
    "college basketball (m)":           "ncaab",
    "ncaa basketball":                  "ncaab",
    # NCAAF
    "ncaaf":                            "ncaaf",
    "college football":                 "ncaaf",
    "ncaa football":                    "ncaaf",
    # UFC / MMA
    "ufc":                              "ufc",
    "mma":                              "ufc",
    "ultimate fighting championship":   "ufc",
    # Soccer — MLS
    "soccer":                           "soccer",
    "mls":                              "mls",
    "major league soccer":              "mls",
    # Soccer — La Liga
    "laliga":                           "laliga",
    "la liga":                          "laliga",
    "spanish la liga":                  "laliga",
    "spain - laliga":                   "laliga",
    "la liga - spain":                  "laliga",
    "laliga - spain":                   "laliga",
    "esp.1":                            "laliga",
    # Soccer — Ligue 1
    "ligue1":                           "ligue1",
    "ligue 1":                          "ligue1",
    "french ligue 1":                   "ligue1",
    "france - ligue 1":                 "ligue1",
    "ligue 1 - france":                 "ligue1",
    "ligue1 - france":                  "ligue1",
    "fra.1":                            "ligue1",
    # Soccer — Premier League
    "epl":                              "epl",
    "premier league":                   "epl",
    "english premier league":           "epl",
    "england - premier league":         "epl",
    "premier league - england":         "epl",
    "epl - england":                    "epl",
    "eng.1":                            "epl",
    # Soccer — Serie A
    "seriea":                           "seriea",
    "serie a":                          "seriea",
    "italian serie a":                  "seriea",
    "italy - serie a":                  "seriea",
    "serie a - italy":                  "seriea",
    "seriea - italy":                   "seriea",
    "ita.1":                            "seriea",
    # Soccer — Bundesliga
    "bundesliga":                       "bundesliga",
    "german bundesliga":                "bundesliga",
    "germany - bundesliga":             "bundesliga",
    "bundesliga - germany":             "bundesliga",
    "ger.1":                            "bundesliga",
    # Soccer — Champions League / Europa / Conference
    "champions league":                 "champions",
    "uefa champions league":            "champions",
    "europa league":                    "europa",
    "uefa europa league":               "europa",
    "conference league":                "conference",
    "uefa europa conference league":    "conference",
    # Soccer — Liga MX
    "liga mx":                          "ligamx",
    "mexico liga mx":                   "ligamx",
    "mex.1":                            "ligamx",
    # Soccer — FIFA World Cup (Odds API sport_title is "FIFA World Cup")
    "fifa world cup":                   "world_cup",
    "world cup":                        "world_cup",
    "fifa.world":                       "world_cup",
    # WNBA
    "wnba":                             "wnba",
    "women's national basketball association": "wnba",
    # CFL (ESPN-graded) — tracking-only add 2026-07-09
    "cfl":                              "cfl",
    "canadian football league":         "cfl",
    # Brazil / Argentina soccer (ESPN-graded) — tracking-only 2026-07-09.
    # Odds API titles carry accents; include unaccented variants too.
    "brazil série a":                   "brazil_serie_a",
    "brazil serie a":                   "brazil_serie_a",
    "primera división - argentina":     "argentina_primera",
    "primera division - argentina":     "argentina_primera",
    # KBO / NPB — TheSportsDB-graded (THESPORTSDB_SPORTS, 2026-07-09)
    "kbo":                              "kbo",
    "kbo league":                       "kbo",
    "korean kbo league":                "kbo",
    "npb":                              "npb",
    "nippon baseball league":           "npb",
    # AFL (Squiggle-graded)
    "afl":                              "afl",
    "australian football league":       "afl",
    "aussie rules":                     "afl",
    # Cricket — currently IPL only (ESPN-cricket-graded). Other tournaments
    # would need their own ESPN league ID added to ESPN_CRICKET_LEAGUES.
    "cricket":                          "cricket",
    "ipl":                              "cricket",
    "indian premier league":            "cricket",
    # Tennis — TennisExplorer-graded, covers both ATP and WTA singles
    "tennis":                           "tennis",
    "atp":                              "tennis",
    "wta":                              "tennis",
    # Handle already-normalised keys passed through
    **{k: k for k in ESPN_SPORT_MAP},
}


def _normalise_sport(raw: str) -> str:
    """Map any AN league-name variant to the canonical ESPN_SPORT_MAP key."""
    lower = raw.lower().strip()
    if lower in _SPORT_NORM:
        return _SPORT_NORM[lower]
    # Tennis tournaments arrive as "ATP <event>" / "WTA <event>" — e.g.
    # "ATP ITALIAN OPEN", "WTA MADRID OPEN". TennisExplorer covers both
    # tours, so collapse all of them to the single "tennis" key.
    if lower.startswith("atp ") or lower.startswith("wta "):
        return "tennis"
    return lower


# ── Non-ESPN result sources ──────────────────────────────────────────
# Sports graded via Squiggle (api.squiggle.com.au — free, no key, AFL only)
SQUIGGLE_SPORTS = {"afl"}

# Sports graded via TheSportsDB (free tier, finals carry strStatus "FT").
# 2026-07-09: covers the tracking-only adds ESPN can't grade — KBO/NPB
# baseball (verified: English team names match the Odds API's exactly)
# and CFL (see the ESPN_SPORT_MAP note). Values are TheSportsDB league ids.
THESPORTSDB_SPORTS = {"kbo": 4830, "npb": 4591, "cfl": 4405}

# Sports graded by scraping box.live (free, public, server-rendered HTML)
BOXLIVE_SPORTS = {"boxing"}

# Sports graded by scraping tennisexplorer.com (server-rendered, free,
# date-queryable via ?type=all&year=Y&month=M&day=D). Doubles entries are
# filtered out since the bot only bets singles.
TENNISEXPLORER_SPORTS = {"tennis"}

# Sports graded via ESPN's cricket endpoint, which uses NUMERIC league IDs
# rather than slugs. Map normalized internal sport key → ESPN league ID.
# To find an ID: check `https://site.api.espn.com/apis/site/v2/sports/cricket`
# for the active league listing. 8048 = IPL 2026 (rolls each year).
ESPN_CRICKET_LEAGUES: dict[str, int] = {
    "cricket": 8048,   # IPL — covers cricket_ipl picks
}

# Sports auto-graded by some result source (ESPN, Squiggle, box.live, etc.)
SCOREABLE = (set(ESPN_SPORT_MAP.keys())
             | SQUIGGLE_SPORTS
             | BOXLIVE_SPORTS
             | set(THESPORTSDB_SPORTS)
             | TENNISEXPLORER_SPORTS
             | set(ESPN_CRICKET_LEAGUES.keys()))

# "Play These" column positions (1-based). Stable across old and new sheet layouts.
_COL_SPORT      = 2
_COL_GAME       = 3
_COL_GAMETIME   = 4
_COL_BOOK       = 5
_COL_MARKET     = 6
_COL_PICK       = 7
_COL_LINE       = 8
_COL_ODDS       = 9
_COL_EV         = 10
_COL_CONFIDENCE = 11
_COL_KELLY      = 12
_COL_BET_AMT    = 13
_COL_POTENTIAL  = 14

# "Promo Picks" section column positions (1-based) — different layout from main section
_PROMO_COL_DESC       = 1   # Promo description (encodes book, boost%, sport scope)
_PROMO_COL_GAME       = 2   # Game ("Away @ Home")
_PROMO_COL_BOOK       = 3   # Book name
_PROMO_COL_MARKET     = 4   # Market (H2H / SPREADS / TOTALS / HR)
_PROMO_COL_PICK       = 5   # Pick
_PROMO_COL_LINE       = 6   # Line (spread/total; None for H2H)
_PROMO_COL_ORIG_ODDS  = 7   # Original odds (American integer)
_PROMO_COL_BOOST_ODDS = 8   # Boosted odds (string "+xxx" or "FREE BET")
_PROMO_COL_ORIG_EV    = 9   # Original EV %
_PROMO_COL_BOOST_EV   = 10  # Boosted EV %
_PROMO_COL_EV_GAIN    = 11  # EV gain from boost
_PROMO_COL_BET_AMT    = 12  # Bet amount
_PROMO_COL_POTENTIAL  = 13  # Potential profit
_PROMO_COL_MIN_ODDS   = 14  # Min odds requirement

VALID_MARKETS = {"H2H", "SPREADS", "TOTALS", "HR"}

# "EV Edges" tab column positions (1-based).
# Col 1 = rank, then: Sport Game GameTime Book Market Pick Line Odds Decimal
# FairProb% ImpliedProb% EV% Edge% Kelly% BetAmt Potential #Books BestOther Pinnacle
_EV_COL_SPORT     = 2
_EV_COL_GAME      = 3
_EV_COL_GAMETIME  = 4
_EV_COL_BOOK      = 5
_EV_COL_MARKET    = 6
_EV_COL_PICK      = 7
_EV_COL_LINE      = 8
_EV_COL_ODDS      = 9
_EV_COL_DECIMAL   = 10
_EV_COL_EV_PCT    = 13
_EV_COL_KELLY     = 15
_EV_COL_BET_AMT   = 16
_EV_COL_POTENTIAL = 17
_EV_COL_NUM_BOOKS = 18
_EV_COL_PINNACLE  = 20
_EV_COL_CAL_EV    = 21    # shadow calibrated EV — appended 2026-07-25

EV_THRESHOLDS = [0, 1, 2, 3, 4, 5, 7, 10]   # min EV % values to bucket by

# ── Player prop constants ──────────────────────────────────────────────────────

# Market display name (Odds Comparison "Market" col) → internal key
PROP_DISPLAY_TO_KEY: dict[str, str] = {
    "points":       "pts",
    "pts":          "pts",
    "rebounds":     "reb",
    "reb":          "reb",
    "assists":      "ast",
    "ast":          "ast",
    "strikeouts":   "strikeouts",
    "total bases":  "total_bases",
}

# ESPN box-score stat label per internal prop market key
# NBA labels: MIN FG 3PT FT OREB DREB REB AST STL BLK TO PF +/- PTS
# MLB pitcher: IP H R ER BB K HR ERA PC-ST
# MLB batter:  AB R H 2B 3B HR RBI BB SO SB AVG
PROP_ESPN_STAT: dict[str, str | None] = {
    "pts":         "PTS",
    "reb":         "REB",
    "ast":         "AST",
    "strikeouts":  "K",       # pitcher strikeouts
    "total_bases": "TB",      # MLB Stats API exposes TB directly
}

# Markets where a missing stat for a roster player who DNP'd should be
# treated as 0 (rather than NO_RESULT). All current props are counting
# stats: missing = didn't accrue any.
_PROP_DNP_DEFAULT_ZERO = {"pts", "reb", "ast", "strikeouts", "total_bases"}

PROP_BANKROLL        = 100.0  # hypothetical bankroll for Kelly sizing props
PROP_KELLY_FRACTION  = 0.5    # half-Kelly (matches Play These)
PROP_MAX_BET         = 5.0    # cap per-prop bet


# ── Date range prompt ──────────────────────────────────────────────────────────

def prompt_date_range() -> tuple[date, date] | None:
    """Tkinter dialog. Returns (start, end) dates or None if cancelled."""
    import tkinter as tk
    from tkinter import messagebox
    result = [None]
    root = tk.Tk()
    root.title("Backtest — Select Date Range")
    root.resizable(False, False)

    tk.Label(root, text="Backtest Date Range",
             font=("Helvetica", 14, "bold"), pady=10).pack()

    frame = tk.Frame(root, padx=24, pady=6)
    frame.pack()

    default_start = (date.today() - timedelta(days=30)).strftime("%Y-%m-%d")
    default_end   = date.today().strftime("%Y-%m-%d")

    tk.Label(frame, text="Start date (YYYY-MM-DD):").grid(
        row=0, column=0, sticky="e", padx=6, pady=5)
    start_var = tk.StringVar(value=default_start)
    tk.Entry(frame, textvariable=start_var, width=14).grid(
        row=0, column=1, padx=6)

    tk.Label(frame, text="End date   (YYYY-MM-DD):").grid(
        row=1, column=0, sticky="e", padx=6, pady=5)
    end_var = tk.StringVar(value=default_end)
    tk.Entry(frame, textvariable=end_var, width=14).grid(
        row=1, column=1, padx=6)

    def apply_preset(days: int):
        today = date.today()
        start_var.set((today - timedelta(days=days)).strftime("%Y-%m-%d"))
        end_var.set(today.strftime("%Y-%m-%d"))

    presets_frame = tk.Frame(root, pady=4)
    presets_frame.pack()
    tk.Label(presets_frame, text="Presets:", font=("Helvetica", 9)).pack(side="left", padx=(0, 6))
    for label, days in [("Last Day", 1), ("Last Week", 7), ("Last Month", 30), ("Last 3 Months", 90)]:
        tk.Button(presets_frame, text=label, font=("Helvetica", 9),
                  command=lambda d=days: apply_preset(d),
                  padx=6, pady=2).pack(side="left", padx=3)

    def on_ok():
        try:
            s = datetime.strptime(start_var.get().strip(), "%Y-%m-%d").date()
            e = datetime.strptime(end_var.get().strip(),   "%Y-%m-%d").date()
        except ValueError:
            messagebox.showerror("Error", "Invalid date. Use YYYY-MM-DD.")
            return
        if s > e:
            messagebox.showerror("Error", "Start date must be ≤ end date.")
            return
        result[0] = (s, e)
        root.destroy()

    btn = tk.Frame(root, pady=12)
    btn.pack()
    tk.Button(btn, text="Run Backtest", command=on_ok,
              bg="#1F4E79", fg="white",
              font=("Helvetica", 11, "bold"), padx=14).pack(side="left", padx=8)
    tk.Button(btn, text="Cancel", command=root.destroy,
              padx=10).pack(side="left", padx=8)

    root.mainloop()
    return result[0]


# ── Archive scanning ───────────────────────────────────────────────────────────

# Filenames written by odds_compare get an "_ANONLY" suffix when every sport
# fell back to Action Network on that run (Odds API quota exhausted).
# Backtest can include or exclude these to keep skill measurement clean.
AN_ONLY_MODES = {"exclude", "include", "only"}


def _is_an_only_file(path: str) -> bool:
    """True if the archive filename has the _ANONLY suffix."""
    return "_ANONLY" in os.path.basename(path).upper()


def _discover_archive_files(start_dt: date, end_dt: date,
                             an_only_mode: str = "exclude"
                             ) -> list[tuple[date, str]]:
    """
    Scan archive/ + script dir for ev_edges_*.xlsx in date range.

    an_only_mode:
      • "exclude" (default) — skip files marked _ANONLY
      • "include"           — return everything
      • "only"              — return only _ANONLY files
    """
    if an_only_mode not in AN_ONLY_MODES:
        an_only_mode = "exclude"

    patterns = [
        os.path.join(ARCHIVE_DIR, "ev_edges_*.xlsx"),
        os.path.join(SCRIPT_DIR,  "ev_edges_*.xlsx"),
    ]
    all_files: list[str] = []
    seen_base: set[str]  = set()
    for pat in patterns:
        for f in glob.glob(pat):
            b = os.path.basename(f)
            if b not in seen_base:
                seen_base.add(b)
                all_files.append(f)

    in_range: list[tuple[date, str]] = []
    for f in sorted(all_files):
        fd = _file_date(f)
        if fd is None or not (start_dt <= fd <= end_dt):
            continue
        is_an = _is_an_only_file(f)
        if an_only_mode == "exclude" and is_an:
            continue
        if an_only_mode == "only" and not is_an:
            continue
        in_range.append((fd, f))
    return in_range


def _file_date(path: str) -> date | None:
    """Extract the generation date from ev_edges_YYYYMMDD_HHMM.xlsx."""
    m = re.search(r"ev_edges_(\d{8})_\d{4}", os.path.basename(path))
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y%m%d").date()
    except ValueError:
        return None


def _parse_game_date(game_time_str, file_dt: date) -> str | None:
    """
    Parse a game timestamp into a "YYYYMMDD" string for ESPN.

    Accepts multiple input formats:
      • "MM/DD HH:MM AM/PM TZ"  (standard odds_compare output)
      • Python datetime / date object  (if Excel auto-converted the cell)
      • ISO strings like "2026-04-02T19:05:00Z"
    """
    # ── datetime / date object (Excel auto-conversion) ────────────────
    if hasattr(game_time_str, "year"):
        try:
            candidate = (game_time_str.date()
                         if hasattr(game_time_str, "date")
                         else date(game_time_str.year,
                                   game_time_str.month,
                                   game_time_str.day))
            delta = (candidate - file_dt).days
            if -7 <= delta <= 30:
                return candidate.strftime("%Y%m%d")
        except Exception:
            pass
        return None

    s = str(game_time_str).strip()

    # ── ISO / YYYY-MM-DD format ───────────────────────────────────────
    iso = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
    if iso:
        try:
            candidate = date(int(iso.group(1)),
                             int(iso.group(2)),
                             int(iso.group(3)))
            delta = (candidate - file_dt).days
            if -7 <= delta <= 30:
                return candidate.strftime("%Y%m%d")
        except ValueError:
            pass
        return None

    # ── Standard "MM/DD ..." format ───────────────────────────────────
    m = re.match(r"(\d{1,2})/(\d{1,2})", s)
    if not m:
        return None
    month, day = int(m.group(1)), int(m.group(2))

    for year_offset in [0, 1, -1]:
        try:
            candidate = date(file_dt.year + year_offset, month, day)
        except ValueError:
            continue
        delta = (candidate - file_dt).days
        if -7 <= delta <= 30:          # wider: covers late-added picks
            return candidate.strftime("%Y%m%d")

    try:
        return date(file_dt.year, month, day).strftime("%Y%m%d")
    except ValueError:
        return None


def _read_play_these(filepath: str, file_dt: date) -> list[dict]:
    """
    Read pick rows from the "Play These" tab of one archive file.

    Detection logic: a pick row has a recognised market string in col 6
    AND a positive numeric bet amount in col 13.  This automatically skips:
      • Section banner rows  (col 6 is empty or a book name)
      • Column header rows   (col 6 = "Market" — not in VALID_MARKETS)
      • Totals rows          (col 6 is empty)
      • Promo Picks section  (those rows have a different column layout where
                              col 6 is Orig Odds — a number, not a string)
    """
    try:
        wb = openpyxl.load_workbook(filepath, read_only=True, data_only=True)
    except Exception as e:
        print(f"  [WARN] Cannot open {os.path.basename(filepath)}: {e}")
        return []

    if "Play These" not in wb.sheetnames:
        wb.close()
        return []

    ws = wb["Play These"]
    picks = []

    for row_vals in ws.iter_rows(min_row=2, values_only=True):
        if len(row_vals) < _COL_BET_AMT:
            continue

        raw_market  = row_vals[_COL_MARKET - 1]
        raw_bet_amt = row_vals[_COL_BET_AMT - 1]

        if not isinstance(raw_market, str):
            continue
        market = raw_market.strip().upper()
        if market not in VALID_MARKETS:
            continue
        # Allow bet_amt=0 (the run may have been /run_skip which sets
        # bankroll=0). The market filter above already rejects header,
        # banner, and totals rows, so we don't need bet_amt as a row
        # validity guard. The bet column must still be numeric (None
        # in totals/banner rows).
        if not isinstance(raw_bet_amt, (int, float)) or raw_bet_amt < 0:
            continue

        def _s(v): return str(v).strip() if v is not None else ""
        def _f(v):
            try:    return float(v)
            except: return None

        sport     = _s(row_vals[_COL_SPORT - 1]).lower()
        game      = _s(row_vals[_COL_GAME - 1])
        game_time = _s(row_vals[_COL_GAMETIME - 1])
        book      = _s(row_vals[_COL_BOOK - 1])
        pick      = _s(row_vals[_COL_PICK - 1])
        line_raw  = row_vals[_COL_LINE - 1]
        odds_raw  = row_vals[_COL_ODDS - 1]
        ev_raw    = row_vals[_COL_EV - 1]
        conf      = _s(row_vals[_COL_CONFIDENCE - 1])
        kelly_raw = row_vals[_COL_KELLY - 1] if len(row_vals) >= _COL_KELLY else None
        bet_amt   = float(raw_bet_amt)
        potential = row_vals[_COL_POTENTIAL - 1]

        if not sport or not game or not pick:
            continue

        # Line value (spread / total number)
        line_num = None
        if line_raw not in (None, ""):
            line_num = _f(line_raw)

        # American odds → also derive decimal for P&L
        odds_am = None
        if odds_raw is not None:
            if isinstance(odds_raw, (int, float)):
                odds_am = int(odds_raw)
            else:
                s = str(odds_raw).strip().replace("+", "")
                try:   odds_am = int(s)
                except: pass

        decimal = None
        if odds_am is not None:
            decimal = (odds_am / 100 + 1) if odds_am > 0 \
                      else (100 / abs(odds_am) + 1)

        game_date = _parse_game_date(game_time, file_dt)

        picks.append({
            "sport":       sport,
            "game":        game,
            "game_time":   game_time,
            "game_date":   game_date,
            "book":        book,
            "market":      market,
            "pick":        pick,
            "line":        line_num,
            "odds":        odds_am,
            "decimal":     round(decimal, 4) if decimal else None,
            "ev_pct":      _f(ev_raw) or 0.0,
            "confidence":  conf,
            "kelly_pct":   _f(kelly_raw),
            "bet_amount":  bet_amt,
            "potential":   _f(potential),
            "source_file": os.path.basename(filepath),
            "file_date":   file_dt,
            # resolved later
            "result":       None,
            "pnl":          None,
            "actual_result": None,
            "times_seen":   1,
            "persistent":   False,
        })

    wb.close()
    return picks


def _extract_sport_from_promo(promo_desc: str) -> str | None:
    """
    Infer sport from a promo description string.
    Returns a normalised ESPN_SPORT_MAP key, or None for "All Sports" promos.
    """
    desc = promo_desc.lower()
    for needle, sport in [
        ("mlb",                "mlb"),
        ("nba",                "nba"),
        ("nhl",                "nhl"),
        ("nfl",                "nfl"),
        ("ncaab",              "ncaab"),
        ("ncaaf",              "ncaaf"),
        ("college basketball", "ncaab"),
        ("college football",   "ncaaf"),
        ("ufc",                "ufc"),
        ("mma",                "ufc"),
        ("premier league",     "epl"),
        ("bundesliga",         "bundesliga"),
        ("serie a",            "seriea"),
        ("ligue 1",            "ligue1"),
        ("la liga",            "laliga"),
        ("champions league",   "champions"),
        ("europa league",      "europa"),
        ("conference league",  "conference"),
        ("liga mx",            "ligamx"),
        ("mls",                "mls"),
        # World Cup before the generic "soccer" needle so goalscorer promos
        # ("... / World Cup") resolve to the fifa.world ESPN feed instead of
        # falling through to All-Sports (which never searches world_cup).
        ("world cup",          "world_cup"),
        ("fifa",               "world_cup"),
        ("soccer",             "soccer"),
        ("wnba",               "wnba"),
        ("afl",                "afl"),
        ("aussie rules",       "afl"),
        ("boxing",             "boxing"),
        ("cricket",            "cricket"),
        ("ipl",                "cricket"),
        ("tennis",             "tennis"),
    ]:
        if needle in desc:
            return sport
    return None   # "All Sports" or unrecognised — will be resolved at grade time


def _read_promo_picks(filepath: str, file_dt: date) -> list[dict]:
    """
    Read Promo Picks rows from the "Play These" tab.

    Detection:  a promo data row has a non-empty market string in col 4 AND
    a positive numeric bet amount in col 12, and appears AFTER a banner row
    whose first cell contains "promo".  Rows with "No qualifying pick" in col 2
    are skipped automatically (col 4 is empty there).

    Markets aren't gated to the core set — prop promos (PTS, REB, DD, etc.)
    are accepted too, with player + side parsed from the Pick column when it
    follows the "<player> O|U|Y|N" pattern. Markets that grade_prop knows
    are normalised to its lowercase keys (PTS → pts); unknowns pass through
    and grade as UNKNOWN, but are still tracked instead of silently dropped.
    """
    try:
        wb = openpyxl.load_workbook(filepath, read_only=True, data_only=True)
    except Exception as e:
        print(f"  [WARN] Cannot open {os.path.basename(filepath)}: {e}")
        return []

    if "Play These" not in wb.sheetnames:
        wb.close()
        return []

    ws = wb["Play These"]
    picks = []
    in_promo_section = False

    for row_vals in ws.iter_rows(min_row=2, values_only=True):
        # Detect the promo section banner (first cell contains "promo")
        if not in_promo_section:
            first = str(row_vals[0] or "").lower() if row_vals else ""
            if "promo" in first:
                in_promo_section = True
            continue

        if len(row_vals) < _PROMO_COL_BET_AMT:
            continue

        raw_market  = row_vals[_PROMO_COL_MARKET - 1]
        raw_bet_amt = row_vals[_PROMO_COL_BET_AMT - 1]
        raw_promo   = row_vals[_PROMO_COL_DESC - 1]

        if not isinstance(raw_market, str):
            continue
        market = raw_market.strip().upper()
        if not market:
            continue
        if not isinstance(raw_bet_amt, (int, float)) or raw_bet_amt <= 0:
            continue
        if not raw_promo:
            continue

        def _s(v): return str(v).strip() if v is not None else ""
        def _f(v):
            try:    return float(v)
            except: return None

        promo_desc   = _s(raw_promo)
        game         = _s(row_vals[_PROMO_COL_GAME - 1])
        book         = _s(row_vals[_PROMO_COL_BOOK - 1])
        pick         = _s(row_vals[_PROMO_COL_PICK - 1])
        line_raw     = row_vals[_PROMO_COL_LINE - 1]
        orig_odds_r  = row_vals[_PROMO_COL_ORIG_ODDS - 1]
        boost_odds_r = row_vals[_PROMO_COL_BOOST_ODDS - 1]
        orig_ev      = _f(row_vals[_PROMO_COL_ORIG_EV - 1])  or 0.0
        boost_ev     = _f(row_vals[_PROMO_COL_BOOST_EV - 1]) or 0.0
        ev_gain      = _f(row_vals[_PROMO_COL_EV_GAIN - 1])  or 0.0
        bet_amt      = float(raw_bet_amt)
        potential    = _f(row_vals[_PROMO_COL_POTENTIAL - 1])
        min_odds     = _s(row_vals[_PROMO_COL_MIN_ODDS - 1]) \
                       if len(row_vals) >= _PROMO_COL_MIN_ODDS else ""

        if not game or not pick:
            continue

        # Parse original odds
        orig_odds_am = None
        if orig_odds_r is not None:
            if isinstance(orig_odds_r, (int, float)):
                orig_odds_am = int(orig_odds_r)
            else:
                s = str(orig_odds_r).strip().replace("+", "")
                try:   orig_odds_am = int(s)
                except: pass

        # Parse boosted odds (may be "FREE BET")
        is_free_bet   = False
        boost_odds_am = None
        if boost_odds_r is not None:
            sv = str(boost_odds_r).strip()
            if "free bet" in sv.lower():
                is_free_bet = True
            else:
                sv2 = sv.replace("+", "")
                try:   boost_odds_am = int(sv2)
                except: pass

        # Use boosted odds for P&L; fall back to original if no boost parsed
        odds_for_pnl = boost_odds_am if boost_odds_am is not None else orig_odds_am
        decimal = None
        if odds_for_pnl is not None:
            decimal = (odds_for_pnl / 100 + 1) if odds_for_pnl > 0 \
                      else (100 / abs(odds_for_pnl) + 1)

        line_num = None
        if line_raw not in (None, ""):
            line_num = _f(line_raw)

        sport = _extract_sport_from_promo(promo_desc) or "unknown"

        # Prop layout: Pick is "<player> <O|U|Y|N>". O/U map to over/under
        # (gradable thresholds — pts, reb, ast, K, total_bases). Y/N are
        # binary props (DD, anytime TD, etc.) — we capture them so the bet
        # is tracked, but grade_prop returns UNKNOWN for markets it doesn't
        # know, which rolls up under MANUAL/unresolved in the report.
        is_prop      = False
        player       = None
        prop_side    = None
        market_final = market
        for suffix, side_label in (
            (" O", "over"), (" U", "under"),
            (" Y", "yes"),  (" N", "no"),
        ):
            if pick.upper().endswith(suffix):
                player    = pick[:-len(suffix)].strip()
                prop_side = side_label
                is_prop   = bool(player)   # need a non-empty player name
                if is_prop:
                    # Writer emits the market uppercased and underscored
                    # ("TOTAL_BASES"); the PROP_DISPLAY_TO_KEY lookup uses
                    # spaced/lowercased keys ("total bases"). Try both.
                    mkey = market.lower()
                    pkey = (PROP_DISPLAY_TO_KEY.get(mkey)
                            or PROP_DISPLAY_TO_KEY.get(mkey.replace("_", " ")))
                    if pkey:
                        market_final = pkey
                break

        # No game_time column in the promo section — use file date as proxy
        game_date = file_dt.strftime("%Y%m%d")

        picks.append({
            "sport":          sport,
            "game":           game,
            "game_time":      "",
            "game_date":      game_date,
            "book":           book,
            "market":         market_final,
            "pick":           pick,
            "player":         player,
            "prop_side":      prop_side,
            "is_prop":        is_prop,
            "line":           line_num,
            "odds":           boost_odds_am,        # boosted odds for display/grading
            "orig_odds":      orig_odds_am,
            "boost_odds_str": _s(boost_odds_r),
            "decimal":        round(decimal, 4) if decimal else None,
            "ev_pct":         boost_ev,
            "orig_ev":        orig_ev,
            "boost_ev":       boost_ev,
            "ev_gain":        ev_gain,
            "confidence":     "",
            "bet_amount":     bet_amt,
            "potential":      potential,
            "min_odds":       min_odds,
            "promo_desc":     promo_desc,
            "is_free_bet":    is_free_bet,
            "source_file":    os.path.basename(filepath),
            "file_date":      file_dt,
            # resolved later
            "result":         None,
            "pnl":            None,
            "actual_result":  None,
            "times_seen":     1,
            "persistent":     False,
        })

    wb.close()
    return picks


def _dedup_promo_picks(raw: list[dict]) -> list[dict]:
    """
    Deduplicate promo picks to first appearance by
    (promo_desc, game, market, pick, line).
    """
    raw_sorted = sorted(raw, key=lambda p: p["file_date"])
    deduped: dict[tuple, dict] = {}
    for p in raw_sorted:
        line_key = round(p["line"], 2) if p["line"] is not None else ""
        key = (
            p["promo_desc"].lower().strip(),
            p["game"].lower().strip(),
            p["market"],
            p["pick"].lower().strip(),
            line_key,
        )
        if key not in deduped:
            p["times_seen"] = 1
            deduped[key]    = p
        else:
            deduped[key]["times_seen"] += 1
    result = list(deduped.values())
    for p in result:
        p["persistent"] = p["times_seen"] >= 3
    return result


def _load_from_picks_db(tab: str, start_dt: date, end_dt: date,
                        an_only_mode: str) -> list[dict] | None:
    """Fast path (2026-07-09): query the SQLite parse-cache instead of
    re-reading every archive workbook. sync() first ingests any files
    the cache hasn't seen, through the same _read_* functions the scan
    path uses — so rows are identical either way. Returns None on ANY
    problem; callers fall back to the original file scan, so the cache
    can never block a backtest."""
    try:
        import picks_db
        stats = picks_db.sync()
        if stats.get("new_files"):
            print(f"  picks.db: ingested {stats['new_files']} new file(s) "
                  f"(+{stats['new_rows']} rows)")
        rows = picks_db.load_raw(tab, start_dt, end_dt, an_only_mode)
        print(f"  picks.db: {len(rows)} raw {tab} rows "
              f"({start_dt} → {end_dt}, {an_only_mode})")
        return rows
    except Exception as e:
        print(f"  picks.db unavailable ({type(e).__name__}: {e}) — "
              f"falling back to xlsx scan")
        return None


def load_archive_promo_picks(start_dt: date, end_dt: date,
                              an_only_mode: str = "exclude") -> list[dict]:
    """
    Scan archive/ for ev_edges_*.xlsx in range and read the Promo Picks section
    from each "Play These" tab.  Deduplicates to first appearance only.
    """
    raw = _load_from_picks_db("promo", start_dt, end_dt, an_only_mode)
    if raw is None:
        in_range = _discover_archive_files(start_dt, end_dt, an_only_mode)

        if not in_range:
            print("  No promo picks found in range.")
            return []

        print(f"Loading Promo Picks from {len(in_range)} file(s):")
        raw = []
        for fd, path in sorted(in_range):
            picks = _read_promo_picks(path, fd)
            print(f"  {os.path.basename(path)}: {len(picks)} promo picks")
            raw.extend(picks)

    if not raw:
        return []

    result = _dedup_promo_picks(raw)
    dupes  = len(raw) - len(result)
    print(f"  → {len(result)} unique promo picks  ({dupes} duplicates removed)\n")
    return result


def load_archive_picks(start_dt: date, end_dt: date,
                        an_only_mode: str = "exclude") -> list[dict]:
    """
    Scan archive/ (and script dir) for ev_edges_*.xlsx within the date range.
    Returns all raw pick rows, then deduplicates to first appearance.
    """
    raw = _load_from_picks_db("pt", start_dt, end_dt, an_only_mode)
    if raw is None:
        in_range = _discover_archive_files(start_dt, end_dt, an_only_mode)

        if not in_range:
            print(f"  No archive files found between {start_dt} and {end_dt}.")
            return []

        print(f"Found {len(in_range)} file(s) in range:")
        raw = []
        for fd, path in sorted(in_range):
            picks = _read_play_these(path, fd)
            print(f"  {os.path.basename(path)}: {len(picks)} picks")
            raw.extend(picks)

    result     = _dedup_picks(raw)
    dupes      = len(raw) - len(result)
    persistent = sum(1 for p in result if p["persistent"])
    print(f"\n  → {len(result)} unique picks  ({dupes} duplicates removed)")
    print(f"  → {persistent} persistent edges (appeared in 3+ sheets)\n")
    return result


# ── EV Edges reader ───────────────────────────────────────────────────────────

def _read_ev_edges(filepath: str, file_dt: date) -> list[dict]:
    """
    Read pick rows from the "EV Edges" tab of one archive file.
    Detection: col 6 (Market) in VALID_MARKETS AND col 16 (Bet Amount) > 0.
    Also reads the Pinnacle flag (col 20) and # Books (col 18).
    """
    try:
        wb = openpyxl.load_workbook(filepath, read_only=True, data_only=True)
    except Exception:
        wb = None
    if wb is None or "EV Edges" not in wb.sheetnames:
        if wb: wb.close()
        return []

    ws   = wb["EV Edges"]
    picks = []

    for row_vals in ws.iter_rows(min_row=2, values_only=True):
        if len(row_vals) < _EV_COL_BET_AMT:
            continue

        raw_market  = row_vals[_EV_COL_MARKET - 1]
        raw_bet_amt = row_vals[_EV_COL_BET_AMT - 1]

        if not isinstance(raw_market, str):
            continue
        market = raw_market.strip().upper()
        if market not in VALID_MARKETS:
            continue
        # Allow bet_amt=0 (the run may have been /run_skip with bankroll=0).
        # The market filter above is already enough to reject non-data rows.
        if not isinstance(raw_bet_amt, (int, float)) or raw_bet_amt < 0:
            continue

        def _s(v): return str(v).strip() if v is not None else ""
        def _f(v):
            try:    return float(v)
            except: return None

        sport     = _s(row_vals[_EV_COL_SPORT - 1]).lower()
        game      = _s(row_vals[_EV_COL_GAME - 1])
        game_time = _s(row_vals[_EV_COL_GAMETIME - 1])
        book      = _s(row_vals[_EV_COL_BOOK - 1])
        pick      = _s(row_vals[_EV_COL_PICK - 1])
        line_raw  = row_vals[_EV_COL_LINE - 1]
        odds_raw  = row_vals[_EV_COL_ODDS - 1]
        dec_raw   = row_vals[_EV_COL_DECIMAL - 1]
        ev_raw    = row_vals[_EV_COL_EV_PCT - 1]
        kelly_raw = row_vals[_EV_COL_KELLY - 1] if len(row_vals) >= _EV_COL_KELLY else None
        bet_amt   = float(raw_bet_amt)
        potential = row_vals[_EV_COL_POTENTIAL - 1]
        num_books_raw = row_vals[_EV_COL_NUM_BOOKS - 1] \
                        if len(row_vals) >= _EV_COL_NUM_BOOKS else None
        pin_raw   = row_vals[_EV_COL_PINNACLE - 1] \
                    if len(row_vals) >= _EV_COL_PINNACLE else None
        cal_raw   = row_vals[_EV_COL_CAL_EV - 1] \
                    if len(row_vals) >= _EV_COL_CAL_EV else None

        if not sport or not game or not pick:
            continue

        line_num = None
        if line_raw not in (None, ""):
            line_num = _f(line_raw)

        odds_am = None
        if odds_raw is not None:
            if isinstance(odds_raw, (int, float)):
                odds_am = int(odds_raw)
            else:
                s = str(odds_raw).strip().replace("+", "")
                try:   odds_am = int(s)
                except: pass

        decimal = _f(dec_raw)
        if decimal is None and odds_am is not None:
            decimal = (odds_am / 100 + 1) if odds_am > 0 \
                      else (100 / abs(odds_am) + 1)

        pinnacle = str(pin_raw).strip() == "✓" if pin_raw else False

        game_date = _parse_game_date(game_time, file_dt)

        picks.append({
            "sport":       sport,
            "game":        game,
            "game_time":   game_time,
            "game_date":   game_date,
            "book":        book,
            "market":      market,
            "pick":        pick,
            "line":        line_num,
            "odds":        odds_am,
            "decimal":     round(decimal, 4) if decimal else None,
            "ev_pct":      _f(ev_raw) or 0.0,
            "confidence":  "",           # EV Edges has no confidence label
            "kelly_pct":   _f(kelly_raw),
            "bet_amount":  bet_amt,
            "potential":   _f(potential),
            "num_books":   int(num_books_raw) if num_books_raw else 0,
            "pinnacle":    pinnacle,
            "cal_ev_pct":  _f(cal_raw),   # None on pre-shadow archives
            "source_file": os.path.basename(filepath),
            "file_date":   file_dt,
            # resolved later
            "result":       None,
            "pnl":          None,
            "actual_result": None,
            "times_seen":   1,
            "persistent":   False,
        })

    wb.close()
    return picks


def _dedup_picks(raw: list[dict]) -> list[dict]:
    """Shared deduplication: first appearance by (sport,game,market,pick,book,line)."""
    raw_sorted = sorted(raw, key=lambda p: p["file_date"])
    deduped: dict[tuple, dict] = {}
    for p in raw_sorted:
        line_key = round(p["line"], 2) if p["line"] is not None else ""
        key = (p["sport"], p["game"].lower().strip(),
               p["market"], p["pick"].lower().strip(),
               p["book"], line_key)
        if key not in deduped:
            p["times_seen"]   = 1
            p["odds_history"] = [p["odds"]]
            p["ev_history"]   = [p["ev_pct"]]
            deduped[key] = p
        else:
            existing = deduped[key]
            existing["times_seen"] += 1
            existing["odds_history"].append(p["odds"])
            existing["ev_history"].append(p["ev_pct"])
    result = list(deduped.values())
    for p in result:
        p["persistent"] = p["times_seen"] >= 3
    return result


def load_archive_ev_edges(start_dt: date, end_dt: date,
                            an_only_mode: str = "exclude") -> list[dict]:
    """
    Load all +EV picks from the "EV Edges" tab across archive files in range.
    Deduplicates to first appearance only.
    """
    raw = _load_from_picks_db("ev", start_dt, end_dt, an_only_mode)
    if raw is None:
        in_range = _discover_archive_files(start_dt, end_dt, an_only_mode)

        if not in_range:
            return []

        print(f"\nLoading EV Edges tab from {len(in_range)} file(s):")
        raw = []
        for fd, path in in_range:
            picks = _read_ev_edges(path, fd)
            print(f"  {os.path.basename(path)}: {len(picks)} EV edges")
            raw.extend(picks)

    result     = _dedup_picks(raw)
    dupes      = len(raw) - len(result)
    persistent = sum(1 for p in result if p["persistent"])
    print(f"  → {len(result)} unique EV picks  ({dupes} duplicates removed)")
    print(f"  → {persistent} persistent edges (appeared in 3+ sheets)\n")
    return result


# ── Player props reader ───────────────────────────────────────────────────────

def _read_props_from_odds_comparison(filepath: str, file_dt: date) -> list[dict]:
    """
    Read player-prop rows from the "Odds Comparison" tab.

    A row is a prop if its Type cell (col 2) is NOT "GAME LINE" and its
    Market cell (col 5) maps to a known prop key via PROP_DISPLAY_TO_KEY.
    Pick cell (col 6) encodes player + side: "LeBron James O" / "LeBron James U".
    Odds are taken from DraftKings first, then FanDuel (first non-null).
    Bet amount is derived via half-Kelly against PROP_BANKROLL.
    """
    try:
        wb = openpyxl.load_workbook(filepath, read_only=True, data_only=True)
    except Exception as e:
        print(f"  [WARN] Cannot open {os.path.basename(filepath)}: {e}")
        return []

    if "Odds Comparison" not in wb.sheetnames:
        wb.close()
        return []

    ws   = wb["Odds Comparison"]
    rows = list(ws.iter_rows(min_row=1, values_only=True))
    wb.close()

    if not rows:
        return []

    # Locate DraftKings, FanDuel, and the optional Commence column (added
    # 2026-04: lets us resolve to the real game date instead of the file date).
    header   = rows[0]
    dk_col   = None
    fd_col   = None
    com_col  = None
    bb_col   = None
    for ci, h in enumerate(header):
        if not isinstance(h, str):
            continue
        hl = h.strip().lower()
        if hl == "draftkings":
            dk_col = ci
        elif hl == "fanduel":
            fd_col = ci
        elif hl == "commence":
            com_col = ci
        elif hl == "best book":
            bb_col = ci

    props = []
    for row_vals in rows[1:]:
        if len(row_vals) < 8:
            continue

        type_val   = str(row_vals[1] or "").strip()    # col 2: Type
        sport_val  = str(row_vals[2] or "").strip()    # col 3: Sport
        game_val   = str(row_vals[3] or "").strip()    # col 4: Game
        market_val = str(row_vals[4] or "").strip()    # col 5: Market
        pick_val   = str(row_vals[5] or "").strip()    # col 6: Pick
        line_raw   = row_vals[6]                        # col 7: Line
        ev_raw     = row_vals[7]                        # col 8: EV%

        if not type_val or "GAME LINE" in type_val.upper() or not game_val:
            continue

        market_key = PROP_DISPLAY_TO_KEY.get(market_val.lower())
        if not market_key:
            continue

        # Decode player name and side from pick string ("LeBron James O")
        if pick_val.endswith(" O"):
            player = pick_val[:-2].strip()
            side   = "over"
        elif pick_val.endswith(" U"):
            player = pick_val[:-2].strip()
            side   = "under"
        else:
            continue

        try:
            line_num = float(line_raw) if line_raw is not None else None
        except (ValueError, TypeError):
            line_num = None

        try:
            ev_pct = float(ev_raw) if ev_raw is not None else 0.0
        except (ValueError, TypeError):
            ev_pct = 0.0

        # Book attribution: prefer the explicit "Best Book" column (added
        # 2026-07) — it names the book the edge was actually priced at, which
        # is what the closing-line CLV lookup is keyed on. Older archives
        # without the column fall back to DK-first, which mis-attributes
        # FanDuel-only edges to DraftKings whenever DK also lists the line.
        book_pref = [(dk_col, "DraftKings"), (fd_col, "FanDuel")]
        if bb_col is not None and bb_col < len(row_vals):
            bb = str(row_vals[bb_col] or "").strip()
            if bb == "FanDuel":
                book_pref = [(fd_col, "FanDuel"), (dk_col, "DraftKings")]

        odds_am = None
        book    = None
        for ci, bk_name in book_pref:
            if ci is not None and ci < len(row_vals) and row_vals[ci] is not None:
                try:
                    odds_am = int(row_vals[ci])
                    book    = bk_name
                    break
                except (ValueError, TypeError):
                    pass

        if odds_am is None or book is None:
            continue

        decimal = (odds_am / 100 + 1) if odds_am > 0 else (100 / abs(odds_am) + 1)

        # Half-Kelly bet sizing against PROP_BANKROLL
        bet_amt = 0.0
        if ev_pct > 0 and decimal > 1:
            kelly_full = (ev_pct / 100) / (decimal - 1)
            bet_amt    = round(min(kelly_full * PROP_KELLY_FRACTION * PROP_BANKROLL,
                                   PROP_MAX_BET), 2)

        if bet_amt <= 0:
            continue

        # Prefer the real commence date from the Commence column (added 2026-04).
        # Older archives without that column fall back to the file date proxy,
        # which is why playoff-era props 3+ days out used to miss grading.
        game_date_str = file_dt.strftime("%Y%m%d")
        if com_col is not None and com_col < len(row_vals):
            c_raw = row_vals[com_col]
            if c_raw:
                c_str = c_raw.strftime("%Y-%m-%d") if hasattr(c_raw, "strftime") else str(c_raw).strip()
                try:
                    game_date_str = datetime.strptime(
                        c_str[:10], "%Y-%m-%d").strftime("%Y%m%d")
                except ValueError:
                    pass

        props.append({
            "sport":       _normalise_sport(sport_val),
            "game":        game_val,
            "game_time":   "",
            "game_date":   game_date_str,
            "book":        book,
            "market":      market_key,
            "pick":        pick_val,
            "player":      player,
            "prop_side":   side,
            "line":        line_num,
            "odds":        odds_am,
            "decimal":     round(decimal, 4),
            "ev_pct":      ev_pct,
            "confidence":  "",
            "bet_amount":  bet_amt,
            "potential":   round(bet_amt * (decimal - 1), 2),
            "source_file": os.path.basename(filepath),
            "file_date":   file_dt,
            "is_prop":     True,
            "result":      None,
            "pnl":         None,
            "actual_result": None,
            "times_seen":  1,
            "persistent":  False,
        })

    return props


def load_archive_props(start_dt: date, end_dt: date,
                        an_only_mode: str = "exclude") -> list[dict]:
    """Load and deduplicate player-prop edges from Odds Comparison tabs."""
    raw = _load_from_picks_db("prop", start_dt, end_dt, an_only_mode)
    if raw is None:
        in_range = _discover_archive_files(start_dt, end_dt, an_only_mode)

        if not in_range:
            return []

        print(f"\nLoading props from Odds Comparison tab "
              f"({len(in_range)} file(s)):")
        raw = []
        for fd, path in in_range:
            picks = _read_props_from_odds_comparison(path, fd)
            print(f"  {os.path.basename(path)}: {len(picks)} prop edges")
            raw.extend(picks)

    result     = _dedup_picks(raw)
    dupes      = len(raw) - len(result)
    persistent = sum(1 for p in result if p["persistent"])
    print(f"  → {len(result)} unique prop picks  ({dupes} duplicates removed)")
    print(f"  → {persistent} persistent prop edges (appeared in 3+ sheets)\n")
    return result


# ── ESPN results ───────────────────────────────────────────────────────────────

_espn_cache:     dict[str, list[dict]] = {}   # completed games only
_espn_all_cache: dict[str, list[dict]] = {}   # all statuses (final + scheduled + in-progress)
_espn_session = requests.Session()
_espn_session.headers.update({
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"
})

# Date the caches were last seeded. When date.today() advances we wipe both
# caches via _refresh_espn_cache_if_new_day(); see that function for the why.
_espn_cache_seeded_on: date | None = None


def _refresh_espn_cache_if_new_day() -> None:
    """Wipe in-memory ESPN caches at calendar-day rollover.

    The caches are keyed by (sport, date) and never expire within a process
    lifetime. That's fine for cache hits within one day, but the daily
    backtest job grades yesterday's picks at 3 AM ET and pre-fetches a ±1
    day buffer — so yesterday's run cached "today's games" when they were
    STATUS_SCHEDULED. Without this guard, today's run hits that stale cache
    and marks every game as NOT_STARTED instead of re-fetching the (now
    final) results.
    """
    global _espn_cache_seeded_on
    today = date.today()
    if _espn_cache_seeded_on is None:
        _espn_cache_seeded_on = today
        return
    if today != _espn_cache_seeded_on:
        _espn_all_cache.clear()
        _espn_cache.clear()
        _espn_cache_seeded_on = today


def fetch_espn_games(sport: str, date_str: str) -> list[dict]:
    """
    Fetch ALL games for one sport + date (YYYYMMDD), regardless of status.
    Each result dict includes a "completed" bool so callers can distinguish
    between "graded a final game" vs "found a scheduled or in-progress game"
    (used to mark picks as PENDING instead of mis-grading them against an
    adjacent-day game).

    Despite the name, this is the central dispatcher across result sources:
    ESPN's main API is the default, but Squiggle (AFL) and ESPN's numeric-
    cricket-league endpoint take over for sports ESPN's main API doesn't
    cover. All branches return the same dict shape so downstream matching
    and grading code is source-agnostic.

    Cached per (sport, date) under _espn_all_cache.
    """
    _refresh_espn_cache_if_new_day()
    cache_key = f"{sport}|{date_str}"
    if cache_key in _espn_all_cache:
        return _espn_all_cache[cache_key]

    # Non-ESPN result sources first.
    if sport in SQUIGGLE_SPORTS:
        games = _fetch_squiggle_games(date_str)
        _espn_all_cache[cache_key] = games
        return games
    if sport in THESPORTSDB_SPORTS:
        games = _fetch_thesportsdb_games(THESPORTSDB_SPORTS[sport], date_str)
        _espn_all_cache[cache_key] = games
        return games
    if sport in BOXLIVE_SPORTS:
        games = _fetch_boxlive_games(date_str)
        _espn_all_cache[cache_key] = games
        return games
    if sport in TENNISEXPLORER_SPORTS:
        games = _fetch_tennisexplorer_games(date_str)
        _espn_all_cache[cache_key] = games
        return games
    if sport in ESPN_CRICKET_LEAGUES:
        games = _fetch_espn_cricket_games(ESPN_CRICKET_LEAGUES[sport], date_str)
        _espn_all_cache[cache_key] = games
        return games

    espn_path = ESPN_SPORT_MAP.get(sport)
    if not espn_path:
        _espn_all_cache[cache_key] = []
        return []

    url = f"{ESPN_BASE}/{espn_path}/scoreboard?dates={date_str}&limit=100"
    try:
        resp = _espn_session.get(url, timeout=15)
        if resp.status_code != 200:
            _espn_all_cache[cache_key] = []
            return []
        events = resp.json().get("events", [])
    except Exception:
        _espn_all_cache[cache_key] = []
        return []

    # ESPN status names that mean the game is fully over
    _FINAL = {"STATUS_FINAL", "STATUS_FULL_TIME", "STATUS_END_PERIOD",
              "STATUS_FORFEIT", "STATUS_ABANDONED"}

    games = []
    for event in events:
        # Iterate ALL competitions, not just [0]. Team sports have one
        # competition per event (the game), but combat sports (UFC, MMA)
        # pack the entire fight card under one event with N competitions
        # — taking [0] silently dropped 11/12 fights per night.
        for comp in event.get("competitions", []):
            status_obj  = comp.get("status", {}).get("type", {})
            status_name = status_obj.get("name", "")
            completed   = status_obj.get("completed", False) \
                          or status_name in _FINAL

            # MMA / boxing competitors carry homeAway: None instead of
            # "home" / "away". Fall back to ESPN's `order` field
            # (1 = first listed, 2 = second listed) — emit both
            # orderings later so it doesn't matter which side the pick
            # named.
            slots: list[dict] = []
            for competitor in comp.get("competitors", []):
                # Team sports populate `team`; combat sports (UFC/MMA)
                # populate `athlete` instead (same field shape: fullName,
                # displayName, shortName).
                info = (competitor.get("team")
                        or competitor.get("athlete")
                        or {})
                ha = competitor.get("homeAway") or ""
                # ESPN MMA competitors return winner as a Python bool;
                # cricket returns it as a "true"/"false" STRING. Handle
                # both safely.
                raw_w = competitor.get("winner", False)
                is_winner = (raw_w is True
                             or str(raw_w).strip().lower() == "true")
                # Athlete records don't have abbreviation/shortDisplayName,
                # but they do have shortName ("J. Siraj") — handy as a
                # fuzzy-match alias.
                slot = {
                    "name":   info.get("displayName") or info.get("fullName") or "",
                    "abbr":   info.get("abbreviation", ""),
                    "short":  info.get("shortDisplayName") or info.get("shortName") or "",
                    "score":  int(competitor.get("score", 0) or 0),
                    "winner": is_winner,
                    "ha":     ha,
                    "order":  competitor.get("order", 0),
                }
                slots.append(slot)

            # Need exactly two competitors to make a head-to-head game.
            if len(slots) != 2:
                continue

            # Team sport: ESPN tells us which is home and which is away.
            home_slot = away_slot = None
            for s in slots:
                if s["ha"] == "home": home_slot = s
                elif s["ha"] == "away": away_slot = s
            if home_slot and away_slot:
                winner_team = ""
                if completed:
                    winner_team = (home_slot["name"] if home_slot["winner"]
                                   else away_slot["name"])
                games.append(_build_game_dict(home_slot, away_slot,
                                              winner_team, completed,
                                              status_name, event.get("id", "")))
                continue

            # Combat sport (no homeAway): fall back to `order`. Emit
            # BOTH orderings so the pick's "Away @ Home" string can
            # match either way. ESPN's score field is 0 for combat
            # sports (no points scored), so we synthesize 1/0 from the
            # winner flag — otherwise grade_pick would see equal scores
            # and PUSH every fight.
            slots.sort(key=lambda s: s.get("order") or 0)
            a, b = slots[0], slots[1]
            winner_name = ""
            if completed:
                if a["winner"]: winner_name = a["name"]
                elif b["winner"]: winner_name = b["name"]
            for away_s, home_s in ((a, b), (b, a)):
                a_won = bool(away_s["winner"]) and completed
                h_won = bool(home_s["winner"]) and completed
                games.append({
                    "home_team":  home_s["name"],
                    "home_abbr":  home_s["abbr"],
                    "home_short": home_s["short"],
                    "home_score": 1 if h_won else 0,
                    "away_team":  away_s["name"],
                    "away_abbr":  away_s["abbr"],
                    "away_short": away_s["short"],
                    "away_score": 1 if a_won else 0,
                    "winner":     winner_name,
                    "total":      1 if completed else 0,
                    "event_id":   event.get("id", ""),
                    "completed":  completed,
                    "status":     status_name,
                })

    _espn_all_cache[cache_key] = games
    time.sleep(0.15)
    return games


def _build_game_dict(home_slot: dict, away_slot: dict, winner_team: str,
                     completed: bool, status_name: str, event_id: str) -> dict:
    """Assemble the standard ESPN-shape game dict from two competitor slots."""
    return {
        "home_team":  home_slot["name"],
        "home_abbr":  home_slot["abbr"],
        "home_short": home_slot["short"],
        "home_score": home_slot["score"],
        "away_team":  away_slot["name"],
        "away_abbr":  away_slot["abbr"],
        "away_short": away_slot["short"],
        "away_score": away_slot["score"],
        "winner":     winner_team,
        "total":      home_slot["score"] + away_slot["score"],
        "event_id":   event_id,
        "completed":  completed,
        "status":     status_name,
    }


# ── TheSportsDB (KBO / NPB / CFL) ─────────────────────────────────────
# Free community API, one call per league-day. Finals carry strStatus
# "FT" with integer scores; postponed games come back "POST" with null
# scores. Team names are plain English ("Doosan Bears", "BC Lions") and
# match the Odds API's naming, so the standard fuzzy matcher works.
_TSDB_BASE = "https://www.thesportsdb.com/api/v1/json/123"
_TSDB_HEADERS = {
    "User-Agent": "arbyx-odds-compare/1.0 (https://github.com/reastep3/odds-compare-cloud)",
}


def _fetch_thesportsdb_games(league_id: int, date_str: str) -> list[dict]:
    """One league's games for a YYYYMMDD date via TheSportsDB eventsday.

    2026-07-25: Cloudflare rate-limits (HTTP 429 / an "error code: 1015"
    body) must NOT read as an empty slate — that silently un-grades
    every pick for the league-days after the limiter kicks in (found
    when a local analysis run "lost" NPB's month). Retry once with a
    backoff, pace successive calls, and give up loudly in the log.
    """
    d_iso = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"
    url = f"{_TSDB_BASE}/eventsday.php?d={d_iso}&l={league_id}"
    events = None
    for attempt in (1, 2):
        try:
            resp = _espn_session.get(url, headers=_TSDB_HEADERS, timeout=15)
            limited = (resp.status_code in (429, 403, 503)
                       or resp.text.lstrip().startswith("error code"))
            if resp.status_code == 200 and not limited:
                events = resp.json().get("events") or []
                break
        except Exception:
            pass
        if attempt == 1:
            time.sleep(4)          # backoff before the one retry
    if events is None:
        print(f"  [TheSportsDB/{league_id}] unavailable (rate-limited?) "
              f"for {d_iso} — picks stay ungraded rather than mis-graded")
        return []
    time.sleep(0.35)               # pace the next league-day call

    games = []
    for e in events:
        status = (e.get("strStatus") or "").upper()
        hs, aw = e.get("intHomeScore"), e.get("intAwayScore")
        try:
            h, a = int(hs), int(aw)
            have_score = True
        except (TypeError, ValueError):
            h = a = 0
            have_score = False
        # FT/AET/AOT = final. POST/CANC/ABD pass through RAW so the
        # grader's VOID branch can see them (2026-07-13 — an ABD'd KBO
        # game sat "IN_PROGRESS" for days because this used to mask
        # postponements as SCHEDULED). Only not-started maps.
        completed = have_score and status in ("FT", "AET", "AOT", "MATCH FINISHED")
        if status in ("NS", ""):
            status = "SCHEDULED"
        home = {"name": e.get("strHomeTeam") or "", "abbr": "",
                "short": e.get("strHomeTeam") or "", "score": h}
        away = {"name": e.get("strAwayTeam") or "", "abbr": "",
                "short": e.get("strAwayTeam") or "", "score": a}
        winner = (home["name"] if h > a else
                  away["name"] if a > h else "")
        games.append(_build_game_dict(
            home, away, winner, completed, status,
            str(e.get("idEvent") or "")))
    return games


# ── Squiggle (AFL) ────────────────────────────────────────────────────
# Free, no auth, see https://api.squiggle.com.au/. Their etiquette policy
# requires identifying contact via the User-Agent header.
_SQUIGGLE_BASE = "https://api.squiggle.com.au/"
_SQUIGGLE_HEADERS = {
    "User-Agent": "arbyx-odds-compare/1.0 (https://github.com/reastep3/odds-compare-cloud)",
}
_squiggle_year_cache: dict[int, list[dict]] = {}


def _squiggle_games_for_year(year: int) -> list[dict]:
    """Fetch + cache the entire AFL season's games (a few hundred KB).
    Then date-filtering is just an in-memory scan.

    NOTE: Squiggle's q=games;year=YYYY query uses semicolons that MUST
    NOT be percent-encoded — passing via requests' params dict breaks
    the API and returns the docs HTML page. The URL is built by hand.
    """
    if year in _squiggle_year_cache:
        return _squiggle_year_cache[year]
    url = f"{_SQUIGGLE_BASE}?q=games;year={year}"
    try:
        resp = _espn_session.get(url, headers=_SQUIGGLE_HEADERS, timeout=20)
        if resp.status_code != 200:
            print(f"  [Squiggle] HTTP {resp.status_code}")
            _squiggle_year_cache[year] = []
            return []
        games = resp.json().get("games", []) or []
    except Exception as e:
        print(f"  [Squiggle] error: {e}")
        games = []
    _squiggle_year_cache[year] = games
    return games


def _fetch_squiggle_games(date_str: str) -> list[dict]:
    """
    Squiggle returns one row per game with hteam/ateam/hscore/ascore/winner/
    complete/timestr. We normalize to the same shape ESPN returns so the
    downstream matcher and grader stay source-agnostic.
    """
    if len(date_str) != 8:
        return []
    year  = int(date_str[:4])
    month = int(date_str[4:6])
    day   = int(date_str[6:])
    target = f"{year:04d}-{month:02d}-{day:02d}"

    games = []
    for g in _squiggle_games_for_year(year):
        # Squiggle date format: "2026-05-01 19:50:00" (local Aussie time).
        # Match on the date prefix only.
        gd = (g.get("date") or "")[:10]
        if gd != target:
            continue
        complete_pct = int(g.get("complete") or 0)
        completed    = complete_pct >= 100
        # Squiggle marks live games with complete < 100 and a numeric
        # timestr (e.g. "Q3 12:34"). Status string for downstream:
        if completed:
            status = "STATUS_FINAL"
        elif complete_pct > 0:
            status = "STATUS_IN_PROGRESS"
        else:
            status = "STATUS_SCHEDULED"

        home = (g.get("hteam") or "").strip()
        away = (g.get("ateam") or "").strip()
        hsc  = int(g.get("hscore") or 0)
        asc  = int(g.get("ascore") or 0)
        winner_team = ""
        if completed:
            w = (g.get("winner") or "").strip()
            winner_team = w if w else (home if hsc > asc else
                                       (away if asc > hsc else ""))
        games.append({
            "home_team":  home,
            "home_abbr":  "",
            "home_short": "",
            "home_score": hsc,
            "away_team":  away,
            "away_abbr":  "",
            "away_short": "",
            "away_score": asc,
            "winner":     winner_team,
            "total":      hsc + asc,
            "event_id":   str(g.get("id") or ""),
            "completed":  completed,
            "status":     status,
        })
    return games


# ── ESPN cricket (numeric league ID, IPL = 8048) ──────────────────────

def _fetch_espn_cricket_games(league_id: int, date_str: str) -> list[dict]:
    """
    ESPN's cricket scoreboard uses cricinfo numeric series IDs in place of
    a slug (e.g. .../sports/cricket/8048/scoreboard for IPL 2026).
    Status semantics differ from the team-sports endpoint: completed
    games carry `description="Result"` and/or `detail="Final"` rather
    than `completed=True` / `name="STATUS_FINAL"`. Total runs scored
    sums both innings.
    """
    url = f"{ESPN_BASE}/cricket/{league_id}/scoreboard?dates={date_str}&limit=100"
    try:
        resp = _espn_session.get(url, timeout=15)
        if resp.status_code != 200:
            return []
        events = resp.json().get("events", [])
    except Exception:
        return []

    games = []
    for event in events:
        comp        = event.get("competitions", [{}])[0]
        status_obj  = comp.get("status", {}).get("type", {})
        status_name = status_obj.get("name") or ""
        description = (status_obj.get("description") or "").lower()
        detail      = (status_obj.get("detail") or "").lower()
        # Completion: ESPN cricket marks final games via description="Result"
        # or detail starting with "final"/"complete". Falls back to the
        # standard completed flag for safety.
        completed = bool(
            status_obj.get("completed")
            or description in ("result", "final", "complete")
            or detail.startswith("final")
            or detail.startswith("complete")
            or "won by" in description
            or "won by" in detail
        )

        teams: dict[str, dict] = {}
        for competitor in comp.get("competitors", []):
            team_info = competitor.get("team", {})
            ha = competitor.get("homeAway", "")
            # Cricket scores: ESPN often uses "225/6" strings — extract
            # the run total. Some events expose linescores per innings.
            raw_score = competitor.get("score", "0")
            runs = 0
            if isinstance(raw_score, (int, float)):
                runs = int(raw_score)
            else:
                # "225/6" → 225, "225/6 d" → 225
                head = str(raw_score).split("/", 1)[0].strip()
                try:
                    runs = int(head)
                except ValueError:
                    runs = 0
            # ESPN cricket returns winner as a STRING ("true"/"false"),
            # not a JSON bool — bool("false") is True, so we string-check.
            raw_winner = competitor.get("winner", False)
            is_winner  = (raw_winner is True
                          or str(raw_winner).strip().lower() == "true")
            teams[ha] = {
                "name":   team_info.get("displayName", ""),
                "abbr":   team_info.get("abbreviation", ""),
                "short":  team_info.get("shortDisplayName", ""),
                "score":  runs,
                "winner": is_winner,
            }

        if "home" not in teams or "away" not in teams:
            continue

        winner_team = ""
        if completed:
            winner_team = (teams["home"]["name"] if teams["home"]["winner"]
                           else teams["away"]["name"])

        games.append({
            "home_team":  teams["home"]["name"],
            "home_abbr":  teams["home"]["abbr"],
            "home_short": teams["home"]["short"],
            "home_score": teams["home"]["score"],
            "away_team":  teams["away"]["name"],
            "away_abbr":  teams["away"]["abbr"],
            "away_short": teams["away"]["short"],
            "away_score": teams["away"]["score"],
            "winner":     winner_team,
            "total":      teams["home"]["score"] + teams["away"]["score"],
            "event_id":   event.get("id", ""),
            "completed":  completed,
            "status":     status_name,
        })

    time.sleep(0.15)
    return games


# ── box.live (Boxing) ─────────────────────────────────────────────────
# Free, public, server-rendered HTML. The `text-schedule__row` /
# `text-schedule__undercard` blocks are the parser-friendly ones — each
# has the format:
#   {fighter A last name}
#   <span class="...text-winner...">METHOD</span>
#   vs
#   <span class="...text-primary...">METHOD</span>
#   {fighter B last name}
# The text-winner span sits next to whichever fighter won (left or right).
_BOXLIVE_URL = "https://box.live/fight-results/"
_BOXLIVE_TTL = 1800   # 30 min — page covers ~3 weeks of results
_boxlive_cache: list[dict] | None = None
_boxlive_cache_time: float = 0.0


def _fetch_boxlive_results() -> list[dict]:
    """Scrape and parse the box.live fight-results page once per TTL.
    Returns a flat list of fight dicts: {date_str, fighter_a, fighter_b,
    winner, method, completed}. Date filtering happens in the caller."""
    global _boxlive_cache, _boxlive_cache_time
    now = time.time()
    if _boxlive_cache is not None and (now - _boxlive_cache_time) < _BOXLIVE_TTL:
        return _boxlive_cache
    try:
        resp = _espn_session.get(
            _BOXLIVE_URL,
            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"},
            timeout=20,
        )
        if resp.status_code != 200:
            print(f"  [box.live] HTTP {resp.status_code}")
            _boxlive_cache = []
            _boxlive_cache_time = now
            return []
        html = resp.text
    except Exception as e:
        print(f"  [box.live] error: {e}")
        return []

    fights: list[dict] = []
    # Section delimiter: a date heading (h3 or h4) immediately preceding a
    # text-schedule div. Parse each section between consecutive headings.
    date_pattern = re.compile(
        r'<h[34][^>]*>\s*(\w+,\s+\d+\s+\w+\s+20\d\d)\s*</h[34]>',
        re.IGNORECASE,
    )
    matches = list(date_pattern.finditer(html))
    for i, m in enumerate(matches):
        try:
            dt = datetime.strptime(m.group(1).strip(), "%A, %d %B %Y")
        except ValueError:
            continue
        date_yyyymmdd = dt.strftime("%Y%m%d")
        section_start = m.end()
        section_end   = matches[i + 1].start() if i + 1 < len(matches) else len(html)
        section_html  = html[section_start:section_end]
        # Only the text-schedule (compact list) parser is reliable; the
        # schedule-card layout is used for upcoming PPV bouts.
        if "text-schedule" not in section_html:
            continue
        for title_html in re.findall(
                r'text-schedule__fight-title[^>]*>(.*?)</h5>',
                section_html, re.DOTALL):
            f = _parse_boxlive_title(title_html, date_yyyymmdd)
            if f:
                fights.append(f)
        for under_html in re.findall(
                r'text-schedule__undercard[^>]*>\s*<h6[^>]*>(.*?)</h6>',
                section_html, re.DOTALL):
            f = _parse_boxlive_title(under_html, date_yyyymmdd)
            if f:
                fights.append(f)

    _boxlive_cache = fights
    _boxlive_cache_time = now
    return fights


def _parse_boxlive_title(title_html: str, date_str: str) -> dict | None:
    """Parse one fight title block. The two <span> elements between the
    fighter names carry the result method; `text-winner` class identifies
    the winning side."""
    spans = list(re.finditer(
        r'<span class="([^"]*)"[^>]*>\s*([^<]*?)\s*</span>',
        title_html, re.DOTALL))
    if len(spans) < 2:
        return None
    fighter_a = re.sub(r'<[^>]+>', '', title_html[:spans[0].start()]).strip()
    fighter_b = re.sub(r'<[^>]+>', '', title_html[spans[1].end():]).strip()
    fighter_a = re.sub(r'\s+', ' ', fighter_a)
    fighter_b = re.sub(r'\s+', ' ', fighter_b)
    if not fighter_a or not fighter_b:
        return None
    a_class = spans[0].group(1)
    b_class = spans[1].group(1)
    if "text-winner" in a_class:
        winner = fighter_a
    elif "text-winner" in b_class:
        winner = fighter_b
    else:
        winner = ""
    completed = bool(winner)
    method = (spans[0].group(2) or spans[1].group(2)).strip()
    return {
        "date_str":  date_str,
        "fighter_a": fighter_a,
        "fighter_b": fighter_b,
        "winner":    winner,
        "method":    method,
        "completed": completed,
    }


# ── TennisExplorer (Tennis) ───────────────────────────────────────────
# Server-rendered HTML, free, date-queryable. Each match is two
# consecutive <tr id="rN"> + <tr id="rNb"> rows under a "result" table.
# Player names are formatted as "Lastname I." (e.g., "Sinner J.") which
# matches the Odds API's full names ("Jannik Sinner") via word-overlap
# on the shared last name.
_TENNISEXPLORER_BASE = "https://www.tennisexplorer.com/results/"
_tennisexplorer_cache: dict[str, list[dict]] = {}


def _fetch_tennisexplorer_games(date_str: str) -> list[dict]:
    """Scrape TennisExplorer's daily results page and return ATP + WTA
    singles matches in the standard ESPN result shape. Both tour types
    come back in one request via type=all (faster than two calls).

    Order between Odds API ("home"/"away") and TE ("first listed" vs
    "second listed") may differ, so we emit BOTH orderings as separate
    result entries — same trick as the boxing scraper.
    """
    if date_str in _tennisexplorer_cache:
        return _tennisexplorer_cache[date_str]
    if len(date_str) != 8:
        return []
    url = (f"{_TENNISEXPLORER_BASE}?type=all"
           f"&year={date_str[:4]}&month={date_str[4:6]}&day={date_str[6:]}")
    try:
        resp = _espn_session.get(
            url,
            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"},
            timeout=20,
        )
        if resp.status_code != 200:
            print(f"  [TennisExplorer] HTTP {resp.status_code}")
            _tennisexplorer_cache[date_str] = []
            return []
        html = resp.text
    except Exception as e:
        print(f"  [TennisExplorer] error: {e}")
        return []

    # Pair up consecutive <tr id="rN"> (player A row) and <tr id="rNb">
    # (player B row). The "rN" row carries the time + first player + their
    # set count + per-set scores; the "rNb" row has the second player +
    # their set count.
    matches: list[dict] = []
    # Match ID + class on first row carries useful metadata; we just need
    # to find pairs where the second row is the matching "Nb" id.
    row_pat = re.compile(
        r'<tr id="r(\d+)(b?)"[^>]*>(.*?)</tr>', re.DOTALL)
    rows = list(row_pat.finditer(html))
    by_id: dict[str, dict[str, str]] = {}
    for m in rows:
        rid, suffix, body = m.group(1), m.group(2), m.group(3)
        slot = "second" if suffix == "b" else "first"
        by_id.setdefault(rid, {})[slot] = body

    for rid, slots in by_id.items():
        if "first" not in slots or "second" not in slots:
            continue
        m = _parse_tennisexplorer_pair(slots["first"], slots["second"])
        if m:
            matches.append(m)

    games: list[dict] = []
    for m in matches:
        a, b = m["player_a"], m["player_b"]
        winner = m["winner"]
        completed = m["completed"]
        for away, home in ((a, b), (b, a)):
            a_won = (winner == away) if completed else False
            h_won = (winner == home) if completed else False
            games.append({
                "home_team":  home,
                "home_abbr":  "",
                "home_short": home.split()[0] if home else "",  # last name
                "home_score": 1 if h_won else 0,
                "away_team":  away,
                "away_abbr":  "",
                "away_short": away.split()[0] if away else "",
                "away_score": 1 if a_won else 0,
                "winner":     winner,
                "total":      1,
                "event_id":   "",
                "completed":  completed,
                "status":     "STATUS_FINAL" if completed else "STATUS_SCHEDULED",
            })
    _tennisexplorer_cache[date_str] = games
    return games


def _parse_tennisexplorer_pair(first_html: str, second_html: str) -> dict | None:
    """Parse one (rN, rNb) row pair into {player_a, player_b, winner, completed}."""
    # Extract player names from the <a href="/player/...">NAME</a> link.
    a_match = re.search(r'<a href="/player/[^"]+">\s*([^<]+?)\s*</a>', first_html)
    b_match = re.search(r'<a href="/player/[^"]+">\s*([^<]+?)\s*</a>', second_html)
    if not a_match or not b_match:
        return None
    player_a = re.sub(r'\s+', ' ', a_match.group(1)).strip()
    player_b = re.sub(r'\s+', ' ', b_match.group(1)).strip()
    if not player_a or not player_b:
        return None
    # Skip doubles: "Arevalo M./Pavic M." style names contain a slash.
    if "/" in player_a or "/" in player_b:
        return None

    # Sets won is in the FIRST <td class="result"> of each row.
    a_sets = _parse_te_result_cell(first_html)
    b_sets = _parse_te_result_cell(second_html)
    if a_sets is None or b_sets is None:
        # Not yet completed — no result cell or it's empty.
        return {"player_a": player_a, "player_b": player_b,
                "winner": "", "completed": False}
    if a_sets == b_sets:
        # Tie / retirement / walkover with no clear winner — leave ungraded.
        return {"player_a": player_a, "player_b": player_b,
                "winner": "", "completed": False}
    winner = player_a if a_sets > b_sets else player_b
    return {"player_a": player_a, "player_b": player_b,
            "winner": winner, "completed": True}


def _parse_te_result_cell(row_html: str) -> int | None:
    """The first <td class="result">N</td> in a player row holds set count."""
    m = re.search(r'<td class="result"[^>]*>\s*(\d+)\s*</td>', row_html)
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


def _fetch_boxlive_games(date_str: str) -> list[dict]:
    """Filter parsed box.live fights to one date and emit results in the
    standard ESPN shape (home_team/away_team/scores/winner). The Odds API
    and box.live can disagree on which fighter is "home" vs "away", so we
    emit BOTH orderings — _find_game_result will pick whichever matches
    the pick's "Away @ Home" convention."""
    games: list[dict] = []
    for f in _fetch_boxlive_results():
        if f["date_str"] != date_str:
            continue
        a, b = f["fighter_a"], f["fighter_b"]
        winner = f["winner"]
        completed = f["completed"]
        for away, home in ((a, b), (b, a)):
            a_won = (winner == away) if completed else False
            h_won = (winner == home) if completed else False
            games.append({
                "home_team":  home,
                "home_abbr":  "",
                "home_short": home.split()[-1] if home else "",
                "home_score": 1 if h_won else 0,
                "away_team":  away,
                "away_abbr":  "",
                "away_short": away.split()[-1] if away else "",
                "away_score": 1 if a_won else 0,
                "winner":     winner,
                "total":      1,
                "event_id":   "",
                "completed":  completed,
                "status":     "STATUS_FINAL" if completed else "STATUS_SCHEDULED",
            })
    return games


def fetch_espn_results(sport: str, date_str: str) -> list[dict]:
    """
    Fetch completed game results from ESPN for one sport + date (YYYYMMDD).
    Backward-compatible wrapper that filters fetch_espn_games() to only
    games that have actually finished. Used everywhere the caller is
    purely interested in graded outcomes.
    """
    _refresh_espn_cache_if_new_day()
    cache_key = f"{sport}|{date_str}"
    if cache_key in _espn_cache:
        return _espn_cache[cache_key]
    completed_only = [g for g in fetch_espn_games(sport, date_str)
                      if g.get("completed")]
    _espn_cache[cache_key] = completed_only
    return completed_only


# ── ESPN box score (player stats for prop grading) ────────────────────────────

_boxscore_cache: dict[str, dict] = {}


# DNP marker: present in the stat dict means the player was on the roster
# but did not play. Counting stats default to 0 for these players (so an
# "Over 4.5 rebounds" pick on a DNP grades as LOSS instead of NO_RESULT).
_DNP_FLAG = "__DNP__"


def _fetch_mlb_player_stats(game_date: str, home_team: str,
                            away_team: str) -> dict[str, dict]:
    """
    Fetch MLB player stats from the official MLB Stats API
    (statsapi.mlb.com — free, no auth) instead of ESPN's summary box,
    which omits doubles/triples and so can't compute total bases.
    Returns the same shape as fetch_player_stats: {name: {label: value}}.
    Labels exposed: H, 2B, 3B, HR, TB (batting), K (pitching strikeouts).
    """
    if not game_date or not home_team or not away_team:
        return {}
    cache_key = f"mlb-statsapi|{game_date}|{away_team}@{home_team}"
    if cache_key in _boxscore_cache:
        return _boxscore_cache[cache_key]

    iso_date = f"{game_date[:4]}-{game_date[4:6]}-{game_date[6:]}"
    sched_url = ("https://statsapi.mlb.com/api/v1/schedule"
                 f"?sportId=1&date={iso_date}")
    try:
        sched = _espn_session.get(sched_url, timeout=15).json()
    except Exception:
        _boxscore_cache[cache_key] = {}
        return {}

    game_pk = None
    for d in sched.get("dates", []):
        for g in d.get("games", []):
            a = g.get("teams", {}).get("away", {}).get("team", {}).get("name", "")
            h = g.get("teams", {}).get("home", {}).get("team", {}).get("name", "")
            if (_word_overlap(a, away_team) >= 0.5
                    and _word_overlap(h, home_team) >= 0.5):
                game_pk = g.get("gamePk")
                break
        if game_pk:
            break
    if not game_pk:
        _boxscore_cache[cache_key] = {}
        return {}

    box_url = f"https://statsapi.mlb.com/api/v1/game/{game_pk}/boxscore"
    try:
        box = _espn_session.get(box_url, timeout=15).json()
    except Exception:
        _boxscore_cache[cache_key] = {}
        return {}

    player_stats: dict[str, dict] = {}
    for side in ("away", "home"):
        for pdata in box.get("teams", {}).get(side, {}).get("players", {}).values():
            name = pdata.get("person", {}).get("fullName", "")
            if not name:
                continue
            stats = pdata.get("stats", {})
            bat = stats.get("batting") or {}
            pit = stats.get("pitching") or {}
            d: dict[str, float] = {}
            if bat:
                d["H"]  = float(bat.get("hits", 0))
                d["2B"] = float(bat.get("doubles", 0))
                d["3B"] = float(bat.get("triples", 0))
                d["HR"] = float(bat.get("homeRuns", 0))
                d["TB"] = float(bat.get("totalBases", 0))
            if pit:
                d["K"] = float(pit.get("strikeOuts", 0))
            if d:
                player_stats[name] = d
            else:
                # Roster but did not bat or pitch — treat as DNP so counting
                # markets grade as 0 (LOSS for over, WIN for under > 0.5).
                player_stats[name] = {_DNP_FLAG: True}

    _boxscore_cache[cache_key] = player_stats
    time.sleep(0.12)
    return player_stats


def fetch_player_stats(sport: str, event_id: str,
                       game_result: dict | None = None,
                       game_date: str | None = None) -> dict[str, dict]:
    """
    Fetch player stat lines for one completed game.
    Returns {player_display_name: {stat_label: numeric_value}}.
    NBA labels include: PTS, REB, AST.
    MLB labels (via MLB Stats API) include: H, 2B, 3B, HR, TB, K.
    Results cached per event_id (or per gamePk lookup for MLB).
    DNP players are recorded with {_DNP_FLAG: True} so grade_prop can
    grade their counting-stat picks as zero rather than NO_RESULT.
    """
    if sport == "mlb" and game_result and game_date:
        return _fetch_mlb_player_stats(
            game_date,
            game_result.get("home_team", ""),
            game_result.get("away_team", ""),
        )

    if not event_id:
        return {}
    cache_key = f"{sport}|{event_id}"
    if cache_key in _boxscore_cache:
        return _boxscore_cache[cache_key]

    espn_path = ESPN_SPORT_MAP.get(sport)
    if not espn_path:
        _boxscore_cache[cache_key] = {}
        return {}

    url = f"{ESPN_BASE}/{espn_path}/summary?event={event_id}"
    try:
        resp = _espn_session.get(url, timeout=15)
        if resp.status_code != 200:
            _boxscore_cache[cache_key] = {}
            return {}
        data = resp.json()
    except Exception:
        _boxscore_cache[cache_key] = {}
        return {}

    player_stats: dict[str, dict] = {}
    boxscore = data.get("boxscore", {})

    for team_data in boxscore.get("players", []):
        for stat_block in team_data.get("statistics", []):
            labels = stat_block.get("labels", []) or stat_block.get("names", [])
            for athlete_entry in stat_block.get("athletes", []):
                ath  = athlete_entry.get("athlete", {})
                name = ath.get("displayName") or ath.get("shortName", "")
                if not name:
                    continue
                raw_stats = athlete_entry.get("stats", [])
                if not raw_stats:
                    # On the roster but DNP (sat, late scratch, ejected, etc.)
                    # Record presence so grade_prop can grade as zero.
                    player_stats.setdefault(name, {})[_DNP_FLAG] = True
                    continue
                stat_dict: dict[str, float] = {}
                for label, raw_val in zip(labels, raw_stats):
                    try:
                        stat_dict[label] = float(raw_val)
                    except (ValueError, TypeError):
                        pass  # skip strings like "35:42" or "8-17"
                if name not in player_stats:
                    player_stats[name] = {}
                # An actual stat line supersedes a prior DNP marker (if a
                # player appeared in multiple stat blocks).
                player_stats[name].pop(_DNP_FLAG, None)
                player_stats[name].update(stat_dict)

    _boxscore_cache[cache_key] = player_stats
    time.sleep(0.12)
    return player_stats


# ── Game / team matching ───────────────────────────────────────────────────────

def _fuzzy(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def _word_overlap(a: str, b: str) -> float:
    """
    Token-level Jaccard similarity — helps with "LA Lakers" vs
    "Los Angeles Lakers" where character-level fuzzy undershoots.
    """
    wa = set(a.lower().split())
    wb = set(b.lower().split())
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)


def _team_match_score(pick_name: str, side: str, result: dict) -> float:
    """
    Best similarity between pick_name and any ESPN name variant for one side.
    Uses both character-level SequenceMatcher and word-level Jaccard, taking
    the higher score — this handles full names, abbreviations, and short names.
    """
    best = 0.0
    for key in (f"{side}_team", f"{side}_abbr", f"{side}_short"):
        espn_val = result.get(key, "")
        if not espn_val:
            continue
        s = max(_fuzzy(pick_name, espn_val),
                _word_overlap(pick_name, espn_val))
        if s > best:
            best = s
    return best


def _name_tokens(name: str) -> set[str]:
    """Lowercase, diacritic-stripped, non-initial word tokens of a name.
    'Sinner J.' → {'sinner'}; 'Sara Sorribes Tormo' → {'sara','sorribes',
    'tormo'}; 'Karolína Plíšková' → {'karolina','pliskova'}. Used by the
    tennis-specific matcher where char-level fuzzy is too permissive
    (e.g. 'Alexander Zverev' would otherwise match 'Alexandrescou Y.')."""
    import unicodedata
    out = set()
    # NFKD splits accented chars into base + combining marks; we drop the
    # combining marks so 'í' → 'i', 'š' → 's', etc.
    folded = unicodedata.normalize("NFKD", name)
    folded = "".join(c for c in folded if not unicodedata.combining(c))
    for t in folded.split():
        clean = t.lower().rstrip(".")
        if len(clean) > 1:   # drop initials like 'J.'
            out.add(clean)
    return out


def _tennis_token_score(pick_name: str, side: str, result: dict) -> float:
    """For tennis: ratio of shared name tokens between pick and result side
    over the smaller token set. Requires at least one full last-name token
    to match — initials and prefixes can't trigger a false positive."""
    p_tokens = _name_tokens(pick_name)
    if not p_tokens:
        return 0.0
    best = 0.0
    for key in (f"{side}_team", f"{side}_short", f"{side}_abbr"):
        val = result.get(key, "")
        if not val:
            continue
        r_tokens = _name_tokens(val)
        if not r_tokens:
            continue
        shared = p_tokens & r_tokens
        if shared:
            score = len(shared) / min(len(p_tokens), len(r_tokens))
            if score > best:
                best = score
    return best


def _find_game_result(pick: dict, results: list[dict]) -> dict | None:
    """
    Match pick's game string "AWAY @ HOME" against ESPN results.

    Scoring: each side is matched independently.  A game is accepted when
    BOTH sides score ≥ 0.40 AND their average ≥ 0.50.  This is more
    permissive than requiring average ≥ 0.55 while still preventing
    false positives (one side matching perfectly but wrong game).

    Tennis uses a stricter token-based matcher because the standard fuzzy
    falsely matches names that share a long prefix (e.g. 'Alexander Zverev'
    vs 'Alexandrescou Y.' — both start with 'Alex'). Token-overlap scoring
    requires a full word in common, not just substring overlap.
    """
    parts = pick["game"].split("@")
    if len(parts) != 2:
        return None
    away_pick = parts[0].strip()
    home_pick = parts[1].strip()

    sport = (pick.get("sport") or "").lower()
    use_tennis_match = sport == "tennis"
    score_fn = _tennis_token_score if use_tennis_match else _team_match_score
    side_thresh = 0.5 if use_tennis_match else 0.40
    avg_thresh  = 0.5 if use_tennis_match else 0.50

    best_match = None
    best_score = 0.0

    for r in results:
        away_s = score_fn(away_pick, "away", r)
        home_s = score_fn(home_pick, "home", r)

        # Both sides must independently look plausible
        if away_s < side_thresh or home_s < side_thresh:
            continue

        score = (away_s + home_s) / 2
        if score > best_score:
            best_score = score
            best_match = r

    return best_match if best_score >= avg_thresh else None


def _match_side(pick_name: str, result: dict, sport: str = "") -> str | None:
    """
    Return 'home' or 'away' depending on which team pick_name refers to.
    Uses both character-level and word-level similarity. Tennis switches
    to the token-based matcher for the same false-positive reasons as
    _find_game_result.
    """
    score_fn = _tennis_token_score if sport.lower() == "tennis" else _team_match_score
    threshold = 0.5 if sport.lower() == "tennis" else 0.45
    home_score = score_fn(pick_name, "home", result)
    away_score = score_fn(pick_name, "away", result)

    if home_score >= threshold and home_score >= away_score:
        return "home"
    if away_score >= threshold:
        return "away"
    return None


def grade_pick(pick: dict, result: dict) -> str:
    """
    Grade a pick as WIN / LOSS / PUSH / UNKNOWN given the ESPN game result.
    """
    market  = pick["market"]
    outcome = pick["pick"]
    line    = pick.get("line")
    sport   = pick.get("sport", "")

    if market == "H2H":
        if outcome.lower() == "draw":
            return "WIN" if result["home_score"] == result["away_score"] else "LOSS"
        if result["home_score"] == result["away_score"]:
            return "PUSH"
        side = _match_side(outcome, result, sport)
        if side is None:
            return "UNKNOWN"
        my_score  = result[f"{side}_score"]
        opp_score = result["away_score" if side == "home" else "home_score"]
        return "WIN" if my_score > opp_score else "LOSS"

    elif market == "SPREADS":
        if line is None:
            return "UNKNOWN"
        side = _match_side(outcome, result, sport)
        if side is None:
            return "UNKNOWN"
        my_score  = result[f"{side}_score"]
        opp_score = result["away_score" if side == "home" else "home_score"]
        margin    = my_score + float(line) - opp_score
        if margin > 0:   return "WIN"
        elif margin < 0: return "LOSS"
        else:            return "PUSH"

    elif market == "TOTALS":
        if line is None:
            return "UNKNOWN"
        total      = float(result["total"])
        line_total = float(line)
        is_over    = outcome.lower().startswith("over")
        diff       = total - line_total
        if diff == 0:
            return "PUSH"
        if is_over:
            return "WIN" if diff > 0 else "LOSS"
        else:
            return "WIN" if diff < 0 else "LOSS"

    return "UNKNOWN"


# ── Prop grading ──────────────────────────────────────────────────────────────

def _find_player_in_stats(player_name: str,
                           player_stats: dict[str, dict]) -> dict | None:
    """
    Fuzzy-match player_name against the ESPN box-score player dict.
    Returns the stat dict for the best match, or None if no match ≥ 0.60.
    """
    best_name  = None
    best_score = 0.0
    pn_lower   = player_name.lower()
    for espn_name in player_stats:
        score = max(
            SequenceMatcher(None, pn_lower, espn_name.lower()).ratio(),
            # last-name match: check if last word of query is in ESPN name
            (0.75 if pn_lower.split()[-1] in espn_name.lower() else 0.0),
        )
        if score > best_score:
            best_score = score
            best_name  = espn_name
    if best_score >= 0.60 and best_name is not None:
        return player_stats[best_name]
    return None


def grade_prop(pick: dict, game_result: dict) -> str:
    """
    Grade a player-prop pick given the matched game result.
    For MLB the box score is fetched from MLB Stats API (totalBases is
    only exposed directly there); other sports use ESPN's summary.
    DNP players grade as stat=0 for counting markets.
    Returns WIN / LOSS / PUSH / NO_RESULT / UNKNOWN.
    """
    sport    = pick["sport"]
    player   = pick.get("player", "")
    market   = pick["market"]
    side     = pick.get("prop_side", "")
    line     = pick.get("line")

    if line is None or not player or not side:
        return "NO_RESULT"

    # MLB uses MLB Stats API (date + teams), no ESPN event_id needed.
    # Other sports require an event_id from the matched ESPN game.
    if sport != "mlb" and not game_result.get("event_id"):
        return "NO_RESULT"

    player_stats = fetch_player_stats(
        sport,
        game_result.get("event_id", ""),
        game_result=game_result,
        game_date=pick.get("game_date"),
    )
    if not player_stats:
        return "NO_RESULT"

    stats = _find_player_in_stats(player, player_stats)
    if stats is None:
        return "NO_RESULT"

    stat_label = PROP_ESPN_STAT.get(market)
    if not stat_label:
        return "UNKNOWN"
    raw = stats.get(stat_label)
    if raw is None:
        # Player was on the roster but didn't play — counting stats are 0.
        if stats.get(_DNP_FLAG) and market in _PROP_DNP_DEFAULT_ZERO:
            stat_val = 0.0
        else:
            return "NO_RESULT"
    else:
        stat_val = float(raw)

    diff = stat_val - float(line)
    if diff == 0:
        return "PUSH"
    if side == "over":
        return "WIN" if diff > 0 else "LOSS"
    else:
        return "WIN" if diff < 0 else "LOSS"


# ── Backtest runner ────────────────────────────────────────────────────────────

def _backtest_pick_key(pick: dict) -> str:
    """Build the closing-lines pick_key from a backtest pick dict.
    Backtest uses 'pick'/'line'; the canonical key uses 'outcome'/'point'."""
    from odds_compare import _make_pick_key
    return _make_pick_key({
        "game":    pick.get("game"),
        "book":    pick.get("book"),
        "market":  pick.get("market"),
        "outcome": pick.get("pick"),
        "point":   pick.get("line"),
    })


def _stamp_clv(pick: dict, lookup: dict | None) -> None:
    """Attach close odds + CLV% to a pick if the close was captured.
    Always sets the four CLV fields (None when not captured) so xlsx
    write code doesn't need to special-case missing keys.

    Lookup is keyed by (game_date_yyyymmdd, pick_key) — date namespacing
    prevents the same matchup on different days from cross-contaminating
    (e.g., yesterday's NY Yankees ML close attaching to today's pick)."""
    pick["close_odds_american"] = None
    pick["close_implied_pct"]   = None
    pick["clv_pct"]             = None
    pick["clv_captured"]        = False
    if not lookup:
        return
    game_date = pick.get("game_date")
    if not game_date:
        # No parseable game date — can't safely match across days.
        return
    close = lookup.get((game_date, _backtest_pick_key(pick)))
    if not close:
        return
    pick["close_odds_american"] = close.get("odds_american")
    pick["close_implied_pct"]   = close.get("implied_pct")
    pick["clv_pct"]             = close.get("clv_pct")
    pick["clv_captured"]        = close.get("clv_pct") is not None


def run_backtest(picks: list[dict],
                  closing_lines_lookup: dict | None = None,
                  progress_label: str | None = None) -> list[dict]:
    """Grade all picks. Attaches 'result', 'pnl', 'actual_result' to each.

    progress_label names this pool ("EV pool", "props", …) in the live
    progress feed the webapp's job-status endpoints poll (progress.py).

    If closing_lines_lookup is provided, also attaches 'close_odds_american',
    'close_implied_pct', 'clv_pct', 'clv_captured' for every pick (None
    when no close was captured for that pick — the worker only fires for
    picks with an Odds API event_id, so AN-fallback picks are uncapturable).
    """
    today = date.today()

    # Pre-fetch all needed (sport, date) combos, including ±1 day buffers
    sport_dates: set[tuple[str, str]] = set()
    for p in picks:
        sport_norm = _normalise_sport(p["sport"])
        if sport_norm in SCOREABLE and p["game_date"]:
            gd = datetime.strptime(p["game_date"], "%Y%m%d").date()
            if gd <= today:
                for off in (0, 1, -1):
                    sport_dates.add(
                        (sport_norm,
                         (gd + timedelta(days=off)).strftime("%Y%m%d")))

    import progress as _progress
    _plabel = f"Grading {progress_label}" if progress_label else "Grading picks"

    print(f"Fetching results for {len(sport_dates)} sport-date combos from ESPN...")
    for i, (sport, dt_str) in enumerate(sorted(sport_dates)):
        fetch_espn_results(sport, dt_str)
        _progress.report("backtest", f"{_plabel}: fetching results",
                         i + 1, len(sport_dates), note=sport)
        if (i + 1) % 15 == 0:
            print(f"  {i+1}/{len(sport_dates)}...")
    total_games = sum(len(v) for v in _espn_cache.values())
    print(f"  Done — {total_games} completed games cached\n")

    matched = unmatched = skipped = 0

    for _pick_i, pick in enumerate(picks):
        if _pick_i % 25 == 0:
            _progress.report("backtest", f"{_plabel}: grading",
                             _pick_i, len(picks))
        # Normalise sport name (AN may store full league name)
        pick["sport"] = _normalise_sport(pick["sport"])
        sport   = pick["sport"]
        game_dt = pick.get("game_date")

        # Non-scoreable markets / sports
        if pick["market"] == "HR" or sport == "pga":
            pick["result"] = "MANUAL"
            skipped += 1
            continue
        if sport not in SCOREABLE:
            pick["result"] = "MANUAL"
            skipped += 1
            continue
        if not game_dt:
            pick["result"] = "NO_RESULT"
            unmatched += 1
            continue

        gd = datetime.strptime(game_dt, "%Y%m%d").date()
        if gd > today:
            # Future game — no scoreboard query needed, definitely not started.
            pick["result"] = "NOT_STARTED"
            continue

        # First check the original date including unfinished games. If the
        # matchup IS on the schedule but hasn't completed, distinguish:
        #   • STATUS_SCHEDULED / *PRE_GAME* / *DELAYED* → NOT_STARTED
        #   • Anything else (in-progress, halftime, etc.) → IN_PROGRESS
        # Either way, skip the ±1 day fallback so we don't grade against
        # yesterday's same-series game.
        same_day_all = fetch_espn_games(sport, gd.strftime("%Y%m%d"))
        same_day_match = _find_game_result(pick, same_day_all)
        if same_day_match is not None and not same_day_match.get("completed"):
            status = (same_day_match.get("status") or "").upper()
            # POSTPONED / CANCELED / ABANDONED (2026-07-13, from the gap
            # audit's first real catch): books void these bets, and the
            # makeup runs as a fresh fixture (often a doubleheader) that
            # the original pick must NOT grade against. VOID is terminal:
            # excluded from P&L like MANUAL, but named for what it is.
            if ("POST" in status or "CANC" in status or "ABD" in status
                    or "ABAN" in status):
                pick["result"] = "VOID"
            elif "SCHED" in status or "PRE" in status or "DELAY" in status:
                pick["result"] = "NOT_STARTED"
            else:
                pick["result"] = "IN_PROGRESS"
            continue

        # Now look for a completed game. Try exact date first, then ±1 day
        # (handles timezone boundary games — ESPN occasionally indexes
        # a late-night game under the UTC date rather than ET). Only the
        # ±1 fallback runs when the exact date had NO match at all, since
        # we already filtered out the "scheduled today" case above.
        game_result = None
        for day_offset in (0, 1, -1):
            check_dt  = (gd + timedelta(days=day_offset)).strftime("%Y%m%d")
            day_results = fetch_espn_results(sport, check_dt)
            game_result = _find_game_result(pick, day_results)
            if game_result is not None:
                break

        if game_result is None:
            pick["result"] = "NO_RESULT"
            unmatched += 1
            continue

        # Props need box-score grading; game lines use score-based grading
        if pick.get("is_prop"):
            grade = grade_prop(pick, game_result)
        else:
            grade = grade_pick(pick, game_result)
        pick["result"]        = grade
        pick["actual_result"] = game_result
        matched += 1

        bet = pick["bet_amount"]
        dec = pick.get("decimal") or 1.0

        if grade == "WIN":
            pick["pnl"] = round(bet * (dec - 1), 2)
        elif grade == "LOSS":
            pick["pnl"] = round(-bet, 2)
        elif grade == "PUSH":
            pick["pnl"] = 0.0
        else:
            pick["pnl"] = None   # UNKNOWN — exclude from stats

    print(f"Matched: {matched}  |  No result: {unmatched}  "
          f"|  MANUAL/skipped: {skipped}")

    # ── CLV stamping (independent of grading status) ─────────────────────
    # Every pick — graded or not — gets the four CLV fields set so
    # downstream code can iterate without checking for missing keys.
    # Captured CLVs come from the closing-lines worker; AN-fallback
    # picks (no Odds API event_id) will always be uncaptured here.
    if closing_lines_lookup:
        captured = 0
        for pick in picks:
            _stamp_clv(pick, closing_lines_lookup)
            if pick["clv_captured"]:
                captured += 1
        print(f"  CLV captured: {captured} / {len(picks)} picks")
    else:
        for pick in picks:
            _stamp_clv(pick, None)

    return picks


# Sports tried in order when promo pick has sport="unknown" (All Sports promos)
_ALL_SPORTS_FALLBACK = [
    "nba", "mlb", "nhl", "nfl", "ncaab", "ncaaf",
    "epl", "mls", "laliga", "ligue1", "seriea", "bundesliga",
    "champions", "europa", "ufc",
]


def _grade_hr_anytime(pick: dict, game_result: dict) -> str:
    """Grade an anytime-home-run promo pick (market="HR", implicitly Over
    0.5 HR for the named batter).

    Uses the MLB Stats API box score (same source as total_bases), which
    distinguishes "batted, 0 HR" (LOSS) from "on roster, did not bat"
    (DNP → PUSH, the book voids and refunds the stake). A batter absent
    from the box score is treated the same as a DNP (void). This is the
    DNP detection whose absence kept HR/GOAL on MANUAL before.
    Returns WIN / LOSS / PUSH / NO_RESULT."""
    player = (pick.get("player") or pick.get("pick") or "").strip()
    if not player:
        return "NO_RESULT"
    stats = _fetch_mlb_player_stats(
        pick.get("game_date", ""),
        game_result.get("home_team", ""),
        game_result.get("away_team", ""))
    if not stats:
        return "NO_RESULT"
    pstat = _find_player_in_stats(player, stats)
    if pstat is None or pstat.get(_DNP_FLAG):
        return "PUSH"          # did not appear / did not bat → voided
    hr = pstat.get("HR")
    if hr is None:
        return "PUSH"
    return "WIN" if float(hr) >= 1 else "LOSS"


def _fetch_espn_summary(espn_path: str, event_id: str) -> dict:
    """Fetch + cache one ESPN game summary (keyEvents, rosters, boxscore)."""
    if not event_id:
        return {}
    cache_key = f"summary|{espn_path}|{event_id}"
    if cache_key in _boxscore_cache:
        return _boxscore_cache[cache_key]
    url = f"{ESPN_BASE}/{espn_path}/summary?event={event_id}"
    try:
        resp = _espn_session.get(url, timeout=15)
        data = resp.json() if resp.status_code == 200 else {}
    except Exception:
        data = {}
    _boxscore_cache[cache_key] = data
    time.sleep(0.12)
    return data


def _grade_goal_anytime(pick: dict, game_result: dict) -> str:
    """Grade an anytime-goalscorer promo pick (market="GOAL", World Cup).

    WIN if the named player is credited with a goal in the match's
    keyEvents (own goals excluded). Otherwise the player's lineup status
    decides: started or subbed in → LOSS; never entered the match → PUSH
    (the book voids and refunds the stake on a DNP).
    Returns WIN / LOSS / PUSH / NO_RESULT."""
    player = (pick.get("player") or pick.get("pick") or "").strip()
    if not player:
        return "NO_RESULT"
    espn_path = ESPN_SPORT_MAP.get("world_cup", "soccer/fifa.world")
    summ = _fetch_espn_summary(espn_path, game_result.get("event_id", ""))
    if not summ:
        return "NO_RESULT"
    pn = player.lower()

    def _match(name: str) -> bool:
        nm = (name or "").lower()
        return (SequenceMatcher(None, pn, nm).ratio() >= 0.80
                or (pn.split()[-1] in nm if pn.split() else False))

    for ke in summ.get("keyEvents", []):
        ttype = (ke.get("type") or {}).get("text", "") if isinstance(
            ke.get("type"), dict) else ""
        if "goal" in ttype.lower() and "own" not in ttype.lower():
            for ath in ke.get("athletesInvolved") or []:
                if _match(ath.get("displayName", "")):
                    return "WIN"

    appeared = None  # None → couldn't determine from rosters
    for team in summ.get("rosters", []):
        for entry in team.get("roster", []):
            ath = entry.get("athlete", {}) or {}
            if _match(ath.get("displayName", "")):
                appeared = bool(entry.get("starter") or entry.get("subbedIn"))
                break
        if appeared is not None:
            break
    if appeared is False:
        return "PUSH"          # named but never came on → voided
    # appeared True, or rosters unavailable: a goalscorer pick is made
    # inside the lineup window, so a non-scorer who was in the XI/subs is a
    # LOSS; default to LOSS when rosters can't confirm (the realistic case).
    return "LOSS"


def run_backtest_promo(picks: list[dict]) -> list[dict]:
    """
    Grade promo picks.  Similar to run_backtest but:
      • "All Sports" promos (sport="unknown") try every sport in
        _ALL_SPORTS_FALLBACK until a game match is found.
      • Free-bet losses count as $0 (stake not at risk).
      • P&L is calculated from boosted odds (stored in pick["decimal"]).
    """
    today = date.today()

    # Pre-fetch ESPN results for all required (sport, date) pairs
    sport_dates: set[tuple[str, str]] = set()
    for p in picks:
        if not p.get("game_date"):
            continue
        gd = datetime.strptime(p["game_date"], "%Y%m%d").date()
        if gd > today:
            continue
        sports_to_fetch = (
            [p["sport"]] if p["sport"] != "unknown" and p["sport"] in SCOREABLE
            else _ALL_SPORTS_FALLBACK
        )
        for s in sports_to_fetch:
            if s in SCOREABLE:
                for off in (0, 1, -1):
                    sport_dates.add((s, (gd + timedelta(days=off)).strftime("%Y%m%d")))

    print(f"Fetching promo results for {len(sport_dates)} sport-date combos from ESPN...")
    for i, (sp, dt_str) in enumerate(sorted(sport_dates)):
        fetch_espn_results(sp, dt_str)
        if (i + 1) % 15 == 0:
            print(f"  {i+1}/{len(sport_dates)}...")
    print(f"  Done — {sum(len(v) for v in _espn_cache.values())} games in cache\n")

    matched = unmatched = skipped = 0

    for pick in picks:
        sport   = pick["sport"]
        game_dt = pick.get("game_date")

        if not game_dt:
            pick["result"] = "NO_RESULT"
            unmatched += 1
            continue

        gd = datetime.strptime(game_dt, "%Y%m%d").date()
        if gd > today:
            pick["result"] = "NOT_STARTED"
            continue

        # PENDING-aware: if the matchup is on today's schedule but hasn't
        # completed, classify NOT_STARTED vs IN_PROGRESS and skip the
        # ±1 day fallback (which would grab yesterday's same-series game).
        gd_str = gd.strftime("%Y%m%d")
        same_day_match = None
        if sport != "unknown" and sport in SCOREABLE:
            same_day_match = _find_game_result(
                pick, fetch_espn_games(sport, gd_str))
        else:
            for s in _ALL_SPORTS_FALLBACK:
                if s not in SCOREABLE:
                    continue
                m = _find_game_result(pick, fetch_espn_games(s, gd_str))
                if m is not None:
                    same_day_match = m
                    break
        if same_day_match is not None and not same_day_match.get("completed"):
            status = (same_day_match.get("status") or "").upper()
            # POSTPONED / CANCELED / ABANDONED (2026-07-13, from the gap
            # audit's first real catch): books void these bets, and the
            # makeup runs as a fresh fixture (often a doubleheader) that
            # the original pick must NOT grade against. VOID is terminal:
            # excluded from P&L like MANUAL, but named for what it is.
            if ("POST" in status or "CANC" in status or "ABD" in status
                    or "ABAN" in status):
                pick["result"] = "VOID"
            elif "SCHED" in status or "PRE" in status or "DELAY" in status:
                pick["result"] = "NOT_STARTED"
            else:
                pick["result"] = "IN_PROGRESS"
            continue

        # Find matching ESPN game result
        game_result    = None
        matched_sport  = sport

        if sport != "unknown" and sport in SCOREABLE:
            for day_off in (0, 1, -1):
                check_dt    = (gd + timedelta(days=day_off)).strftime("%Y%m%d")
                game_result = _find_game_result(pick, fetch_espn_results(sport, check_dt))
                if game_result is not None:
                    break
        else:
            # "All Sports" promo — try each sport until we find the game
            for s in _ALL_SPORTS_FALLBACK:
                if s not in SCOREABLE:
                    continue
                for day_off in (0, 1, -1):
                    check_dt    = (gd + timedelta(days=day_off)).strftime("%Y%m%d")
                    game_result = _find_game_result(pick, fetch_espn_results(s, check_dt))
                    if game_result is not None:
                        matched_sport = s
                        break
                if game_result is not None:
                    break

        pick["sport"] = matched_sport   # persist discovered sport

        if game_result is None:
            pick["result"] = "NO_RESULT"
            unmatched += 1
            continue

        # Anytime binary markets (MLB HR, WC goalscorer) carry no O/U suffix
        # so is_prop is False — grade them via box score with DNP→void
        # detection (a void grades PUSH: stake refunded, P&L 0). Props need
        # box-score grading; game lines use score-based grading.
        # Mirrors the same branch in run_backtest — without it, prop promos
        # were getting graded as game-line bets and returning bogus W/Ls.
        if pick["market"] == "HR":
            grade = _grade_hr_anytime(pick, game_result)
        elif pick["market"] == "GOAL":
            grade = _grade_goal_anytime(pick, game_result)
        elif pick.get("is_prop"):
            grade = grade_prop(pick, game_result)
        else:
            grade = grade_pick(pick, game_result)
        pick["result"]        = grade
        pick["actual_result"] = game_result
        matched += 1

        bet = pick["bet_amount"]
        dec = pick.get("decimal") or 1.0

        if grade == "WIN":
            pick["pnl"] = round(bet * (dec - 1), 2)
        elif grade == "LOSS":
            # Free bets cost nothing when they lose
            pick["pnl"] = 0.0 if pick.get("is_free_bet") else round(-bet, 2)
        elif grade == "PUSH":
            pick["pnl"] = 0.0
        else:
            pick["pnl"] = None

    print(f"Promo — Matched: {matched}  |  No result: {unmatched}  "
          f"|  MANUAL/skipped: {skipped}")
    return picks


# ── Console analysis ───────────────────────────────────────────────────────────

def analyze(picks: list[dict], label: str = "All +EV Picks") -> list[dict]:
    """Print comprehensive backtest analysis to console. Returns settled picks."""
    settled = [p for p in picks if p["result"] in ("WIN", "LOSS", "PUSH")]

    if not settled:
        print("\nNo settled picks found — games may not have finished yet.")
        return settled

    wins          = [p for p in settled if p["result"] == "WIN"]
    losses        = [p for p in settled if p["result"] == "LOSS"]
    pushes        = [p for p in settled if p["result"] == "PUSH"]
    total_wagered = sum(p["bet_amount"] for p in settled)
    total_pnl     = sum(p["pnl"]       for p in settled)
    roi           = total_pnl / total_wagered * 100 if total_wagered else 0
    wr            = len(wins) / (len(wins) + len(losses)) * 100 if (wins or losses) else 0

    w = 62
    print(f"\n{'='*w}")
    print(f"  BACKTEST RESULTS — {label}")
    print(f"{'='*w}")
    print(f"  Settled:  {len(settled)}  "
          f"(W{len(wins)} / L{len(losses)} / P{len(pushes)})")
    print(f"  Win Rate: {wr:.1f}%  (excl. pushes)")
    print(f"  Wagered:  ${total_wagered:,.2f}")
    print(f"  P&L:      ${total_pnl:+,.2f}")
    print(f"  ROI:      {roi:+.1f}%")
    print(f"{'='*w}")

    def _section(title: str, groups: dict):
        print(f"\n  {title}")
        print(f"  {'─'*58}")
        for label, stats in sorted(groups.items(),
                                    key=lambda x: x[1]["pnl"], reverse=True):
            wr_ = stats["w"] / (stats["w"] + stats["l"]) * 100 \
                  if (stats["w"] + stats["l"]) else 0
            roi_= stats["pnl"] / stats["wagered"] * 100 if stats["wagered"] else 0
            print(f"  {label:<20}  W{stats['w']}/L{stats['l']}/P{stats['p']}"
                  f"  WR {wr_:.0f}%  "
                  f"P&L ${stats['pnl']:>+8.2f}  ROI {roi_:>+.1f}%")

    def _agg(picks_, key_fn):
        grp = defaultdict(lambda: {"w":0,"l":0,"p":0,"wagered":0.0,"pnl":0.0})
        for p in picks_:
            k = grp[key_fn(p)]
            k["wagered"] += p["bet_amount"]
            k["pnl"]     += p["pnl"]
            if p["result"] == "WIN":  k["w"] += 1
            elif p["result"] == "LOSS": k["l"] += 1
            else:                     k["p"] += 1
        return dict(grp)

    _section("P&L BY BOOK",       _agg(settled, lambda p: p["book"]))
    _section("P&L BY SPORT",      _agg(settled, lambda p: p["sport"].upper()))
    _section("P&L BY MARKET",     _agg(settled, lambda p: p["market"]))
    _section("P&L BY CONFIDENCE", _agg(settled, lambda p: p["confidence"] or "?"))

    # EV tiers
    ev_tiers = [
        ("EV >= 5%", lambda p: (p["ev_pct"] or 0) >= 5),
        ("EV 3–5%",  lambda p: 3 <= (p["ev_pct"] or 0) < 5),
        ("EV 1–3%",  lambda p: 1 <= (p["ev_pct"] or 0) < 3),
        ("EV < 1%",  lambda p: (p["ev_pct"] or 0) < 1),
    ]
    print(f"\n  P&L BY EV TIER")
    print(f"  {'─'*58}")
    for label, filt in ev_tiers:
        tp = [p for p in settled if filt(p)]
        if not tp: continue
        tw_ = sum(p["bet_amount"] for p in tp)
        tpnl = sum(p["pnl"] for p in tp)
        ww = sum(1 for p in tp if p["result"] == "WIN")
        ll = sum(1 for p in tp if p["result"] == "LOSS")
        wr_ = ww / (ww + ll) * 100 if (ww + ll) else 0
        roi_ = tpnl / tw_ * 100 if tw_ else 0
        print(f"  {label:<20}  W{ww}/L{ll}  WR {wr_:.0f}%  "
              f"P&L ${tpnl:>+8.2f}  ROI {roi_:>+.1f}%")

    # Edge durability
    dur_tiers = [
        ("Persistent (3+)", lambda p: p.get("times_seen", 1) >= 3),
        ("Seen twice",      lambda p: p.get("times_seen", 1) == 2),
        ("One-time",        lambda p: p.get("times_seen", 1) == 1),
    ]
    print(f"\n  P&L BY EDGE DURABILITY")
    print(f"  {'─'*58}")
    for label, filt in dur_tiers:
        dp = [p for p in settled if filt(p)]
        if not dp: continue
        tw_ = sum(p["bet_amount"] for p in dp)
        tpnl = sum(p["pnl"] for p in dp)
        ww = sum(1 for p in dp if p["result"] == "WIN")
        ll = sum(1 for p in dp if p["result"] == "LOSS")
        wr_ = ww / (ww + ll) * 100 if (ww + ll) else 0
        roi_ = tpnl / tw_ * 100 if tw_ else 0
        print(f"  {label:<20}  W{ww}/L{ll}  WR {wr_:.0f}%  "
              f"P&L ${tpnl:>+8.2f}  ROI {roi_:>+.1f}%")

    # Prop market × EV-tier cross-slice — feeds _qualifies_prop() calibration.
    # Non-Pinn only: the qualifier already lets any Pinn-anchored prop through
    # at ev >= 1.0, so threshold decisions are about the non-Pinn band.
    prop_settled = [p for p in settled
                    if p.get("is_prop") and not p.get("pinnacle_anchored")]
    if prop_settled:
        ev_buckets = [
            ("1–2%",  lambda v: 1.0 <= v < 2.0),
            ("2–3%",  lambda v: 2.0 <= v < 3.0),
            ("3–5%",  lambda v: 3.0 <= v < 5.0),
            ("5%+",   lambda v: v >= 5.0),
        ]
        by_market: dict[str, list] = defaultdict(list)
        for p in prop_settled:
            by_market[p["market"]].append(p)

        print(f"\n  PROP MARKET × EV TIER (non-Pinn only)")
        print(f"  {'─'*70}")
        for mkt in sorted(by_market.keys()):
            mp = by_market[mkt]
            mw = sum(1 for p in mp if p["result"] == "WIN")
            ml = sum(1 for p in mp if p["result"] == "LOSS")
            mwager = sum(p["bet_amount"] for p in mp)
            mpnl   = sum(p["pnl"] for p in mp)
            mroi   = mpnl / mwager * 100 if mwager else 0
            print(f"  {mkt:<14}  total: W{mw}/L{ml}  "
                  f"P&L ${mpnl:>+7.2f}  ROI {mroi:>+5.1f}%  (n={len(mp)})")
            for blabel, bfilt in ev_buckets:
                bp = [p for p in mp if bfilt(p.get("ev_pct") or 0)]
                if not bp:
                    continue
                bw = sum(1 for p in bp if p["result"] == "WIN")
                bl = sum(1 for p in bp if p["result"] == "LOSS")
                bwager = sum(p["bet_amount"] for p in bp)
                bpnl   = sum(p["pnl"] for p in bp)
                broi   = bpnl / bwager * 100 if bwager else 0
                note = ""
                if (bw + bl) >= 30 and broi >= 5.0:
                    note = "  ← promote candidate"
                elif (bw + bl) >= 30 and broi <= -5.0:
                    note = "  ← exclude"
                print(f"    {blabel:<8}  W{bw}/L{bl}  "
                      f"P&L ${bpnl:>+7.2f}  ROI {broi:>+5.1f}%  "
                      f"(n={len(bp)}){note}")

    return settled


# ── Excel report ───────────────────────────────────────────────────────────────

def _hfill(color: str) -> PatternFill:
    return PatternFill(start_color=color, end_color=color, fill_type="solid")

def _thin():
    s = Side(style="thin")
    return Border(left=s, right=s, top=s, bottom=s)

RESULT_FILL = {
    "WIN":    "C6EFCE", "LOSS":   "FCE4EC",
    "PUSH":   "DAEEF3", "PENDING":"FFEB9C",
    "MANUAL": "EEEEEE", "NO_RESULT":"EEEEEE", "UNKNOWN":"EEEEEE",
}
RESULT_FONT_COLOR = {
    "WIN":    "006100", "LOSS":   "9C0006",
    "PUSH":   "1F4E79", "PENDING":"9C5700",
    "MANUAL": "888888", "NO_RESULT":"AAAAAA", "UNKNOWN":"AAAAAA",
}


def _hdr(ws, cols: list[tuple], color: str, row: int = 1):
    hf    = _hfill(color)
    hfont = Font(bold=True, color="FFFFFF", size=11)
    for ci, (name, width) in enumerate(cols, 1):
        c = ws.cell(row=row, column=ci, value=name)
        c.font      = hfont
        c.fill      = hf
        c.alignment = Alignment(horizontal="center", wrap_text=True)
        c.border    = _thin()
        ws.column_dimensions[get_column_letter(ci)].width = width


def _breakdown_tab(wb, name: str, picks: list[dict], key_fn, label: str, color: str):
    ws = wb.create_sheet(name)
    cols = [
        (label, 22), ("Picks", 8), ("Resolved", 9),
        ("W", 5), ("L", 5), ("P", 5), ("Win %", 8),
        ("Wagered", 13), ("P&L", 13), ("ROI %", 9), ("Avg EV %", 9),
    ]
    _hdr(ws, cols, color)

    groups: dict[str, list] = defaultdict(list)
    for p in picks:
        groups[key_fn(p)].append(p)

    row = 2
    grand = defaultdict(float)

    for grp in sorted(groups, key=str.lower):
        gp  = groups[grp]
        res = [p for p in gp if p["result"] in ("WIN","LOSS","PUSH")]
        gw  = sum(1 for p in res if p["result"] == "WIN")
        gl  = sum(1 for p in res if p["result"] == "LOSS")
        gp_ = sum(1 for p in res if p["result"] == "PUSH")
        gwr    = gw / (gw + gl) * 100 if (gw + gl) else 0
        gwager = sum(p["bet_amount"] for p in res)
        gpnl   = sum(p["pnl"] for p in res)
        groi   = gpnl / gwager * 100 if gwager else 0
        gevpct = sum(p["ev_pct"] or 0 for p in gp) / len(gp) if gp else 0

        grand["picks"]  += len(gp)
        grand["res"]    += len(res)
        grand["w"]      += gw
        grand["l"]      += gl
        grand["p"]      += gp_
        grand["wager"]  += gwager
        grand["pnl"]    += gpnl
        grand["ev"]     += sum(p["ev_pct"] or 0 for p in gp)

        base = "F2F2F2" if row % 2 == 0 else None
        vals = [grp, len(gp), len(res), gw, gl, gp_,
                gwr / 100, gwager, gpnl, groi / 100, gevpct / 100]
        for ci, val in enumerate(vals, 1):
            c = ws.cell(row=row, column=ci, value=val)
            c.border    = _thin()
            c.alignment = Alignment(horizontal="center")
            if base: c.fill = _hfill(base)
        ws.cell(row=row, column=7).number_format  = '0.0%'
        ws.cell(row=row, column=8).number_format  = '$#,##0.00'
        ws.cell(row=row, column=9).number_format  = '$#,##0.00'
        ws.cell(row=row, column=10).number_format = '0.0%'
        ws.cell(row=row, column=11).number_format = '0.0%'
        pnl_c = ws.cell(row=row, column=9)
        pnl_c.font = Font(bold=True, color="006100" if gpnl >= 0 else "9C0006")
        roi_c = ws.cell(row=row, column=10)
        roi_c.font = Font(bold=True, color="006100" if groi >= 0 else "9C0006")
        row += 1

    data_rows = row - 2  # number of group rows (excl header + total)

    # Totals
    tot_res = int(grand["res"])
    tot_wr  = grand["w"] / (grand["w"] + grand["l"]) * 100 \
              if (grand["w"] + grand["l"]) else 0
    tot_roi = grand["pnl"] / grand["wager"] * 100 if grand["wager"] else 0
    tot_ev  = grand["ev"] / grand["picks"] if grand["picks"] else 0
    for ci, val in enumerate([
        "TOTAL", int(grand["picks"]), tot_res,
        int(grand["w"]), int(grand["l"]), int(grand["p"]),
        tot_wr / 100, grand["wager"], grand["pnl"],
        tot_roi / 100, tot_ev / 100,
    ], 1):
        c = ws.cell(row=row, column=ci, value=val)
        c.font      = Font(bold=True, size=11)
        c.border    = _thin()
        c.alignment = Alignment(horizontal="center")
    ws.cell(row=row, column=7).number_format  = '0.0%'
    ws.cell(row=row, column=8).number_format  = '$#,##0.00'
    ws.cell(row=row, column=9).number_format  = '$#,##0.00'
    ws.cell(row=row, column=10).number_format = '0.0%'
    ws.cell(row=row, column=11).number_format = '0.0%'
    ws.cell(row=row, column=9).font = Font(
        bold=True, size=11, color="006100" if grand["pnl"] >= 0 else "9C0006")
    ws.freeze_panes = "A2"

    # ── Bar chart: P&L and Win % by group ─────────────────────────────────
    if data_rows >= 1:
        chart_row = row + 2   # place chart below the table

        # P&L bar series (col 9)
        chart_pnl = BarChart()
        chart_pnl.type    = "col"
        chart_pnl.title   = f"P&L by {label}"
        chart_pnl.y_axis.title = "P&L ($)"
        chart_pnl.x_axis.title = label
        chart_pnl.style   = 10
        chart_pnl.width   = 20
        chart_pnl.height  = 12
        chart_pnl.grouping = "clustered"

        pnl_data  = Reference(ws, min_col=9, min_row=1, max_row=1 + data_rows)
        cats      = Reference(ws, min_col=1, min_row=2, max_row=1 + data_rows)
        chart_pnl.add_data(pnl_data, titles_from_data=True)
        chart_pnl.set_categories(cats)
        chart_pnl.series[0].graphicalProperties.solidFill = "1F4E79"

        # Win % line series on secondary axis (col 7)
        chart_wr = LineChart()
        wr_data  = Reference(ws, min_col=7, min_row=1, max_row=1 + data_rows)
        chart_wr.add_data(wr_data, titles_from_data=True)
        chart_wr.set_categories(cats)
        chart_wr.y_axis.axId = 200
        chart_wr.y_axis.crosses = "max"
        chart_wr.y_axis.title   = "Win %"
        chart_wr.series[0].graphicalProperties.line.solidFill = "C55A11"
        chart_wr.series[0].graphicalProperties.line.width = 20000
        chart_wr.series[0].marker.symbol = "circle"
        chart_wr.series[0].marker.size   = 6

        chart_pnl += chart_wr
        ws.add_chart(chart_pnl, f"A{chart_row}")


def _stats_row(picks: list[dict]) -> dict:
    """Return a dict of summary stats for a collection of graded picks."""
    res  = [p for p in picks if p.get("result") in ("WIN", "LOSS", "PUSH")]
    wins = sum(1 for p in res if p["result"] == "WIN")
    loss = sum(1 for p in res if p["result"] == "LOSS")
    push = sum(1 for p in res if p["result"] == "PUSH")
    wager = sum(p["bet_amount"] for p in res)
    pnl   = sum(p["pnl"] for p in res)
    wr    = wins / (wins + loss) * 100 if (wins + loss) else 0
    roi   = pnl / wager * 100 if wager else 0
    return {"total": len(picks), "resolved": len(res),
            "w": wins, "l": loss, "p": push,
            "wr": wr, "wager": wager, "pnl": pnl, "roi": roi}


def _write_stats_row(ws, row: int, label: str, s: dict, base_fill: str | None):
    """Write one stats row onto a worksheet."""
    pnl_color = "006100" if s["pnl"] >= 0 else "9C0006"
    vals = [
        label, s["total"], s["resolved"],
        s["w"], s["l"], s["p"],
        s["wr"] / 100, s["wager"], s["pnl"], s["roi"] / 100,
    ]
    for ci, val in enumerate(vals, 1):
        c = ws.cell(row=row, column=ci, value=val)
        c.border    = _thin()
        c.alignment = Alignment(horizontal="center" if ci > 1 else "left")
        if base_fill: c.fill = _hfill(base_fill)
    ws.cell(row=row, column=7).number_format  = '0.0%'
    ws.cell(row=row, column=8).number_format  = '$#,##0.00'
    ws.cell(row=row, column=9).number_format  = '$#,##0.00'
    ws.cell(row=row, column=10).number_format = '0.0%'
    ws.cell(row=row, column=9).font  = Font(bold=True, color=pnl_color)
    ws.cell(row=row, column=10).font = Font(bold=True, color=pnl_color)


def _add_ev_threshold_tab(wb, ev_picks: list[dict]):
    """
    "EV Thresholds" tab — cumulative P&L at each EV minimum.

    Each row answers: "if I only bet picks with EV ≥ X%, what would happen?"
    Columns: Threshold | # Picks | Resolved | W | L | P | Win% | Wagered | P&L | ROI%
    Also adds a sub-section per market type at each threshold.
    """
    ws = wb.create_sheet("EV Thresholds")
    cols = [
        ("Min EV %", 14), ("# Picks", 9), ("Resolved", 9),
        ("W", 5), ("L", 5), ("P", 5), ("Win %", 8),
        ("Wagered", 13), ("P&L", 13), ("ROI %", 9),
    ]
    _hdr(ws, cols, "2F5496")
    ws.column_dimensions["A"].width = 14

    row = 2
    for thresh in EV_THRESHOLDS:
        subset = [p for p in ev_picks if (p["ev_pct"] or 0) >= thresh]
        s      = _stats_row(subset)
        label  = f"≥ {thresh}%"
        base   = "F2F2F2" if row % 2 == 0 else None
        _write_stats_row(ws, row, label, s, base)
        row += 1

    # Blank gap then per-market breakdown at each threshold
    row += 1
    # Section header
    mkt_banner = ws.cell(row=row, column=1,
                         value="Breakdown by Market at each EV threshold")
    mkt_banner.font = Font(bold=True, size=12, color="2F5496")
    row += 1

    mkt_sub_cols = [
        ("Min EV %", 14), ("Market", 10), ("# Picks", 9), ("Resolved", 9),
        ("W", 5), ("L", 5), ("P", 5), ("Win %", 8),
        ("Wagered", 13), ("P&L", 13), ("ROI %", 9),
    ]
    _hdr(ws, mkt_sub_cols, "375623", row=row)
    ws.column_dimensions["B"].width = 10
    row += 1

    markets = sorted({p["market"] for p in ev_picks})
    for thresh in EV_THRESHOLDS:
        for mkt in markets:
            subset = [p for p in ev_picks
                      if (p["ev_pct"] or 0) >= thresh and p["market"] == mkt]
            if not subset:
                continue
            s    = _stats_row(subset)
            base = "F2F2F2" if row % 2 == 0 else None
            pnl_color = "006100" if s["pnl"] >= 0 else "9C0006"
            vals = [
                f"≥ {thresh}%", mkt, s["total"], s["resolved"],
                s["w"], s["l"], s["p"],
                s["wr"] / 100, s["wager"], s["pnl"], s["roi"] / 100,
            ]
            for ci, val in enumerate(vals, 1):
                c = ws.cell(row=row, column=ci, value=val)
                c.border    = _thin()
                c.alignment = Alignment(horizontal="center" if ci > 1 else "left")
                if base: c.fill = _hfill(base)
            ws.cell(row=row, column=8).number_format  = '0.0%'
            ws.cell(row=row, column=9).number_format  = '$#,##0.00'
            ws.cell(row=row, column=10).number_format = '$#,##0.00'
            ws.cell(row=row, column=11).number_format = '0.0%'
            ws.cell(row=row, column=10).font = Font(bold=True, color=pnl_color)
            ws.cell(row=row, column=11).font = Font(bold=True, color=pnl_color)
            row += 1

    ws.freeze_panes = "A2"


def _add_pinnacle_tab(wb, ev_picks: list[dict], play_these_settled: list[dict]):
    """
    "Pinnacle Analysis" tab — compares Pinnacle-anchored vs non-Pinnacle bets.

    Section 1: Overall split  (all EV picks)
    Section 2: By EV threshold for Pinnacle-only vs non-Pinnacle
    Section 3: By market for Pinnacle-only
    Section 4: Play These (backtest-validated filter) for reference
    """
    ws = wb.create_sheet("Pinnacle Analysis")
    ws.column_dimensions["A"].width = 28

    cols = [
        ("Category", 28), ("# Picks", 9), ("Resolved", 9),
        ("W", 5), ("L", 5), ("P", 5), ("Win %", 8),
        ("Wagered", 13), ("P&L", 13), ("ROI %", 9),
    ]

    def _section_hdr(row: int, title: str, color: str):
        c = ws.cell(row=row, column=1, value=title)
        c.font = Font(bold=True, size=12, color=color)
        _hdr(ws, cols, color, row=row + 1)
        return row + 2

    # ── Section 1: Overall ────────────────────────────────────────────────────
    row = _section_hdr(1, "1 — All EV Picks: Pinnacle-Anchored vs Not", "1F4E79")
    pinn_picks = [p for p in ev_picks if p.get("pinnacle")]
    non_pinn   = [p for p in ev_picks if not p.get("pinnacle")]

    for label, subset, base in [
        ("All EV Picks",           ev_picks,   None),
        ("Pinnacle-Anchored",      pinn_picks, "E8F0FB"),
        ("Not Pinnacle-Anchored",  non_pinn,   None),
    ]:
        _write_stats_row(ws, row, label, _stats_row(subset), base)
        row += 1

    row += 1  # gap

    # ── Section 2: Pinnacle vs Non at each EV threshold ───────────────────────
    row = _section_hdr(row, "2 — Pinnacle-Anchored vs Not, by EV Threshold", "375623")
    thresh_cols = [
        ("Min EV %", 14), ("Group", 22), ("# Picks", 9), ("Resolved", 9),
        ("W", 5), ("L", 5), ("P", 5), ("Win %", 8),
        ("Wagered", 13), ("P&L", 13), ("ROI %", 9),
    ]
    _hdr(ws, thresh_cols, "375623", row=row - 1)

    for thresh in EV_THRESHOLDS:
        for label, subset, base in [
            ("Pinnacle",     [p for p in pinn_picks if (p["ev_pct"] or 0) >= thresh], "E8F0FB"),
            ("Non-Pinnacle", [p for p in non_pinn   if (p["ev_pct"] or 0) >= thresh], None),
        ]:
            if not subset:
                continue
            s    = _stats_row(subset)
            pnl_color = "006100" if s["pnl"] >= 0 else "9C0006"
            vals = [
                f"≥ {thresh}%", label, s["total"], s["resolved"],
                s["w"], s["l"], s["p"],
                s["wr"] / 100, s["wager"], s["pnl"], s["roi"] / 100,
            ]
            for ci, val in enumerate(vals, 1):
                c = ws.cell(row=row, column=ci, value=val)
                c.border    = _thin()
                c.alignment = Alignment(horizontal="center" if ci > 1 else "left")
                if base: c.fill = _hfill(base)
            ws.cell(row=row, column=8).number_format  = '0.0%'
            ws.cell(row=row, column=9).number_format  = '$#,##0.00'
            ws.cell(row=row, column=10).number_format = '$#,##0.00'
            ws.cell(row=row, column=11).number_format = '0.0%'
            ws.cell(row=row, column=10).font = Font(bold=True, color=pnl_color)
            ws.cell(row=row, column=11).font = Font(bold=True, color=pnl_color)
            row += 1
        row += 1  # gap between thresholds

    row += 1

    # ── Section 3: Pinnacle by market ─────────────────────────────────────────
    row = _section_hdr(row, "3 — Pinnacle-Anchored Picks by Market", "7B2D8E")
    mkts = sorted({p["market"] for p in pinn_picks})
    for i, mkt in enumerate(mkts):
        subset = [p for p in pinn_picks if p["market"] == mkt]
        base   = "F2F2F2" if i % 2 == 1 else None
        _write_stats_row(ws, row, mkt, _stats_row(subset), base)
        row += 1

    row += 2

    # ── Section 4: Play These reference ───────────────────────────────────────
    if play_these_settled:
        row = _section_hdr(row, "4 — Play These (backtest-validated filter) — for reference", "8B0000")
        pt_all   = play_these_settled
        pt_pinn  = [p for p in pt_all if p.get("pinnacle")]
        pt_npinn = [p for p in pt_all if not p.get("pinnacle")]
        for label, subset, base in [
            ("Play These — All",           pt_all,   None),
            ("Play These — Pinnacle",      pt_pinn,  "E8F0FB"),
            ("Play These — Non-Pinnacle",  pt_npinn, None),
        ]:
            if not subset: continue
            _write_stats_row(ws, row, label, _stats_row(subset), base)
            row += 1

    ws.freeze_panes = "A2"


def _add_props_tab(wb, prop_picks: list[dict]):
    """
    "Player Props" tab in the backtest report.

    Sections:
      1. All Picks detail table (one row per prop)
      2. Summary by prop market (pts / reb / ast / strikeouts / total_bases)
      3. Summary by sport (NBA / MLB)
      4. Non-Pinn market × EV tier — feeds _qualifies_prop() calibration
    """
    ws = wb.create_sheet("Player Props")

    PROP_MARKET_LABELS = {
        "pts":         "Points",
        "reb":         "Rebounds",
        "ast":         "Assists",
        "threes":      "3-Pointers",
        "pra":         "Pts+Reb+Ast",
        "dd":          "Double-Double",
        "strikeouts":  "Strikeouts",
        "total_bases": "Total Bases",
        "hits":        "Hits",
        "rbis":        "RBIs",
        "hr":          "Home Runs",
        "sog":         "Shots on Goal",
        "nhl_pts":     "NHL Points",
        "goal":        "Goalscorer",
    }

    # ── Detail table ────────────────────────────────────────────────────────
    # Close + CLV % trail the original columns (same convention as All
    # Picks) so the positional Result/P&L styling below stays valid and
    # build_training_table's header-driven reader picks them up for the
    # _qualifies_prop gate calibration.
    detail_cols = [
        ("#", 5), ("File Date", 11), ("Game Date", 11), ("Sport", 8),
        ("Game", 30), ("Book", 13), ("Market", 13), ("Player", 22),
        ("Side", 6), ("Line", 7), ("Odds", 8), ("EV %", 7),
        ("Bet Amt", 10), ("Seen", 6), ("Result", 9), ("P&L", 11),
        ("Close", 8), ("CLV %", 9),
    ]
    _hdr(ws, detail_cols, "375623")

    sorted_props = sorted(
        prop_picks,
        key=lambda p: (p.get("game_date") or "00000000", p["sport"], p["game"]),
    )

    for i, p in enumerate(sorted_props):
        row    = i + 2
        result = p.get("result", "NO_RESULT") or "NO_RESULT"
        pnl    = p.get("pnl")
        base   = "F2F2F2" if i % 2 == 1 else None

        odds = p.get("odds")
        odds_str = (f"+{odds}" if odds and odds > 0 else str(odds)) if odds else ""
        gd  = p.get("game_date") or ""
        gd_d = f"{gd[:4]}-{gd[4:6]}-{gd[6:]}" if len(gd) == 8 else gd
        fd  = p.get("file_date")
        fd_d = fd.strftime("%Y-%m-%d") if fd else ""

        side_disp = "Over" if p.get("prop_side") == "over" else "Under"
        mkt_label = PROP_MARKET_LABELS.get(p["market"], p["market"])

        close_am  = p.get("close_odds_american")
        close_str = (f"+{close_am}" if close_am and close_am > 0
                     else str(close_am)) if close_am is not None else ""

        vals = [
            i + 1, fd_d, gd_d, p["sport"].upper(),
            p["game"], p["book"], mkt_label,
            p.get("player", ""), side_disp, p.get("line", ""),
            odds_str, p.get("ev_pct"), p["bet_amount"],
            p.get("times_seen", 1), result, pnl,
            close_str, p.get("clv_pct"),
        ]
        for ci, val in enumerate(vals, 1):
            c = ws.cell(row=row, column=ci, value=val)
            c.border    = _thin()
            c.alignment = Alignment(horizontal="center")
            if base: c.fill = _hfill(base)

        # Result color
        rc = ws.cell(row=row, column=15)
        rc.fill = _hfill(RESULT_FILL.get(result, "EEEEEE"))
        rc.font = Font(bold=True, color=RESULT_FONT_COLOR.get(result, "000000"))

        # P&L color
        pc = ws.cell(row=row, column=16)
        pc.number_format = '$#,##0.00'
        if pnl is not None:
            pc.font = Font(bold=True,
                           color="006100" if pnl > 0 else ("9C0006" if pnl < 0 else "1F4E79"))

        # Bet amount gold
        bc = ws.cell(row=row, column=13)
        bc.fill          = _hfill("FFF2CC")
        bc.font          = Font(bold=True, color="7B4D00")
        bc.number_format = '$#,##0.00'

        # Side color
        sc = ws.cell(row=row, column=9)
        sc.font = Font(bold=True,
                       color="006100" if p.get("prop_side") == "over" else "9C0006")

        # CLV % — green when the line moved your way, red when against
        clv = p.get("clv_pct")
        if clv is not None:
            cv = ws.cell(row=row, column=18)
            cv.number_format = '+0.00;-0.00;0.00'
            cv.font = Font(bold=True, color="006100" if clv > 0 else
                           ("9C0006" if clv < 0 else "1F4E79"))

    ws.freeze_panes = "A2"
    if sorted_props:
        ws.auto_filter.ref = (
            f"A1:{get_column_letter(len(detail_cols))}{len(sorted_props) + 1}")

    # ── Summary section (below detail table) ────────────────────────────────
    gap_row  = len(sorted_props) + 3
    sum_cols = [
        ("Category", 20), ("Picks", 8), ("Resolved", 9),
        ("W", 5), ("L", 5), ("P", 5), ("Win %", 8),
        ("Wagered", 13), ("P&L", 13), ("ROI %", 9), ("Avg CLV %", 11),
    ]

    def _prop_summary_section(title: str, groups: dict, start_row: int, color: str) -> int:
        c = ws.cell(row=start_row, column=1, value=title)
        c.font = Font(bold=True, size=12, color=color)
        _hdr(ws, sum_cols, color, row=start_row + 1)
        r = start_row + 2
        for label in sorted(groups.keys()):
            gp  = groups[label]
            res = [p for p in gp if p["result"] in ("WIN", "LOSS", "PUSH")]
            gw  = sum(1 for p in res if p["result"] == "WIN")
            gl  = sum(1 for p in res if p["result"] == "LOSS")
            gp_ = sum(1 for p in res if p["result"] == "PUSH")
            gwr = gw / (gw + gl) * 100 if (gw + gl) else 0
            gwa = sum(p["bet_amount"] for p in res)
            gpn = sum(p["pnl"] for p in res)
            gro = gpn / gwa * 100 if gwa else 0
            # CLV across every pick in the group with a captured close —
            # graded or not, CLV is valid the moment the game starts.
            clvs = [p["clv_pct"] for p in gp
                    if p.get("clv_pct") is not None]
            gclv = sum(clvs) / len(clvs) if clvs else None
            clv_disp = (f"{gclv:+.2f}% ({len(clvs)})"
                        if gclv is not None else "—")
            pnl_color = "006100" if gpn >= 0 else "9C0006"
            base = "F2F2F2" if r % 2 == 0 else None
            for ci, val in enumerate([
                label, len(gp), len(res), gw, gl, gp_,
                f"{gwr:.1f}%", gwa, gpn, f"{gro:.1f}%", clv_disp,
            ], 1):
                cell = ws.cell(row=r, column=ci, value=val)
                cell.border    = _thin()
                cell.alignment = Alignment(horizontal="center" if ci > 1 else "left")
                if base: cell.fill = _hfill(base)
            ws.cell(row=r, column=8).number_format = '$#,##0.00'
            ws.cell(row=r, column=9).number_format = '$#,##0.00'
            ws.cell(row=r, column=9).font  = Font(bold=True, color=pnl_color)
            ws.cell(row=r, column=10).font = Font(bold=True, color=pnl_color)
            if gclv is not None:
                ws.cell(row=r, column=11).font = Font(
                    bold=True, color="006100" if gclv > 0 else
                    ("9C0006" if gclv < 0 else "1F4E79"))
            r += 1
        return r + 1

    by_market: dict[str, list] = defaultdict(list)
    by_sport:  dict[str, list] = defaultdict(list)
    for p in prop_picks:
        by_market[PROP_MARKET_LABELS.get(p["market"], p["market"])].append(p)
        by_sport[p["sport"].upper()].append(p)

    next_row = _prop_summary_section(
        "By Prop Market", by_market, gap_row, "375623")
    next_row = _prop_summary_section(
        "By Sport", by_sport, next_row + 1, "1F4E79")

    # Non-Pinn market × EV tier — _qualifies_prop() calibration view.
    # Pinn-anchored props auto-pass at ev >= 1.0 so the threshold decision
    # is purely about the non-Pinn band.
    ev_buckets = [
        ("1–2%", lambda v: 1.0 <= v < 2.0),
        ("2–3%", lambda v: 2.0 <= v < 3.0),
        ("3–5%", lambda v: 3.0 <= v < 5.0),
        ("5%+",  lambda v: v >= 5.0),
    ]
    by_market_tier: dict[str, list] = defaultdict(list)
    for p in prop_picks:
        if p.get("pinnacle_anchored"):
            continue
        ev = p.get("ev_pct") or 0
        mkt_label = PROP_MARKET_LABELS.get(p["market"], p["market"])
        for blabel, bfilt in ev_buckets:
            if bfilt(ev):
                by_market_tier[f"{mkt_label} {blabel}"].append(p)
                break

    if by_market_tier:
        _prop_summary_section(
            "Non-Pinn Market × EV Tier (calibration view)",
            by_market_tier, next_row + 1, "7B4D00")


def _add_promo_tab(wb, promo_picks: list[dict]):
    """
    "Promo P&L" tab — tracks P&L from promo/boost picks completely separately.

    Layout:
      Section 1: Detail table — one row per promo pick
      Section 2: Summary by Book
      Section 3: Summary by Sport
      Section 4: Summary by Market
    """
    ws = wb.create_sheet("Promo P&L")

    # ── Detail table ────────────────────────────────────────────────────────────
    detail_cols = [
        ("#",          5),  ("File Date",  11), ("Game",       34),
        ("Book",      14),  ("Market",      9), ("Pick",       22),
        ("Line",       7),  ("Orig Odds",  10), ("Boost Odds", 11),
        ("Orig EV %",  9),  ("Boost EV %", 10), ("EV Gain",    9),
        ("Bet Amt",   10),  ("Free Bet",    9), ("Result",      9),
        ("P&L",       11),
    ]
    _hdr(ws, detail_cols, "6A0572")   # purple header to distinguish from other tabs

    sorted_promos = sorted(
        promo_picks,
        key=lambda p: (p.get("game_date") or "00000000", p.get("book", ""), p["game"]),
    )

    for i, p in enumerate(sorted_promos):
        row    = i + 2
        result = p.get("result", "NO_RESULT") or "NO_RESULT"
        pnl    = p.get("pnl")
        base   = "F2F2F2" if i % 2 == 1 else None

        fd   = p.get("file_date")
        fd_d = fd.strftime("%Y-%m-%d") if fd else ""

        orig_odds  = p.get("orig_odds")
        boost_str  = p.get("boost_odds_str", "")
        orig_str   = (f"+{orig_odds}" if orig_odds and orig_odds > 0
                      else str(orig_odds)) if orig_odds else ""

        vals = [
            i + 1,
            fd_d,
            p["game"],
            p["book"],
            p["market"],
            p["pick"],
            p["line"] if p["line"] is not None else "",
            orig_str,
            boost_str,
            p.get("orig_ev", 0.0),
            p.get("boost_ev", 0.0),
            p.get("ev_gain", 0.0),
            p["bet_amount"],
            "✓" if p.get("is_free_bet") else "",
            result,
            pnl,
        ]

        for ci, val in enumerate(vals, 1):
            c = ws.cell(row=row, column=ci, value=val)
            c.border    = _thin()
            c.alignment = Alignment(horizontal="center")
            if base:
                c.fill = _hfill(base)

        # Result cell coloring
        rc = ws.cell(row=row, column=15)
        rc.fill = _hfill(RESULT_FILL.get(result, "EEEEEE"))
        rc.font = Font(bold=True, color=RESULT_FONT_COLOR.get(result, "000000"))

        # P&L cell
        pc = ws.cell(row=row, column=16)
        pc.number_format = '$#,##0.00'
        if pnl is not None:
            pc.font = Font(bold=True,
                           color="006100" if pnl > 0 else
                           ("9C0006" if pnl < 0 else "1F4E79"))

        # Bet amount gold
        bc = ws.cell(row=row, column=13)
        bc.fill          = _hfill("FFF2CC")
        bc.font          = Font(bold=True, color="7B4D00")
        bc.number_format = '$#,##0.00'

        # Numeric EV % formatting
        for col_idx in (10, 11, 12):
            ws.cell(row=row, column=col_idx).number_format = '0.0'

        # Promo tooltip: full promo description as cell comment via a narrow hidden col
        # (Store it in a wide col A note — openpyxl doesn't support comments in r/o,
        #  so just put the promo desc in a tooltip-style cell at far right instead)

    ws.freeze_panes = "A2"
    if sorted_promos:
        ws.auto_filter.ref = (
            f"A1:{get_column_letter(len(detail_cols))}{len(sorted_promos) + 1}")

    # Add a 17th column for the promo description (tooltip-style)
    ws.cell(row=1, column=17, value="Promo Description").font = Font(
        bold=True, color="FFFFFF", size=11)
    ws.cell(row=1, column=17).fill      = _hfill("6A0572")
    ws.cell(row=1, column=17).alignment = Alignment(horizontal="center", wrap_text=True)
    ws.cell(row=1, column=17).border    = _thin()
    ws.column_dimensions[get_column_letter(17)].width = 48
    for i, p in enumerate(sorted_promos):
        c = ws.cell(row=i + 2, column=17, value=p.get("promo_desc", ""))
        c.border    = _thin()
        c.alignment = Alignment(horizontal="left", wrap_text=True)
        if i % 2 == 1:
            c.fill = _hfill("F2F2F2")

    # ── Summary sections ────────────────────────────────────────────────────────
    gap_row  = len(sorted_promos) + 3
    sum_cols = [
        ("Category",  22), ("Picks",  8), ("Resolved",  9),
        ("W",          5), ("L",      5), ("P",         5), ("Win %",   8),
        ("Wagered",   13), ("P&L",   13), ("ROI %",     9),
    ]

    def _promo_summary_section(title: str, groups: dict,
                                start_row: int, color: str) -> int:
        c = ws.cell(row=start_row, column=1, value=title)
        c.font = Font(bold=True, size=12, color=color)
        _hdr(ws, sum_cols, color, row=start_row + 1)
        r = start_row + 2
        for label in sorted(groups.keys(), key=str.lower):
            gp  = groups[label]
            res = [p for p in gp if p.get("result") in ("WIN", "LOSS", "PUSH")]
            gw  = sum(1 for p in res if p["result"] == "WIN")
            gl  = sum(1 for p in res if p["result"] == "LOSS")
            gp_ = sum(1 for p in res if p["result"] == "PUSH")
            gwr = gw / (gw + gl) * 100 if (gw + gl) else 0
            gwa = sum(p["bet_amount"] for p in res)
            gpn = sum(p["pnl"] for p in res)
            gro = gpn / gwa * 100 if gwa else 0
            pnl_color = "006100" if gpn >= 0 else "9C0006"
            base = "F2F2F2" if r % 2 == 0 else None
            for ci, val in enumerate([
                label, len(gp), len(res), gw, gl, gp_,
                gwr / 100, gwa, gpn, gro / 100,
            ], 1):
                cell = ws.cell(row=r, column=ci, value=val)
                cell.border    = _thin()
                cell.alignment = Alignment(horizontal="center" if ci > 1 else "left")
                if base:
                    cell.fill = _hfill(base)
            ws.cell(row=r, column=7).number_format  = '0.0%'
            ws.cell(row=r, column=8).number_format  = '$#,##0.00'
            ws.cell(row=r, column=9).number_format  = '$#,##0.00'
            ws.cell(row=r, column=10).number_format = '0.0%'
            ws.cell(row=r, column=9).font  = Font(bold=True, color=pnl_color)
            ws.cell(row=r, column=10).font = Font(bold=True, color=pnl_color)
            r += 1

        # Totals row
        tot_grp   = {k: v for k, v in groups.items()}   # reuse per-section groups
        sec_picks = [p for grp in tot_grp.values() for p in grp]
        sec_res   = [p for p in sec_picks if p.get("result") in ("WIN","LOSS","PUSH")]
        tw  = sum(1 for p in sec_res if p["result"] == "WIN")
        tl  = sum(1 for p in sec_res if p["result"] == "LOSS")
        tp_ = sum(1 for p in sec_res if p["result"] == "PUSH")
        twa = sum(p["bet_amount"] for p in sec_res)
        tpn = sum(p["pnl"] for p in sec_res)
        twr = tw / (tw + tl) * 100 if (tw + tl) else 0
        tro = tpn / twa * 100 if twa else 0
        pnl_color = "006100" if tpn >= 0 else "9C0006"
        for ci, val in enumerate([
            "TOTAL", len(sec_picks), len(sec_res),
            tw, tl, tp_, twr / 100, twa, tpn, tro / 100,
        ], 1):
            cell = ws.cell(row=r, column=ci, value=val)
            cell.font      = Font(bold=True, size=11)
            cell.border    = _thin()
            cell.alignment = Alignment(horizontal="center" if ci > 1 else "left")
        ws.cell(row=r, column=7).number_format  = '0.0%'
        ws.cell(row=r, column=8).number_format  = '$#,##0.00'
        ws.cell(row=r, column=9).number_format  = '$#,##0.00'
        ws.cell(row=r, column=10).number_format = '0.0%'
        ws.cell(row=r, column=9).font  = Font(bold=True, size=11, color=pnl_color)
        ws.cell(row=r, column=10).font = Font(bold=True, size=11, color=pnl_color)
        return r + 2

    by_book:   dict[str, list] = defaultdict(list)
    by_sport:  dict[str, list] = defaultdict(list)
    by_market: dict[str, list] = defaultdict(list)
    for p in promo_picks:
        by_book[p["book"] or "Unknown"].append(p)
        by_sport[(p["sport"] or "unknown").upper()].append(p)
        by_market[p["market"]].append(p)

    next_row = _promo_summary_section("By Book",   by_book,   gap_row,       "6A0572")
    next_row = _promo_summary_section("By Sport",  by_sport,  next_row + 1,  "1F4E79")
    _promo_summary_section("By Market", by_market, next_row + 1, "375623")


def _add_parlay_sim_tab(wb, sim: dict):
    """
    "Parlay Sim" tab — hypothetical parlays built from each day's graded
    picks (no real bets were placed). Summary grid across leg counts /
    boost levels / SGP price haircuts, then per-parlay detail for the
    headline configs.
    """
    ws = wb.create_sheet("Parlay Sim")
    hdr_font = Font(bold=True, color="FFFFFF")
    hdr_fill = PatternFill("solid", fgColor="4472C4")

    ws.cell(row=1, column=1,
            value="PARLAY SIMULATION — hypothetical, $%.0f flat stake. "
                  "Cross-game payouts are exact (product of leg prices); "
                  "SGP payouts are the naive product minus the shown "
                  "haircut, since the book's correlation-adjusted quote "
                  "isn't recoverable." % _PARLAY_SIM_UNIT
            ).font = Font(bold=True, italic=True)

    headers = ["Type", "Legs", "Boost %", "SGP Haircut %", "Parlays",
               "Wins", "Pushes", "Hit %", "Staked $", "P&L $", "ROI %"]
    for c, h in enumerate(headers, 1):
        cell = ws.cell(row=3, column=c, value=h)
        cell.font, cell.fill = hdr_font, hdr_fill

    r = 4
    for row in sim.get("summary", []):
        decided = row["parlays"] - row["pushes"]
        hit = row["wins"] / decided * 100 if decided else 0.0
        vals = [row["kind"].upper(), row["n_legs"], row["boost_pct"],
                row["haircut_pct"] if row["kind"] == "sgp" else "—",
                row["parlays"], row["wins"], row["pushes"],
                round(hit, 1), row["staked"], row["pnl"], row["roi_pct"]]
        for c, v in enumerate(vals, 1):
            ws.cell(row=r, column=c, value=v)
        r += 1

    # Per-parlay detail for headline configs
    for label, rows in sim.get("detail", {}).items():
        r += 2
        ws.cell(row=r, column=1, value=f"Detail — {label}").font = Font(bold=True)
        r += 1
        det_headers = ["Date", "Book", "Legs", "Naive Odds", "Priced Odds",
                       "Exp EV %", "Result", "P&L $"]
        for c, h in enumerate(det_headers, 1):
            cell = ws.cell(row=r, column=c, value=h)
            cell.font, cell.fill = hdr_font, hdr_fill
        r += 1
        for p in rows:
            legs_txt = "; ".join(
                f"{l['game']} {l['market']} {l['pick']}"
                + (f" {l['line']}" if l.get("line") not in (None, "") else "")
                for l in p["legs"])
            vals = [p["date"], p["book"], legs_txt,
                    round(p["naive_dec"], 2), round(p["priced_dec"], 2),
                    p["exp_ev"], p["result"], p["pnl"]]
            for c, v in enumerate(vals, 1):
                ws.cell(row=r, column=c, value=v)
            r += 1

    ws.column_dimensions["A"].width = 10
    ws.column_dimensions["B"].width = 12
    ws.column_dimensions["C"].width = 70
    for col in "DEFGHIJK":
        ws.column_dimensions[col].width = 12


def write_backtest_excel(all_picks: list[dict], settled: list[dict],
                         output_path: str, start_dt: date, end_dt: date,
                         play_these_picks: list[dict] | None = None,
                         prop_picks: list[dict] | None = None,
                         promo_picks: list[dict] | None = None,
                         parlay_sim: dict | None = None):
    """
    Write the full backtest Excel report.

    all_picks        — ALL +EV picks (from EV Edges tab). Drives all main tabs.
    settled          — Settled subset of all_picks (WIN/LOSS/PUSH).
    play_these_picks — Play These filtered picks. Written to PT breakdown tabs
                       and a comparison block in the Summary tab.
    prop_picks       — Player props picks for the Props tab.
    promo_picks      — Promo/boost picks for the dedicated "Promo P&L" tab.
    """
    wb = openpyxl.Workbook()

    total_bet = sum(p["bet_amount"] for p in settled)
    total_pnl = sum(p["pnl"]       for p in settled)
    roi       = total_pnl / total_bet * 100 if total_bet else 0
    wins      = sum(1 for p in settled if p["result"] == "WIN")
    losses    = sum(1 for p in settled if p["result"] == "LOSS")
    pushes    = sum(1 for p in settled if p["result"] == "PUSH")
    win_rate  = wins / (wins + losses) * 100 if (wins + losses) else 0
    pending   = sum(1 for p in all_picks if p.get("result") == "PENDING")
    manual    = sum(1 for p in all_picks
                    if p.get("result") in ("MANUAL", "NO_RESULT", "UNKNOWN"))

    # ── Summary ────────────────────────────────────────────────────────────────
    ws = wb.active
    ws.title = "Summary"
    ws.column_dimensions["A"].width = 32
    ws.column_dimensions["B"].width = 20

    ws["A1"] = "ODDS COMPARE — BACKTEST REPORT"
    ws["A1"].font = Font(bold=True, size=16, color="1F4E79")
    ws["A2"] = f"Date range: {start_dt}  →  {end_dt}"
    ws["A2"].font = Font(italic=True, size=11, color="444444")
    ws["A3"] = f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    ws["A3"].font = Font(italic=True, size=10, color="888888")
    ws["A4"] = "Source: All +EV picks (EV Edges tab)  |  Play These comparison shown below"
    ws["A4"].font = Font(italic=True, size=9, color="666666")

    def _stat(r, label, value, fmt=None, color=None):
        ws.cell(row=r, column=1, value=label).font = Font(bold=True, size=12)
        c = ws.cell(row=r, column=2, value=value)
        c.font = Font(size=12, color=color or "000000")
        if fmt: c.number_format = fmt

    r = 6
    _stat(r,    "Total Unique Picks",           len(all_picks))
    _stat(r+1,  "Settled (W / L / P)",          len(settled))
    _stat(r+2,  "Pending (future games)",        pending,   color="9C5700")
    _stat(r+3,  "Manual / Unresolved",           manual,    color="888888")
    _stat(r+4,  "Wins",                          wins,      color="006100")
    _stat(r+5,  "Losses",                        losses,    color="9C0006")
    _stat(r+6,  "Pushes",                        pushes,    color="1F4E79")
    _stat(r+7,  "Win Rate (excl. pushes)",        f"{win_rate:.1f}%",
          color="006100" if win_rate >= 53 else "9C5700")
    _stat(r+8,  "Total Wagered",                 total_bet, fmt='$#,##0.00')
    _stat(r+9,  "Total P&L",                     total_pnl, fmt='$#,##0.00',
          color="006100" if total_pnl >= 0 else "9C0006")
    _stat(r+10, "ROI",                           f"{roi:.1f}%",
          color="006100" if roi >= 0 else "9C0006")

    # Overall CLV — averaged across all settled picks where the close
    # was actually captured. Positive = your selections systematically
    # got better-than-close odds, which is the gold-standard +EV signal
    # (converges in dozens of bets vs. hundreds for W/L).
    clv_settled = [p for p in settled if p.get("clv_pct") is not None]
    if clv_settled:
        avg_clv = sum(p["clv_pct"] for p in clv_settled) / len(clv_settled)
        _stat(r+11, "Avg CLV %",
              f"{avg_clv:+.2f}%  ({len(clv_settled)}/{len(settled)} captured)",
              color="006100" if avg_clv >= 0 else "9C0006")

    # Inline sport summary at r+13
    ws.cell(row=r+13, column=1, value="By Sport:").font = Font(bold=True, size=12)
    sport_grps: dict[str, list] = defaultdict(list)
    for p in settled:
        sport_grps[p["sport"].upper()].append(p)
    sr = r + 14
    for sp in sorted(sport_grps, key=lambda s: sum(p["pnl"] for p in sport_grps[s]), reverse=True):
        sg = sport_grps[sp]
        sw = sum(1 for p in sg if p["result"] == "WIN")
        sl = sum(1 for p in sg if p["result"] == "LOSS")
        sp_pnl = sum(p["pnl"] for p in sg)
        sp_bet = sum(p["bet_amount"] for p in sg)
        sp_roi = sp_pnl / sp_bet * 100 if sp_bet else 0
        wr_str = f"{sw/(sw+sl)*100:.0f}%" if (sw+sl) else "—"
        # Avg CLV — across picks where the close was actually captured.
        # Reported as `(captured_count / total_count)` so you can see how
        # much of the sample is uncapturable (AN-fallback events).
        clv_picks = [p for p in sg if p.get("clv_pct") is not None]
        if clv_picks:
            avg_clv = sum(p["clv_pct"] for p in clv_picks) / len(clv_picks)
            clv_str = f"  CLV {avg_clv:+.2f}% ({len(clv_picks)}/{len(sg)})"
        else:
            clv_str = ""
        ws.cell(row=sr, column=1, value=sp).font = Font(bold=True)
        ws.cell(row=sr, column=2,
                value=(f"W{sw}/L{sl}  P&L ${sp_pnl:+.2f}  "
                       f"ROI {sp_roi:.1f}%  WR {wr_str}{clv_str}")
                ).font = Font(color="006100" if sp_pnl >= 0 else "9C0006")
        sr += 1

    # ── Play These comparison block ───────────────────────────────────────────
    if play_these_picks:
        pt_settled_s = [p for p in play_these_picks
                        if p.get("result") in ("WIN", "LOSS", "PUSH")]
        if pt_settled_s:
            pt_w   = sum(1 for p in pt_settled_s if p["result"] == "WIN")
            pt_l   = sum(1 for p in pt_settled_s if p["result"] == "LOSS")
            pt_bet = sum(p["bet_amount"] for p in pt_settled_s)
            pt_pnl = sum(p["pnl"]       for p in pt_settled_s)
            pt_wr  = pt_w / (pt_w + pt_l) * 100 if (pt_w + pt_l) else 0
            pt_roi = pt_pnl / pt_bet * 100 if pt_bet else 0
            pt_pending = sum(1 for p in play_these_picks
                             if p.get("result") == "PENDING")
            pt_manual  = sum(1 for p in play_these_picks
                             if p.get("result") in ("MANUAL", "NO_RESULT", "UNKNOWN"))

            hdr_row = sr + 1
            hdr_c = ws.cell(row=hdr_row, column=1,
                            value="▶ PLAY THESE (Filtered Picks)")
            hdr_c.font = Font(bold=True, size=13, color="FFFFFF")
            hdr_c.fill = PatternFill(start_color="8B0000", end_color="8B0000",
                                     fill_type="solid")
            hdr_c.alignment = Alignment(horizontal="left")
            ws.merge_cells(start_row=hdr_row, start_column=1,
                           end_row=hdr_row, end_column=2)

            def _pt_stat(row_, lbl, val, fmt=None, color=None):
                ws.cell(row=row_, column=1, value=lbl).font = Font(bold=True, size=11)
                c = ws.cell(row=row_, column=2, value=val)
                c.font = Font(size=11, color=color or "000000")
                if fmt: c.number_format = fmt

            pr = hdr_row + 1
            _pt_stat(pr,    "Total Unique Picks",     len(play_these_picks))
            _pt_stat(pr+1,  "Settled (W / L / P)",    len(pt_settled_s))
            _pt_stat(pr+2,  "Pending",                 pt_pending,  color="9C5700")
            _pt_stat(pr+3,  "Manual / Unresolved",     pt_manual,   color="888888")
            _pt_stat(pr+4,  "Wins",                    pt_w,        color="006100")
            _pt_stat(pr+5,  "Losses",                  pt_l,        color="9C0006")
            _pt_stat(pr+6,  "Win Rate (excl. pushes)", f"{pt_wr:.1f}%",
                     color="006100" if pt_wr >= 53 else "9C5700")
            _pt_stat(pr+7,  "Total Wagered",           pt_bet,      fmt='$#,##0.00')
            _pt_stat(pr+8,  "Total P&L",               pt_pnl,      fmt='$#,##0.00',
                     color="006100" if pt_pnl >= 0 else "9C0006")
            _pt_stat(pr+9,  "ROI",                     f"{pt_roi:.1f}%",
                     color="006100" if pt_roi >= 0 else "9C0006")

            ws.cell(row=pr+11, column=1,
                    value="By Sport (Play These):").font = Font(bold=True, size=11)
            pt_sport_grps: dict[str, list] = defaultdict(list)
            for p in pt_settled_s:
                pt_sport_grps[p["sport"].upper()].append(p)
            ptr = pr + 12
            for sp in sorted(pt_sport_grps,
                             key=lambda s: sum(p["pnl"] for p in pt_sport_grps[s]),
                             reverse=True):
                sg   = pt_sport_grps[sp]
                sw_  = sum(1 for p in sg if p["result"] == "WIN")
                sl_  = sum(1 for p in sg if p["result"] == "LOSS")
                sp_pnl = sum(p["pnl"]       for p in sg)
                sp_bet = sum(p["bet_amount"] for p in sg)
                sp_roi = sp_pnl / sp_bet * 100 if sp_bet else 0
                wr_str = f"{sw_/(sw_+sl_)*100:.0f}%" if (sw_+sl_) else "—"
                ws.cell(row=ptr, column=1, value=sp).font = Font(bold=True)
                ws.cell(row=ptr, column=2,
                        value=f"W{sw_}/L{sl_}  P&L ${sp_pnl:+.2f}"
                              f"  ROI {sp_roi:.1f}%  WR {wr_str}"
                        ).font = Font(color="006100" if sp_pnl >= 0 else "9C0006")
                ptr += 1
            sr = ptr

    # ── Props summary on Summary tab ──────────────────────────────────────────
    if prop_picks:
        prop_settled_sum = [p for p in prop_picks
                            if p.get("result") in ("WIN", "LOSS", "PUSH")]
        if prop_settled_sum:
            pr_pnl = sum(p["pnl"] for p in prop_settled_sum)
            pr_bet = sum(p["bet_amount"] for p in prop_settled_sum)
            pr_w   = sum(1 for p in prop_settled_sum if p["result"] == "WIN")
            pr_l   = sum(1 for p in prop_settled_sum if p["result"] == "LOSS")
            pr_wr  = pr_w / (pr_w + pr_l) * 100 if (pr_w + pr_l) else 0
            pr_roi = pr_pnl / pr_bet * 100 if pr_bet else 0

            ws.cell(row=sr + 1, column=1,
                    value="Player Props:").font = Font(bold=True, size=12)
            ws.cell(row=sr + 2, column=1,
                    value=f"  Settled: {len(prop_settled_sum)}  "
                          f"(W{pr_w}/L{pr_l})  "
                          f"WR {pr_wr:.0f}%  "
                          f"P&L ${pr_pnl:+.2f}  ROI {pr_roi:.1f}%"
                    ).font = Font(size=11,
                                  color="006100" if pr_pnl >= 0 else "9C0006")
            by_mkt: dict = defaultdict(list)
            for p in prop_settled_sum:
                by_mkt[p["market"]].append(p)
            _mkt_labels = {
                "pts": "Points", "reb": "Rebounds", "ast": "Assists",
                "strikeouts": "Strikeouts", "total_bases": "Total Bases",
            }
            msr = sr + 3
            for mkt in sorted(by_mkt):
                mp = by_mkt[mkt]
                mw = sum(1 for p in mp if p["result"] == "WIN")
                ml = sum(1 for p in mp if p["result"] == "LOSS")
                mb = sum(p["bet_amount"] for p in mp)
                mn = sum(p["pnl"] for p in mp)
                mr = mn / mb * 100 if mb else 0
                lbl = _mkt_labels.get(mkt, mkt)
                ws.cell(row=msr, column=1, value=f"  {lbl}").font = Font(bold=True)
                ws.cell(row=msr, column=2,
                        value=f"W{mw}/L{ml}  P&L ${mn:+.2f}  ROI {mr:.1f}%"
                        ).font = Font(color="006100" if mn >= 0 else "9C0006")
                msr += 1

    # ── All Picks ──────────────────────────────────────────────────────────────
    # Close + CLV % live at the end so existing column-index code (Result=16,
    # P&L=17, Bet Amt=13) is unaffected. CLV captures: see closing_lines.py;
    # blank when the close wasn't captured (AN-fallback, future game, etc.)
    ws_all = wb.create_sheet("All Picks")
    all_cols = [
        ("#", 5), ("File Date", 11), ("Game Date", 11), ("Sport", 8),
        ("Game", 30), ("Book", 13), ("Market", 9), ("Pick", 22),
        ("Line", 7), ("Odds", 8), ("EV %", 7), ("Confidence", 12),
        ("Bet Amt", 11), ("Seen", 6), ("Persistent", 10),
        ("Result", 9), ("P&L", 11),
        ("Close", 8), ("CLV %", 9),
    ]
    _hdr(ws_all, all_cols, "2F5496")

    sorted_all = sorted(
        all_picks,
        key=lambda p: (p.get("game_date") or "00000000", p["sport"], p["game"]),
    )

    for i, p in enumerate(sorted_all):
        row    = i + 2
        result = p.get("result", "NO_RESULT") or "NO_RESULT"
        pnl    = p.get("pnl")
        base   = "F2F2F2" if i % 2 == 1 else None

        odds = p.get("odds")
        odds_str = (f"+{odds}" if odds and odds > 0 else str(odds)) if odds else ""

        # Close odds — same +/- sign convention as the open odds column
        close_am = p.get("close_odds_american")
        close_str = (f"+{close_am}" if close_am and close_am > 0
                     else str(close_am)) if close_am is not None else ""

        gd = p.get("game_date") or ""
        gd_display = f"{gd[:4]}-{gd[4:6]}-{gd[6:]}" if len(gd) == 8 else gd
        fd = p.get("file_date")
        fd_display = fd.strftime("%Y-%m-%d") if fd else ""

        vals = [
            i + 1, fd_display, gd_display, p["sport"].upper(),
            p["game"], p["book"], p["market"], p["pick"],
            p["line"] if p["line"] is not None else "",
            odds_str, p.get("ev_pct"), p.get("confidence"),
            p["bet_amount"],
            p.get("times_seen", 1),
            "✓" if p.get("persistent") else "",
            result, pnl,
            close_str, p.get("clv_pct"),
        ]

        for ci, val in enumerate(vals, 1):
            c = ws_all.cell(row=row, column=ci, value=val)
            c.border    = _thin()
            c.alignment = Alignment(horizontal="center")
            if base: c.fill = _hfill(base)

        # Result cell
        rc = ws_all.cell(row=row, column=16)
        rc.fill = _hfill(RESULT_FILL.get(result, "EEEEEE"))
        rc.font = Font(bold=True, color=RESULT_FONT_COLOR.get(result, "000000"))

        # P&L cell
        pc = ws_all.cell(row=row, column=17)
        pc.number_format = '$#,##0.00'
        if pnl is not None:
            pc.font = Font(bold=True, color="006100" if pnl > 0 else
                           ("9C0006" if pnl < 0 else "1F4E79"))

        # Bet Amount — gold
        bc = ws_all.cell(row=row, column=13)
        bc.fill          = _hfill("FFF2CC")
        bc.font          = Font(bold=True, color="7B4D00")
        bc.number_format = '$#,##0.00'

        if p.get("ev_pct") is not None:
            ws_all.cell(row=row, column=11).number_format = '0.0'

        # CLV % cell — green for positive (line moved your way), red for
        # negative (line moved against you), neutral when uncaptured
        clv = p.get("clv_pct")
        if clv is not None:
            cv = ws_all.cell(row=row, column=19)
            cv.number_format = '+0.00;-0.00;0.00'
            cv.font = Font(bold=True, color="006100" if clv > 0 else
                            ("9C0006" if clv < 0 else "1F4E79"))

    ws_all.freeze_panes = "A2"
    if sorted_all:
        ws_all.auto_filter.ref = (
            f"A1:{get_column_letter(len(all_cols))}{len(sorted_all)+1}")

    # ── Breakdown tabs — ALL +EV picks ────────────────────────────────────────
    # (Confidence column not meaningful here — EV Edges tab doesn't store it;
    #  see PT By Confidence tab for the Play These confidence breakdown)
    _breakdown_tab(wb, "By Sport",      all_picks,
                   lambda p: p["sport"].upper(),     "Sport",   "1F4E79")
    _breakdown_tab(wb, "By Market",     all_picks,
                   lambda p: p["market"],            "Market",  "375623")
    _breakdown_tab(wb, "By Book",       all_picks,
                   lambda p: p["book"],              "Book",    "8B0000")
    _breakdown_tab(wb, "By EV Tier",    all_picks,
                   lambda p: (
                       "EV >= 5%" if (p["ev_pct"] or 0) >= 5 else
                       "EV 3–5%"  if (p["ev_pct"] or 0) >= 3 else
                       "EV 1–3%"  if (p["ev_pct"] or 0) >= 1 else
                       "EV < 1%"
                   ),                               "EV Tier",  "2F5496")
    _breakdown_tab(wb, "By Durability", all_picks,
                   lambda p: (
                       "Persistent (3+)" if p.get("times_seen", 1) >= 3 else
                       "Seen Twice"      if p.get("times_seen", 1) == 2 else
                       "One-Time"
                   ),                               "Durability", "375623")

    # ── Play These breakdown tabs ──────────────────────────────────────────────
    if play_these_picks:
        _breakdown_tab(wb, "PT By Sport",
                       play_these_picks,
                       lambda p: p["sport"].upper(),     "Sport",      "8B0000")
        _breakdown_tab(wb, "PT By Market",
                       play_these_picks,
                       lambda p: p["market"],            "Market",     "7B2D8E")
        _breakdown_tab(wb, "PT By Confidence",
                       play_these_picks,
                       lambda p: p.get("confidence") or "?", "Confidence", "375623")
        _breakdown_tab(wb, "PT By Book",
                       play_these_picks,
                       lambda p: p["book"],              "Book",       "1B4332")
        _breakdown_tab(wb, "PT By EV Tier",
                       play_these_picks,
                       lambda p: (
                           "EV >= 5%" if (p["ev_pct"] or 0) >= 5 else
                           "EV 3–5%"  if (p["ev_pct"] or 0) >= 3 else
                           "EV 1–3%"  if (p["ev_pct"] or 0) >= 1 else
                           "EV < 1%"
                       ),                               "EV Tier",    "8B0000")

    # ── Equity Curve ───────────────────────────────────────────────────────────
    ws_eq = wb.create_sheet("Equity Curve")
    eq_cols = [
        ("Game Date", 12), ("Sport", 8), ("Game", 30), ("Market", 9),
        ("Pick", 22), ("Odds", 8), ("Bet Amt", 11),
        ("Result", 9), ("P&L", 11), ("Running P&L", 14),
    ]
    _hdr(ws_eq, eq_cols, "2F5496")

    equity_picks = sorted(
        [p for p in all_picks if p.get("pnl") is not None and p.get("game_date")],
        key=lambda p: (p["game_date"], p["sport"]),
    )

    cum_pnl = 0.0
    for i, p in enumerate(equity_picks):
        cum_pnl += p["pnl"]
        row     = i + 2
        result  = p["result"]
        base    = "F2F2F2" if i % 2 == 1 else None

        gd = p["game_date"]
        gd_disp = f"{gd[:4]}-{gd[4:6]}-{gd[6:]}" if len(gd) == 8 else gd
        odds = p.get("odds")
        odds_str = (f"+{odds}" if odds and odds > 0 else str(odds)) if odds else ""

        vals = [
            gd_disp, p["sport"].upper(), p["game"],
            p["market"], p["pick"], odds_str,
            p["bet_amount"], result, p["pnl"], cum_pnl,
        ]
        for ci, val in enumerate(vals, 1):
            c = ws_eq.cell(row=row, column=ci, value=val)
            c.border    = _thin()
            c.alignment = Alignment(horizontal="center")
            if base: c.fill = _hfill(base)

        rc = ws_eq.cell(row=row, column=8)
        rc.fill = _hfill(RESULT_FILL.get(result, "EEEEEE"))
        rc.font = Font(bold=True, color=RESULT_FONT_COLOR.get(result, "000000"))

        bc = ws_eq.cell(row=row, column=7)
        bc.fill          = _hfill("FFF2CC")
        bc.font          = Font(bold=True, color="7B4D00")
        bc.number_format = '$#,##0.00'

        ws_eq.cell(row=row, column=9).number_format  = '$#,##0.00'
        ws_eq.cell(row=row, column=9).font           = Font(
            bold=True, color="006100" if p["pnl"] > 0 else
            ("9C0006" if p["pnl"] < 0 else "1F4E79"))
        ws_eq.cell(row=row, column=10).number_format = '$#,##0.00'
        ws_eq.cell(row=row, column=10).font          = Font(
            bold=True, color="006100" if cum_pnl >= 0 else "9C0006")

    ws_eq.freeze_panes = "A2"

    # ── Equity curve line chart ────────────────────────────────────────────────
    if equity_picks:
        n = len(equity_picks)
        eq_chart = LineChart()
        eq_chart.title        = "Equity Curve — Running P&L"
        eq_chart.y_axis.title = "Running P&L ($)"
        eq_chart.x_axis.title = "Bet #"
        eq_chart.style        = 10
        eq_chart.width        = 30
        eq_chart.height       = 16
        eq_chart.legend       = None

        running_data = Reference(ws_eq, min_col=10, min_row=1, max_row=n + 1)
        eq_chart.add_data(running_data, titles_from_data=True)

        # Color the line green if final P&L positive, red if negative
        final_color = "375623" if cum_pnl >= 0 else "9C0006"
        eq_chart.series[0].graphicalProperties.line.solidFill = final_color
        eq_chart.series[0].graphicalProperties.line.width = 22000
        eq_chart.series[0].smooth = True

        # Add a zero-baseline reference
        eq_chart.y_axis.numFmt = '$#,##0.00'
        eq_chart.y_axis.crosses = "autoZero"

        ws_eq.add_chart(eq_chart, f"A{n + 4}")

    # ── EV Threshold + Pinnacle Analysis — always runs on all_picks ───────────
    _add_ev_threshold_tab(wb, all_picks)
    pt_settled_ref = [p for p in (play_these_picks or [])
                      if p.get("result") in ("WIN", "LOSS", "PUSH")]
    _add_pinnacle_tab(wb, all_picks, pt_settled_ref)

    # ── Player Props tab (optional) ────────────────────────────────────────────
    if prop_picks:
        resolved_props = [p for p in prop_picks
                          if p.get("result") in ("WIN", "LOSS", "PUSH",
                                                   "PENDING", "NO_RESULT", "MANUAL")]
        if resolved_props:
            _add_props_tab(wb, resolved_props)

    # ── Promo P&L tab (completely separate from all other data) ────────────────
    if promo_picks:
        _add_promo_tab(wb, promo_picks)

    if parlay_sim and parlay_sim.get("summary"):
        _add_parlay_sim_tab(wb, parlay_sim)

    wb.save(output_path)
    print(f"\nBacktest report saved to: {output_path}")


# ── Weekly digest ──────────────────────────────────────────────────────────────

def _digest_top_line(settled: list[dict], label: str) -> str:
    w = sum(1 for p in settled if p["result"] == "WIN")
    l = sum(1 for p in settled if p["result"] == "LOSS")
    pu = sum(1 for p in settled if p["result"] == "PUSH")
    pnl = sum(p["pnl"] for p in settled)
    bet = sum(p["bet_amount"] for p in settled)
    roi = pnl / bet * 100 if bet else 0
    sgn = "+" if pnl >= 0 else ""
    record = f"W{w}/L{l}" + (f"/P{pu}" if pu else "")
    return f"<b>{html.escape(label)}</b>: {record}  {sgn}${pnl:.2f}  ROI {sgn}{roi:.1f}%"


def _digest_by_key(settled: list[dict], key_fn, heading: str,
                   min_size: int = 2) -> str:
    groups: dict[str, list[dict]] = defaultdict(list)
    for p in settled:
        try:
            k = key_fn(p)
        except Exception:
            continue
        if k:
            groups[k].append(p)
    rows: list[str] = []
    for key in sorted(groups, key=lambda k: (-len(groups[k]), k)):
        g = groups[key]
        if len(g) < min_size:
            continue
        w = sum(1 for p in g if p["result"] == "WIN")
        l = sum(1 for p in g if p["result"] == "LOSS")
        pnl = sum(p["pnl"] for p in g)
        bet = sum(p["bet_amount"] for p in g)
        roi = pnl / bet * 100 if bet else 0
        sgn = "+" if pnl >= 0 else ""
        rows.append(f"  {html.escape(str(key))}: W{w}/L{l}  "
                    f"{sgn}${pnl:.0f}  ROI {sgn}{roi:.0f}%")
    if not rows:
        return ""
    return f"\n<b>By {html.escape(heading)}</b>\n" + "\n".join(rows)


def _confidence_tier(p: dict) -> str:
    """EV-percentage buckets — a proxy for signal strength."""
    ev = p.get("ev_pct")
    if ev is None:
        return ""
    if ev >= 5:
        return "Strong (EV≥5%)"
    if ev >= 3:
        return "Medium (3-5%)"
    return "Light (<3%)"


# Band edges mirror the 2026-06 promo odds-band P&L analysis: the edge has
# lived in +150..+300; +300..+700 (the boost-stake danger zone) and >+700
# have been bleeders. Bucketing by the P&L basis odds (boosted when a boost
# applied — that's what pick["decimal"] holds) keeps the digest consistent
# with how those picks were actually graded.
_PROMO_BAND_ORDER = ["≤+100", "+100..+150", "+150..+300", "+300..+700", ">+700"]
_PROMO_DANGER_BANDS = frozenset({"+300..+700", ">+700"})


def _promo_odds_band(p: dict) -> str | None:
    """Bucket a promo pick by American odds derived from its P&L decimal."""
    dec = p.get("decimal")
    if not isinstance(dec, (int, float)) or dec <= 1:
        return None
    am = (dec - 1) * 100 if dec >= 2 else -100 / (dec - 1)
    if am <= 100:
        return "≤+100"
    if am <= 150:
        return "+100..+150"
    if am <= 300:
        return "+150..+300"
    if am <= 700:
        return "+300..+700"
    return ">+700"


def _digest_promo_bands(promo_settled: list[dict]) -> str:
    """Promo P&L by odds band, in fixed band order (not group size).

    Separate from _digest_by_key because band order matters more than
    sample size here — the whole point is watching whether the +150..+300
    sweet spot holds and the >+300 danger bands keep bleeding. Danger
    bands get a ⚠ whenever they lose money in the window.
    """
    groups: dict[str, list[dict]] = defaultdict(list)
    for p in promo_settled:
        band = _promo_odds_band(p)
        if band:
            groups[band].append(p)
    rows: list[str] = []
    for band in _PROMO_BAND_ORDER:
        g = groups.get(band)
        if not g:
            continue
        w   = sum(1 for p in g if p["result"] == "WIN")
        l   = sum(1 for p in g if p["result"] == "LOSS")
        pnl = sum(p["pnl"] for p in g)
        bet = sum(p["bet_amount"] for p in g)
        roi = pnl / bet * 100 if bet else 0
        sgn = "+" if pnl >= 0 else ""
        warn = "  ⚠" if band in _PROMO_DANGER_BANDS and pnl < 0 else ""
        rows.append(f"  {band}: W{w}/L{l}  {sgn}${pnl:.0f}  "
                    f"ROI {sgn}{roi:.0f}%{warn}")
    if not rows:
        return ""
    return "\n<b>Promos by Odds Band</b>\n" + "\n".join(rows)


def weekly_digest_text(start_dt: date, end_dt: date) -> str:
    """Build an HTML-formatted weekly P&L digest string for Telegram.

    Grades Play These + Promo Picks from the archive window and reports
    overall, by-category, by-sport, by-market, and by-confidence-tier ROI.
    """
    print(f"[digest] Building digest for {start_dt} .. {end_dt}")

    def _settled(lst: list[dict]) -> list[dict]:
        return [p for p in lst
                if p.get("result") in ("WIN", "LOSS", "PUSH")
                and p.get("pnl") is not None]

    pt_graded    = run_backtest(load_archive_picks(start_dt, end_dt) or [])
    promo_graded = run_backtest_promo(
        load_archive_promo_picks(start_dt, end_dt) or [])

    pt_s    = _settled(pt_graded)
    promo_s = _settled(promo_graded)
    total   = pt_s + promo_s

    header = (f"📊 <b>Weekly P&amp;L — "
              f"{start_dt.strftime('%b %d')}–{end_dt.strftime('%b %d')}</b>")

    if not total:
        return header + "\n\nNo settled picks in the last 7 days."

    w   = sum(1 for p in total if p["result"] == "WIN")
    l   = sum(1 for p in total if p["result"] == "LOSS")
    pu  = sum(1 for p in total if p["result"] == "PUSH")
    pnl = sum(p["pnl"] for p in total)
    bet = sum(p["bet_amount"] for p in total)
    roi = pnl / bet * 100 if bet else 0
    sgn = "+" if pnl >= 0 else ""

    lines: list[str] = [header, ""]
    lines.append(
        f"<b>Overall</b>: W{w}/L{l}" + (f"/P{pu}" if pu else "") +
        f"  {sgn}${pnl:.2f}  ROI {sgn}{roi:.1f}%  ({len(total)} settled)")

    lines.append("")
    lines.append("<b>By Category</b>")
    if pt_s:
        lines.append("  " + _digest_top_line(pt_s, "+EV Picks"))
    if promo_s:
        lines.append("  " + _digest_top_line(promo_s, "Promos"))

    # Promo band watch — validates the cashable-band guard week over week
    # (promos are play money; this never mixes into the main P&L numbers).
    if promo_s:
        band_block = _digest_promo_bands(promo_s)
        if band_block:
            lines.append(band_block)

    sport_block  = _digest_by_key(
        pt_s, lambda p: (p.get("sport") or "").upper(), "Sport")
    if sport_block:
        lines.append(sport_block)

    market_block = _digest_by_key(
        pt_s, lambda p: (p.get("market") or "").title(), "Market")
    if market_block:
        lines.append(market_block)

    tier_block   = _digest_by_key(
        pt_s, _confidence_tier, "Confidence")
    if tier_block:
        lines.append(tier_block)

    best  = max(total, key=lambda p: p["pnl"])
    worst = min(total, key=lambda p: p["pnl"])
    lines.append("")
    lines.append(f"🏆 Best:  {html.escape(str(best.get('pick','?')))} "
                 f"(+${best['pnl']:.2f})")
    if worst is not best and worst["pnl"] < 0:
        lines.append(f"💀 Worst: {html.escape(str(worst.get('pick','?')))} "
                     f"(${worst['pnl']:.2f})")

    # SGP calibration — predicted vs realized joint hit rate from graded
    # /sgpcheck slips (see sgp_log.py). Absent until slips exist.
    try:
        import sgp_log
        sgp_block = sgp_log.calibration_summary()
        if sgp_block:
            lines.append(sgp_block)
    except Exception as e:
        print(f"[digest] SGP calibration block failed: {e}")

    return "\n".join(lines)


def _replay_pt_from_ev(ev_graded: list[dict]) -> list[dict]:
    """Filter graded EV Edges through odds_compare._qualifies() (current rules).

    Bridges the field-name differences between the backtest pick dict and the
    live edge dict that _qualifies() expects: market is uppercased here but
    lowercase in the live filter; pinnacle/num_books use different keys.
    """
    from odds_compare import _qualifies
    out = []
    for p in ev_graded:
        adapted = {
            "ev_pct":             p.get("ev_pct", 0.0),
            "num_other_books":    p.get("num_books", 0),
            "market":             (p.get("market") or "").lower(),
            "sport":              p.get("sport", ""),
            "pinnacle_anchored":  p.get("pinnacle", False),
            # 2026-07-09: _qualifies now hard-gates on the odds band —
            # without the price the replay would reject every pick.
            "odds_decimal":       p.get("decimal") or 0,
        }
        if _qualifies(adapted):
            out.append(p)
    print(f"  → {len(out)} of {len(ev_graded)} picks pass current _qualifies()")
    return out


# ── Parlay / SGP simulation ────────────────────────────────────────────────────
#
# "What if each day's graded picks had been combined into a boosted parlay?"
# Hypothetical only — selection uses pick-time data (odds + the sheet's own
# EV), never results, so there's no hindsight bias in which combos get built.
# Cross-game parlays price exactly (books quote the product of leg prices);
# SGPs get a correlation-adjusted quote we can't recover, so SGP results are
# reported across a grid of price haircuts off the naive product.

_PARLAY_SIM_UNIT      = 10.0   # flat stake per simulated parlay
_PARLAY_SIM_LEG_MAX   = 2.5    # decimal ≈ +150 per leg (mirrors live builder)
_PARLAY_SIM_TOTAL_MAX = 8.0    # decimal ≈ +700 combined (mirrors live builder)
_PARLAY_SIM_POOL_CAP  = 12     # top legs per pool — keeps brute force cheap

# Configs that also get a per-parlay detail listing in the Excel tab.
_PARLAY_SIM_HEADLINES = {("cross", 3, 30, 0), ("sgp", 3, 30, 20)}


def _sim_fair_prob(p: dict) -> float:
    """Back out the fair win prob the sheet believed at pick time:
    ev_pct = (fair × decimal − 1) × 100  →  fair = (1 + ev/100) / decimal."""
    dec = p.get("decimal") or 0
    if dec <= 1:
        return 0.0
    fair = (1 + (p.get("ev_pct") or 0) / 100) / dec
    return min(0.99, max(0.01, fair))


def _sim_price(dec: float, boost_pct: float, haircut_pct: float) -> float:
    """Naive product price → SGP haircut on the payout, then profit boost."""
    cut = 1 + (dec - 1) * (1 - haircut_pct / 100)
    return 1 + (cut - 1) * (1 + boost_pct / 100)


def _sim_day_pool(legs: list[dict], unique_games: bool) -> list[dict]:
    """Dedup a day's legs the way the live builder does (one per game+market;
    props keyed per player) — or one per game for cross-game parlays, since
    two same-game legs would make the book reprice the ticket as an SGP.
    Returns the top legs by fair prob, capped for brute-force search."""
    from odds_compare import _PLAYER_PROP_MARKETS
    best_by_key: dict[tuple, dict] = {}
    for p in legs:
        mkt = (p.get("market") or "").lower()
        if unique_games:
            key = (p["game"],)
        elif mkt in _PLAYER_PROP_MARKETS:
            key = (p["game"], mkt, p.get("pick"))
        else:
            key = (p["game"], mkt)
        cur = best_by_key.get(key)
        if cur is None or _sim_fair_prob(p) > _sim_fair_prob(cur):
            best_by_key[key] = p
    pool = sorted(best_by_key.values(), key=_sim_fair_prob, reverse=True)
    return pool[:_PARLAY_SIM_POOL_CAP]


def _sim_best_combo(pool: list[dict], n_legs: int, boost_pct: float,
                    haircut_pct: float, sgp: bool):
    """Best n-leg combo by EV at the (haircut + boosted) price, mirroring the
    live builder's total-odds cap. Returns (combo, ev) or (None, 0.0)."""
    from itertools import combinations
    from odds_compare import _sgp_combo_ok
    best, best_ev = None, 0.0
    for combo in combinations(pool, n_legs):
        if sgp and not _sgp_combo_ok(
                [{"market": (l.get("market") or "").lower(),
                  "outcome": l.get("pick")} for l in combo]):
            continue
        dec, fair = 1.0, 1.0
        for leg in combo:
            dec  *= leg["decimal"]
            fair *= _sim_fair_prob(leg)
        if dec > _PARLAY_SIM_TOTAL_MAX:
            continue
        ev = fair * _sim_price(dec, boost_pct, haircut_pct) - 1
        if ev > best_ev:
            best, best_ev = combo, ev
    return best, best_ev


def _grade_sim_parlay(combo, boost_pct: float, haircut_pct: float):
    """Industry rules: a pushed leg drops out and the ticket reprices; the
    parlay wins only if every remaining leg won. Returns (result, pnl, price)."""
    live = [l for l in combo if l["result"] != "PUSH"]
    if not live:
        return "PUSH", 0.0, 1.0
    dec = 1.0
    for l in live:
        dec *= l["decimal"]
    priced = _sim_price(dec, boost_pct, haircut_pct)
    if all(l["result"] == "WIN" for l in live):
        return "WIN", round(_PARLAY_SIM_UNIT * (priced - 1), 2), priced
    return "LOSS", -_PARLAY_SIM_UNIT, priced


def simulate_parlays(pt_graded: list[dict],
                     prop_graded: list[dict] | None = None) -> dict:
    """
    Simulate one parlay per day per book from graded picks, across a grid of
    configs. Play These game lines + graded props form the leg pool (props
    are what make 3+ leg SGPs possible — PT alone carries at most one
    compatible game-line pair per game).

    Returns {"summary": [config rows], "detail": {label: [parlay rows]}}.
    """
    pool_src = list(pt_graded or []) + list(prop_graded or [])
    settled = [p for p in pool_src
               if p.get("result") in ("WIN", "LOSS", "PUSH")
               and (p.get("decimal") or 0) > 1
               and p.get("game_date") and p.get("game")]
    legs_pool = [p for p in settled if p["decimal"] <= _PARLAY_SIM_LEG_MAX]

    by_day_book: dict[tuple, list] = defaultdict(list)
    for p in legs_pool:
        by_day_book[(p["game_date"], p["book"])].append(p)

    configs = ([("cross", n, b, 0) for n in (3, 4) for b in (0, 25, 30)]
               + [("sgp", n, b, h) for n in (2, 3, 4) for b in (25, 30)
                  for h in (0, 10, 20, 30)])

    summary: list[dict] = []
    detail:  dict[str, list] = {}
    for kind, n_legs, boost, haircut in configs:
        rows = []
        for (day, book), day_legs in sorted(by_day_book.items()):
            if kind == "sgp":
                groups: dict[str, list] = defaultdict(list)
                for p in day_legs:
                    groups[p["game"]].append(p)
                pools = [g for g in groups.values() if len(g) >= n_legs]
            else:
                pools = [day_legs] if len(day_legs) >= n_legs else []

            best_combo, best_ev = None, 0.0
            for pl in pools:
                pool = _sim_day_pool(pl, unique_games=(kind == "cross"))
                combo, ev = _sim_best_combo(
                    pool, n_legs, boost, haircut, sgp=(kind == "sgp"))
                if combo is not None and ev > best_ev:
                    best_combo, best_ev = combo, ev
            if best_combo is None:
                continue

            result, pnl, priced = _grade_sim_parlay(best_combo, boost, haircut)
            naive_dec = 1.0
            for l in best_combo:
                naive_dec *= l["decimal"]
            rows.append({
                "date":       day,
                "book":       book,
                "kind":       kind,
                "legs":       list(best_combo),
                "n_legs":     n_legs,
                "naive_dec":  round(naive_dec, 3),
                "priced_dec": round(priced, 3),
                "exp_ev":     round(best_ev * 100, 2),
                "result":     result,
                "pnl":        pnl,
            })

        wins   = sum(1 for r in rows if r["result"] == "WIN")
        pushes = sum(1 for r in rows if r["result"] == "PUSH")
        staked = _PARLAY_SIM_UNIT * sum(
            1 for r in rows if r["result"] != "PUSH")
        pnl    = round(sum(r["pnl"] for r in rows), 2)
        summary.append({
            "kind":        kind,
            "n_legs":      n_legs,
            "boost_pct":   boost,
            "haircut_pct": haircut,
            "parlays":     len(rows),
            "wins":        wins,
            "pushes":      pushes,
            "staked":      staked,
            "pnl":         pnl,
            "roi_pct":     round(pnl / staked * 100, 1) if staked else 0.0,
        })
        if (kind, n_legs, boost, haircut) in _PARLAY_SIM_HEADLINES:
            detail[f"{kind} {n_legs}-leg +{boost}% "
                   f"(haircut {haircut}%)"] = rows

    return {"summary": summary, "detail": detail}


def _print_parlay_sim(sim: dict) -> None:
    rows = [r for r in sim.get("summary", []) if r["parlays"]]
    if not rows:
        print("  No days had enough qualifying legs to build a parlay.")
        return
    for r in rows:
        hc = f" cut{r['haircut_pct']:>3}%" if r["kind"] == "sgp" else "      "
        print(f"  {r['kind']:<5} {r['n_legs']}-leg +{r['boost_pct']:>2}%{hc}"
              f"  n={r['parlays']:<3} W{r['wins']:<3}"
              f" P&L ${r['pnl']:+8.2f}  ROI {r['roi_pct']:+6.1f}%")


# ── Grading-gap audit ──────────────────────────────────────────────────────────

def _gap_seen_path() -> str:
    return os.path.join(os.environ.get("DATA_DIR") or SCRIPT_DIR,
                        "gap_audit_seen.json")


def _gap_key(a: dict) -> str:
    return (f"{a['sport']}|{a['game']}|{a['market']}|"
            f"{a['game_date']}|{a['status']}")


def grading_gap_audit(days: int = 3, remember: bool = False) -> dict:
    """Nightly watchdog for silent grading holes (2026-07-09).

    Re-grades the trailing `days` of broad-pool picks (window ends
    yesterday) in memory — no report written — and splits whatever is
    still ungraded into:

      anomalies — SCOREABLE sport, game date passed, yet the pick grades
                  NO_RESULT / UNKNOWN / NOT_STARTED. A result source went
                  quiet, a slug died (how the dead ESPN CFL path was
                  found), or name matching failed. Someone should look.
      expected  — known-ungradeable cohorts (MANUAL sports like PGA/HR
                  props, or sports with no results source configured).

    The Telegram daily job DMs the admin only when anomalies exist, so
    silence means the grading pipeline is healthy.
    """
    end   = date.today() - timedelta(days=1)
    start = end - timedelta(days=days - 1)
    picks  = load_archive_ev_edges(start, end, "exclude")
    graded = run_backtest(picks) if picks else []

    today_s = date.today().strftime("%Y%m%d")
    anomalies: list[dict] = []
    expected: dict[str, int] = defaultdict(int)
    settled = 0
    for p in graded:
        r = p.get("result")
        if r in ("WIN", "LOSS", "PUSH"):
            settled += 1
            continue
        gd = p.get("game_date") or ""
        if not gd or gd >= today_s:
            continue                    # today/future — nothing due yet
        sport = p.get("sport", "?")
        if sport not in SCOREABLE or r in ("MANUAL", "VOID"):
            # VOID (2026-07-13): postponed/abandoned — book refunds the
            # stake; terminal and expected, not a pipeline failure.
            expected[sport] += 1
            continue
        # Grace period for live/suspended states: an overseas evening
        # game (AFL/KBO run "yesterday" ET while genuinely live) or a
        # rain-suspended MLB game resuming next day legitimately reads
        # IN_PROGRESS/NOT_STARTED for a day. Only 2+ days stale is a
        # real stuck state; NO_RESULT (no fixture found at all) alerts
        # after one day, since it's the signature of a dead source or
        # broken name matching.
        two_days_ago = (date.today() - timedelta(days=2)).strftime("%Y%m%d")
        if r in ("IN_PROGRESS", "NOT_STARTED") and gd > two_days_ago:
            continue
        anomalies.append({
            "sport":     sport,
            "game":      p.get("game"),
            "market":    p.get("market"),
            "game_date": gd,
            "status":    r,
        })

    # remember=True (the nightly job): DM-worthy anomalies are only the
    # ones never alerted before — ungradeable club-card bouts and
    # vanished postponements would otherwise nag every night until they
    # age out of the window. Everything still lands in the log/text.
    new_anomalies = anomalies
    if remember:
        try:
            seen: dict[str, str] = {}
            if os.path.exists(_gap_seen_path()):
                with open(_gap_seen_path(), "r", encoding="utf-8") as f:
                    seen = json.load(f)
            new_anomalies = [a for a in anomalies
                             if _gap_key(a) not in seen]
            today_iso = date.today().isoformat()
            for a in new_anomalies:
                seen[_gap_key(a)] = today_iso
            cutoff = (date.today() - timedelta(days=14)).isoformat()
            seen = {k: v for k, v in seen.items() if v >= cutoff}
            with open(_gap_seen_path(), "w", encoding="utf-8") as f:
                json.dump(seen, f)
        except Exception:
            new_anomalies = anomalies      # state trouble → alert anyway

    exp_str = (", ".join(f"{s}:{n}" for s, n in sorted(expected.items()))
               or "none")
    text = (f"gap audit {start}..{end}: {settled} settled, "
            f"{len(anomalies)} anomalies ({len(new_anomalies)} new), "
            f"expected-ungradeable [{exp_str}]")
    return {"start": str(start), "end": str(end), "settled": settled,
            "anomalies": anomalies, "new_anomalies": new_anomalies,
            "expected": dict(expected), "text": text}


# ── Main ───────────────────────────────────────────────────────────────────────

def run_full_backtest(start_dt: date, end_dt: date,
                       an_only_mode: str = "exclude",
                       replay: bool = False) -> dict:
    """Load, grade, and write the full Excel backtest report.

    an_only_mode controls inclusion of _ANONLY archive files (see
    AN_ONLY_MODES). Default "exclude" keeps skill measurement clean.

    replay: when True, the Play These subset is derived by re-applying the
    *current* odds_compare._qualifies() filter to the historical EV Edges
    pool, instead of reading each archive's frozen-at-write-time PT tab.
    Use this to evaluate the live filter against history without ghost
    picks from leagues/markets that have since been blocked.

    Returns a dict with:
        primary_picks      — graded EV Edges (or Play These fallback); drives Excel
        play_these_graded  — graded Play These subset
        excel_path         — absolute path to the written .xlsx, or None if no picks
    """
    print(f"  AN-only sheets: {an_only_mode}")
    if replay:
        print("  Mode: REPLAY (PT subset = current _qualifies() applied to EV Edges)")

    import progress as _progress
    _progress.report("backtest", "Loading picks from archive")

    # ── Closing-line lookup — built once for the whole date range ────────────
    # Reads pending_closes_*.json files seeded by every scan. Picks without
    # captured closes (AN-fallback events, or games still in the future)
    # simply won't appear in the lookup, so CLV columns will be blank for
    # them — graceful degradation rather than blocking the backtest.
    try:
        from closing_lines import load_closing_lines_for_dates
        clv_lookup = load_closing_lines_for_dates(start_dt, end_dt)
        print(f"  CLV lookup: {len(clv_lookup)} captured closes loaded")
    except Exception as e:
        print(f"  CLV lookup unavailable: {e}")
        clv_lookup = {}

    # ── All +EV Picks (primary dataset — EV Edges tab) ────────────────────────
    print("─── All +EV Picks (EV Edges tab) ────────────────────────")
    ev_raw    = load_archive_ev_edges(start_dt, end_dt, an_only_mode)
    ev_graded = (run_backtest(ev_raw, clv_lookup,
                           progress_label="EV pool")
                 if ev_raw else [])
    if ev_graded:
        ev_settled_list = analyze(ev_graded, label="All +EV Picks")
    else:
        ev_settled_list = []
        print("  No EV Edges data found — falling back to Play These as primary.")

    # ── Play These (filtered subset) ──────────────────────────────────────────
    if replay:
        print("\n─── Play These (REPLAYED with current filter) ───────────")
        graded = _replay_pt_from_ev(ev_graded)
        if graded:
            pt_settled_list = analyze(graded, label="Play These (replayed)")
        else:
            pt_settled_list = []
            print("  No picks pass current _qualifies().")
    else:
        print("\n─── Play These (filtered picks) ─────────────────────────")
        picks  = load_archive_picks(start_dt, end_dt, an_only_mode)
        graded = (run_backtest(picks, clv_lookup,
                                progress_label="Play These")
                  if picks else [])
        if graded:
            pt_settled_list = analyze(graded, label="Play These (filtered)")
        else:
            pt_settled_list = []
            print("  No Play These picks found.")

    if not ev_graded and not graded:
        print("\nNo picks found in the selected date range.")
        return {"primary_picks": [], "play_these_graded": [], "excel_path": None}

    primary_picks   = ev_graded  if ev_graded  else graded
    primary_settled = ev_settled_list if ev_settled_list else pt_settled_list

    # ── Promo Picks (separate P&L tracking) ──────────────────────────────────
    print("\n─── Promo Picks (Play These — promo section) ────────────")
    promo_raw    = load_archive_promo_picks(start_dt, end_dt, an_only_mode)
    promo_graded = run_backtest_promo(promo_raw) if promo_raw else []
    if promo_graded:
        promo_settled = [p for p in promo_graded
                         if p.get("result") in ("WIN", "LOSS", "PUSH")]
        if promo_settled:
            pr_pnl = sum(p["pnl"] for p in promo_settled)
            pr_bet = sum(p["bet_amount"] for p in promo_settled)
            pr_w   = sum(1 for p in promo_settled if p["result"] == "WIN")
            pr_l   = sum(1 for p in promo_settled if p["result"] == "LOSS")
            pr_roi = pr_pnl / pr_bet * 100 if pr_bet else 0
            print(f"  Promos: W{pr_w}/L{pr_l}  P&L ${pr_pnl:+.2f}  ROI {pr_roi:.1f}%")
    else:
        promo_graded = []

    # ── Player Props ──────────────────────────────────────────────────────────
    print("\n─── Player Props (Odds Comparison tab) ──────────────────")
    prop_raw    = load_archive_props(start_dt, end_dt, an_only_mode)
    prop_graded = (run_backtest(prop_raw, clv_lookup,
                                 progress_label="player props")
                   if prop_raw else [])
    if prop_graded:
        prop_settled = [p for p in prop_graded if p.get("result") in ("WIN","LOSS","PUSH")]
        print(f"Props settled: {len(prop_settled)} picks")
        if prop_settled:
            pr_pnl = sum(p["pnl"] for p in prop_settled)
            pr_bet = sum(p["bet_amount"] for p in prop_settled)
            pr_w   = sum(1 for p in prop_settled if p["result"] == "WIN")
            pr_l   = sum(1 for p in prop_settled if p["result"] == "LOSS")
            pr_roi = pr_pnl / pr_bet * 100 if pr_bet else 0
            print(f"  Props: W{pr_w}/L{pr_l}  P&L ${pr_pnl:+.2f}  ROI {pr_roi:.1f}%")
            by_mkt: dict[str, list] = defaultdict(list)
            for p in prop_settled:
                by_mkt[p["market"]].append(p)
            for mkt, mp in sorted(by_mkt.items()):
                mw = sum(1 for p in mp if p["result"] == "WIN")
                ml = sum(1 for p in mp if p["result"] == "LOSS")
                mb = sum(p["bet_amount"] for p in mp)
                mn = sum(p["pnl"] for p in mp)
                mr = mn / mb * 100 if mb else 0
                print(f"    {mkt:<14} W{mw}/L{ml}  P&L ${mn:+.2f}  ROI {mr:.1f}%")
    else:
        prop_graded = None

    # ── Parlay / SGP simulation (hypothetical tickets, no real bets) ─────────
    print("\n─── Parlay / SGP simulation (PT legs + props) ───────────")
    try:
        parlay_sim = simulate_parlays(graded, prop_graded or [])
        _print_parlay_sim(parlay_sim)
    except Exception as e:
        print(f"  Parlay sim failed: {e}")
        parlay_sim = None

    # ── Archive old backtest reports ──────────────────────────────────────────
    # Run timestamp is baked into the new file's name (after the date range,
    # joined by '__'), so re-running the same date range no longer clobbers
    # the prior archive copy on disk OR collapses to a single dedup'd entry
    # in the listing UI. Each run becomes its own visible report.
    bt_archive = os.path.join(SCRIPT_DIR, "Backtest Archives")
    old_reports = glob.glob(os.path.join(SCRIPT_DIR, "backtest_*.xlsx"))
    if old_reports:
        os.makedirs(bt_archive, exist_ok=True)
        for old in old_reports:
            shutil.move(old, os.path.join(bt_archive, os.path.basename(old)))
        print(f"\nArchived {len(old_reports)} old report(s) → Backtest Archives/")

    run_stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_name = f"backtest_{start_dt}_{end_dt}__{run_stamp}.xlsx"
    out_path = os.path.join(SCRIPT_DIR, out_name)
    _progress.report("backtest", "Writing report")
    write_backtest_excel(primary_picks, primary_settled, out_path, start_dt, end_dt,
                         play_these_picks=graded if graded else None,
                         prop_picks=prop_graded,
                         promo_picks=promo_graded,
                         parlay_sim=parlay_sim)

    return {
        "primary_picks": primary_picks,
        "play_these_graded": graded,
        "excel_path": out_path,
        "parlay_sim": parlay_sim,
    }


def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="Odds Compare backtest engine — grade past picks vs results.")
    parser.add_argument(
        "--an-only", choices=sorted(AN_ONLY_MODES), default="exclude",
        help=("How to handle archive sheets generated when the Odds API was "
              "exhausted and the bot fell back to Action Network "
              "(filename suffix _ANONLY). 'exclude' (default) skips them, "
              "'include' counts them, 'only' uses them exclusively."))
    parser.add_argument(
        "--replay", action="store_true",
        help=("Replay history through the *current* odds_compare._qualifies() "
              "filter instead of reading each archive's frozen-at-write-time "
              "Play These tab. Use to evaluate live filter changes without "
              "ghost picks from leagues/markets blocked after the fact."))
    args = parser.parse_args()

    print("=" * 62)
    print("  ODDS COMPARE — BACKTEST ENGINE")
    print("  Source: Play These + EV Edges  |  Results: ESPN (free)")
    print("=" * 62)

    date_range = prompt_date_range()
    if not date_range:
        print("Cancelled.")
        return

    start_dt, end_dt = date_range
    print(f"\nDate range: {start_dt}  →  {end_dt}")
    print(f"Archive:    {ARCHIVE_DIR}\n")

    if not os.path.isdir(ARCHIVE_DIR):
        if not glob.glob(os.path.join(SCRIPT_DIR, "ev_edges_*.xlsx")):
            print("No archive or ev_edges files found. Run odds_compare.py first.")
            return

    run_full_backtest(start_dt, end_dt, an_only_mode=args.an_only,
                      replay=args.replay)


if __name__ == "__main__":
    main()
