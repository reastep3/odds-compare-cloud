"""
Stage 2 of the auto-calibration workstream: automate the manual combing.

This replaces the loop you run by hand today (run analyze_ev_tiers.py -> eyeball
the EV-tier ROI slices -> decide which thresholds to move -> edit _qualifies /
_qualifies_prop). It computes, per (sport x market x EV-band), the realized
performance with a SIGNIFICANCE test and an empirical-Bayes shrunk estimate,
probes your LIVE gate functions for what they'd do, and emits explicit
PROMOTE / EXCLUDE proposals where the data significantly disagrees with the
current gate.

Why this and not the ML model: Stage 1 showed a meta-model doesn't beat the
hand-tuned gate on available features (the market's efficient; the missing
lever is a sharp-line anchor we don't capture yet). So the win isn't a fancier
predictor — it's removing the MANUAL step from the tuning you already do well.

Honesty / scope:
* Decisions use realized flat unit-ROI with a 95% CI (signal must clear noise)
  plus a minimum sample size. Shrinkage tempers thin-cell point estimates.
* PROPS proposals are EXACT: production props are all non-Pinnacle and the gate
  has no live Pinn path, so the probe matches live behaviour 1:1.
* GAME-LINE proposals are flagged "review": graded reports don't preserve the
  per-pick Pinnacle flag, so cells mix Pinn + non-Pinn while the live gate is
  Pinn-aware. We probe the non-Pinn path (the soft-book majority) and label
  divergences for review rather than as hard automatic changes.
* Nothing here touches live code. It prints proposals + writes gate_proposals.txt.
  Applying any change stays a human decision (eventual Telegram-approval hook).

Run:  python gate_autotune.py     (after build_training_table.py)
"""
import csv
import math
import os
from collections import defaultdict
from datetime import date

import odds_compare as oc

# Neutralize the timing guard so we probe the EV/sport/market POLICY, not a
# specific game's clock. We pass books>=4 honestly below; only timing is mocked.
oc._starts_within_days = lambda *a, **k: True   # noqa: E305

ROOT = os.path.dirname(os.path.abspath(__file__))
CSV_PATH = os.path.join(ROOT, "training_table.csv")
OUT_TXT = os.path.join(ROOT, "gate_proposals.txt")

# Prop display name (lowercased, as stored in training_table) -> gate key.
PROP_DISPLAY_TO_KEY = {
    "points": "pts", "rebounds": "reb", "assists": "ast",
    "3-pointers": "threes", "pts+reb+ast": "pra", "double-double": "dd",
    "strikeouts": "strikeouts", "total bases": "total_bases",
    "hits": "hits", "rbis": "rbis", "shots on goal": "sog", "anytime hr": "hr",
}

# EV bands aligned to the gate's actual breakpoints (1.0/1.5/2.0/2.5/3.0/5.0)
# so a midpoint probe of the live gate is exact within each band.
BANDS = [(0, 1), (1, 1.5), (1.5, 2), (2, 2.5), (2.5, 3), (3, 5), (5, 99)]

MIN_BAND_N = 20    # below this a band is "thin" — no hard verdict
MIN_CELL_N = 40    # cells smaller than this are summarized, not proposed on


# ───────────────────────── helpers ───────────────────────────────────

def american_to_decimal(a):
    return 1 + (a / 100 if a > 0 else 100 / -a)


def band_label(lo, hi):
    return f"{lo:g}-{hi:g}%" if hi < 99 else f"{lo:g}%+"


def band_of(ev):
    for lo, hi in BANDS:
        if lo <= ev < hi:
            return (lo, hi)
    return None


def flat_ci(units):
    """95% CI for mean unit-ROI (fraction). Returns (mean, lo, hi)."""
    n = len(units)
    if n == 0:
        return 0.0, 0.0, 0.0
    m = sum(units) / n
    if n < 2:
        return m, -9.9, 9.9
    var = sum((u - m) ** 2 for u in units) / (n - 1)
    se = math.sqrt(var / n)
    return m, m - 1.96 * se, m + 1.96 * se


# ───────────────────────── load ──────────────────────────────────────

def load():
    """Settled WIN/LOSS picks -> list of dicts. Props mapped to gate keys."""
    rows = []
    with open(CSV_PATH, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r["won"] not in ("0", "1"):
                continue
            try:
                odds = int(r["odds"])
            except ValueError:
                continue
            if r["ev"] in ("", None):
                continue
            ev = float(r["ev"])
            is_prop = r["is_prop"] == "1"
            market = r["market"]
            if is_prop:
                market = PROP_DISPLAY_TO_KEY.get(market, market)
            won = int(r["won"])
            rows.append({
                "is_prop": is_prop, "sport": r["sport"], "market": market,
                "ev": ev, "won": won,
                "bet": float(r["bet"]) if r["bet"] not in ("", None) else 0.0,
                "pnl": float(r["pnl"]) if r["pnl"] not in ("", None) else 0.0,
                "unit": (american_to_decimal(odds) - 1) if won else -1.0,
                "clv": float(r["clv_pct"]) if r["clv_pct"] not in ("", None) else None,
                "date": date.fromisoformat(r["game_date"]),
            })
    return rows


# ───────────────────────── live-gate probes ──────────────────────────

def gate_gameline(sport, market, ev, pinn):
    return oc._qualifies({"sport": sport, "market": market, "ev_pct": ev,
                          "num_other_books": 10, "pinnacle_anchored": pinn,
                          "commence": "2099-01-01T00:00:00Z"})


def gate_prop(market, ev, pinn):
    return oc._qualifies_prop({"market": market, "ev_pct": ev,
                               "pinnacle_anchored": pinn,
                               "commence": "2099-01-01T00:00:00Z"})


def gate_plays(is_prop, sport, market, ev, pinn=False):
    return gate_prop(market, ev, pinn) if is_prop else gate_gameline(sport, market, ev, pinn)


# ───────────────────────── empirical-Bayes shrinkage ─────────────────

def shrink(cells):
    """Random-effects (DerSimonian-Laird) EB shrinkage of each cell's mean
    unit-ROI toward the inverse-variance-weighted group mean. cells:
    {key: [units...]}. Returns {key: shrunk_mean}.

    D-L (vs naive method-of-moments) weights each cell by 1/se^2, so a tiny
    fat-tailed cell (e.g. a soccer league full of +400 longshots with huge
    unit variance) can't swamp the between-cell variance estimate and collapse
    every cell onto the grand mean."""
    means, se2 = {}, {}
    for k, u in cells.items():
        n = len(u)
        if n < 2:
            continue
        m = sum(u) / n
        within = sum((x - m) ** 2 for x in u) / (n - 1)
        means[k] = m
        se2[k] = max(within / n, 1e-6)
    if len(means) < 2:
        return {k: means.get(k, 0.0) for k in cells}
    w = {k: 1.0 / se2[k] for k in means}
    W = sum(w.values())
    ybar = sum(w[k] * means[k] for k in means) / W          # IV-weighted mean
    Q = sum(w[k] * (means[k] - ybar) ** 2 for k in means)
    C = W - sum(wv ** 2 for wv in w.values()) / W
    tau2 = max(0.0, (Q - (len(means) - 1)) / C) if C > 0 else 0.0
    out = {}
    for k in cells:
        if k not in means:
            out[k] = ybar
            continue
        lam = tau2 / (tau2 + se2[k])        # weight on the cell's own data
        out[k] = ybar + lam * (means[k] - ybar)
    return out


# ───────────────────────── analysis ──────────────────────────────────

class Tee:
    def __init__(self, path):
        self.f = open(path, "w", encoding="utf-8")

    def __call__(self, *a):
        s = " ".join(str(x) for x in a)
        print(s)
        self.f.write(s + "\n")

    def close(self):
        self.f.close()


def analyze(rows, is_prop, out):
    grp = [r for r in rows if r["is_prop"] == is_prop]
    title = "PLAYER PROPS  (exact — all non-Pinnacle)" if is_prop else \
            "GAME LINES  (review — cells mix Pinn+non-Pinn; gate probed non-Pinn)"
    out("\n" + "=" * 90)
    out(title)
    out("=" * 90)

    # Cells = (sport, market) for game lines; (market) for props.
    cells = defaultdict(list)
    for r in grp:
        key = r["market"] if is_prop else (r["sport"], r["market"])
        cells[key].append(r)

    shrunk = shrink({k: [r["unit"] for r in v] for k, v in cells.items()})
    proposals = []

    for key in sorted(cells, key=lambda k: -len(cells[k])):
        picks = cells[key]
        n = len(picks)
        if n < MIN_CELL_N:
            continue
        sport = "" if is_prop else key[0]
        market = key if is_prop else key[1]
        m, lo, hi = flat_ci([r["unit"] for r in picks])
        clvs = [r["clv"] for r in picks if r["clv"] is not None]
        clv = f"{sum(clvs)/len(clvs):+.2f}%" if clvs else "n/a"
        label = market if is_prop else f"{sport} {market}"
        out(f"\n{label:<22} n={n:<4} flatROI {m*100:+5.1f}% "
            f"[{lo*100:+5.1f},{hi*100:+5.1f}]  shrunk {shrunk[key]*100:+5.1f}%  CLV {clv}")
        out(f"    {'band':<9}{'n':>5}{'W/L':>9}{'flatROI':>9}{'  95% CI':>16}"
            f"{'$ROI':>8}{'  gate':>7}{'  data':>8}")

        for lo_b, hi_b in BANDS:
            bp = [r for r in picks if lo_b <= r["ev"] < hi_b]
            nb = len(bp)
            if nb == 0:
                continue
            bm, blo, bhi = flat_ci([r["unit"] for r in bp])
            wager = sum(r["bet"] for r in bp)
            pnl = sum(r["pnl"] for r in bp)
            droi = pnl / wager * 100 if wager else 0.0
            w = sum(r["won"] for r in bp)
            mid = (lo_b + (hi_b if hi_b < 99 else 6)) / 2
            plays = gate_plays(is_prop, sport, market, mid, pinn=False)

            if nb < MIN_BAND_N:
                verdict = "thin"
            elif blo > 0:
                verdict = "PLAY"
            elif bhi < 0:
                verdict = "SKIP"
            else:
                verdict = "mixed"

            tag = ""
            if verdict == "PLAY" and not plays:
                tag = "  <= PROMOTE"
                proposals.append(("PROMOTE", label, band_label(lo_b, hi_b),
                                  nb, bm, blo, bhi, droi))
            elif verdict == "SKIP" and plays:
                tag = "  <= EXCLUDE"
                proposals.append(("EXCLUDE", label, band_label(lo_b, hi_b),
                                  nb, bm, blo, bhi, droi))

            out(f"    {band_label(lo_b,hi_b):<9}{nb:>5}{f'{w}/{nb-w}':>9}"
                f"{bm*100:>+8.1f}%{f'[{blo*100:+.0f},{bhi*100:+.0f}]':>16}"
                f"{droi:>+7.0f}%{('play' if plays else 'skip'):>7}"
                f"{verdict:>8}{tag}")

    return proposals


def main():
    out = Tee(OUT_TXT)
    rows = load()
    dts = [r["date"] for r in rows]
    out(f"Gate auto-tuner — {len(rows)} settled picks, {min(dts)} .. {max(dts)}")
    out(f"Verdict rule: flat-ROI 95% CI clears 0 AND n>={MIN_BAND_N}.  "
        f"'gate' = your live _qualifies probed non-Pinn.")

    gl = analyze(rows, False, out)
    pp = analyze(rows, True, out)

    out("\n" + "=" * 90)
    out("ACTIONABLE PROPOSALS  (data significantly disagrees with the live gate)")
    out("=" * 90)
    allp = pp + gl  # props first (exact), then game lines (review)
    if not allp:
        out("None — the live gate already matches every significant slice.")
    for kind, label, band, n, m, lo, hi, droi in sorted(
            allp, key=lambda p: (p[0] != "EXCLUDE", -abs(p[4]) * math.sqrt(p[3]))):
        scope = "" if label.split()[0] in ("pts", "reb", "ast", "threes",
                                           "strikeouts", "total_bases", "hits",
                                           "rbis", "sog", "hr", "pra", "dd") else " [review: Pinn-mixed]"
        out(f"  {kind:<8} {label:<20} EV {band:<8} "
            f"n={n:<4} flatROI {m*100:+.1f}% [{lo*100:+.0f},{hi*100:+.0f}]  "
            f"$ROI {droi:+.0f}%{scope}")
    out(f"\nWritten to {os.path.relpath(OUT_TXT, ROOT)}")
    out.close()


if __name__ == "__main__":
    main()
