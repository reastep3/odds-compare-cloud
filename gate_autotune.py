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
* Decisions use realized flat unit-ROI with a 95% CI AND an exact
  Poisson-binomial tail test (2026-07-25 — the Wald CI alone proved
  anti-conservative at cohort sizes; both must agree before a proposal
  fires) plus a minimum sample size. Shrinkage tempers thin cells.
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

# NOTE (2026-07-25): this module used to monkeypatch
# oc._starts_within_days = True at IMPORT time to neutralize the timing
# guard for probes. That patch leaked process-wide: the Sunday job
# imports this module inside the bot, so every Sunday-11AM run silently
# disabled the live 24h bracket until the next restart (the likely
# cause of the Jul-13 "promo outside the bracket" reports). Probes now
# simply OMIT the commence key — _starts_within_days treats a missing
# timestamp as "starts soon" — so no global mutation is needed.

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


def pb_pvalues(picks):
    """Exact tail probabilities under the null that each pick wins with
    its own implied breakeven probability (1/decimal).

    2026-07-25: added after the Jul-22 review panel showed the Wald CI
    above is anti-conservative on skewed unit returns at cohort sizes
    (n~30-60) — it produced a false "significant" on WNBA spreads that
    this exact Poisson-binomial test (P computed by dynamic programming)
    correctly rejected. Proposals now require BOTH tests to agree.

    Returns (p_under, p_over): P(W <= w_obs) and P(W >= w_obs)."""
    probs = [1.0 / r["dec"] for r in picks]
    w_obs = sum(r["won"] for r in picks)
    dist = [1.0]
    for q in probs:
        dist = [(dist[k] if k < len(dist) else 0.0) * (1 - q)
                + (dist[k - 1] * q if k > 0 else 0.0)
                for k in range(len(dist) + 1)]
    p_under = sum(dist[: w_obs + 1])
    p_over = sum(dist[w_obs:])
    return p_under, p_over


# ───────────────────────── load ──────────────────────────────────────

def load(csv_path=None):
    """Settled WIN/LOSS picks -> list of dicts. Props mapped to gate keys."""
    rows = []
    with open(csv_path or CSV_PATH, encoding="utf-8") as f:
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
                "dec": american_to_decimal(odds),
                "unit": (american_to_decimal(odds) - 1) if won else -1.0,
                "clv": float(r["clv_pct"]) if r["clv_pct"] not in ("", None) else None,
                "date": date.fromisoformat(r["game_date"]),
            })
    return rows


# ───────────────────────── live-gate probes ──────────────────────────

def gate_gameline(sport, market, ev, pinn):
    # The probe must satisfy every hard gate that is NOT the subject of
    # the probe. 2026-07-25 fixes: odds_decimal=2.0 (mid-band — the
    # Jul-9 odds band made price-less probes auto-fail, which silently
    # disabled EXCLUDE proposals and spammed spurious PROMOTEs for two
    # Sundays), and NO commence key (absent = "starts soon" per
    # _starts_within_days; the old 2099 stamp failed the 1-day window).
    return oc._qualifies({"sport": sport, "market": market, "ev_pct": ev,
                          "num_other_books": 10, "pinnacle_anchored": pinn,
                          "odds_decimal": 2.0})


def gate_prop(market, ev, pinn):
    return oc._qualifies_prop({"market": market, "ev_pct": ev,
                               "pinnacle_anchored": pinn,
                               "odds_decimal": 2.0})


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


def _mk_proposal(kind, is_prop, sport, market, lo, hi, n, m, blo, bhi,
                 droi, exact_p):
    """Structured proposal record (consumed by gate_overrides + the bot)."""
    return {
        "kind": kind, "is_prop": bool(is_prop),
        "sport": "" if is_prop else sport, "market": market,
        "ev_lo": float(lo), "ev_hi": float(hi), "n": int(n),
        "flat_roi": round(m * 100, 1),
        "ci_lo": round(blo * 100), "ci_hi": round(bhi * 100),
        "exact_p": round(exact_p, 4),
        "droi": round(droi),
        # Exact when the live gate behaviour is unambiguous: props are all
        # non-Pinn, and tennis has no Pinnacle coverage. Other game lines mix
        # Pinn/non-Pinn in the reports, so flag them for review.
        "exact": bool(is_prop or sport == "TENNIS"),
        "label": market if is_prop else f"{sport} {market}",
        "band": band_label(lo, hi),
    }


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
            p_under, p_over = pb_pvalues(bp)
            wager = sum(r["bet"] for r in bp)
            pnl = sum(r["pnl"] for r in bp)
            droi = pnl / wager * 100 if wager else 0.0
            w = sum(r["won"] for r in bp)
            mid = (lo_b + (hi_b if hi_b < 99 else 6)) / 2
            plays = gate_plays(is_prop, sport, market, mid, pinn=False)

            # Verdicts require the Wald CI AND the exact PB test to agree
            # (the CI alone flagged false significants — see pb_pvalues).
            if nb < MIN_BAND_N:
                verdict = "thin"
            elif blo > 0 and p_over < 0.05:
                verdict = "PLAY"
            elif bhi < 0 and p_under < 0.05:
                verdict = "SKIP"
            elif blo > 0 or bhi < 0:
                verdict = "ci-only"          # CI fired, exact test didn't
            else:
                verdict = "mixed"

            tag = ""
            if verdict == "PLAY" and not plays:
                tag = "  <= PROMOTE"
                proposals.append(_mk_proposal("PROMOTE", is_prop, sport, market,
                                              lo_b, hi_b, nb, bm, blo, bhi,
                                              droi, p_over))
            elif verdict == "SKIP" and plays:
                tag = "  <= EXCLUDE"
                proposals.append(_mk_proposal("EXCLUDE", is_prop, sport, market,
                                              lo_b, hi_b, nb, bm, blo, bhi,
                                              droi, p_under))

            p_show = p_under if bm < 0 else p_over
            out(f"    {band_label(lo_b,hi_b):<9}{nb:>5}{f'{w}/{nb-w}':>9}"
                f"{bm*100:>+8.1f}%{f'[{blo*100:+.0f},{bhi*100:+.0f}]':>16}"
                f"{f'p={p_show:.3f}':>9}"
                f"{droi:>+7.0f}%{('play' if plays else 'skip'):>7}"
                f"{verdict:>8}{tag}")

    return proposals


def sort_proposals(props):
    """EXCLUDE first, then by evidence strength |flatROI| * sqrt(n)."""
    return sorted(props, key=lambda p: (p["kind"] != "EXCLUDE",
                                        -abs(p["flat_roi"]) * math.sqrt(p["n"])))


# ── Pre-registered decisions (2026-07-25) ────────────────────────────
# Committed IN ADVANCE so the 2026-08-08 review is decided by data, not
# re-litigated mid-stream. Included in every Sunday DM until resolved.
PREREGISTERED = """\
PRE-REGISTERED DECISIONS — resolve on 2026-08-08 (do not re-litigate early):
  1. KBO promotion: PROMOTE to Play These iff by Aug 8 the broad-pool
     cohort (since Jul 9) shows n>=100 settled, flat ROI > 0, and mean
     CLV >= +1.0% on >=50 captured closes. Markets per their own splits.
     (Standing Jul 25: n=82, +13.6%, CLV +1.37% on 71 — on track.)
  2. WNBA Pinn-anchored spreads: EXCLUDE iff exact PB p(under) < 0.05
     on the full rule-inception window (2026-05-22 onward — no
     sub-window selection); otherwise KEEP and close the watch item.
     (Standing Jul 25: 31d p=0.144 — not significant.)
  3. Odds band (-250..+350): KEEP unless the band-era exact p(under)
     < 0.05 with CLV also below +0.5%; positive CLV keeps the variance
     interpretation. (Standing Jul 25: p=0.129, CLV +2.35% — keep.)"""


def _run(rows, out):
    """Run the full analysis, emitting each line via out(); return proposals."""
    dts = [r["date"] for r in rows]
    out(f"Gate auto-tuner — {len(rows)} settled picks, {min(dts)} .. {max(dts)}")
    out(f"Verdict rule: flat-ROI 95% CI clears 0 AND exact PB p<0.05 "
        f"AND n>={MIN_BAND_N}.  'gate' = your live _qualifies probed non-Pinn.")

    gl = analyze(rows, False, out)
    pp = analyze(rows, True, out)

    out("\n" + "=" * 90)
    out("ACTIONABLE PROPOSALS  (data significantly disagrees with the live gate)")
    out("=" * 90)
    allp = sort_proposals(pp + gl)
    if not allp:
        out("None — the live gate already matches every significant slice.")
    for p in allp:
        scope = "" if p["exact"] else " [review: Pinn-mixed]"
        out(f"  {p['kind']:<8} {p['label']:<20} EV {p['band']:<8} "
            f"n={p['n']:<4} flatROI {p['flat_roi']:+.1f}% "
            f"[{p['ci_lo']:+.0f},{p['ci_hi']:+.0f}] p={p['exact_p']:.3f} "
            f"$ROI {p['droi']:+.0f}%{scope}")
    out("\n" + PREREGISTERED)
    return allp


def analysis_report(csv_path=None):
    """Return (full_report_text, sorted_proposals).

    The text is the complete per-cell analysis — the context the Telegram
    Claude-analyst reasons over, so it can weigh cells the mechanical filter
    rejected as thin and reference proposals by number."""
    rows = load(csv_path)
    buf = []
    props = _run(rows, lambda *a: buf.append(" ".join(str(x) for x in a)))
    return "\n".join(buf), props


def generate_proposals(csv_path=None):
    """Structured proposals only (back-compat wrapper around analysis_report)."""
    return analysis_report(csv_path)[1]


def main():
    out = Tee(OUT_TXT)
    _run(load(), out)
    out(f"\nWritten to {os.path.relpath(OUT_TXT, ROOT)}")
    out.close()


if __name__ == "__main__":
    main()
