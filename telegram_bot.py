#!/usr/bin/env python3
"""
Telegram bot for remote odds_compare.py execution.

Usage:
    1. Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID (see bot_config.py)
    2. python3 telegram_bot.py
    3. Open Telegram on your phone  ->  send /run to your bot

Commands:
    /run         — Start a new odds compare run (asks for bankrolls + promos)
    /run_single  — Run for a single book of your choosing
    /run_skip    — Just list the picks (no bankrolls, no promos)
    /run_promos  — Only show promo picks (skip Summary + Play These)
    /run_live    — Quick +EV picks on currently live games (for promos)
    /quickbet    — Standalone Kelly calculator for promos (no API, no archive)
    /kelly       — Show or set your preferred Kelly fraction
    /backtest    — Run a backtest on past picks
    /books       — View or change your sportsbook selection
    /add_user    — (Admin) Authorize a new user
    /status      — Check if a run is in progress
    /help        — Show usage instructions
"""

import asyncio
import base64
import glob
import json
import logging
import re
import sys
import os
from datetime import datetime, date, time as dtime, timedelta, timezone
from zoneinfo import ZoneInfo

try:
    import anthropic
except ImportError:
    anthropic = None

from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove, BotCommand
from telegram.ext import (
    Application,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from telegram.constants import ParseMode

# Add project dir to path so we can import odds_compare
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bot_config import (
    TELEGRAM_BOT_TOKEN, ADMIN_CHAT_ID, USERS_FILE, validate_required_secrets,
)

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)

# ── Conversation states ──────────────────────────────────────────────
ASK_DK_BANKROLL, ASK_FD_BANKROLL, ASK_PROMOS, ASK_GAME_PICK = range(4)
ASK_BT_PERIOD, ASK_BT_START, ASK_BT_END, ASK_BT_AN = range(10, 14)
ASK_BOOKS = 20
ASK_RUN_SINGLE_BOOK = 21
ASK_PROMO_GAMESPEC = 22
ASK_PHOTO_BOOK = 23
ASK_QB_PICK, ASK_QB_TRUE, ASK_QB_BOOST, ASK_QB_BANKROLL = range(30, 34)

_GAMESPEC_YES = "🎯 Specific game"
_GAMESPEC_NO  = "Any game"

# ── Run lock ────────────────────────────────────────────────────────
_run_lock = asyncio.Lock()

# ── User persistence ────────────────────────────────────────────────

_users: dict[str, dict] = {}


def load_users():
    global _users
    if os.path.exists(USERS_FILE):
        try:
            with open(USERS_FILE, "r") as f:
                _users = json.load(f)
        except (json.JSONDecodeError, IOError):
            log.warning("Corrupt users.json — re-seeding with admin.")
            _users = {}
    # Ensure admin is always present
    admin_key = str(ADMIN_CHAT_ID)
    if admin_key not in _users:
        _users[admin_key] = {"books": ["DraftKings", "FanDuel"]}
    save_users()


def save_users():
    # Always keep admin
    admin_key = str(ADMIN_CHAT_ID)
    _users.setdefault(admin_key, {"books": ["DraftKings", "FanDuel"]})
    with open(USERS_FILE, "w") as f:
        json.dump(_users, f, indent=2)


# ── Authorization ────────────────────────────────────────────────────

def is_authorized(update: Update) -> bool:
    return str(update.effective_user.id) in _users


def is_admin(update: Update) -> bool:
    return update.effective_user.id == ADMIN_CHAT_ID


def get_user_books(update: Update) -> list[str] | None:
    profile = _users.get(str(update.effective_user.id))
    if profile and profile.get("books"):
        return profile["books"]
    return None


# ── Per-user Kelly fraction ──────────────────────────────────────────

# odds_compare.py bakes edges with KELLY_FRACTION=0.5. When a user
# prefers a different fraction, we scale the stored kelly_pct at
# display time by (user_fraction / DEFAULT_KELLY_FRACTION).
DEFAULT_KELLY_FRACTION = 0.5


def get_user_kelly(update: Update) -> float:
    """User's preferred Kelly fraction. Defaults to 0.5 (half-Kelly)."""
    profile = _users.get(str(update.effective_user.id))
    if not profile:
        return DEFAULT_KELLY_FRACTION
    frac = profile.get("kelly_fraction")
    if not isinstance(frac, (int, float)) or not (0 < frac <= 1):
        return DEFAULT_KELLY_FRACTION
    return float(frac)


def set_user_kelly(update: Update, fraction: float) -> None:
    uid = str(update.effective_user.id)
    profile = _users.setdefault(uid, {})
    profile["kelly_fraction"] = float(fraction)
    save_users()


# ── Promo parser ─────────────────────────────────────────────────────

# Sport aliases → canonical key. Common names (hockey, basketball) map
# to the underlying league so the user can type what's on the promo
# slip ("any ice hockey event") without looking up the league code.
PROMO_SPORT_KEYWORDS = {
    "nba": "nba",   "basketball": "nba",
    "mlb": "mlb",   "baseball":   "mlb",
    "nhl": "nhl",   "hockey":     "nhl",
    "nfl": "nfl",   "football":   "nfl",
    "ncaab": "ncaab", "cbb":      "ncaab",
    "ncaaf": "ncaaf", "cfb":      "ncaaf",
    "soccer": "soccer",
    # Single-token shortcuts for individual European leagues narrow to
    # the european_soccer canonical key — typing "epl" should never
    # surface an MLS pick (which would not qualify for the promo).
    "epl":        "european_soccer",
    "laliga":     "european_soccer",
    "bundesliga": "european_soccer",
    "ucl":        "european_soccer",
    "uel":        "european_soccer",
    "ufc": "ufc",   "mma":        "ufc",
    "pga": "pga",   "golf":       "pga",
    "wnba": "wnba",
    "afl": "afl",
    "boxing": "boxing",
    "cricket": "cricket", "ipl": "cricket",
    "tennis": "tennis", "atp": "tennis", "wta": "tennis",
}

# Two-word sport phrases checked with one-token lookahead.
PROMO_SPORT_PHRASES = {
    "ice hockey":         "nhl",
    "premier league":     "european_soccer",
    "college basketball": "ncaab",
    "college football":   "ncaaf",
    "american football":  "nfl",
    "champions league":   "european_soccer",
    "europa league":      "european_soccer",
    "serie a":            "european_soccer",
    "ligue 1":            "european_soccer",
    "liga mx":            "soccer",
    "la liga":            "european_soccer",
    "aussie rules":       "afl",
    "australian rules":   "afl",
    # "european soccer" / "euro soccer" / "europe soccer" all narrow to
    # the major European leagues so a promo whose terms exclude MLS /
    # Liga MX doesn't accidentally match a non-European game. The filter
    # works on the Odds API sport_key tag attached to each edge.
    "european soccer":    "european_soccer",
    "europe soccer":      "european_soccer",
    "euro soccer":        "european_soccer",
}

PROMO_MARKET_KEYWORDS = {
    "ml": "h2h", "moneyline": "h2h", "h2h": "h2h",
    "spreads": "spreads", "spread": "spreads", "ats": "spreads",
    "totals": "totals", "total": "totals", "ou": "totals", "o/u": "totals",
    "hr": "hr", "homerun": "hr", "homeruns": "hr",
    "homer": "hr", "homers": "hr",
    "strikeouts": "strikeouts", "ks": "strikeouts",
    "hits": "hits",
    "rbi": "rbis", "rbis": "rbis",
    "threes": "threes", "3pt": "threes", "3ptm": "threes",
    "pra": "pra",
    "dd": "dd", "doubledouble": "dd",
    "sog": "sog", "shotsongoal": "sog",
    # First-inning total runs (MLB) — used for FanDuel "First Inning Friday"
    # and similar promos. Single-token forms; multi-token "first inning" is
    # caught by PROMO_MARKET_PHRASES below.
    "1st_inning": "totals_1st", "first_inning": "totals_1st",
    "first-inning": "totals_1st", "1st-inning": "totals_1st",
    "firstinning": "totals_1st", "1stinning": "totals_1st",
    "f1": "totals_1st",
}

# Two-word market phrases checked with one-token lookahead. Mirrors the
# PROMO_SPORT_PHRASES pattern so the user can type "fd 50% first inning mlb"
# or "fd 50% 1st inning mlb" naturally.
PROMO_MARKET_PHRASES = {
    "first inning": "totals_1st",
    "1st inning":   "totals_1st",
    "home run":     "hr",
    "home runs":    "hr",
}

# Display labels for non-trivial market keys. Markets not listed here fall
# back to mkt.capitalize() in the promo label builder.
PROMO_MARKET_DISPLAY = {
    "totals_1st": "1st Inning",
}

# Natural-language filler tokens that carry no semantic weight for the
# parser. Lets users paste a near-verbatim promo description like
# "25% any ice hockey event, 3+ leg parlays, $10 max wager, +100 min".
PROMO_FILLER_TOKENS = {
    "any", "all", "every",
    "event", "events",
    "bet", "bets", "parlay", "parlays",
    "boost", "boosts", "wager", "wagers",
    "min", "max", "minimum", "maximum",
    "no", "odds", "legs", "leg",
    "the", "a", "an", "on", "only", "just", "for",
    "and", "or", "of",
    "profit",  # "25% profit boost"
}


def parse_promo(text: str) -> dict | None:
    """
    Parse a promo string into a promo dict. Tolerant of natural phrasing:
    filler words are ignored, common sport/market aliases accepted, odds
    with a sign (+100, -200) and dollar amounts ($10) don't need a
    min:/max: prefix.

    Examples:
        dk 25%
        fd bonus $25 mlb
        dk 30% nba spreads min:-200 max:50
        dk 25% 3-leg nba
        dk 25% 3+ ice hockey $10 +100
        dk 20% mlb game
    """
    text = text.strip().lower()
    if not text:
        return None

    # Remove punctuation that would split natural-language tokens
    text = text.replace(",", " ").replace(";", " ")

    tokens = text.split()
    if not tokens:
        return None

    # Book
    book_token = tokens[0]
    if book_token in ("dk", "draftkings"):
        book = "DraftKings"
    elif book_token in ("fd", "fanduel"):
        book = "FanDuel"
    else:
        return None

    promo = {
        "book":          book,
        "sport":         "all",
        "market":        "all",
        "promo_type":    "straight",
        "boost_pct":     None,
        "bet_amount":    None,
        "min_odds":      None,
        "max_bet":       None,
        "num_legs":      None,
        "game_specific": False,
        "game_filter":   None,
        "game_hint":     None,
        "sgp":           False,
        "count":         1,
        "label":         "",
    }

    rest = tokens[1:]
    i = 0
    while i < len(rest):
        tok = rest[i]

        # Two-word sport phrase (e.g. "ice hockey", "premier league")
        if i + 1 < len(rest):
            two_word = f"{tok} {rest[i + 1]}"
            if two_word in PROMO_SPORT_PHRASES:
                promo["sport"] = PROMO_SPORT_PHRASES[two_word]
                i += 2
                continue
            # Two-word market phrase (e.g. "first inning", "1st inning")
            if two_word in PROMO_MARKET_PHRASES:
                promo["market"] = PROMO_MARKET_PHRASES[two_word]
                i += 2
                continue

        # Bonus bet: "bonus $25" or "bonus 25" or "bonus bet $25"
        if tok == "bonus":
            promo["promo_type"] = "bonus_bet"
            # Skip filler ("bet", "free") between "bonus" and the amount
            j = i + 1
            while j < len(rest) and rest[j] in ("bet", "free"):
                j += 1
            if j < len(rest):
                amt_str = rest[j].lstrip("$")
                try:
                    promo["bet_amount"] = float(amt_str)
                    i = j + 1
                    continue
                except ValueError:
                    pass
            i += 1
            continue

        # Parlay: "3-leg", "3leg", "3+leg", "3legs", "3+legs"
        leg_match = re.match(r"^(\d+)[-+]?legs?$", tok)
        if leg_match:
            promo["promo_type"] = "parlay"
            promo["num_legs"] = int(leg_match.group(1))
            i += 1
            continue

        # Parlay "3+" — matches DK's "3+ leg parlays" wording
        plus_match = re.match(r"^(\d+)\+$", tok)
        if plus_match:
            promo["promo_type"] = "parlay"
            promo["num_legs"] = int(plus_match.group(1))
            i += 1
            continue

        # min:-200 or min:+300 (also min:300)
        min_match = re.match(r"^min:([+-]?\d+)$", tok)
        if min_match:
            promo["min_odds"] = int(min_match.group(1))
            i += 1
            continue

        # max:50 or max:$50
        max_match = re.match(r"^max:\$?(\d+(?:\.\d+)?)$", tok)
        if max_match:
            promo["max_bet"] = float(max_match.group(1))
            i += 1
            continue

        # Standalone American odds: "+100" or "-200" (explicit sign required)
        signed_match = re.match(r"^([+-])(\d+)$", tok)
        if signed_match and promo["min_odds"] is None:
            sign, num = signed_match.groups()
            promo["min_odds"] = int(num) if sign == "+" else -int(num)
            i += 1
            continue

        # Standalone dollar amount: "$10" → max_bet (or bonus amount for
        # bonus_bet that somehow missed the lookahead above)
        dollar_match = re.match(r"^\$(\d+(?:\.\d+)?)$", tok)
        if dollar_match:
            amt = float(dollar_match.group(1))
            if (promo["promo_type"] == "bonus_bet"
                    and promo["bet_amount"] is None):
                promo["bet_amount"] = amt
            elif promo["max_bet"] is None:
                promo["max_bet"] = amt
            i += 1
            continue

        # sgp (same-game parlay)
        if tok == "sgp":
            promo["sgp"] = True
            i += 1
            continue

        # "Nx" — the user has N copies of this promo (e.g. "2x", "3x")
        count_match = re.match(r"^(\d+)x$", tok)
        if count_match:
            n = int(count_match.group(1))
            if n >= 1:
                promo["count"] = n
            i += 1
            continue

        # game:TeamA-TeamB — matchup hint. TeamA/B may be short codes
        # (ATL), nicknames (Braves), or cities (Atlanta). Multi-word
        # names use underscores: Red_Sox, New_York. Matched case-
        # insensitively as substrings in the eligible-game string.
        game_hint_match = re.match(
            r"^game:([A-Za-z][A-Za-z_]*)-([A-Za-z][A-Za-z_]*)$", tok)
        if game_hint_match:
            promo["game_specific"] = True
            promo["game_hint"] = (
                f"{game_hint_match.group(1)}-"
                f"{game_hint_match.group(2)}")
            i += 1
            continue

        # game (game-specific, no hint — will prompt manually)
        if tok == "game":
            promo["game_specific"] = True
            i += 1
            continue

        # Sport keyword (after sgp/game to avoid clash with "game")
        if tok in PROMO_SPORT_KEYWORDS:
            promo["sport"] = PROMO_SPORT_KEYWORDS[tok]
            i += 1
            continue

        # Market keyword
        if tok in PROMO_MARKET_KEYWORDS:
            promo["market"] = PROMO_MARKET_KEYWORDS[tok]
            i += 1
            continue

        # Boost percentage: "25%" or "25" — lower priority than min-odds
        # so a bare "100" after other fields isn't misread as the boost.
        pct_match = re.match(r"^(\d+(?:\.\d+)?)%?$", tok)
        if pct_match and promo["boost_pct"] is None:
            promo["boost_pct"] = float(pct_match.group(1))
            i += 1
            continue

        # Filler token — ignore
        if tok in PROMO_FILLER_TOKENS:
            i += 1
            continue

        i += 1

    # Build label
    parts = [book]
    if promo["promo_type"] == "bonus_bet":
        parts.append(f"${promo['bet_amount']:.0f} bonus bet"
                     if promo["bet_amount"] else "bonus bet")
    elif promo["promo_type"] == "parlay":
        sgp_str = "SGP " if promo.get("sgp") else ""
        parts.append(f"{promo['boost_pct']}% {promo['num_legs']}-leg {sgp_str}parlay boost"
                     if promo["num_legs"] else f"{promo['boost_pct']}% {sgp_str}parlay boost")
    else:
        parts.append(f"{promo['boost_pct']}% boost" if promo["boost_pct"] else "boost")

    if promo["sport"] != "all":
        # Multi-word sport keys (european_soccer) render as Title Case so
        # the label reads "European Soccer" rather than "EUROPEAN_SOCCER".
        sport_key = promo["sport"]
        if "_" in sport_key:
            sport_label = " ".join(w.capitalize() for w in sport_key.split("_"))
        else:
            sport_label = sport_key.upper()
        parts.append(f"/ {sport_label}")
    if promo["market"] != "all":
        mkt_label = PROMO_MARKET_DISPLAY.get(
            promo["market"], promo["market"].capitalize())
        parts.append(f"/ {mkt_label}")
    if promo["min_odds"] is not None:
        parts.append(f"(min {promo['min_odds']:+d})")
    if promo["max_bet"] is not None:
        parts.append(f"(max ${promo['max_bet']:.0f})")
    if promo.get("count", 1) > 1:
        parts.append(f"×{promo['count']}")
    promo["label"] = " ".join(parts)

    return promo


# ── Message formatters ───────────────────────────────────────────────

def _deep_link(edge: dict) -> str:
    """Construct a sportsbook deep link for the given edge, if possible."""
    if edge["book"] == "DraftKings" and edge.get("dk_event_id"):
        return f"https://sportsbook.draftkings.com/event/{edge['dk_event_id']}"
    if edge["book"] == "FanDuel" and edge.get("fd_event_id"):
        return f"https://sportsbook.fanduel.com/sport/event/{edge['fd_event_id']}"
    return ""


def _esc(text) -> str:
    """Escape <, >, & for Telegram HTML mode."""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _format_american(odds: int) -> str:
    return f"+{odds}" if odds > 0 else str(odds)


def _parse_any_odds(text: str) -> float | None:
    """Parse American (+150, -110) or decimal (2.50) odds. Returns decimal,
    or None if unparseable. Values with |v| >= 100 are treated as American."""
    t = (text or "").strip().replace(",", "")
    if not t:
        return None
    try:
        val = float(t)
    except ValueError:
        return None
    if val >= 100 or val <= -100:
        from odds_compare import american_to_decimal
        return american_to_decimal(int(round(val)))
    if val > 1.0:
        return val
    return None


def _parse_kelly_input(text: str) -> float | None:
    """Parse '0.25', '25%', '25', or word shortcuts (full/half/quarter/eighth).
    Bare non-integer values > 1 (e.g. '1.5') are rejected as ambiguous."""
    t = (text or "").strip().lower()
    if not t:
        return None
    shortcuts = {"full": 1.0, "half": 0.5, "quarter": 0.25, "eighth": 0.125}
    if t in shortcuts:
        return shortcuts[t]
    has_percent = t.endswith("%")
    t = t.rstrip("%").strip()
    try:
        val = float(t)
    except ValueError:
        return None
    if has_percent:
        val = val / 100
    elif val > 1:
        # Accept bare whole-integer percents (e.g. "25" → 0.25) but reject
        # ambiguous fractional values > 1 like "1.5".
        if val == int(val) and 2 <= val <= 100:
            val = val / 100
        else:
            return None
    return val if 0 < val <= 1 else None


def _parse_amount(text: str) -> float | None:
    """Parse a dollar amount. Strips $ and commas."""
    try:
        val = float((text or "").replace("$", "").replace(",", "").strip())
        return val if val > 0 else None
    except ValueError:
        return None


def _format_point(point, market: str = "") -> str:
    """Format a point/line value. Only add '+' for spreads."""
    if point is None:
        return ""
    val = float(point)
    if market.lower() == "spreads" and val > 0:
        return f"+{point}"
    return str(point)


def format_summary(result: dict, user_books: list[str],
                   skip_mode: bool = False) -> str:
    """Format the summary message."""
    s = result["summary"]
    now = datetime.now().strftime("%m/%d %I:%M %p ET")
    dk_ct = sum(1 for e in result["all_edges"] if e["book"] == "DraftKings")
    fd_ct = sum(1 for e in result["all_edges"] if e["book"] == "FanDuel")
    pinn_ct = sum(1 for e in result["all_edges"] if e.get("pinnacle_anchored"))

    edge_parts = [f"Pinnacle: {pinn_ct}/{s['total_edges']}"]
    if "DraftKings" in user_books:
        edge_parts.append(f"DK: {dk_ct}")
    if "FanDuel" in user_books:
        edge_parts.append(f"FD: {fd_ct}")

    lines = []
    if s.get("an_only"):
        lines.append("⚠ <b>AN-ONLY RUN</b> — Odds API quota exhausted, "
                     "fell back to Action Network. Pinnacle blending "
                     "degraded; consider sizing down.")
        lines.append("")
    lines.extend([
        f"<b>ODDS COMPARE</b>  {now}",
        f"----------------------------",
        f"{s['total_games']} games | {s['total_edges']} +EV edges",
        " | ".join(edge_parts),
    ])
    if not skip_mode:
        lines.append("")
        if "DraftKings" in user_books:
            lines.append(f"DK bankroll: ${result['dk_bankroll']:,.2f}")
        if "FanDuel" in user_books:
            lines.append(f"FD bankroll: ${result['fd_bankroll']:,.2f}")
    return "\n".join(lines)


def format_play_edges(play_edges: list[dict], bankrolls: dict,
                      skip_mode: bool = False,
                      kelly_scale: float = 1.0) -> list[str]:
    """
    Format "Play These" edges into Telegram messages (one per book).
    Returns a list of message strings. In skip_mode, omits bet amounts.
    kelly_scale rescales the stored (half-Kelly) kelly_pct to the user's
    preferred fraction — pass user_fraction / DEFAULT_KELLY_FRACTION.
    """
    from odds_compare import clamp_bet, _confidence, EASTERN

    messages = []

    for book_name in ("DraftKings", "FanDuel"):
        book_edges = [e for e in play_edges if e["book"] == book_name]
        if not book_edges:
            continue

        short = "DK" if book_name == "DraftKings" else "FD"
        br = bankrolls.get(book_name, 0)
        total_bet = 0
        total_profit = 0
        lines = [f"<b>{short} PLAYS ({len(book_edges)})</b>"]

        for i, edge in enumerate(book_edges, 1):
            bet = clamp_bet(
                br * edge["kelly_pct"] * kelly_scale / 100, edge["book"])
            profit = bet * (edge["odds_decimal"] - 1)
            total_bet += bet
            total_profit += profit

            try:
                ct = datetime.fromisoformat(
                    edge["commence"].replace("Z", "+00:00"))
                ct_et = ct.astimezone(EASTERN)
                time_str = ct_et.strftime("%-I:%M %p")
            except Exception:
                time_str = ""

            pick_str = _esc(edge["outcome"])
            pt = _format_point(edge.get("point"), edge.get("market", ""))
            if pt:
                pick_str += f" {pt}"

            conf = _confidence(edge)
            odds_str = _format_american(edge["odds_american"])
            game = _esc(edge["game"])
            sport = edge["sport"].upper()
            market = edge["market"].upper()

            time_part = f" | {time_str}" if time_str else ""
            if skip_mode:
                third_line = f"    EV {edge['ev_pct']}% | {conf}"
            else:
                third_line = (f"    EV {edge['ev_pct']}% | {conf} | "
                              f"${bet:.2f} -> +${profit:.2f}")
            lines.append(
                f"\n<b>{i}.</b> {sport} | {game}{time_part}\n"
                f"    {market} | {pick_str} | {odds_str}\n"
                f"{third_line}"
            )

            link = _deep_link(edge)
            if link:
                lines.append(f'    <a href="{link}">Open in {short}</a>')

        if not skip_mode:
            lines.append(
                f"\n----------------------------\n"
                f"Deployed: ${total_bet:.2f} | Potential: +${total_profit:.2f}"
            )
        messages.append("\n".join(lines))

    if not messages:
        messages.append("No qualifying 'Play These' picks this run.")

    return messages


def format_promo_picks(promo_picks: list[dict], bankrolls: dict,
                       kelly_fraction: float = DEFAULT_KELLY_FRACTION,
                       ) -> str | None:
    """Format promo picks into a single message.
    kelly_fraction is the user's preferred Kelly fraction; defaults to
    DEFAULT_KELLY_FRACTION (0.5, matches the module-level default)."""
    if not promo_picks:
        return None

    from odds_compare import (apply_profit_boost, calc_ev, format_american,
                              kelly_bet, clamp_bet)

    lines = ["<b>PROMO PICKS</b>"]

    for pp in promo_picks:
        promo = pp["promo"]
        edge = pp["edge"]
        n_copies = pp.get("copy_of", 1) or 1
        copy_suffix = (f" <i>[pick {pp['copy_idx'] + 1}/{n_copies}]</i>"
                       if n_copies > 1 else "")

        lines.append(f"\n<b>{_esc(promo['label'])}</b>{copy_suffix}")

        if edge is None:
            reason = pp.get("reason") or "No qualifying pick found"
            lines.append(f"  {_esc(reason)}")
            continue

        is_fallback = pp.get("is_fallback", False)
        if is_fallback:
            lines.append(
                "  <i>No +EV pick — fallback to least-bad option:</i>")

        pick_str = _esc(edge["outcome"])
        pt = _format_point(edge.get("point"), edge.get("market", ""))
        if pt:
            pick_str += f" {pt}"
        elif edge.get("market", "").lower() in ("spreads", "totals", "totals_1st"):
            log.warning(
                "Promo pick on %s/%s has no point: %r",
                edge.get("market"), edge.get("game"), edge)
            pick_str += " <b>[!MISSING LINE!]</b>"
        odds_str = _format_american(edge["odds_american"])
        game = _esc(edge["game"])
        market = _esc(edge.get("market", "?")).upper()

        lines.append(f"  {game} | {market} | {pick_str}")

        if promo.get("promo_type") == "bonus_bet":
            amt = promo.get("bet_amount", 0)
            exp_profit = (edge["fair_prob"] / 100) * amt * (edge["odds_decimal"] - 1)
            lines.append(
                f"  {odds_str} | Bonus ${amt:.0f} | ~${exp_profit:.2f} expected"
            )
        else:
            boost_dec = apply_profit_boost(
                edge["odds_decimal"], promo.get("boost_pct") or 0)
            fair_prob = edge["fair_prob"] / 100
            boosted_ev = calc_ev(fair_prob, boost_dec)
            boosted_odds = format_american(boost_dec)
            boosted_k = kelly_bet(fair_prob, boost_dec, fraction=kelly_fraction)
            br = bankrolls.get(edge["book"], 0)
            # For +EV picks: Kelly stake (capped by promo max).
            # For fallback -EV picks: Kelly=0, so default to the promo's
            # max_bet (or a small flat if no cap) — burning a promo on a
            # max-$5 play is fine; don't recommend large stakes on -EV.
            if is_fallback:
                bet = promo.get("max_bet") or 5.0
            else:
                bet = clamp_bet(br * boosted_k, edge["book"])
                if promo.get("max_bet") and bet > promo["max_bet"]:
                    bet = promo["max_bet"]
            profit = bet * (boost_dec - 1)
            lines.append(
                f"  {odds_str} -&gt; {boosted_odds} | "
                f"EV {edge['ev_pct']}% -&gt; {boosted_ev:+.1f}%"
            )
            bet_label = "Bet" if not is_fallback else "Bet (promo max)"
            lines.append(
                f"  {bet_label} ${bet:.2f} -&gt; +${profit:.2f}"
            )

        link = _deep_link(edge)
        if link:
            short = "DK" if "draftkings" in link else "FD"
            lines.append(f'  <a href="{link}">Open in {short}</a>')

    return "\n".join(lines)


def format_parlay_picks(parlay_rows: list[dict]) -> str | None:
    """Format parlay picks into a Telegram message."""
    if not parlay_rows:
        return None

    lines = ["<b>PARLAY PICKS</b>"]

    for row in parlay_rows:
        promo = row["promo"]
        legs = row["legs"]
        n_copies = row.get("copy_of", 1) or 1
        copy_suffix = (f" <i>[pick {row['copy_idx'] + 1}/{n_copies}]</i>"
                       if n_copies > 1 else "")

        # Placeholder row — no qualifying parlay could be built
        if legs is None:
            lines.append(f"\n<b>{_esc(promo['label'])}</b>{copy_suffix}")
            lines.append(
                "  No qualifying parlay found "
                "<i>(not enough legs on the slate, or no combo is +EV "
                "after the boost)</i>"
            )
            continue

        lines.append(
            f"\n<b>{_esc(promo['label'])} ({row['num_legs']} legs)</b>"
            f"{copy_suffix}"
        )
        lines.append(
            f"  {row['parlay_american']} -&gt; {row['boost_american']} (boosted)"
        )
        lines.append(
            f"  EV {row['boosted_ev']}% | Win prob {row['parlay_fair_prob']:.1f}%"
        )
        lines.append(
            f"  Bet ${row['bet']:.2f} -&gt; +${row['profit']:.2f}"
        )
        lines.append("")
        for i, leg in enumerate(legs, 1):
            pick = _esc(leg["outcome"])
            pt = _format_point(leg.get("point"), leg.get("market", ""))
            if pt:
                pick += f" {pt}"
            odds = _format_american(leg["odds_american"])
            lines.append(
                f"  {i}. {leg['sport'].upper()} | {_esc(leg['game'])}"
            )
            lines.append(
                f"     {leg['market'].upper()} | {pick} | {odds}"
            )

    return "\n".join(lines)


# ── Books conversation ──────────────────────────────────────────────

BOOKS_PROMPT = "Which sportsbooks do you use?"

# (button label, books-list) — labels also serve as accepted text input
_BOOK_CHOICES: list[tuple[str, list[str]]] = [
    ("DraftKings only", ["DraftKings"]),
    ("FanDuel only",    ["FanDuel"]),
    ("Both",            ["DraftKings", "FanDuel"]),
]


def _books_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[label] for label, _ in _BOOK_CHOICES],
        one_time_keyboard=True, resize_keyboard=True,
    )


def _parse_book_choice(text: str) -> list[str] | None:
    t = text.strip().lower()
    for label, books in _BOOK_CHOICES:
        if t == label.lower():
            return books
    # Legacy / shorthand inputs
    if t in ("1", "dk", "draftkings"):
        return ["DraftKings"]
    if t in ("2", "fd", "fanduel"):
        return ["FanDuel"]
    if t in ("3", "both"):
        return ["DraftKings", "FanDuel"]
    return None


async def cmd_books(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_authorized(update):
        await update.message.reply_text("Unauthorized.")
        return ConversationHandler.END

    current = get_user_books(update)
    if current:
        book_str = ", ".join(current)
        text = f"Current books: {book_str}\n\n{BOOKS_PROMPT}"
    else:
        text = f"Welcome!\n\n{BOOKS_PROMPT}"
    await update.message.reply_text(text, reply_markup=_books_keyboard())
    return ASK_BOOKS


async def recv_book_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    books = _parse_book_choice(update.message.text)
    if books is None:
        await update.message.reply_text(
            "Tap one of the options.", reply_markup=_books_keyboard())
        return ASK_BOOKS

    uid = str(update.effective_user.id)
    _users.setdefault(uid, {})["books"] = books
    save_users()

    book_str = ", ".join(books)
    await update.message.reply_text(
        f"Saved! You'll see edges for: {book_str}.",
        reply_markup=ReplyKeyboardRemove(),
    )
    return ConversationHandler.END


# ── /add_user command ────────────────────────────────────────────────

async def cmd_add_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("Admin only.")
        return

    if not context.args or len(context.args) != 1:
        await update.message.reply_text("Usage: /add_user <telegram_user_id>")
        return

    new_id = context.args[0]
    if not new_id.isdigit():
        await update.message.reply_text("User ID must be a number.")
        return

    if new_id in _users:
        await update.message.reply_text(f"User {new_id} is already authorized.")
        return

    _users[new_id] = {}
    save_users()
    await update.message.reply_text(
        f"User {new_id} authorized.\n"
        f"They'll be asked to pick books on first command."
    )


def _promo_keyboard(update: Update,
                    context: ContextTypes.DEFAULT_TYPE | None = None,
                    ) -> ReplyKeyboardMarkup | None:
    """Build a reply keyboard from the user's saved promos.

    Returned only when explicitly requested (initial prompt or /recent).
    After any tap/message we send ReplyKeyboardRemove so it doesn't
    cling to the bottom — the user can summon it back via /recent or
    by typing 'show'/'recent'/'promos'.
    """
    uid = str(update.effective_user.id)
    profile = _users.get(uid, {})
    saved = profile.get("saved_promos", [])
    if not saved:
        return None
    buttons = [[p] for p in saved[:6]]
    has_entries = bool(context and context.user_data.get("promos"))
    buttons.append(["done" if has_entries else "skip"])
    return ReplyKeyboardMarkup(buttons, one_time_keyboard=True,
                               resize_keyboard=True)


async def _send_promo_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send the promo prompt, with saved promos as quick-tap buttons if available."""
    keyboard = _promo_keyboard(update, context)

    photo_hint = (
        " or 📸 send a screenshot"
        if os.environ.get("ANTHROPIC_API_KEY") else "")
    if keyboard:
        text = (
            f"<b>Promos</b> — tap a saved one, type a new one{photo_hint}, "
            "or <b>skip</b>/<b>done</b>."
        )
    else:
        text = (
            f"<b>Promos</b> — one per message{photo_hint}, or "
            "<b>skip</b>/<b>done</b>.\n\n"
            "<i>Quick examples:</i>\n"
            "  <code>dk 25%</code>\n"
            "  <code>fd bonus $25 mlb</code>\n"
            "  <code>dk 30% 3-leg sgp nba</code>\n"
            "  <code>fd 25% spreads $50 -200</code>\n"
            "  <code>dk 25% 3+ hockey $10 +100</code>\n\n"
            "<i>Tips:</i>\n"
            "  • Signed odds (<code>+100</code>, <code>-200</code>) set min odds\n"
            "  • Dollar amounts (<code>$10</code>) set max bet\n"
            "  • <code>3+</code> or <code>3-leg</code> → 3+ leg parlay\n"
            "  • Filler words (<code>any</code>, <code>event</code>, "
            "<code>min</code>, <code>max</code>, commas) are ignored\n\n"
            "<i>Sports:</i> nba/basketball · mlb/baseball · nhl/hockey · "
            "nfl/football · soccer · ufc · pga/golf · ncaab · ncaaf\n"
            "<i>Markets:</i> ml · spreads/ats · totals/ou · hr · strikeouts"
        )

    await update.message.reply_text(
        text, parse_mode="HTML",
        reply_markup=keyboard or ReplyKeyboardRemove(),
    )


# ── Conversation handlers ────────────────────────────────────────────

async def cmd_run_skip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Shortcut for /run skip — just list the picks, no bet sizing."""
    context.args = ["skip"]
    return await cmd_run(update, context)


async def cmd_run_promos(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Fetch + list PROMO PICKS only — suppresses the Summary / Play These
    output. Still asks for bankroll, since bet sizing (boosted Kelly, max-
    bet cap) depends on it."""
    if not is_authorized(update):
        await update.message.reply_text("Unauthorized.")
        return ConversationHandler.END

    if context.bot_data.get("running"):
        await update.message.reply_text(
            "A run is already in progress. Wait for it to finish.")
        return ConversationHandler.END

    user_books = get_user_books(update)
    if not user_books:
        await update.message.reply_text("Set up your books first with /books")
        return ConversationHandler.END

    uid = str(update.effective_user.id)
    profile = _users.get(uid, {})
    last_bankrolls = profile.get("last_bankrolls", {})

    context.user_data.clear()
    context.user_data["books"]       = user_books
    context.user_data["promos"]      = []
    context.user_data["promos_only"] = True

    if "DraftKings" in user_books:
        msg = "Enter your DraftKings bankroll ($):"
        if last_bankrolls.get("DraftKings"):
            msg += f"\n<i>Last: ${last_bankrolls['DraftKings']:,.2f}</i>"
        await update.message.reply_text(msg, parse_mode="HTML")
        return ASK_DK_BANKROLL
    else:
        context.user_data["dk_bankroll"] = 0
        msg = "Enter your FanDuel bankroll ($):"
        if last_bankrolls.get("FanDuel"):
            msg += f"\n<i>Last: ${last_bankrolls['FanDuel']:,.2f}</i>"
        await update.message.reply_text(msg, parse_mode="HTML")
        return ASK_FD_BANKROLL


# ── /run_single command ──────────────────────────────────────────────

_RUN_SINGLE_BOOKS = ["DraftKings", "FanDuel"]


def _run_single_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[b] for b in _RUN_SINGLE_BOOKS],
        one_time_keyboard=True, resize_keyboard=True,
    )


def _parse_run_single_book(text: str) -> str | None:
    t = (text or "").strip().lower()
    if t in ("dk", "draftkings"):
        return "DraftKings"
    if t in ("fd", "fanduel"):
        return "FanDuel"
    return None


async def cmd_run_single(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Prompt for a single book, then hand off to the /run flow with that book only."""
    if not is_authorized(update):
        await update.message.reply_text("Unauthorized.")
        return ConversationHandler.END

    if context.bot_data.get("running"):
        await update.message.reply_text(
            "A run is already in progress. Wait for it to finish.")
        return ConversationHandler.END

    if not get_user_books(update):
        await update.message.reply_text("Set up your books first with /books")
        return ConversationHandler.END

    args_text = " ".join(context.args).strip().lower() if context.args else ""
    skip = args_text in ("skip", "fast", "quick")

    context.user_data.clear()
    context.user_data["_run_single_skip"] = skip

    await update.message.reply_text(
        "Which book for this run?",
        reply_markup=_run_single_keyboard(),
    )
    return ASK_RUN_SINGLE_BOOK


async def recv_run_single_book(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    book = _parse_run_single_book(update.message.text)
    if book is None:
        await update.message.reply_text(
            "Tap DraftKings or FanDuel.", reply_markup=_run_single_keyboard())
        return ASK_RUN_SINGLE_BOOK

    skip = context.user_data.get("_run_single_skip", False)
    context.user_data["_single_book_override"] = [book]
    context.args = ["skip"] if skip else []
    return await cmd_run(update, context)


async def cmd_run_live(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List +EV picks on currently in-progress games. For grabbing a quick line
    to attach a live promo to. No archive, no Excel, no prev-pick tracking."""
    if not is_authorized(update):
        await update.message.reply_text("Unauthorized.")
        return

    chat_id = update.effective_chat.id

    if _run_lock.locked():
        await update.message.reply_text(
            "A run is already in progress. Wait for it to finish.")
        return

    user_books = get_user_books(update)
    if not user_books:
        await update.message.reply_text("Set up your books first with /books")
        return

    await update.message.reply_text("Fetching live games... (~30s)")

    async with _run_lock:
        context.bot_data["running"] = True
        try:
            from odds_compare import run_headless_live
            edges = await asyncio.to_thread(run_headless_live)
        except Exception as e:
            context.bot_data["running"] = False
            await context.bot.send_message(chat_id, f"Live fetch failed: {e}")
            return
        context.bot_data["running"] = False

    edges = [e for e in edges if e["book"] in user_books]
    if not edges:
        await context.bot.send_message(
            chat_id, "No qualifying live +EV edges right now.")
        return

    messages = format_play_edges(edges, bankrolls={}, skip_mode=True)
    for msg in messages:
        for chunk in _split_message(msg):
            await context.bot.send_message(
                chat_id, chunk, parse_mode="HTML",
                disable_web_page_preview=True)


async def cmd_run(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_authorized(update):
        await update.message.reply_text("Unauthorized.")
        return ConversationHandler.END

    if context.bot_data.get("running"):
        await update.message.reply_text("A run is already in progress. Wait for it to finish.")
        return ConversationHandler.END

    user_books = get_user_books(update)
    if not user_books:
        await update.message.reply_text("Set up your books first with /books")
        return ConversationHandler.END

    # Single-book override set by /run_single picker — scoped to this run only
    single_override = context.user_data.get("_single_book_override")
    if single_override:
        user_books = single_override

    # Check for "/run skip" shortcut — just list the picks, no bet sizing
    args_text = " ".join(context.args).strip().lower() if context.args else ""
    skip_mode = args_text in ("skip", "fast", "quick")

    uid = str(update.effective_user.id)
    profile = _users.get(uid, {})
    last_bankrolls = profile.get("last_bankrolls", {})

    context.user_data.clear()
    context.user_data["books"] = user_books
    context.user_data["promos"] = []

    if skip_mode:
        # No bankrolls — just show the picks
        context.user_data["dk_bankroll"] = 0
        context.user_data["fd_bankroll"] = 0
        context.user_data["skip_mode"] = True
        return await _start_run(update, context)

    if "DraftKings" in user_books:
        msg = "Enter your DraftKings bankroll ($):"
        if last_bankrolls.get("DraftKings"):
            msg += f"\n<i>Last: ${last_bankrolls['DraftKings']:,.2f}</i>"
        await update.message.reply_text(msg, parse_mode="HTML")
        return ASK_DK_BANKROLL
    else:
        # FD only — skip DK
        context.user_data["dk_bankroll"] = 0
        msg = "Enter your FanDuel bankroll ($):"
        if last_bankrolls.get("FanDuel"):
            msg += f"\n<i>Last: ${last_bankrolls['FanDuel']:,.2f}</i>"
        await update.message.reply_text(msg, parse_mode="HTML")
        return ASK_FD_BANKROLL


async def recv_dk_bankroll(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        val = float(update.message.text.replace("$", "").replace(",", ""))
        if val <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Enter a valid dollar amount:")
        return ASK_DK_BANKROLL

    context.user_data["dk_bankroll"] = val

    user_books = context.user_data["books"]
    if "FanDuel" in user_books:
        uid = str(update.effective_user.id)
        last_fd = _users.get(uid, {}).get("last_bankrolls", {}).get("FanDuel")
        msg = "Enter your FanDuel bankroll ($):"
        if last_fd:
            msg += f"\n<i>Last: ${last_fd:,.2f}</i>"
        await update.message.reply_text(msg, parse_mode="HTML")
        return ASK_FD_BANKROLL
    else:
        # DK only — skip FD, go to promos
        context.user_data["fd_bankroll"] = 0
        await _send_promo_prompt(update, context)
        return ASK_PROMOS


async def recv_fd_bankroll(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        val = float(update.message.text.replace("$", "").replace(",", ""))
        if val <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Enter a valid dollar amount:")
        return ASK_FD_BANKROLL

    context.user_data["fd_bankroll"] = val
    await _send_promo_prompt(update, context)
    return ASK_PROMOS


async def _accept_parsed_promo(update: Update,
                               context: ContextTypes.DEFAULT_TYPE,
                               promo: dict, original_text: str) -> int:
    """Validate + store a parsed promo, then prompt for the next input.
    Shared by recv_promo (text) and recv_promo_photo (image).

    Always dismisses the saved-promos keyboard with ReplyKeyboardRemove
    so it doesn't cling to the bottom of the chat. The user can summon
    it again with /recent."""
    user_books = context.user_data["books"]
    if promo["book"] not in user_books:
        await update.message.reply_text(
            f"You don't have {promo['book']} selected. "
            f"Use /books to change. Try again or tap/type 'done'.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return ASK_PROMOS

    # Boosts/parlays require a single boost %. Reject ambiguous cases
    # (e.g. photo extraction produced "20-50%" from a stepped-up boost)
    # so the user can fix it now instead of finalize crashing later.
    if (promo.get("promo_type") in ("straight", "parlay")
            and promo.get("boost_pct") is None):
        await update.message.reply_text(
            "Couldn't read a single boost % from that promo — stepped "
            "or tiered boosts need to be resolved to one number. Try a "
            "clearer screenshot, or type manually, e.g.\n"
            "<code>dk 50% 4+ sgp nhl $5</code> (for a 20/33/50% stepped-up boost)",
            parse_mode="HTML",
            reply_markup=ReplyKeyboardRemove(),
        )
        return ASK_PROMOS

    # If a semantically-equivalent promo is already in this run, merge
    # by incrementing its count instead of adding a second entry. This
    # lets users with e.g. 2 copies of the same DK boost signal that
    # fact (either via the '2x' suffix or by adding the promo twice);
    # the analyzer will then produce 2 distinct picks for that promo.
    existing_list = context.user_data.get("promos", [])
    merged = next((e for e in existing_list if _promos_equivalent(promo, e)),
                  None)
    if merged is not None:
        merged["count"] = merged.get("count", 1) + promo.get("count", 1)
        # Rebuild the label's ×N suffix to reflect the new total count
        merged["label"] = re.sub(r"\s+×\d+$", "", merged["label"])
        if merged["count"] > 1:
            merged["label"] += f" ×{merged['count']}"
        _save_promo_string(str(update.effective_user.id), original_text)
        saved_kb = _promo_keyboard(update, context)
        await update.message.reply_text(
            f"Already have this promo — now at ×{merged['count']}.\n"
            f"<i>{_esc(merged['label'])}</i>\n"
            f"Tap a saved promo, type/send another, or tap 'done' to finish.",
            parse_mode="HTML",
            reply_markup=saved_kb or ReplyKeyboardRemove(),
        )
        return ASK_PROMOS

    context.user_data["promos"].append(promo)
    context.user_data.setdefault("promo_strings", []).append(original_text)

    # Persist to saved_promos immediately so /recent is up-to-date even
    # if this run crashes or is cancelled before finalize. Typed and
    # photo-extracted entries treated identically.
    _save_promo_string(str(update.effective_user.id), original_text)

    # Game-specific is implicit from the promo text. If the extractor
    # also captured a matchup hint (game:XXX-YYY), we'll auto-resolve
    # it after fetch — no prompt needed.
    if promo.get("game_hint"):
        tag = f" — tied to game {promo['game_hint']}"
    elif promo.get("game_specific"):
        tag = " — marked game-specific (will ask which game)"
    else:
        tag = ""

    # Re-attach the saved-promos keyboard after each add so 'done' +
    # picking another saved promo are one tap each.
    saved_kb = _promo_keyboard(update, context)
    await update.message.reply_text(
        f"Added: {promo['label']}{tag}\n"
        f"Tap a saved promo, type/send another, or tap 'done' to finish.",
        reply_markup=saved_kb or ReplyKeyboardRemove(),
    )
    return ASK_PROMOS


_EQUIV_FIELDS = (
    "book", "sport", "market", "promo_type", "boost_pct",
    "bet_amount", "min_odds", "max_bet", "num_legs",
    "game_specific", "sgp", "game_hint",
)


def _promos_equivalent(a: dict, b: dict) -> bool:
    """Return True if two parsed promos are semantically the same.
    Used to dedupe within a single conversation — tokens-in-different-
    order strings still produce the same dict and are considered equal."""
    return all(a.get(f) == b.get(f) for f in _EQUIV_FIELDS)


def _save_promo_string(uid: str, promo_string: str) -> None:
    """Upsert a promo string into the user's saved_promos list. Most-recent
    first, deduped case-insensitively, capped at 6. Called incrementally
    as each promo is added, not only at run-finalize time."""
    profile = _users.setdefault(uid, {})
    saved = profile.get("saved_promos", [])
    key = promo_string.lower().strip()
    saved = [s for s in saved if s.lower().strip() != key]
    saved.insert(0, promo_string.strip())
    profile["saved_promos"] = saved[:6]
    save_users()


async def recv_promo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip().lower()

    if text in ("done", "skip", "no", "none"):
        # /run_promos with no promos is pointless — cancel instead of fetching
        if (context.user_data.get("promos_only")
                and not context.user_data.get("promos")):
            await update.message.reply_text(
                "No promos entered — cancelled.",
                reply_markup=ReplyKeyboardRemove(),
            )
            return ConversationHandler.END
        return await _start_run(update, context)

    # 'show'/'recent'/'promos' text re-summons the saved-promos keyboard
    # (typing an alternative to the /recent slash command).
    if text in ("show", "recent", "promos"):
        await _send_promo_prompt(update, context)
        return ASK_PROMOS

    promo = parse_promo(update.message.text)
    if promo is None:
        await update.message.reply_text(
            "Couldn't parse that promo. Try again, /recent for saved, "
            "or 'done'/'skip' to finish.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return ASK_PROMOS

    return await _accept_parsed_promo(
        update, context, promo, update.message.text.strip())


async def cmd_recent_promos(update: Update,
                            context: ContextTypes.DEFAULT_TYPE) -> int:
    """/recent — re-show the saved-promos reply keyboard on demand."""
    await _send_promo_prompt(update, context)
    return ASK_PROMOS


# ── Photo → promo via Claude vision ──────────────────────────────────

_VISION_PROMPT = """Extract the sportsbook promo shown in this screenshot \
and output ONLY one line in this compact format:

<book> <boost%> [Nleg|N+] [sgp] [sport] [market] [+min_odds|-min_odds] [$max_bet]

Rules:
- book: "dk" (DraftKings) or "fd" (FanDuel). Look for VISIBLE BRAND
  TEXT first — "DraftKings"/"DK" or "FanDuel"/"Fanduel"/"f" logo. If
  no brand text is visible (the image is just a promo card crop),
  output your best guess anyway — the user will confirm. Both books
  now ship dark-theme promo cards with yellow boost icons, so color
  alone is NOT a reliable signal. Do not hallucinate brand text.
- boost: a SINGLE percentage, e.g. "25%". Never a range.
  For STEPPED/TIERED boosts (different % per leg count, e.g. "20% for 2
  legs, 33% for 3 legs, 50% for 4+ legs"), pick the HIGHEST tier and
  emit the matching minimum leg count. The example above → "50% 4+".
- If it's a parlay boost: add "3-leg" (exactly 3) or "3+" (3 or more).
  Include "sgp" if it's a same-game-parlay / SGP+ boost.
- sport: one of nba, mlb, nhl, nfl, soccer, ncaab, ncaaf, ufc, pga.
  Omit if the promo applies to all sports.
- market: one of ml, spreads, totals, hr, strikeouts. Omit if any
  market or if the market isn't one of these (e.g. "goalscorer" — omit).
- min_odds: e.g. "+100" or "-200" (include the sign).
  If the promo lists "Min Total Odds" or similar, use that.
- max_bet: e.g. "$10". If "Max Wager" is given, use it.
- game: if the promo is restricted to ONE specific matchup (e.g.
  "Bet on ATL Braves vs PHI Phillies", "Lakers vs Celtics SGP"),
  output `game:TeamA-TeamB` where TeamA and TeamB are the team
  NICKNAMES (Braves, Phillies, Yankees, Lakers) joined with a hyphen.
  For multi-word nicknames use an underscore: `game:Yankees-Red_Sox`.
  Examples:
    · "ATL Braves vs PHI Phillies" → `game:Braves-Phillies`
    · "Lakers vs Celtics SGP"      → `game:Lakers-Celtics`
    · "Yankees at Red Sox only"    → `game:Yankees-Red_Sox`
  If no specific matchup is mentioned (e.g. "Any NBA event", a sport-
  wide SGP boost), OMIT the game token entirely. DO NOT add "game" or
  "game:..." for generic SGP/parlay boosts that apply league-wide.
- For bonus bets: "<book> bonus $<amount> [sport]"

If the image is not a sportsbook promo (DraftKings or FanDuel), respond \
with exactly: ERROR: not a promo

Output ONLY the single line — no preamble, no explanation."""


def _vision_client() -> "anthropic.Anthropic | None":
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key or anthropic is None:
        return None
    return anthropic.Anthropic(api_key=key)


async def _image_to_promo_string(img_bytes: bytes,
                                 media_type: str = "image/jpeg") -> str:
    """Send image to Claude vision, return the compact promo string
    (or a string starting with 'ERROR:' on failure)."""
    client = _vision_client()
    if client is None:
        return "ERROR: vision not configured"
    b64 = base64.standard_b64encode(bytes(img_bytes)).decode("utf-8")
    try:
        resp = await asyncio.to_thread(
            client.messages.create,
            model="claude-sonnet-4-6",
            max_tokens=200,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image",
                     "source": {"type": "base64",
                                "media_type": media_type,
                                "data": b64}},
                    {"type": "text", "text": _VISION_PROMPT},
                ],
            }],
        )
        return resp.content[0].text.strip()
    except Exception as e:
        return f"ERROR: {e}"


async def recv_promo_photo(update: Update,
                           context: ContextTypes.DEFAULT_TYPE) -> int:
    """Extract a promo from a photo and add it like a typed one."""
    if anthropic is None or not os.environ.get("ANTHROPIC_API_KEY"):
        await update.message.reply_text(
            "Photo-to-promo isn't configured (ANTHROPIC_API_KEY missing). "
            "Type the promo instead."
        )
        return ASK_PROMOS

    photo = update.message.photo[-1]  # largest size
    status = await update.message.reply_text("📸 Reading promo from image…")

    try:
        file = await context.bot.get_file(photo.file_id)
        img_bytes = await file.download_as_bytearray()
    except Exception as e:
        await status.edit_text(f"Couldn't download the image: {e}")
        return ASK_PROMOS

    extracted = await _image_to_promo_string(img_bytes)

    if extracted.startswith("ERROR:"):
        await status.edit_text(
            f"Couldn't read a promo from that image.\n"
            f"<i>{_esc(extracted)}</i>\n"
            f"Try a clearer screenshot or type the promo.",
            parse_mode="HTML",
        )
        return ASK_PROMOS

    promo = parse_promo(extracted)
    if promo is None:
        await status.edit_text(
            f"Extracted: <code>{_esc(extracted)}</code>\n"
            f"…but couldn't parse it. Type it manually and adjust as needed.",
            parse_mode="HTML",
        )
        return ASK_PROMOS

    user_books = context.user_data["books"]

    # If only one book is selected, the book is unambiguous — override
    # whatever the model guessed and skip the confirmation step.
    if len(user_books) == 1:
        forced = user_books[0]
        if promo["book"] != forced:
            promo["book"] = forced
            extracted = re.sub(
                r"^(dk|fd|draftkings|fanduel)\b",
                "dk" if forced == "DraftKings" else "fd",
                extracted, count=1, flags=re.IGNORECASE)
        await status.edit_text(
            f"Extracted: <code>{_esc(extracted)}</code>",
            parse_mode="HTML",
        )
        return await _accept_parsed_promo(update, context, promo, extracted)

    # Both books enabled → confirm with one tap. The model's guess is
    # shown first on the keyboard but user can always flip it.
    await status.edit_text(
        f"Extracted: <code>{_esc(extracted)}</code>\n"
        f"<i>Guessed book: {_esc(promo['book'])}</i>",
        parse_mode="HTML",
    )
    context.user_data["_photo_promo"] = promo
    context.user_data["_photo_text"]  = extracted
    # Put the guessed book first so confirming is a single tap
    guess_first = (["DraftKings", "FanDuel"]
                   if promo["book"] == "DraftKings"
                   else ["FanDuel", "DraftKings"])
    kb = ReplyKeyboardMarkup(
        [guess_first], one_time_keyboard=True, resize_keyboard=True,
    )
    await update.message.reply_text(
        "Which book is this promo from? (tap to confirm or flip)",
        reply_markup=kb,
    )
    return ASK_PHOTO_BOOK


async def recv_photo_book(update: Update,
                          context: ContextTypes.DEFAULT_TYPE) -> int:
    """Accept the user's book confirmation for a photo-extracted promo
    and finish adding it."""
    text = (update.message.text or "").strip().lower()
    if text in ("dk", "draftkings"):
        book = "DraftKings"
    elif text in ("fd", "fanduel"):
        book = "FanDuel"
    else:
        await update.message.reply_text(
            "Tap DraftKings or FanDuel."
        )
        return ASK_PHOTO_BOOK

    stashed = context.user_data.pop("_photo_promo", None)
    extracted = context.user_data.pop("_photo_text", None)
    if stashed is None or extracted is None:
        # State got out of sync — bail back to the promo prompt
        await _send_promo_prompt(update, context)
        return ASK_PROMOS

    short = "dk" if book == "DraftKings" else "fd"
    extracted = re.sub(
        r"^(dk|fd|draftkings|fanduel)\b",
        short, extracted, count=1, flags=re.IGNORECASE)

    # Re-parse the corrected string so the label (shown in "Added:" and
    # in the match pipeline) reflects the user's book choice. Fall back
    # to patching the stashed dict if re-parse fails for any reason.
    promo = parse_promo(extracted)
    if promo is None:
        stashed["book"] = book
        promo = stashed

    return await _accept_parsed_promo(update, context, promo, extracted)


async def recv_promo_gamespec(update: Update,
                              context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle the yes/no answer about whether the last-added promo is
    game-specific. Pick will happen after the fetch completes."""
    text = (update.message.text or "").strip().lower()
    promos = context.user_data.get("promos", [])

    if text.startswith("🎯") or text in ("yes", "y", "specific game",
                                          "specific", "game"):
        if promos:
            promos[-1]["game_specific"] = True
            note = " — marked game-specific."
        else:
            note = ""
    else:
        note = ""

    await update.message.reply_text(
        f"OK{note}\n"
        f"Type/send another, /recent for saved, or 'done' to finish.",
        reply_markup=ReplyKeyboardRemove(),
    )
    return ASK_PROMOS


_GAME_PICK_ANY = "any game"
_GAME_PICK_MAX_BUTTONS = 12


async def _start_run(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Phase 1 of /run: fetch odds, send Summary + Play These, then either
    finalize (no game-specific promos) or enter the game-pick flow."""
    chat_id = update.effective_chat.id

    if _run_lock.locked() or context.bot_data.get("running"):
        await update.message.reply_text("A run is already in progress. Wait for it to finish.")
        return ConversationHandler.END

    dk = context.user_data.get("dk_bankroll", 0)
    fd = context.user_data.get("fd_bankroll", 0)
    promos = context.user_data.get("promos", [])
    user_books = context.user_data["books"]
    skip_mode = context.user_data.get("skip_mode", False)

    # Scope-narrowed fetch: /run_promos with concrete promo scope only
    # fetches the sports/markets those promos can match. Cuts Odds API
    # credits from ~30-50 down to as little as ~1 for a targeted promo
    # like "fd 30% hr". scope=None falls through to the full fetch.
    scope = None
    if context.user_data.get("promos_only"):
        from odds_compare import _derive_promo_scope
        scope = _derive_promo_scope(promos)

    # Build status message
    if skip_mode:
        status_line = "Running odds compare (picks only)..."
    else:
        parts = []
        if "DraftKings" in user_books:
            parts.append(f"DK ${dk:,.0f}")
        if "FanDuel" in user_books:
            parts.append(f"FD ${fd:,.0f}")
        bankroll_str = " | ".join(parts)
        n_promos = f" + {len(promos)} promo(s)" if promos else ""
        status_line = f"Running odds compare...\n{bankroll_str}{n_promos}"

    if scope is not None:
        scope_bits = []
        if scope["sports"]:
            scope_bits.append(
                f"{len(scope['sports'])} game-line sport(s)")
        if scope["prop_sport_markets"]:
            n_prop = sum(len(m) for m in scope["prop_sport_markets"].values())
            scope_bits.append(f"{n_prop} prop market(s)")
        if scope["need_hr"]:   scope_bits.append("HR")
        if scope["need_golf"]: scope_bits.append("Golf")
        scope_desc = ", ".join(scope_bits) if scope_bits else "no fetches"
        fetch_msg = f"Scoped fetch: {scope_desc} — should be quick."
    else:
        fetch_msg = "Fetching 8 sports — this takes ~60-90 seconds."

    await update.message.reply_text(
        f"{status_line}\n{fetch_msg}",
        reply_markup=ReplyKeyboardRemove(),
    )

    context.bot_data["running"] = True
    async with _run_lock:
        try:
            fetched = await asyncio.to_thread(
                _run_odds_compare_fetch, dk, fd, scope)
        except Exception as e:
            context.bot_data["running"] = False
            await context.bot.send_message(chat_id, f"Run failed: {e}")
            return ConversationHandler.END

    # Stash for the finalize phase (and for game-pick callbacks)
    context.user_data["fetched"]     = fetched
    context.user_data["chat_id"]     = chat_id
    context.user_data["kelly_fraction"] = get_user_kelly(update)

    # Send Summary + Play These immediately — these don't depend on promos
    try:
        await _send_early_results(context, fetched, user_books, dk, fd, skip_mode)
    except Exception as e:
        context.bot_data["running"] = False
        await context.bot.send_message(chat_id, f"Error sending results: {e}")
        return ConversationHandler.END

    # Determine which game-specific promos still need a game pick
    game_specific = (
        []
        if skip_mode
        else [p for p in promos
              if p.get("game_specific") and not p.get("game_filter")]
    )

    if not game_specific:
        return await _finalize_and_send(update, context)

    context.user_data["game_pick_queue"] = list(game_specific)
    return await _prompt_next_game_pick(update, context)


async def _send_early_results(context: ContextTypes.DEFAULT_TYPE,
                              fetched: dict, user_books: list[str],
                              dk: float, fd: float,
                              skip_mode: bool) -> None:
    """Send Summary + Play These messages right after fetch, before any
    game-pick prompts or promo finalization."""
    from odds_compare import _qualifies, _qualifies_prop

    # /run_promos: user only wants promo-driven output. Skip Summary + Play
    # These entirely — finalize will still send Promo Picks + Parlay Picks.
    if context.user_data.get("promos_only"):
        return

    chat_id = context.user_data["chat_id"]
    counters = fetched["_counters"]
    all_edges_filt = [e for e in fetched["all_edges"] if e["book"] in user_books]

    summary_shape = {
        "all_edges":   all_edges_filt,
        "dk_bankroll": dk,
        "fd_bankroll": fd,
        "summary": {
            "total_games":    counters["total_games"],
            "pinn_anchored":  counters["pinn_anchored"],
            "bovada_matched": counters["bovada_matched"],
            "total_edges":    len(all_edges_filt),
            "an_only":        counters.get("an_only", False),
        },
    }
    await context.bot.send_message(
        chat_id, format_summary(summary_shape, user_books, skip_mode=skip_mode),
        parse_mode="HTML")

    # Play These — game lines + qualifying player props (independent of promos)
    play_edges = [e for e in fetched["all_edges"]
                  if _qualifies(e) and e["book"] in user_books]
    play_edges.extend(e for e in fetched["player_prop_edges"]
                      if _qualifies_prop(e) and e["book"] in user_books)
    play_edges.sort(key=lambda e: e["ev_pct"], reverse=True)

    bankrolls = {}
    if "DraftKings" in user_books:
        bankrolls["DraftKings"] = dk
    if "FanDuel" in user_books:
        bankrolls["FanDuel"] = fd

    user_kf = context.user_data.get("kelly_fraction", DEFAULT_KELLY_FRACTION)
    kelly_scale = user_kf / DEFAULT_KELLY_FRACTION
    for msg in format_play_edges(play_edges, bankrolls, skip_mode=skip_mode,
                                 kelly_scale=kelly_scale):
        for chunk in _split_message(msg):
            await context.bot.send_message(
                chat_id, chunk, parse_mode="HTML",
                disable_web_page_preview=True)


def _match_game_hint(hint: str | None,
                     eligible_games: list[str]) -> str | None:
    """Try to resolve a `game_hint` (e.g. "ATL-PHI") to exactly one of
    the eligible game strings ("Atlanta Braves @ Philadelphia Phillies").
    Returns the matched game on unique match, else None.

    Matching is case-insensitive substring: each short code must appear
    somewhere in the game string (as a prefix of a team name in practice).
    """
    if not hint or "-" not in hint:
        return None
    # Split "TeamA-TeamB" → ["team a", "team b"]; underscores stand for
    # spaces in multi-word names (e.g. "Red_Sox" → "red sox").
    codes = [c.strip().lower().replace("_", " ")
             for c in hint.split("-") if c.strip()]
    if len(codes) < 2:
        return None
    matches = [
        g for g in eligible_games
        if all(code in g.lower() for code in codes)
    ]
    return matches[0] if len(matches) == 1 else None


async def _prompt_next_game_pick(update: Update,
                                 context: ContextTypes.DEFAULT_TYPE) -> int:
    """Pop the next game-specific promo from the queue and send a tap-keyboard
    of eligible games. Falls through to finalize when the queue is empty."""
    from odds_compare import eligible_games_for_promo

    queue: list[dict] = context.user_data.get("game_pick_queue", [])
    if not queue:
        return await _finalize_and_send(update, context)

    fetched = context.user_data["fetched"]
    chat_id = context.user_data["chat_id"]

    # Find the next promo that has at least one eligible game.
    # Use the broader promo_pool (all DK/FD outcomes) so games without a
    # pre-boost +EV edge still surface — the boost may make them viable.
    promo_edges = fetched.get("promo_pool") or fetched["all_edges"]
    while queue:
        promo = queue[0]
        eligible = eligible_games_for_promo(
            promo, promo_edges, fetched["player_prop_edges"],
            hr_edges=fetched.get("hr_edges"))
        if not eligible:
            # No matching games — log + skip
            queue.pop(0)
            await context.bot.send_message(
                chat_id,
                f"<i>No matching games for promo: {_esc(promo['label'])} — skipping.</i>",
                parse_mode="HTML")
            continue

        # Auto-resolve if the vision extractor captured a game hint
        # (e.g. "ATL-PHI") that uniquely matches one eligible game.
        auto = _match_game_hint(promo.get("game_hint"), eligible)
        if auto:
            promo["game_filter"] = auto
            short = auto.replace(" @ ", " vs ")
            if f"[{short}]" not in promo["label"]:
                promo["label"] = promo["label"].rstrip(")") + f" [{short}]"
            queue.pop(0)
            await context.bot.send_message(
                chat_id,
                f"<i>Auto-matched {_esc(promo['game_hint'])} → "
                f"{_esc(auto)}</i>",
                parse_mode="HTML")
            continue

        break  # need manual pick for this promo

    if not queue:
        return await _finalize_and_send(update, context)

    context.user_data["current_promo"]    = promo
    context.user_data["current_eligible"] = eligible

    buttons = [[g] for g in eligible[:_GAME_PICK_MAX_BUTTONS]]
    buttons.append([_GAME_PICK_ANY])
    keyboard = ReplyKeyboardMarkup(
        buttons, one_time_keyboard=True, resize_keyboard=True)

    n_left = len(queue)
    qtag = f" ({n_left} left)" if n_left > 1 else ""
    await context.bot.send_message(
        chat_id,
        f"<b>Game-specific promo</b>{qtag}\n"
        f"Which game does this apply to?\n"
        f"<i>{_esc(promo['label'])}</i>",
        parse_mode="HTML",
        reply_markup=keyboard,
    )
    return ASK_GAME_PICK


async def recv_game_pick(update: Update,
                         context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive the user's game tap (or text) and store it as the promo's
    game_filter, then prompt for the next promo (or finalize)."""
    text     = update.message.text.strip()
    promo    = context.user_data.get("current_promo")
    eligible = context.user_data.get("current_eligible", [])
    queue    = context.user_data.get("game_pick_queue", [])

    if promo is None or not queue:
        return await _finalize_and_send(update, context)

    if text.lower() in (_GAME_PICK_ANY, "any", "skip"):
        # Leave game_filter unset — promo applies across all eligible games.
        queue.pop(0)
        return await _prompt_next_game_pick(update, context)

    # Match against eligible games (case-insensitive exact match)
    match = next((g for g in eligible if g.lower() == text.lower()), None)
    if match is None:
        await update.message.reply_text(
            "Tap one of the buttons (or 'any game').",
            reply_markup=ReplyKeyboardMarkup(
                [[g] for g in eligible[:_GAME_PICK_MAX_BUTTONS]] + [[_GAME_PICK_ANY]],
                one_time_keyboard=True, resize_keyboard=True),
        )
        return ASK_GAME_PICK

    promo["game_filter"] = match
    # Append game tag to the label so it shows in promo picks output
    short = match[:40] + ("…" if len(match) > 40 else "")
    if f"[{short}]" not in promo["label"]:
        promo["label"] = promo["label"].rstrip(")") + f" [{short}]"

    queue.pop(0)
    return await _prompt_next_game_pick(update, context)


async def _finalize_and_send(update: Update,
                             context: ContextTypes.DEFAULT_TYPE) -> int:
    """Phase 2 of /run: apply game filters, write Excel, send promo + parlay
    picks. Then save state and end the conversation."""
    chat_id    = context.user_data["chat_id"]
    fetched    = context.user_data["fetched"]
    promos     = context.user_data.get("promos", [])
    dk         = context.user_data.get("dk_bankroll", 0)
    fd         = context.user_data.get("fd_bankroll", 0)
    user_books = context.user_data["books"]
    skip_mode  = context.user_data.get("skip_mode", False)

    promos_only = context.user_data.get("promos_only", False)
    try:
        result = await asyncio.to_thread(
            _run_odds_compare_finalize, fetched, promos, promos_only)
    except Exception as e:
        context.bot_data["running"] = False
        await context.bot.send_message(chat_id, f"Finalize failed: {e}",
                                       reply_markup=ReplyKeyboardRemove())
        return ConversationHandler.END
    finally:
        context.bot_data["running"] = False

    # Filter promo picks + parlay rows to user's books for display
    if result.get("promo_picks"):
        result["promo_picks"] = [pp for pp in result["promo_picks"]
                                 if pp["promo"]["book"] in user_books]

    bankrolls = {}
    if "DraftKings" in user_books:
        bankrolls["DraftKings"] = dk
    if "FanDuel" in user_books:
        bankrolls["FanDuel"] = fd

    try:
        if not skip_mode:
            user_kf = context.user_data.get(
                "kelly_fraction", DEFAULT_KELLY_FRACTION)
            promo_msg = format_promo_picks(
                result.get("promo_picks", []), bankrolls,
                kelly_fraction=user_kf)
            if promo_msg:
                for chunk in _split_message(promo_msg):
                    await context.bot.send_message(
                        chat_id, chunk, parse_mode="HTML",
                        disable_web_page_preview=True,
                        reply_markup=ReplyKeyboardRemove())

            parlay_rows = result.get("parlay_rows", [])
            if parlay_rows:
                parlay_rows = [r for r in parlay_rows
                               if r["promo"]["book"] in user_books]
            parlay_msg = format_parlay_picks(parlay_rows)
            if parlay_msg:
                for chunk in _split_message(parlay_msg):
                    await context.bot.send_message(
                        chat_id, chunk, parse_mode="HTML",
                        disable_web_page_preview=True)

        # /run_promos skips the Excel write entirely — don't advertise
        # a file that doesn't exist.
        if result.get("output_path"):
            await context.bot.send_message(
                chat_id,
                f"Excel saved: {os.path.basename(result['output_path'])}",
                reply_markup=ReplyKeyboardRemove(),
            )
    except Exception as e:
        await context.bot.send_message(chat_id, f"Error sending results: {e}")

    # Save promo strings and bankrolls for quick-select next time
    if skip_mode:
        return ConversationHandler.END

    uid = str(update.effective_user.id)
    profile = _users.setdefault(uid, {})

    last_bankrolls = profile.get("last_bankrolls", {})
    if "DraftKings" in user_books and dk > 0:
        last_bankrolls["DraftKings"] = dk
    if "FanDuel" in user_books and fd > 0:
        last_bankrolls["FanDuel"] = fd
    profile["last_bankrolls"] = last_bankrolls

    # Note: saved_promos is updated incrementally in _save_promo_string
    # as each promo is added, so no promo-saving work needed here.

    save_users()
    return ConversationHandler.END


def _run_odds_compare_fetch(dk: float, fd: float,
                            scope: dict | None = None) -> dict:
    """Thread-safe wrapper around run_headless_fetch."""
    from odds_compare import run_headless_fetch
    return run_headless_fetch(dk, fd, scope=scope)


def _run_odds_compare_finalize(fetched: dict, promos: list,
                               promos_only: bool = False) -> dict:
    """Thread-safe wrapper around run_headless_finalize."""
    from odds_compare import run_headless_finalize
    return run_headless_finalize(fetched, promos, skip_excel=promos_only)


def _split_message(text: str, limit: int = 4096) -> list[str]:
    """Split a message into chunks that fit Telegram's character limit."""
    if len(text) <= limit:
        return [text]
    chunks = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break
        # Find last newline before limit
        cut = text.rfind("\n", 0, limit)
        if cut == -1:
            cut = limit
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return chunks


# ── Utility commands ─────────────────────────────────────────────────

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    running = context.bot_data.get("running", False)
    if running:
        await update.message.reply_text("A run is currently in progress...")
    else:
        await update.message.reply_text("Idle. Send /run to start.")


async def cmd_kelly(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show or set the invoking user's Kelly fraction."""
    if not is_authorized(update):
        return

    current = get_user_kelly(update)
    args_text = " ".join(context.args).strip() if context.args else ""

    if not args_text:
        await update.message.reply_text(
            f"Your Kelly fraction is <b>{current:g}</b> "
            f"({current * 100:.0f}% of full Kelly).\n\n"
            f"Set a new one with <code>/kelly 0.25</code>, "
            f"<code>/kelly 25%</code>, or <code>/kelly half</code>.\n"
            f"Accepts 0.01–1.0. Shortcuts: full, half, quarter, eighth.",
            parse_mode="HTML",
        )
        return

    new_frac = _parse_kelly_input(args_text)
    if new_frac is None or not (0.01 <= new_frac <= 1.0):
        await update.message.reply_text(
            "Invalid. Try /kelly 0.25, /kelly 25%, or /kelly half. "
            "Must be between 0.01 and 1.0.")
        return

    set_user_kelly(update, new_frac)
    await update.message.reply_text(
        f"Kelly fraction set to <b>{new_frac:g}</b> "
        f"({new_frac * 100:.0f}% of full Kelly). Applies to /run, /run_promos, "
        f"and /quickbet.",
        parse_mode="HTML",
    )


# ── /quickbet — standalone promo calculator ──────────────────────────

async def cmd_quickbet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Promo sizing calculator. Asks for pick, original odds, boosted odds,
    and bankroll, then returns a Kelly-sized stake. No API calls, no Excel,
    no archive — just the math for promos we can't catch via /run."""
    if not is_authorized(update):
        await update.message.reply_text("Unauthorized.")
        return ConversationHandler.END

    context.user_data.clear()
    await update.message.reply_text(
        "<b>Quickbet calculator</b> — no API call, no archive.\n\n"
        "What's the pick? (e.g. <i>Alcaraz ML</i>)",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardRemove(),
    )
    return ASK_QB_PICK


async def recv_qb_pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    pick = (update.message.text or "").strip()
    if not pick:
        await update.message.reply_text("Enter a short description of the pick:")
        return ASK_QB_PICK
    context.user_data["qb_pick"] = pick[:120]
    await update.message.reply_text(
        "Original odds (the fair-value line, pre-boost).\n"
        "American (<code>+150</code>, <code>-110</code>) or decimal (<code>2.50</code>):",
        parse_mode="HTML",
    )
    return ASK_QB_TRUE


async def recv_qb_true(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    dec = _parse_any_odds(update.message.text)
    if dec is None:
        await update.message.reply_text(
            "Invalid. Enter American (+150, -110) or decimal (2.50):")
        return ASK_QB_TRUE
    context.user_data["qb_true_dec"] = dec
    await update.message.reply_text("Boosted odds (after the promo):")
    return ASK_QB_BOOST


async def recv_qb_boost(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    dec = _parse_any_odds(update.message.text)
    if dec is None:
        await update.message.reply_text(
            "Invalid. Enter American (+200) or decimal (3.00):")
        return ASK_QB_BOOST
    true_dec = context.user_data["qb_true_dec"]
    if dec <= true_dec:
        await update.message.reply_text(
            f"Boosted ({dec:.2f}) isn't better than original ({true_dec:.2f}). "
            f"Re-enter the boosted line:")
        return ASK_QB_BOOST
    context.user_data["qb_boost_dec"] = dec
    await update.message.reply_text("Your bankroll ($)?")
    return ASK_QB_BANKROLL


async def recv_qb_bankroll(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    br = _parse_amount(update.message.text)
    if br is None:
        await update.message.reply_text("Enter a valid dollar amount:")
        return ASK_QB_BANKROLL

    from odds_compare import kelly_bet, format_american

    pick      = context.user_data["qb_pick"]
    true_dec  = context.user_data["qb_true_dec"]
    boost_dec = context.user_data["qb_boost_dec"]
    fair_prob = 1 / true_dec
    k_frac    = get_user_kelly(update)

    kelly_pct = kelly_bet(fair_prob, boost_dec, fraction=k_frac)
    stake     = br * kelly_pct
    ev_pct    = (fair_prob * boost_dec - 1) * 100
    true_am   = format_american(true_dec)
    boost_am  = format_american(boost_dec)

    header = (
        f"<b>{_esc(pick)}</b>\n"
        f"{true_am} -&gt; {boost_am}  (dec {true_dec:.2f} -&gt; {boost_dec:.2f})\n"
        f"Edge: <b>{ev_pct:+.2f}%</b>"
    )

    if kelly_pct <= 0:
        msg = (
            f"{header}\n\n"
            f"Kelly says <b>don't bet</b> — the boosted line isn't +EV "
            f"vs the original odds you gave."
        )
    else:
        profit = stake * (boost_dec - 1)
        msg = (
            f"{header}\n\n"
            f"Kelly ({k_frac:g}): {kelly_pct * 100:.2f}% of bankroll\n"
            f"Stake: <b>${stake:.2f}</b> -&gt; +${profit:.2f} if it wins"
        )

    await update.message.reply_text(
        msg, parse_mode="HTML", reply_markup=ReplyKeyboardRemove())
    context.user_data.clear()
    return ConversationHandler.END


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return

    kf = get_user_kelly(update)
    help_text = (
        "EV Edge Finder Bot\n"
        "\n"
        "/run         — Start a new odds compare run\n"
        "/run_single  — Run for a single book of your choosing\n"
        "/run_skip    — Just list the picks (no bankrolls, no promos)\n"
        "/run_promos  — Only show promo picks (skip Summary + Play These)\n"
        "/run_live    — Quick +EV picks on currently live games (for promos)\n"
        "/quickbet    — Kelly calculator for promos (no API call, no archive)\n"
        "/kelly       — Show or set your Kelly fraction\n"
        "/backtest    — Run a backtest on past picks\n"
        "/digest      — Send the last-7-days P&L digest now\n"
        "/books       — View or change your sportsbooks\n"
        "/status      — Check if a run is in progress\n"
        "/help        — Show this message"
    )
    if is_admin(update):
        help_text += "\n/add_user    — Authorize a new user"

    books = get_user_books(update)
    if books:
        help_text += f"\n\nYour books: {', '.join(books)}"
    help_text += f"\nKelly fraction: {kf:g}"

    await update.message.reply_text(help_text)


async def cmd_digest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Build and send the last-7-days P&L digest to the caller."""
    if not is_authorized(update):
        return
    end_dt   = date.today()
    start_dt = end_dt - timedelta(days=6)
    await update.message.reply_text("Building weekly digest…")
    try:
        from backtest import weekly_digest_text
        text = await asyncio.to_thread(weekly_digest_text, start_dt, end_dt)
    except Exception as e:
        log.exception("[digest] on-demand build failed")
        await update.message.reply_text(f"⚠️ Digest failed: {e}")
        return
    await update.message.reply_text(text, parse_mode="HTML")


async def send_daily_backtest(context: ContextTypes.DEFAULT_TYPE):
    """Scheduled job: run a one-day backtest at 3 AM ET covering the day
    that just finished. Pure data-storage — writes
    /data/output/backtest_<yesterday>_<yesterday>.xlsx and stays silent
    on Telegram. The weekly Sunday digest is the only message users see;
    failure here is logged to the bot output, not DM'd, so the admin
    chat doesn't get noise.
    """
    yday = date.today() - timedelta(days=1)
    log.info(f"[daily-backtest] Job firing for {yday}")
    try:
        from backtest import run_full_backtest
        result = await asyncio.to_thread(
            run_full_backtest, yday, yday, "exclude")
        n = len(result.get("primary_picks") or [])
        path = result.get("excel_path")
        log.info(f"[daily-backtest] Done — {n} picks graded, "
                 f"output: {os.path.basename(path) if path else 'no picks'}")
    except Exception:
        log.exception("[daily-backtest] Run failed")


async def send_daily_skip_run(context: ContextTypes.DEFAULT_TYPE):
    """Scheduled job: run /run skip at 10 AM ET as a backup data snapshot.

    Mirrors /run_skip — fetches odds + computes edges with bankroll=0
    (no bet sizing) and writes the ev_edges_*.xlsx file. Silent: no
    Telegram output, errors logged but not DM'd.

    Skipped when:
      • A manual /run is already in progress.
      • Any scan produced an ev_edges_*.xlsx within the last 12 hours
        (i.e. between 10 PM ET yesterday and now). This is the user's
        "if I already ran overnight, don't waste API credits" guard.

    Backtests dedup picks to FIRST appearance (see _dedup_picks), so the
    user's earlier manual runs always win on dedup. This auto-job only
    contributes data on days the user forgot to /run, ensuring the next
    morning's 3 AM backtest always has something to grade.
    """
    if context.bot_data.get("running"):
        log.info("[daily-skip-run] Manual run in progress — skipping.")
        return

    # Skip if there was a scan in the previous 12h. ev_edges_*.xlsx is the
    # canonical artifact of any scan path (manual /run*, prior auto-run);
    # backtest_*.xlsx is excluded since we want SCAN coverage specifically.
    from odds_compare import OUTPUT_DIR, EASTERN
    now_et = datetime.now(EASTERN)
    window_start = now_et - timedelta(hours=12)
    ev_files: list[str] = []
    for pat in (os.path.join(OUTPUT_DIR, "ev_edges_*.xlsx"),
                os.path.join(OUTPUT_DIR, "archive", "ev_edges_*.xlsx")):
        ev_files.extend(glob.glob(pat))
    if ev_files:
        newest_mtime = max(os.path.getmtime(p) for p in ev_files)
        newest_et = datetime.fromtimestamp(
            newest_mtime, tz=timezone.utc).astimezone(EASTERN)
        if newest_et >= window_start:
            log.info(
                f"[daily-skip-run] Recent scan at {newest_et:%Y-%m-%d %H:%M %Z} "
                f"(within 12h) — skipping auto-run to save credits.")
            return

    log.info("[daily-skip-run] Job firing.")
    context.bot_data["running"] = True
    try:
        from odds_compare import run_headless_fetch, run_headless_finalize
        fetched = await asyncio.to_thread(run_headless_fetch, 0, 0)
        result  = await asyncio.to_thread(run_headless_finalize, fetched, None)
        path    = result.get("output_path")
        n_play  = len(result.get("play_edges") or [])
        n_all   = len(result.get("all_edges")  or [])
        log.info(f"[daily-skip-run] Done — {n_play} Play These / {n_all} all-EV "
                 f"picks, output: {os.path.basename(path) if path else 'no file'}")
    except Exception:
        log.exception("[daily-skip-run] Run failed")
    finally:
        context.bot_data["running"] = False


async def capture_closing_lines_job(context: ContextTypes.DEFAULT_TYPE):
    """Scheduled job: capture closing-line odds for any pending pick whose
    commence is within the next 5 minutes. Runs every 60 seconds, silent.

    Picks are seeded into pending_closes_YYYY-MM-DD.json by every scan
    (manual /run, /run_skip backup, etc.). This job is the consumer side
    that re-queries the Odds API per-event endpoint and stamps each pick
    with its close odds + CLV%. See closing_lines.py for the matching
    logic. Idempotent — already-captured picks are skipped.
    """
    try:
        from closing_lines import capture_pending_closes
        stats = await asyncio.to_thread(capture_pending_closes)
    except Exception:
        log.exception("[closing-lines] Capture job failed")
        return
    if stats.get("captured") or stats.get("no_match"):
        log.info("[closing-lines] %d captured, %d no-match",
                 stats["captured"], stats["no_match"])


async def send_weekly_digest(context: ContextTypes.DEFAULT_TYPE):
    """Scheduled job: build the weekly digest and DM every authorized user.

    Window = the 7 days ending yesterday (Mon–Sun when fired Sunday 10 AM ET).
    """
    end_dt   = date.today() - timedelta(days=1)
    start_dt = end_dt - timedelta(days=6)
    log.info(f"[digest] Weekly digest job firing for {start_dt} .. {end_dt}")
    try:
        from backtest import weekly_digest_text
        text = await asyncio.to_thread(weekly_digest_text, start_dt, end_dt)
    except Exception as e:
        log.exception("[digest] Failed to build weekly digest")
        try:
            await context.bot.send_message(
                chat_id=ADMIN_CHAT_ID,
                text=f"⚠️ Weekly digest build failed: {e}",
            )
        except Exception:
            pass
        return

    for uid in list(_users.keys()):
        try:
            await context.bot.send_message(
                chat_id=int(uid), text=text, parse_mode="HTML")
        except Exception as e:
            log.warning(f"[digest] Failed to send to {uid}: {e}")


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    # Clear the run flag so /run isn't blocked if the user cancels mid-flow
    # (e.g. while the game-specific promo picker is open).
    context.bot_data["running"] = False
    await update.message.reply_text(
        "Cancelled.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END


# ── Backtest conversation handlers ───────────────────────────────────

# Backtest period choices: (button label, days back, custom flag)
# A days value of None means "ask for custom start/end dates".
_BT_PERIODS: list[tuple[str, int | None]] = [
    ("Last day",    1),
    ("Last week",   7),
    ("Last month", 30),
    ("Custom range", None),
]


def _bt_period_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[label] for label, _ in _BT_PERIODS],
        one_time_keyboard=True, resize_keyboard=True,
    )


def _parse_bt_period(text: str) -> int | None | str:
    """Return days-back int, None for 'custom', or 'invalid' string."""
    t = text.strip().lower()
    for label, days in _BT_PERIODS:
        if t == label.lower():
            return days
    # Legacy numeric input
    legacy = {"1": 1, "2": 7, "3": 30, "4": None}
    if t in legacy:
        return legacy[t]
    return "invalid"


async def cmd_backtest(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_authorized(update):
        await update.message.reply_text("Unauthorized.")
        return ConversationHandler.END

    if context.bot_data.get("running"):
        await update.message.reply_text("A run is already in progress. Wait for it to finish.")
        return ConversationHandler.END

    user_books = get_user_books(update)
    if not user_books:
        await update.message.reply_text("Set up your books first with /books")
        return ConversationHandler.END

    context.user_data["books"] = user_books
    await update.message.reply_text(
        "Choose a backtest period:",
        reply_markup=_bt_period_keyboard(),
    )
    return ASK_BT_PERIOD


_BT_AN_INCLUDE = "Include AN-only"
_BT_AN_EXCLUDE = "Exclude AN-only (default)"
_BT_AN_ONLY    = "Only AN-only"


def _bt_an_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[_BT_AN_EXCLUDE], [_BT_AN_INCLUDE], [_BT_AN_ONLY]],
        one_time_keyboard=True, resize_keyboard=True,
    )


async def _ask_bt_an_only(update: Update,
                           context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "Include sheets generated when the bot fell back to Action Network "
        "(Odds API quota exhausted)? AN-only days have noisier fair-prob "
        "math, so they're excluded by default.",
        reply_markup=_bt_an_keyboard(),
    )
    return ASK_BT_AN


async def recv_bt_period(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    choice = _parse_bt_period(update.message.text)
    today = date.today()

    if choice == "invalid":
        await update.message.reply_text(
            "Tap one of the options.", reply_markup=_bt_period_keyboard())
        return ASK_BT_PERIOD

    if choice is None:
        await update.message.reply_text(
            "Enter start date (YYYY-MM-DD):",
            reply_markup=ReplyKeyboardRemove(),
        )
        return ASK_BT_START

    context.user_data["bt_start"] = today - timedelta(days=choice)
    context.user_data["bt_end"]   = today
    return await _ask_bt_an_only(update, context)


async def recv_bt_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        start = date.fromisoformat(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("Invalid date. Use YYYY-MM-DD format:")
        return ASK_BT_START
    context.user_data["bt_start"] = start
    await update.message.reply_text("Enter end date (YYYY-MM-DD):")
    return ASK_BT_END


async def recv_bt_end(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        end = date.fromisoformat(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("Invalid date. Use YYYY-MM-DD format:")
        return ASK_BT_END
    context.user_data["bt_end"] = end
    return await _ask_bt_an_only(update, context)


async def recv_bt_an(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.message.text or "").strip()
    if text == _BT_AN_INCLUDE:
        mode = "include"
    elif text == _BT_AN_ONLY:
        mode = "only"
    elif text == _BT_AN_EXCLUDE:
        mode = "exclude"
    else:
        await update.message.reply_text(
            "Tap one of the options.", reply_markup=_bt_an_keyboard())
        return ASK_BT_AN
    context.user_data["bt_an_only_mode"] = mode
    return await _kick_backtest(update, context)


async def _kick_backtest(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id

    if _run_lock.locked():
        await update.message.reply_text("A run is already in progress. Wait for it to finish.")
        return ConversationHandler.END

    start = context.user_data["bt_start"]
    end = context.user_data["bt_end"]
    an_only_mode = context.user_data.get("bt_an_only_mode", "exclude")
    user_books = context.user_data.get("books", ["DraftKings", "FanDuel"])

    an_note = "" if an_only_mode == "exclude" else f" (AN-only: {an_only_mode})"
    await update.message.reply_text(
        f"Running backtest {start} to {end}{an_note}...\n"
        f"Grading picks against ESPN — this may take a minute.",
        reply_markup=ReplyKeyboardRemove(),
    )

    async with _run_lock:
        context.bot_data["running"] = True
        try:
            result = await asyncio.to_thread(
                _run_backtest_wrapper, start, end, an_only_mode)
        except Exception as e:
            context.bot_data["running"] = False
            await context.bot.send_message(chat_id, f"Backtest failed: {e}")
            return ConversationHandler.END
        context.bot_data["running"] = False

    picks      = result.get("play_these_graded", [])
    excel_path = result.get("excel_path")

    # Filter to user's books
    picks = [p for p in picks if p["book"] in user_books]

    msg = format_backtest_results(picks, start, end, user_books)
    for chunk in _split_message(msg):
        await context.bot.send_message(chat_id, chunk, parse_mode="HTML")

    if excel_path and os.path.exists(excel_path):
        with open(excel_path, "rb") as f:
            await context.bot.send_document(
                chat_id,
                document=f,
                filename=os.path.basename(excel_path),
                caption=f"Backtest {start} to {end}",
            )

    return ConversationHandler.END


def _run_backtest_wrapper(start_dt: date, end_dt: date,
                           an_only_mode: str = "exclude") -> dict:
    from backtest import run_full_backtest
    return run_full_backtest(start_dt, end_dt, an_only_mode=an_only_mode)


def format_backtest_results(picks: list[dict], start_dt: date, end_dt: date,
                            user_books: list[str] | None = None) -> str:
    settled = [p for p in picks if p.get("result") in ("WIN", "LOSS", "PUSH")]

    if not settled:
        if not picks:
            return f"No archive files found for {start_dt} to {end_dt}."
        return (
            f"Backtest {start_dt} to {end_dt}\n"
            f"Loaded {len(picks)} picks but none have settled yet."
        )

    wins = [p for p in settled if p["result"] == "WIN"]
    losses = [p for p in settled if p["result"] == "LOSS"]
    pushes = [p for p in settled if p["result"] == "PUSH"]
    pending = [p for p in picks if p.get("result") == "PENDING"]
    total_wagered = sum(p["bet_amount"] for p in settled)
    total_pnl = sum(p["pnl"] for p in settled)
    roi = total_pnl / total_wagered * 100 if total_wagered else 0
    wr = len(wins) / (len(wins) + len(losses)) * 100 if (wins or losses) else 0

    books_str = ""
    if user_books and len(user_books) == 1:
        short = "DK" if user_books[0] == "DraftKings" else "FD"
        books_str = f"  ({short} only)"

    pending_str = f"  |  {len(pending)} pending" if pending else ""
    lines = [
        f"<b>BACKTEST</b>  {start_dt} to {end_dt}{books_str}",
        f"----------------------------",
        f"W{len(wins)} / L{len(losses)} / P{len(pushes)}{pending_str}",
        f"Win Rate: {wr:.1f}%",
        f"Wagered: ${total_wagered:,.2f}",
        f"<b>P&amp;L: ${total_pnl:+,.2f}  |  ROI: {roi:+.1f}%</b>",
    ]

    # Per-book breakdown
    def _agg(picks_, key_fn):
        grp = {}
        for p in picks_:
            k = key_fn(p)
            s = grp.setdefault(k, {"w": 0, "l": 0, "p": 0, "wagered": 0.0, "pnl": 0.0})
            s["wagered"] += p["bet_amount"]
            s["pnl"] += p["pnl"]
            if p["result"] == "WIN": s["w"] += 1
            elif p["result"] == "LOSS": s["l"] += 1
            else: s["p"] += 1
        return grp

    books = _agg(settled, lambda p: "DK" if p["book"] == "DraftKings" else "FD")
    if len(books) > 1:
        lines.append(f"\n<b>BY BOOK</b>")
        for name, s in sorted(books.items(), key=lambda x: x[1]["pnl"], reverse=True):
            broi = s["pnl"] / s["wagered"] * 100 if s["wagered"] else 0
            lines.append(
                f"  {name}:  W{s['w']}/L{s['l']}/P{s['p']}"
                f"  ${s['pnl']:+,.2f}  ({broi:+.1f}%)"
            )

    sports = _agg(settled, lambda p: p["sport"].upper())
    lines.append(f"\n<b>BY SPORT</b>")
    for name, s in sorted(sports.items(), key=lambda x: x[1]["pnl"], reverse=True):
        sroi = s["pnl"] / s["wagered"] * 100 if s["wagered"] else 0
        lines.append(
            f"  {name}:  W{s['w']}/L{s['l']}/P{s['p']}"
            f"  ${s['pnl']:+,.2f}  ({sroi:+.1f}%)"
        )

    return "\n".join(lines)


# ── Main ─────────────────────────────────────────────────────────────

def main():
    validate_required_secrets()
    if not ADMIN_CHAT_ID:
        print("ERROR: Set TELEGRAM_CHAT_ID environment variable.")
        print("  1. Message @userinfobot on Telegram -> /start")
        print("  2. export TELEGRAM_CHAT_ID='your-chat-id'")
        sys.exit(1)

    load_users()
    log.info(f"Loaded {len(_users)} authorized user(s)")

    async def _register_commands(app):
        commands = [
            BotCommand("run",         "Full odds compare (bankrolls + promos)"),
            BotCommand("run_single",   "Run for a single book of your choosing"),
            BotCommand("run_skip",     "Just list the picks (no bankrolls)"),
            BotCommand("run_promos",   "Only promo picks (bankroll prompt; skip Summary + Play These)"),
            BotCommand("run_live",     "Live in-progress games for promo picks"),
            BotCommand("quickbet",     "Kelly calculator for promos (no API, no archive)"),
            BotCommand("kelly",        "Show or set your Kelly fraction"),
            BotCommand("backtest",     "Backtest past picks"),
            BotCommand("digest",       "Send the last-7-days P&L digest now"),
            BotCommand("books",        "Set or change your sportsbooks"),
            BotCommand("status",       "Check if a run is in progress"),
            BotCommand("help",         "Show command list"),
            BotCommand("cancel",       "Cancel the current conversation"),
        ]
        await app.bot.set_my_commands(commands)
        log.info("Registered command menu with Telegram")

    app = (Application.builder()
           .token(TELEGRAM_BOT_TOKEN)
           .post_init(_register_commands)
           .build())

    books_handler = ConversationHandler(
        entry_points=[CommandHandler("books", cmd_books)],
        states={
            ASK_BOOKS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, recv_book_selection),
            ],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
    )

    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("run", cmd_run),
            CommandHandler("run_skip", cmd_run_skip),
            CommandHandler("run_single", cmd_run_single),
            CommandHandler("run_promos", cmd_run_promos),
        ],
        states={
            ASK_RUN_SINGLE_BOOK: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, recv_run_single_book),
            ],
            ASK_DK_BANKROLL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, recv_dk_bankroll),
            ],
            ASK_FD_BANKROLL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, recv_fd_bankroll),
            ],
            ASK_PROMOS: [
                CommandHandler("recent", cmd_recent_promos),
                MessageHandler(filters.TEXT & ~filters.COMMAND, recv_promo),
                MessageHandler(filters.PHOTO, recv_promo_photo),
            ],
            ASK_PHOTO_BOOK: [
                MessageHandler(filters.TEXT & ~filters.COMMAND,
                               recv_photo_book),
            ],
            ASK_PROMO_GAMESPEC: [
                MessageHandler(filters.TEXT & ~filters.COMMAND,
                               recv_promo_gamespec),
            ],
            ASK_GAME_PICK: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, recv_game_pick),
            ],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
    )

    bt_handler = ConversationHandler(
        entry_points=[CommandHandler("backtest", cmd_backtest)],
        states={
            ASK_BT_PERIOD: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, recv_bt_period),
            ],
            ASK_BT_START: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, recv_bt_start),
            ],
            ASK_BT_END: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, recv_bt_end),
            ],
            ASK_BT_AN: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, recv_bt_an),
            ],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
    )

    quickbet_handler = ConversationHandler(
        entry_points=[CommandHandler("quickbet", cmd_quickbet)],
        states={
            ASK_QB_PICK: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, recv_qb_pick),
            ],
            ASK_QB_TRUE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, recv_qb_true),
            ],
            ASK_QB_BOOST: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, recv_qb_boost),
            ],
            ASK_QB_BANKROLL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, recv_qb_bankroll),
            ],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
    )

    app.add_handler(books_handler)
    app.add_handler(conv_handler)
    app.add_handler(bt_handler)
    app.add_handler(quickbet_handler)
    app.add_handler(CommandHandler("run_live", cmd_run_live))
    app.add_handler(CommandHandler("kelly", cmd_kelly))
    app.add_handler(CommandHandler("add_user", cmd_add_user))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("digest", cmd_digest))
    app.add_handler(CommandHandler("help", cmd_help))

    # Weekly P&L digest — Sundays at 10:00 AM America/New_York.
    # Saturday's late NBA/NHL/NCAAB/CFB are all graded on ESPN by then,
    # and it lands before the Sunday NFL early kick at 1 PM ET.
    # Requires python-telegram-bot[job-queue] (APScheduler) — without
    # the extra, app.job_queue is None and we just log a warning.
    if app.job_queue is not None:
        app.job_queue.run_daily(
            send_weekly_digest,
            time=dtime(hour=10, minute=0,
                       tzinfo=ZoneInfo("America/New_York")),
            days=(6,),  # 0=Mon … 6=Sun per datetime.weekday()
            name="weekly_digest",
        )
        log.info("Weekly P&L digest scheduled — Sundays 10:00 AM ET.")

        # Daily one-day backtest — 3:00 AM ET, silent (no Telegram).
        # Just persists yesterday's graded results to /data/output for
        # the dashboard archive. Late evening West Coast games are all
        # final by 2 AM ET and the scrape sources publish within
        # minutes of full time, so 3 AM safely catches everything.
        app.job_queue.run_daily(
            send_daily_backtest,
            time=dtime(hour=3, minute=0,
                       tzinfo=ZoneInfo("America/New_York")),
            name="daily_backtest",
        )
        log.info("Daily backtest scheduled — 3:00 AM ET (yesterday only, silent).")

        # Daily backup /run_skip — 10:00 AM ET, silent (no Telegram).
        # Safety net for days the user doesn't manually /run, so the
        # next morning's 3 AM backtest always has data. 10 AM is late
        # enough that all books have today's lines posted and stable,
        # early enough that no MLB has started (~12-1 PM ET first pitch).
        # Dedup-by-first-appearance means manual runs always take priority
        # over this on days when both ran.
        app.job_queue.run_daily(
            send_daily_skip_run,
            time=dtime(hour=10, minute=0,
                       tzinfo=ZoneInfo("America/New_York")),
            name="daily_skip_run",
        )
        log.info("Daily backup /run_skip scheduled — 10:00 AM ET (silent).")

        # Closing-line capture — every minute, silent. Reads the day's
        # pending_closes_*.json (seeded by each scan) and re-queries the
        # Odds API per-event endpoint for any pick whose commence is in
        # the next 0–5 min, stamping it with the close odds + CLV%.
        # No-op on most ticks (nothing due); ~60-100 credits/day at peak
        # for a typical 50-60 +EV picks/day on the 20k/mo plan.
        app.job_queue.run_repeating(
            capture_closing_lines_job,
            interval=60, first=30,
            name="closing_lines_capture",
        )
        log.info("Closing-line capture scheduled — every 60s (silent).")
    else:
        log.warning("JobQueue unavailable — weekly digest disabled. "
                    "Install with: pip install 'python-telegram-bot[job-queue]'")

    log.info("Bot starting — listening for commands...")
    return app


async def amain():
    """Async entry point: runs the Telegram bot AND the FastAPI webapp
    in the same asyncio loop so they share the process (one Fly machine,
    one container). Long-polling stays primary; the HTTP server runs as
    a concurrent task and shuts down on the same SIGTERM.
    """
    import asyncio
    import uvicorn

    tg_app = main()  # builds the Application but no longer blocks

    # Initialize and start Telegram polling as background tasks.
    await tg_app.initialize()
    await tg_app.start()
    await tg_app.updater.start_polling(allowed_updates=Update.ALL_TYPES)

    # Bring up the FastAPI server in the same loop. Port 8080 is what
    # fly.toml forwards 80/443 to.
    try:
        from webapp import api as webapp_api
        config = uvicorn.Config(
            webapp_api, host="0.0.0.0", port=8080,
            log_level="info", access_log=False,
        )
        server = uvicorn.Server(config)
        log.info("Web app starting on :8080")
        try:
            await server.serve()
        finally:
            log.info("Web app stopped — shutting down Telegram polling")
    except Exception as e:
        log.error(f"Web app crashed: {e}. Bot will keep polling.")
        # If the webapp fails for any reason, keep the bot alive — falling
        # back to a never-ending await keeps long-polling working.
        await asyncio.Event().wait()
    finally:
        try:
            await tg_app.updater.stop()
        except Exception:
            pass
        try:
            await tg_app.stop()
        except Exception:
            pass
        try:
            await tg_app.shutdown()
        except Exception:
            pass


if __name__ == "__main__":
    import asyncio
    asyncio.run(amain())
