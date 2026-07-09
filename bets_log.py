"""
bets_log.py — the bets the user ACTUALLY placed, vs the sheet's picks.

Every backtest so far grades the sheet — every qualifying pick, at the
recommended stake, whether or not a human ever bet it. This log closes
that gap: /placed records which picks were really placed (at the sheet's
stake), the nightly job grades them, and /mybets reports the user's own
W/L, P&L, ROI and realized CLV. Same storage conventions as sgp_log:
JSONL on the persistent volume, stdlib-only at import time, backtest
imported lazily inside grade_pending().

Entry shape:
    ts            ISO timestamp (UTC) when logged
    source        "play" | "parlay"
    book          "DraftKings" | "FanDuel"
    stake         dollars actually at risk
    decimal       combined payout decimal at placement (parlays: boosted)
    boost_pct     parlay boost, 0 for straights
    label         human-readable pick description
    legs          [sgp_log.leg_from_edge dicts + "pick_key" each]
    result        None until graded → WIN/LOSS/PUSH/UNGRADABLE
    leg_results   per-leg verdicts once graded
    pnl           dollars, set at grade time (None until then)
"""
import json
import logging
import os
from datetime import datetime, timedelta, timezone

# Same distilled leg shape + parlay settlement rules as the SGP slip log.
from sgp_log import leg_from_edge, _joint_result, _GAME_LINE_MARKETS

log = logging.getLogger(__name__)

_DATA_DIR = os.environ.get("DATA_DIR") or os.path.dirname(os.path.abspath(__file__))
STORE = os.path.join(_DATA_DIR, "placed_bets.jsonl")

_UNGRADABLE_AFTER_DAYS = 14


def log_placed(bets: list[dict]) -> int:
    """Append placed-bet records. Returns how many were written.
    Never raises — a lost log line must not break /placed."""
    written = 0
    try:
        with open(STORE, "a", encoding="utf-8") as f:
            for b in bets:
                b = {"ts": datetime.now(timezone.utc).isoformat(),
                     "result": None, "leg_results": None, "pnl": None, **b}
                f.write(json.dumps(b) + "\n")
                written += 1
    except Exception:
        log.exception("[bets-log] failed to append")
    return written


def _load() -> list[dict]:
    if not os.path.exists(STORE):
        return []
    bets = []
    with open(STORE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                bets.append(json.loads(line))
            except json.JSONDecodeError:
                log.warning("[bets-log] skipping corrupt line")
    return bets


def _save(bets: list[dict]) -> None:
    tmp = STORE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for b in bets:
            f.write(json.dumps(b) + "\n")
    os.replace(tmp, STORE)


def _settle_pnl(bet: dict) -> float | None:
    """P&L for a graded bet. Pushed legs drop off a parlay and the ticket
    reprices from the remaining legs' product (boost re-applied); a fully
    pushed ticket refunds. The haircut baked into an SGP's stored decimal
    is NOT recoverable per-leg, so push-repricing there is approximate —
    acceptable: pushes on multi-leg tickets are rare."""
    result = bet.get("result")
    stake = bet.get("stake") or 0
    if result == "PUSH":
        return 0.0
    if result == "LOSS":
        return round(-stake, 2)
    if result != "WIN":
        return None
    legs = bet.get("legs") or []
    leg_results = bet.get("leg_results") or []
    if len(legs) > 1 and "PUSH" in leg_results:
        dec = 1.0
        for leg, r in zip(legs, leg_results):
            if r != "PUSH":
                dec *= leg.get("decimal") or 1.0
        dec = 1 + (dec - 1) * (1 + (bet.get("boost_pct") or 0) / 100)
    else:
        dec = bet.get("decimal") or 1.0
    return round(stake * (dec - 1), 2)


def grade_pending() -> dict:
    """Grade every pending placed bet whose games may have finished.
    Same machinery as sgp_log.grade_pending. Returns counter dict."""
    bets = _load()
    pending = [b for b in bets if b.get("result") is None]
    stats = {"graded": 0, "pending": 0, "ungradable": 0}
    if not pending:
        return stats

    import backtest  # lazy — heavy import, network on grade

    for bet in pending:
        picks = []
        for leg in bet.get("legs", []):
            market = (leg.get("market") or "").lower()
            picks.append({
                "sport":      leg.get("sport") or "",
                "game":       leg.get("game") or "",
                "game_date":  leg.get("game_date"),
                "game_time":  "",
                "market":     (market.upper() if market in _GAME_LINE_MARKETS
                               else market),
                "pick":       leg.get("pick") or "",
                "line":       leg.get("line"),
                "is_prop":    bool(leg.get("is_prop")),
                "player":     leg.get("player"),
                "prop_side":  leg.get("prop_side"),
                "decimal":    leg.get("decimal") or 1.0,
                "bet_amount": 1.0,
            })
        try:
            graded = backtest.run_backtest(picks)
        except Exception:
            log.exception("[bets-log] grading failed for %s", bet.get("ts"))
            continue

        leg_results = [p.get("result") or "UNKNOWN" for p in graded]
        bet["leg_results"] = leg_results
        joint = _joint_result(leg_results)
        if joint is not None:
            bet["result"] = joint
            bet["pnl"] = _settle_pnl(bet)
            bet["graded_at"] = datetime.now(timezone.utc).isoformat()
            stats["graded"] += 1
            continue

        newest = max((leg.get("game_date") or "" for leg in bet["legs"]),
                     default="")
        try:
            newest_dt = datetime.strptime(newest, "%Y%m%d").date()
            expired = (datetime.now(timezone.utc).date() - newest_dt
                       > timedelta(days=_UNGRADABLE_AFTER_DAYS))
        except ValueError:
            expired = True
        if expired:
            bet["result"] = "UNGRADABLE"
            bet["graded_at"] = datetime.now(timezone.utc).isoformat()
            stats["ungradable"] += 1
        else:
            stats["pending"] += 1

    _save(bets)
    return stats


def summary(days: int = 7) -> dict:
    """Stats over placed bets from the last `days` days (by log time).
    Grading is the caller's job (call grade_pending first). CLV is
    averaged over legs whose close was captured by the worker."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    window = []
    for b in _load():
        try:
            ts = datetime.fromisoformat(b.get("ts", ""))
        except ValueError:
            continue
        if ts >= cutoff:
            window.append(b)

    settled = [b for b in window if b.get("result") in ("WIN", "LOSS", "PUSH")]
    wins    = [b for b in settled if b["result"] == "WIN"]
    losses  = [b for b in settled if b["result"] == "LOSS"]
    staked  = sum(b.get("stake") or 0 for b in settled
                  if b["result"] != "PUSH")
    pnl     = sum(b.get("pnl") or 0 for b in settled)

    # Realized CLV — look up each leg's captured close.
    clvs: list[float] = []
    if window:
        try:
            from closing_lines import load_closing_lines_for_dates
            dates = [str(leg.get("game_date"))
                     for b in window for leg in (b.get("legs") or [])
                     if leg.get("game_date")]
            if dates:
                lo = min(dates)
                hi = max(dates)
                lookup = load_closing_lines_for_dates(
                    f"{lo[:4]}-{lo[4:6]}-{lo[6:]}",
                    f"{hi[:4]}-{hi[4:6]}-{hi[6:]}")
                for b in window:
                    for leg in (b.get("legs") or []):
                        rec = lookup.get((str(leg.get("game_date")),
                                          leg.get("pick_key") or ""))
                        if rec and isinstance(rec.get("clv_pct"),
                                              (int, float)):
                            clvs.append(rec["clv_pct"])
        except Exception:
            log.exception("[bets-log] CLV lookup failed")

    by_book: dict[str, dict] = {}
    for b in settled:
        d = by_book.setdefault(b.get("book") or "?",
                               {"n": 0, "w": 0, "pnl": 0.0, "staked": 0.0})
        d["n"] += 1
        d["w"] += 1 if b["result"] == "WIN" else 0
        d["pnl"] += b.get("pnl") or 0
        if b["result"] != "PUSH":
            d["staked"] += b.get("stake") or 0

    return {
        "days":       days,
        "n_placed":   len(window),
        "n_settled":  len(settled),
        "wins":       len(wins),
        "losses":     len(losses),
        "pushes":     len(settled) - len(wins) - len(losses),
        "pending":    sum(1 for b in window if b.get("result") is None),
        "ungradable": sum(1 for b in window
                          if b.get("result") == "UNGRADABLE"),
        "staked":     round(staked, 2),
        "pnl":        round(pnl, 2),
        "roi_pct":    round(pnl / staked * 100, 1) if staked else 0.0,
        "clv_n":      len(clvs),
        "clv_avg":    round(sum(clvs) / len(clvs), 2) if clvs else None,
        "by_book":    by_book,
    }
