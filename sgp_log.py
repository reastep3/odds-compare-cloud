"""
SGP slip log — the feedback loop for the parlay/SGP pricing model.

Every /sgpcheck quote is recorded here (legs, the book's quoted price, the
independence-approximation fair probability, the EV verdict). Once the games
settle, grade_pending() grades each leg through the backtest machinery and
resolves the joint outcome. calibration_summary() then compares the
independence-model's predicted joint hit rate against reality — after ~50
graded slips that gap IS the empirical correlation penalty, replacing the
assumed haircut in the parlay sim and the /sgpcheck warning copy.

Outcomes are graded for SKIP verdicts too: the games run whether or not the
bet was placed, and calibration wants every observation it can get.

Storage: JSONL on the persistent volume (DATA_DIR), one slip per line,
atomically rewritten on grade. Stdlib-only at import time — backtest (and
its ESPN fetching) is imported lazily inside grade_pending() so this module
stays cheap for the bot and trivially monkeypatchable in tests.
"""
import json
import logging
import os
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)

_DATA_DIR = os.environ.get("DATA_DIR") or os.path.dirname(os.path.abspath(__file__))
STORE = os.path.join(_DATA_DIR, "sgp_slips.jsonl")

# A slip whose legs still can't be resolved this long after its newest
# game date is marked UNGRADABLE instead of pending forever (box score
# gaps, exotic markets the graders don't know, voided games...).
_UNGRADABLE_AFTER_DAYS = 14

# Legs whose game-line market grade via grade_pick (uppercase keys there).
_GAME_LINE_MARKETS = {"h2h", "spreads", "totals"}


def leg_from_edge(edge: dict) -> dict:
    """Distill a parlay-row leg (full edge dict) into the fields grading
    needs. game_date is derived from commence in ET — same convention as
    the backtest's pick rows."""
    game_date = None
    commence = edge.get("commence")
    if commence:
        try:
            from odds_compare import EASTERN
            dt = datetime.fromisoformat(str(commence).replace("Z", "+00:00"))
            game_date = dt.astimezone(EASTERN).strftime("%Y%m%d")
        except (ValueError, TypeError):
            pass
    is_prop = bool(edge.get("prop_side"))
    return {
        "sport":     (edge.get("sport") or "").lower(),
        "game":      edge.get("game"),
        "game_date": game_date,
        "market":    edge.get("market"),
        "pick":      edge.get("outcome"),
        "line":      edge.get("point"),
        "is_prop":   is_prop,
        "player":    edge.get("player"),
        "prop_side": edge.get("prop_side"),
        "decimal":   edge.get("odds_decimal"),
    }


def log_slip(slip: dict) -> None:
    """Append one quote-check record. Never raises — losing a log line
    must not break /sgpcheck."""
    try:
        slip = {"ts": datetime.now(timezone.utc).isoformat(),
                "result": None, "leg_results": None, **slip}
        with open(STORE, "a", encoding="utf-8") as f:
            f.write(json.dumps(slip) + "\n")
    except Exception:
        log.exception("[sgp-log] failed to append slip")


def _load_slips() -> list[dict]:
    if not os.path.exists(STORE):
        return []
    slips = []
    with open(STORE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                slips.append(json.loads(line))
            except json.JSONDecodeError:
                log.warning("[sgp-log] skipping corrupt line")
    return slips


def _save_slips(slips: list[dict]) -> None:
    tmp = STORE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for s in slips:
            f.write(json.dumps(s) + "\n")
    os.replace(tmp, STORE)


def _joint_result(leg_results: list[str]) -> str | None:
    """Standard parlay settlement: PUSH legs drop off the ticket; any LOSS
    loses the slip; all remaining WIN wins it; every leg pushed = PUSH.
    None while any leg is still unresolved."""
    if any(r not in ("WIN", "LOSS", "PUSH") for r in leg_results):
        return None
    if any(r == "LOSS" for r in leg_results):
        return "LOSS"
    wins = [r for r in leg_results if r == "WIN"]
    return "WIN" if wins else "PUSH"


def grade_pending() -> dict:
    """Grade every pending slip whose games may have finished.

    Legs are pushed through backtest.run_backtest (ESPN/box-score grading,
    prop vs game-line dispatch) with dummy stakes — only the WIN/LOSS/PUSH
    verdicts are kept. Returns {"graded": n, "pending": n, "ungradable": n}.
    """
    slips = _load_slips()
    pending = [s for s in slips if s.get("result") is None]
    stats = {"graded": 0, "pending": 0, "ungradable": 0}
    if not pending:
        return stats

    import backtest  # lazy — heavy import, network on grade

    for slip in pending:
        picks = []
        for leg in slip.get("legs", []):
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
            log.exception("[sgp-log] grading failed for slip %s",
                          slip.get("ts"))
            continue

        leg_results = [p.get("result") or "UNKNOWN" for p in graded]
        slip["leg_results"] = leg_results
        joint = _joint_result(leg_results)
        if joint is not None:
            slip["result"] = joint
            slip["graded_at"] = datetime.now(timezone.utc).isoformat()
            stats["graded"] += 1
            continue

        # Unresolvable legs: give box scores a fortnight, then close out.
        newest = max((leg.get("game_date") or "" for leg in slip["legs"]),
                     default="")
        try:
            newest_dt = datetime.strptime(newest, "%Y%m%d").date()
            expired = (datetime.now(timezone.utc).date() - newest_dt
                       > timedelta(days=_UNGRADABLE_AFTER_DAYS))
        except ValueError:
            expired = True   # no parseable date → will never grade
        if expired:
            slip["result"] = "UNGRADABLE"
            slip["graded_at"] = datetime.now(timezone.utc).isoformat()
            stats["ungradable"] += 1
        else:
            stats["pending"] += 1

    _save_slips(slips)
    return stats


def observed_haircut_pct(default: float = 15.0, min_n: int = 8) -> float:
    """Mean price haircut the book has actually applied across logged
    quotes: 1 − (quoted−1)/(naive−1), as a percent of the naive payout.

    The haircut is known at quote time, so ungraded slips count too —
    every /sgpcheck contributes an observation. Deduped per ticket
    (latest quote wins). Falls back to `default` until `min_n` distinct
    tickets exist or on any read error; clamped to [0, 60] so one weird
    quote can't push pricing to an extreme.

    2026-07-09: this feeds the parlay builder's assumed SGP pricing, so
    the displayed SGP EV converges on DK's real quoting behaviour as
    slips accumulate."""
    try:
        by_ticket: dict[tuple, float] = {}
        for s in sorted(_load_slips(), key=lambda s: s.get("ts") or ""):
            nd = s.get("naive_dec") or 0
            qd = s.get("quoted_dec") or 0
            if nd > 1 and qd > 1:
                by_ticket[_leg_set_key(s)] = (1 - (qd - 1) / (nd - 1)) * 100
        cuts = list(by_ticket.values())
        if len(cuts) < min_n:
            return default
        return max(0.0, min(60.0, sum(cuts) / len(cuts)))
    except Exception:
        return default


def _leg_set_key(slip: dict) -> tuple:
    """Identity of the underlying ticket, so re-checking the same slip at
    two different quotes doesn't double-count it in calibration."""
    return tuple(sorted(
        (leg.get("game_date") or "", leg.get("game") or "",
         (leg.get("market") or "").lower(), leg.get("pick") or "",
         str(leg.get("line")))
        for leg in slip.get("legs", [])))


def calibration_summary() -> str | None:
    """Predicted (independence) vs realized joint hit rate, for the weekly
    digest. Deduped to one observation per distinct ticket (latest quote
    wins). Returns None when nothing is graded yet."""
    slips = [s for s in _load_slips()
             if s.get("result") in ("WIN", "LOSS")
             and isinstance(s.get("fair_prob"), (int, float))]
    if not slips:
        return None

    by_ticket: dict[tuple, dict] = {}
    for s in sorted(slips, key=lambda s: s.get("ts") or ""):
        by_ticket[_leg_set_key(s)] = s      # latest quote wins
    obs = list(by_ticket.values())

    n         = len(obs)
    predicted = sum(s["fair_prob"] for s in obs) / n
    realized  = sum(1 for s in obs if s["result"] == "WIN") / n

    taxes = []
    for s in obs:
        nd, qd = s.get("naive_dec"), s.get("quoted_dec")
        if isinstance(nd, (int, float)) and isinstance(qd, (int, float)) \
                and nd > 1 and qd > 1:
            taxes.append((1 - (qd - 1) / (nd - 1)) * 100)
    tax_str = (f" · avg book haircut {sum(taxes)/len(taxes):.1f}%"
               if taxes else "")

    lines = [
        "\n<b>SGP Calibration</b>",
        f"  {n} graded slip(s): predicted {predicted * 100:.1f}% "
        f"vs realized {realized * 100:.1f}% joint hit rate{tax_str}",
    ]
    if n < 50:
        lines.append(f"  <i>(directional only — ~50 slips needed, "
                     f"{n} so far)</i>")
    elif predicted > 0:
        # Realized/predicted gap = the empirical correlation factor the
        # independence approximation is missing.
        lines.append(f"  Correlation factor: ×{realized / predicted:.2f} "
                     f"(apply to independence fair prob)")
    return "\n".join(lines)
