"""
Closing-line capture worker.

Reads pending_closes_YYYY-MM-DD.json files written by
odds_compare.write_pending_closes_sidecar(), and for each pick whose
commence is within the next ~5 minutes, fetches the current odds from the
Odds API per-event endpoint and records the result as the "close" — the
de-facto closing line for that pick.

CLV (closing-line value) is the gold-standard +EV metric for sports
betting: it converges on whether your model has a real edge much faster
than win/loss does (variance washes out CLV in dozens of bets, but takes
hundreds of bets to wash out of W/L). A consistently positive CLV means
you're systematically beating the market regardless of short-run results.

Scheduled by telegram_bot.py to run every minute. Idempotent — picks
already captured are skipped.
"""
import glob
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Iterable

import requests

from bot_config import ODDS_API_KEY
from odds_compare import (
    ODDS_API_BASE,
    ODDS_API_BOOKMAKERS,
    ODDS_API_PROP_MARKETS,
    OUTPUT_DIR,
    EASTERN,
    american_to_decimal,
    decimal_to_implied_prob,
)

log = logging.getLogger(__name__)

# Capture window: pick is "due" when commence is within this many minutes
# from now. We aim for the last few minutes pre-game — the standard
# sportsbook-industry definition of the close. We don't fetch after
# commence (negative lead) because in-play prices are not closing lines.
CAPTURE_LEAD_MINUTES_MIN = 0      # don't fetch after commence
CAPTURE_LEAD_MINUTES_MAX = 5      # fetch up to 5 min before commence


# Reverse map: internal market key → Odds API market key.
# h2h/spreads/totals are passed through unchanged. Prop keys come from
# ODDS_API_PROP_MARKETS in odds_compare.py.
INTERNAL_TO_API_MARKET: dict[str, str] = {
    "h2h": "h2h", "spreads": "spreads", "totals": "totals",
}
for _sport_markets in ODDS_API_PROP_MARKETS.values():
    for _api_key, _internal_key, _display in _sport_markets:
        INTERNAL_TO_API_MARKET[_internal_key] = _api_key


# Display-name → Odds API canonical book key.
# Picks store DraftKings/FanDuel as the display name; the API returns
# "draftkings"/"fanduel" as bookmaker keys.
BOOK_DISPLAY_TO_KEY = {
    "DraftKings": "draftkings",
    "FanDuel":    "fanduel",
}


def _list_recent_pending_files(output_dir: str) -> list[str]:
    """Pending files for today + yesterday (ET).

    Yesterday is included so a midnight-crossing capture window
    (e.g. an 11:55 PM ET first pitch) doesn't get missed.
    """
    today_et = datetime.now(EASTERN).date()
    paths = []
    for offset in (0, -1):
        d = today_et + timedelta(days=offset)
        p = os.path.join(output_dir, f"pending_closes_{d.isoformat()}.json")
        if os.path.exists(p):
            paths.append(p)
    return paths


def _load_pending(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        log.warning("[closing-lines] could not read %s: %s", path, e)
        return {}


def _save_pending(path: str, payload: dict) -> None:
    """Atomic write via tmp + rename so a crash mid-write can't corrupt
    the day's capture log."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    os.replace(tmp, path)


def _due_for_capture(pick: dict, now_utc: datetime) -> bool:
    if pick.get("close") is not None:
        return False
    commence = pick.get("commence")
    if not commence:
        return False
    try:
        commence_dt = datetime.fromisoformat(commence.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return False
    minutes_to_commence = (commence_dt - now_utc).total_seconds() / 60
    return CAPTURE_LEAD_MINUTES_MIN <= minutes_to_commence <= CAPTURE_LEAD_MINUTES_MAX


def _outcome_matches(pick: dict, outcome: dict, market_api: str) -> bool:
    """Does this Odds API outcome correspond to our logged pick?"""
    pick_outcome = (pick.get("outcome") or "").strip()
    pick_point = pick.get("point")

    api_point = outcome.get("point")
    if pick_point is not None:
        if api_point is None:
            return False
        # Same 0.5-bucket tolerance as _make_pick_key uses
        if abs(float(api_point) - float(pick_point)) > 0.25:
            return False

    name = (outcome.get("name") or "").strip()
    desc = (outcome.get("description") or "").strip()

    # Game lines: outcome.name is the team name (h2h/spreads) or "Over"/"Under" (totals)
    if market_api in ("h2h", "spreads", "totals"):
        return name.lower() == pick_outcome.lower()

    # Player props: pick_outcome is "{player} O/U" (or Y/N for binary).
    # API returns description=player, name="Over"|"Under" (or "Yes"|"No").
    parts = pick_outcome.rsplit(" ", 1)
    if len(parts) != 2:
        return False
    pick_player, pick_side = parts
    pick_side = pick_side.upper()
    api_side = name.lower()
    side_match = (
        (pick_side in ("O", "Y") and api_side in ("over", "yes")) or
        (pick_side in ("U", "N") and api_side in ("under", "no"))
    )
    return side_match and desc.lower() == pick_player.lower()


def _fetch_event_odds(sport_key: str, event_id: str,
                       markets: Iterable[str]) -> dict | None:
    """GET /sports/{sport}/events/{event_id}/odds for a market set.
    Returns parsed JSON or None on failure."""
    if not ODDS_API_KEY:
        log.error("[closing-lines] ODDS_API_KEY not set — cannot fetch")
        return None
    markets_str = ",".join(sorted(set(markets)))
    if not markets_str:
        return None
    try:
        r = requests.get(
            f"{ODDS_API_BASE}/sports/{sport_key}/events/{event_id}/odds",
            params={
                "apiKey":     ODDS_API_KEY,
                "regions":    "us",
                "markets":    markets_str,
                "oddsFormat": "american",
                "bookmakers": ODDS_API_BOOKMAKERS,
            },
            timeout=15,
        )
    except requests.RequestException as e:
        log.warning("[closing-lines] event %s fetch error: %s", event_id, e)
        return None
    if r.status_code != 200:
        log.info("[closing-lines] event %s HTTP %s", event_id, r.status_code)
        return None
    return r.json()


def compute_clv_pct(open_implied_pct: float | None,
                     close_implied_pct: float) -> float | None:
    """CLV%: how much your implied probability beat the close.

    Positive = your odds were better than the close (good — you got value
    the market eventually corrected toward). Negative = the line moved
    against you, suggesting the model overrated this side.

      clv_pct = (open_implied - close_implied) / close_implied × 100
    """
    if not isinstance(open_implied_pct, (int, float)) or close_implied_pct <= 0:
        return None
    return round((open_implied_pct - close_implied_pct) / close_implied_pct * 100, 3)


def _build_close_record(pick: dict, outcome: dict, now_utc: datetime) -> dict:
    """Construct the `close` dict to attach to a pick once matched."""
    close_am = int(outcome["price"])
    close_dec = american_to_decimal(close_am)
    close_implied = decimal_to_implied_prob(close_dec) * 100
    clv = compute_clv_pct(pick.get("open_implied_pct"), close_implied)

    minutes_before = None
    commence = pick.get("commence")
    if commence:
        try:
            commence_dt = datetime.fromisoformat(commence.replace("Z", "+00:00"))
            minutes_before = round(
                (commence_dt - now_utc).total_seconds() / 60, 2)
        except (TypeError, ValueError):
            pass

    return {
        "odds_american":           close_am,
        "implied_pct":             round(close_implied, 1),
        "clv_pct":                 clv,
        "captured_at":             now_utc.isoformat(),
        "minutes_before_commence": minutes_before,
    }


def _capture_close_for_pick(pick: dict, event_data: dict,
                              now_utc: datetime) -> dict | None:
    """Locate this pick's outcome in the event's odds payload; build close
    record if found, else None."""
    market_internal = (pick.get("market") or "").lower()
    market_api = INTERNAL_TO_API_MARKET.get(market_internal, market_internal)
    book_key = BOOK_DISPLAY_TO_KEY.get(pick.get("book", ""))
    if not book_key:
        return None

    for bm in event_data.get("bookmakers", []):
        if bm.get("key") != book_key:
            continue
        for mkt in bm.get("markets", []):
            if mkt.get("key") != market_api:
                continue
            for outcome in mkt.get("outcomes", []):
                if not _outcome_matches(pick, outcome, market_api):
                    continue
                try:
                    int(outcome.get("price"))   # validate parseable
                except (TypeError, ValueError):
                    continue
                return _build_close_record(pick, outcome, now_utc)
    return None


def capture_pending_closes(output_dir: str | None = None) -> dict:
    """
    Capture closing-line odds for any pending pick whose commence is in the
    capture window. Updates pending_closes_*.json in place.

    Idempotent — picks already captured are skipped. Returns a stats dict
    so the scheduler can log activity:
        {"captured": int, "no_match": int, "scanned": int, "files": list}
    """
    output_dir = output_dir or OUTPUT_DIR
    now_utc = datetime.now(timezone.utc)
    files = _list_recent_pending_files(output_dir)
    stats = {"captured": 0, "no_match": 0, "scanned": 0, "files": files}

    for path in files:
        payload = _load_pending(path)
        picks = payload.get("picks", {})
        if not picks:
            continue

        # Group due picks by (sport_key, event_id) so we issue one API
        # call per event covering every market we need for it.
        batches: dict[tuple, dict] = {}
        for key, pick in picks.items():
            stats["scanned"] += 1
            if not _due_for_capture(pick, now_utc):
                continue
            sport_key = pick.get("sport_key")
            event_id = pick.get("event_id")
            if not sport_key or not event_id:
                continue
            market_internal = (pick.get("market") or "").lower()
            market_api = INTERNAL_TO_API_MARKET.get(
                market_internal, market_internal)
            entry = batches.setdefault((sport_key, event_id),
                                        {"markets": set(), "pick_keys": []})
            entry["markets"].add(market_api)
            entry["pick_keys"].append(key)

        if not batches:
            continue

        for (sport_key, event_id), batch in batches.items():
            event_data = _fetch_event_odds(sport_key, event_id,
                                            batch["markets"])
            if event_data is None:
                continue
            for pick_key in batch["pick_keys"]:
                pick = picks[pick_key]
                close = _capture_close_for_pick(pick, event_data, now_utc)
                if close is not None:
                    pick["close"] = close
                    stats["captured"] += 1
                else:
                    stats["no_match"] += 1

        _save_pending(path, payload)

    if stats["captured"] or stats["no_match"]:
        log.info(
            "[closing-lines] captured=%d no_match=%d scanned=%d files=%d",
            stats["captured"], stats["no_match"],
            stats["scanned"], len(files))
    return stats
