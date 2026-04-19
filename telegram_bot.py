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
    /backtest    — Run a backtest on past picks
    /books       — View or change your sportsbook selection
    /add_user    — (Admin) Authorize a new user
    /status      — Check if a run is in progress
    /help        — Show usage instructions
"""

import asyncio
import json
import logging
import re
import sys
import os
from datetime import datetime, date, timedelta

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
from bot_config import TELEGRAM_BOT_TOKEN, ADMIN_CHAT_ID, USERS_FILE

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)

# ── Conversation states ──────────────────────────────────────────────
ASK_DK_BANKROLL, ASK_FD_BANKROLL, ASK_PROMOS, ASK_GAME_PICK = range(4)
ASK_BT_PERIOD, ASK_BT_START, ASK_BT_END = range(10, 13)
ASK_BOOKS = 20
ASK_RUN_SINGLE_BOOK = 21
ASK_PROMO_GAMESPEC = 22

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
    "soccer": "soccer", "epl":    "soccer",
    "ufc": "ufc",   "mma":        "ufc",
    "pga": "pga",   "golf":       "pga",
}

# Two-word sport phrases checked with one-token lookahead.
PROMO_SPORT_PHRASES = {
    "ice hockey":         "nhl",
    "premier league":     "soccer",
    "college basketball": "ncaab",
    "college football":   "ncaaf",
    "american football":  "nfl",
}

PROMO_MARKET_KEYWORDS = {
    "ml": "h2h", "moneyline": "h2h", "h2h": "h2h",
    "spreads": "spreads", "spread": "spreads", "ats": "spreads",
    "totals": "totals", "total": "totals", "ou": "totals", "o/u": "totals",
    "hr": "hr", "homerun": "hr", "homeruns": "hr",
    "strikeouts": "strikeouts", "ks": "strikeouts",
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
        "sgp":           False,
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

        # game (game-specific)
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
        parts.append(f"/ {promo['sport'].upper()}")
    if promo["market"] != "all":
        parts.append(f"/ {promo['market'].capitalize()}")
    if promo["min_odds"] is not None:
        parts.append(f"(min {promo['min_odds']:+d})")
    if promo["max_bet"] is not None:
        parts.append(f"(max ${promo['max_bet']:.0f})")
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

    lines = [
        f"<b>ODDS COMPARE</b>  {now}",
        f"----------------------------",
        f"{s['total_games']} games | {s['total_edges']} +EV edges",
        " | ".join(edge_parts),
    ]
    if not skip_mode:
        lines.append("")
        if "DraftKings" in user_books:
            lines.append(f"DK bankroll: ${result['dk_bankroll']:,.2f}")
        if "FanDuel" in user_books:
            lines.append(f"FD bankroll: ${result['fd_bankroll']:,.2f}")
    return "\n".join(lines)


def format_play_edges(play_edges: list[dict], bankrolls: dict,
                      skip_mode: bool = False) -> list[str]:
    """
    Format "Play These" edges into Telegram messages (one per book).
    Returns a list of message strings. In skip_mode, omits bet amounts.
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
            bet = clamp_bet(br * edge["kelly_pct"] / 100, edge["book"])
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


def format_promo_picks(promo_picks: list[dict], bankrolls: dict) -> str | None:
    """Format promo picks into a single message."""
    if not promo_picks:
        return None

    from odds_compare import (apply_profit_boost, calc_ev, format_american,
                              kelly_bet, clamp_bet)

    lines = ["<b>PROMO PICKS</b>"]

    for pp in promo_picks:
        promo = pp["promo"]
        edge = pp["edge"]

        lines.append(f"\n<b>{_esc(promo['label'])}</b>")

        if edge is None:
            lines.append("  No qualifying pick found")
            continue

        pick_str = _esc(edge["outcome"])
        pt = _format_point(edge.get("point"), edge.get("market", ""))
        if pt:
            pick_str += f" {pt}"
        odds_str = _format_american(edge["odds_american"])
        game = _esc(edge["game"])

        lines.append(f"  {game} | {pick_str}")

        if promo.get("promo_type") == "bonus_bet":
            amt = promo.get("bet_amount", 0)
            exp_profit = (edge["fair_prob"] / 100) * amt * (edge["odds_decimal"] - 1)
            lines.append(
                f"  {odds_str} | Bonus ${amt:.0f} | ~${exp_profit:.2f} expected"
            )
        else:
            boost_dec = apply_profit_boost(
                edge["odds_decimal"], promo.get("boost_pct", 0))
            fair_prob = edge["fair_prob"] / 100
            boosted_ev = calc_ev(fair_prob, boost_dec)
            boosted_odds = format_american(boost_dec)
            boosted_k = kelly_bet(fair_prob, boost_dec)
            br = bankrolls.get(edge["book"], 0)
            bet = clamp_bet(br * boosted_k, edge["book"])
            if promo.get("max_bet") and bet > promo["max_bet"]:
                bet = promo["max_bet"]
            profit = bet * (boost_dec - 1)
            lines.append(
                f"  {odds_str} -&gt; {boosted_odds} | "
                f"EV {edge['ev_pct']}% -&gt; {boosted_ev:.1f}%"
            )
            lines.append(
                f"  Bet ${bet:.2f} -&gt; +${profit:.2f}"
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
        lines.append(
            f"\n<b>{_esc(promo['label'])} ({row['num_legs']} legs)</b>"
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

    Final button reads 'done' once any promos have been entered, 'skip' before.
    """
    uid = str(update.effective_user.id)
    profile = _users.get(uid, {})
    saved = profile.get("saved_promos", [])
    if not saved:
        return None
    # Build rows of 1 button each (promo strings can be long)
    buttons = [[p] for p in saved[:6]]
    has_entries = bool(context and context.user_data.get("promos"))
    buttons.append(["done" if has_entries else "skip"])
    return ReplyKeyboardMarkup(buttons, one_time_keyboard=True,
                               resize_keyboard=True)


async def _send_promo_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send the promo prompt, with saved promos as quick-tap buttons if available."""
    keyboard = _promo_keyboard(update, context)

    if keyboard:
        text = (
            "<b>Promos</b> — tap a saved one, type a new one, "
            "or <b>skip</b>/<b>done</b>."
        )
    else:
        text = (
            "<b>Promos</b> — one per message, or <b>skip</b>/<b>done</b>.\n\n"
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
            msg += f"\n<i>Last: ${last_bankrolls['DraftKings']:,.0f}</i>"
        await update.message.reply_text(msg, parse_mode="HTML")
        return ASK_DK_BANKROLL
    else:
        context.user_data["dk_bankroll"] = 0
        msg = "Enter your FanDuel bankroll ($):"
        if last_bankrolls.get("FanDuel"):
            msg += f"\n<i>Last: ${last_bankrolls['FanDuel']:,.0f}</i>"
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
            msg += f"\n<i>Last: ${last_bankrolls['DraftKings']:,.0f}</i>"
        await update.message.reply_text(msg, parse_mode="HTML")
        return ASK_DK_BANKROLL
    else:
        # FD only — skip DK
        context.user_data["dk_bankroll"] = 0
        msg = "Enter your FanDuel bankroll ($):"
        if last_bankrolls.get("FanDuel"):
            msg += f"\n<i>Last: ${last_bankrolls['FanDuel']:,.0f}</i>"
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
            msg += f"\n<i>Last: ${last_fd:,.0f}</i>"
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

    # Saved-promo keyboard (None if the user has no saved promos)
    saved_kb = _promo_keyboard(update, context)

    promo = parse_promo(update.message.text)
    if promo is None:
        await update.message.reply_text(
            "Couldn't parse that promo. Try again or tap/type 'done'/'skip'.",
            reply_markup=saved_kb or ReplyKeyboardRemove(),
        )
        return ASK_PROMOS

    # Reject promos for books the user doesn't have
    user_books = context.user_data["books"]
    if promo["book"] not in user_books:
        await update.message.reply_text(
            f"You don't have {promo['book']} selected. "
            f"Use /books to change. Try again or tap/type 'done'.",
            reply_markup=saved_kb or ReplyKeyboardRemove(),
        )
        return ASK_PROMOS

    context.user_data["promos"].append(promo)
    context.user_data.setdefault("promo_strings", []).append(
        update.message.text.strip())

    # If the promo string already included "game", skip the follow-up prompt.
    if promo.get("game_specific"):
        await update.message.reply_text(
            f"Added: {promo['label']} — marked game-specific.\n"
            f"Tap another saved promo, type a new one, or tap/type 'done'.",
            reply_markup=saved_kb or ReplyKeyboardRemove(),
        )
        return ASK_PROMOS

    # Ask whether this boost applies to a single game (e.g. SGP boost,
    # "bet $50 on Arsenal vs Man City"). Always asked so the option is
    # always visible to the user.
    kb = ReplyKeyboardMarkup(
        [[_GAMESPEC_YES], [_GAMESPEC_NO]],
        one_time_keyboard=True, resize_keyboard=True,
    )
    await update.message.reply_text(
        f"Added: {promo['label']}\n\n"
        f"Is this boost for a <b>specific game</b> (e.g. SGP or "
        f"game-only parlay boost)?",
        parse_mode="HTML",
        reply_markup=kb,
    )
    return ASK_PROMO_GAMESPEC


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

    saved_kb = _promo_keyboard(update, context)
    await update.message.reply_text(
        f"OK{note}\n"
        f"Tap another saved promo, type a new one, or tap/type 'done'.",
        reply_markup=saved_kb or ReplyKeyboardRemove(),
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

    await update.message.reply_text(
        f"{status_line}\nFetching 8 sports — this takes ~60-90 seconds.",
        reply_markup=ReplyKeyboardRemove(),
    )

    context.bot_data["running"] = True
    async with _run_lock:
        try:
            fetched = await asyncio.to_thread(_run_odds_compare_fetch, dk, fd)
        except Exception as e:
            context.bot_data["running"] = False
            await context.bot.send_message(chat_id, f"Run failed: {e}")
            return ConversationHandler.END

    # Stash for the finalize phase (and for game-pick callbacks)
    context.user_data["fetched"]     = fetched
    context.user_data["chat_id"]     = chat_id

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

    bankrolls = {}
    if "DraftKings" in user_books:
        bankrolls["DraftKings"] = dk
    if "FanDuel" in user_books:
        bankrolls["FanDuel"] = fd

    for msg in format_play_edges(play_edges, bankrolls, skip_mode=skip_mode):
        for chunk in _split_message(msg):
            await context.bot.send_message(
                chat_id, chunk, parse_mode="HTML",
                disable_web_page_preview=True)


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
            promo, promo_edges, fetched["player_prop_edges"])
        if eligible:
            break
        # No matching games — log + skip
        queue.pop(0)
        await context.bot.send_message(
            chat_id,
            f"<i>No matching games for promo: {_esc(promo['label'])} — skipping.</i>",
            parse_mode="HTML")

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

    try:
        result = await asyncio.to_thread(
            _run_odds_compare_finalize, fetched, promos)
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
            promo_msg = format_promo_picks(result.get("promo_picks", []), bankrolls)
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

    promo_strings = context.user_data.get("promo_strings", [])
    if promo_strings:
        seen = set()
        unique = []
        for ps in promo_strings:
            key = ps.lower()
            if key not in seen:
                seen.add(key)
                unique.append(ps)
        old = profile.get("saved_promos", [])
        for op in old:
            if op.lower() not in seen:
                seen.add(op.lower())
                unique.append(op)
        profile["saved_promos"] = unique[:6]

    save_users()
    return ConversationHandler.END


def _run_odds_compare_fetch(dk: float, fd: float) -> dict:
    """Thread-safe wrapper around run_headless_fetch."""
    from odds_compare import run_headless_fetch
    return run_headless_fetch(dk, fd)


def _run_odds_compare_finalize(fetched: dict, promos: list) -> dict:
    """Thread-safe wrapper around run_headless_finalize."""
    from odds_compare import run_headless_finalize
    return run_headless_finalize(fetched, promos)


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


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return

    help_text = (
        "EV Edge Finder Bot\n"
        "\n"
        "/run         — Start a new odds compare run\n"
        "/run_single  — Run for a single book of your choosing\n"
        "/run_skip    — Just list the picks (no bankrolls, no promos)\n"
        "/run_promos  — Only show promo picks (skip Summary + Play These)\n"
        "/run_live    — Quick +EV picks on currently live games (for promos)\n"
        "/backtest    — Run a backtest on past picks\n"
        "/books       — View or change your sportsbooks\n"
        "/status      — Check if a run is in progress\n"
        "/help        — Show this message"
    )
    if is_admin(update):
        help_text += "\n/add_user    — Authorize a new user"

    books = get_user_books(update)
    if books:
        help_text += f"\n\nYour books: {', '.join(books)}"

    await update.message.reply_text(help_text)


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
    return await _kick_backtest(update, context)


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
    return await _kick_backtest(update, context)


async def _kick_backtest(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id

    if _run_lock.locked():
        await update.message.reply_text("A run is already in progress. Wait for it to finish.")
        return ConversationHandler.END

    start = context.user_data["bt_start"]
    end = context.user_data["bt_end"]
    user_books = context.user_data.get("books", ["DraftKings", "FanDuel"])

    await update.message.reply_text(
        f"Running backtest {start} to {end}...\n"
        f"Grading picks against ESPN — this may take a minute."
    )

    async with _run_lock:
        context.bot_data["running"] = True
        try:
            picks = await asyncio.to_thread(_run_backtest_wrapper, start, end)
        except Exception as e:
            context.bot_data["running"] = False
            await context.bot.send_message(chat_id, f"Backtest failed: {e}")
            return ConversationHandler.END
        context.bot_data["running"] = False

    # Filter to user's books
    picks = [p for p in picks if p["book"] in user_books]

    msg = format_backtest_results(picks, start, end, user_books)
    for chunk in _split_message(msg):
        await context.bot.send_message(chat_id, chunk, parse_mode="HTML")

    return ConversationHandler.END


def _run_backtest_wrapper(start_dt: date, end_dt: date) -> list[dict]:
    from backtest import load_archive_picks, run_backtest
    picks = load_archive_picks(start_dt, end_dt)
    if not picks:
        return []
    return run_backtest(picks)


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
    if not TELEGRAM_BOT_TOKEN:
        print("ERROR: Set TELEGRAM_BOT_TOKEN environment variable.")
        print("  1. Message @BotFather on Telegram -> /newbot")
        print("  2. export TELEGRAM_BOT_TOKEN='your-token-here'")
        sys.exit(1)
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
            BotCommand("backtest",     "Backtest past picks"),
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
                MessageHandler(filters.TEXT & ~filters.COMMAND, recv_promo),
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
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
    )

    app.add_handler(books_handler)
    app.add_handler(conv_handler)
    app.add_handler(bt_handler)
    app.add_handler(CommandHandler("run_live", cmd_run_live))
    app.add_handler(CommandHandler("add_user", cmd_add_user))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("help", cmd_help))

    log.info("Bot starting — listening for commands...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
