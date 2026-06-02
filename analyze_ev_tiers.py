"""
EV-tier ROI analysis for _qualifies() / _qualifies_prop() calibration.

Reads the BROAD +EV pool from graded backtest reports ("All Picks" =
game lines, "Player Props" = props) — not the post-filter Play These tab —
so we can see how each sport × market × EV-tier band actually performed and
decide where the live filter thresholds belong.

Methodology mirrors backtest.py's print_summary cross-slice:
  ROI       = sum(P&L) / sum(Bet Amt)        (Kelly-weighted, what live uses)
  flat ROI  = sum(unit_profit) / n           (each pick = 1u; robust to outliers)
  settled   = WIN / LOSS / PUSH
Promote candidate: n>=30 and ROI>=+5%.  Exclude: n>=30 and ROI<=-5%.

NOTE: graded reports do not preserve the pinnacle_anchored flag, so game-line
slices are COMBINED Pinn+non-Pinn. Props are all non-Pinn in production
(Pinnacle props aren't wired into the live pricer), so the prop slices match
the live non-Pinn gate exactly.
"""
import glob
import os
import re
import sys
from collections import defaultdict
from datetime import date

import openpyxl

ROOT = os.path.dirname(os.path.abspath(__file__))

# Prop display name (as written to the sheet) -> internal market key.
PROP_DISPLAY_TO_KEY = {
    "Points": "pts", "Rebounds": "reb", "Assists": "ast",
    "3-Pointers": "threes", "Pts+Reb+Ast": "pra", "Double-Double": "dd",
    "Strikeouts": "strikeouts", "Total Bases": "total_bases",
    "Hits": "hits", "RBIs": "rbis",
    "Shots on Goal": "sog", "Anytime HR": "hr",
}

# Current live non-Pinn gates (odds_compare._qualifies_prop), for reference.
CURRENT_PROP_GATES = {"pts": 2.0, "strikeouts": 4.0, "total_bases": 1.5}


def parse_american(v):
    if v is None:
        return None
    m = re.match(r"^([+-]?)(\d+)$", str(v).strip().replace(" ", ""))
    if not m:
        return None
    sign, num = m.groups()
    return -int(num) if sign == "-" else int(num)


def decimal_from_american(a):
    return 1 + (a / 100 if a > 0 else 100 / -a)


def parse_game_date(v):
    if isinstance(v, date):
        return v
    if v is None:
        return None
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", str(v).strip())
    if not m:
        return None
    try:
        return date(int(m[1]), int(m[2]), int(m[3]))
    except ValueError:
        return None


def all_files():
    files = sorted(glob.glob(os.path.join(ROOT, "backtest_*.xlsx")))
    files += sorted(glob.glob(os.path.join(ROOT, "_bt_recent", "*.xlsx")))
    files += sorted(glob.glob(os.path.join(ROOT, "Backtest Archives", "*.xlsx")))
    return files


def load(window_start, window_end):
    """Dedup'd broad-pool picks across all graded reports in window."""
    bets = {}
    for fp in all_files():
        try:
            wb = openpyxl.load_workbook(fp, read_only=True, data_only=True)
        except Exception as e:
            print(f"  skip {os.path.basename(fp)}: {e}")
            continue
        for sheet, is_prop in (("All Picks", False), ("Player Props", True)):
            if sheet not in wb.sheetnames:
                continue
            ws = wb[sheet]
            it = ws.iter_rows(values_only=True)
            hdr = next(it, None)
            if not hdr:
                continue
            ix = {n: i for i, n in enumerate(hdr)}
            for r in it:
                try:
                    gd = parse_game_date(r[ix["Game Date"]])
                    if gd is None or gd < window_start or gd > window_end:
                        continue
                    result = r[ix["Result"]]
                    if result not in ("WIN", "LOSS", "PUSH"):
                        continue
                    odds = parse_american(r[ix["Odds"]])
                    if odds is None:
                        continue
                    if is_prop:
                        pick = f"{r[ix['Player']]}/{r[ix['Side']]}"
                        market = PROP_DISPLAY_TO_KEY.get(
                            r[ix["Market"]], str(r[ix["Market"]]).lower())
                    else:
                        pick = r[ix["Pick"]]
                        market = str(r[ix["Market"]]).lower()
                    key = (gd.isoformat(), r[ix["Sport"]], r[ix["Game"]],
                           r[ix["Book"]], market, pick, r[ix["Line"]], odds)
                    if key in bets:
                        continue
                    ev = r[ix["EV %"]]
                    bet = r[ix["Bet Amt"]] or 0
                    pnl = r[ix["P&L"]]
                    if pnl is None:
                        # Reconstruct from result if P&L cell blank.
                        dec = decimal_from_american(odds)
                        pnl = (dec - 1) * bet if result == "WIN" else \
                              (-bet if result == "LOSS" else 0)
                    bets[key] = {
                        "sport": str(r[ix["Sport"]]).upper(),
                        "market": market,
                        "odds": odds,
                        "ev": float(ev) if ev is not None else 0.0,
                        "bet": float(bet),
                        "pnl": float(pnl),
                        "result": result,
                        "is_prop": is_prop,
                        "unit": (decimal_from_american(odds) - 1) if result == "WIN"
                                else (-1.0 if result == "LOSS" else 0.0),
                    }
                except (KeyError, IndexError, TypeError, ValueError):
                    continue
        wb.close()
    return bets


def roi_row(picks):
    w = sum(1 for p in picks if p["result"] == "WIN")
    l = sum(1 for p in picks if p["result"] == "LOSS")
    wager = sum(p["bet"] for p in picks)
    pnl = sum(p["pnl"] for p in picks)
    roi = pnl / wager * 100 if wager else 0.0
    flat = sum(p["unit"] for p in picks) / len(picks) * 100 if picks else 0.0
    return w, l, len(picks), pnl, roi, flat


EV_TIERS = [
    ("0-1%", lambda v: v < 1.0),
    ("1-2%", lambda v: 1.0 <= v < 2.0),
    ("2-3%", lambda v: 2.0 <= v < 3.0),
    ("3-5%", lambda v: 3.0 <= v < 5.0),
    ("5%+",  lambda v: v >= 5.0),
]


def fmt(label, picks, indent=0, gate=None):
    w, l, n, pnl, roi, flat = roi_row(picks)
    if n == 0:
        return
    flag = ""
    if (w + l) >= 30 and roi >= 5.0 and flat >= 2.0:
        flag = "  <- PROMOTE"
    elif (w + l) >= 30 and roi <= -5.0 and flat <= -2.0:
        flag = "  <- EXCLUDE"
    elif (w + l) < 30:
        flag = "  (thin)"
    pad = " " * indent
    g = f"  [live gate >={gate}%]" if gate is not None else ""
    print(f"{pad}{label:<22} W{w:>3}/L{l:<3} n={n:>3}  "
          f"P&L ${pnl:>+7.2f}  ROI {roi:>+6.1f}%  flat {flat:>+6.1f}%{g}{flag}")


def section(title):
    print("\n" + "=" * 92)
    print(title)
    print("=" * 92)


def main():
    end = date(2026, 6, 2)
    start = date(2026, 4, 1)
    if len(sys.argv) >= 3:
        start = parse_game_date(sys.argv[1])
        end = parse_game_date(sys.argv[2])
    print(f"Window: {start} .. {end}")
    bets = load(start, end)
    games = {k: v for k, v in bets.items() if not v["is_prop"]}
    props = {k: v for k, v in bets.items() if v["is_prop"]}
    print(f"Settled (WIN/LOSS/PUSH): {len(bets)} total  "
          f"({len(games)} game-line, {len(props)} prop)")

    gl = list(games.values())
    pl = list(props.values())

    section("OVERALL")
    fmt("ALL", list(bets.values()))
    fmt("Game lines", gl)
    fmt("Props", pl)

    # ---- Props: market x EV tier (calibrates _qualifies_prop) ----
    section("PROPS — market x EV tier (all non-Pinn; calibrates _qualifies_prop)")
    by_mkt = defaultdict(list)
    for p in pl:
        by_mkt[p["market"]].append(p)
    for mkt in sorted(by_mkt, key=lambda m: -len(by_mkt[m])):
        gate = CURRENT_PROP_GATES.get(mkt)
        fmt(f"{mkt} (TOTAL)", by_mkt[mkt], gate=gate)
        for tlabel, tfilt in EV_TIERS:
            fmt(tlabel, [p for p in by_mkt[mkt] if tfilt(p["ev"])], indent=4)
        print()

    # ---- Game lines: sport x market x EV tier (combined Pinn+non-Pinn) ----
    section("GAME LINES — sport x market x EV tier (COMBINED Pinn+non-Pinn)")
    by_sm = defaultdict(list)
    for p in gl:
        by_sm[(p["sport"], p["market"])].append(p)
    for (sport, mkt) in sorted(by_sm, key=lambda k: -len(by_sm[k])):
        picks = by_sm[(sport, mkt)]
        if len(picks) < 10:
            continue
        fmt(f"{sport} {mkt} (TOTAL)", picks)
        for tlabel, tfilt in EV_TIERS:
            fmt(tlabel, [p for p in picks if tfilt(p["ev"])], indent=4)
        print()

    # ---- Game lines: market x EV tier (cross-sport) ----
    section("GAME LINES — market x EV tier (cross-sport, COMBINED)")
    by_m = defaultdict(list)
    for p in gl:
        by_m[p["market"]].append(p)
    for mkt in sorted(by_m, key=lambda m: -len(by_m[m])):
        fmt(f"{mkt} (TOTAL)", by_m[mkt])
        for tlabel, tfilt in EV_TIERS:
            fmt(tlabel, [p for p in by_m[mkt] if tfilt(p["ev"])], indent=4)
        print()


if __name__ == "__main__":
    main()
