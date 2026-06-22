"""
Stage 1 of the auto-calibration workstream: fit a calibrated meta-model that
decides which broad-pool +EV game-line picks are worth playing, and prove
(walk-forward, out-of-sample) whether it beats the current hand-tuned EV gate.

DESIGN / HONESTY NOTES
----------------------
* Game lines only. Props carry no CLV and are a smaller, separate problem.
* Target = realized WIN (the correct, slow label). We do NOT train on CLV:
  CLV is only known AFTER the bet (the close happens post-open), so using it
  as an input feature would be temporal leakage in a real-time gate. Instead
  CLV is an INDEPENDENT out-of-sample check — a good model's "play" set should
  also beat the close.
* Bet-time-only features: implied_prob (market anchor), EV tier (one-hot, so a
  linear model can learn the non-monotone "high EV = stale-line trap"
  inversion found in Stage 0), sport, market, book (soft-book signal),
  lead_time. Deliberately excludes seen/persistent (ambiguous bet-time
  availability) and clv/close (post-bet).
* Model = L2 logistic regression. With ~4.7k rows and many one-hot columns,
  a regularized linear model is the honest choice; boosting would overfit.
  EV tiers + book one-hots let it capture the key non-linearities anyway.
* Validation = expanding-window walk-forward (train on the past, predict the
  next week, roll). Never random K-fold — that leaks time and ignores
  season / line-efficiency drift.

The money metric is the matched-volume decision comparison: at the SAME number
of bets the incumbent EV gate would place, does ranking by model score earn a
better out-of-sample ROI?

Run:  python train_model.py        (after build_training_table.py)
"""
import csv
import math
import os
from collections import Counter, defaultdict
from datetime import date, timedelta

import numpy as np
from sklearn.feature_extraction import DictVectorizer
from sklearn.linear_model import LogisticRegression

ROOT = os.path.dirname(os.path.abspath(__file__))
CSV_PATH = os.path.join(ROOT, "training_table.csv")

# Cardinality control: collapse sports/books seen fewer than this many times
# (overall) into "OTHER". A structural choice on global counts — not leaky.
RARE_MIN = 30

# Walk-forward knobs.
BURN_IN_FRAC = 0.40   # first 40% of the timeline trains the first model
STEP_DAYS = 7         # predict one week forward per fold

# Incumbent gate proxy: the hand-tuned system's sweet spot is the 1–5% EV band
# (Stage 0 showed <1% marginal, >=5% a stale-line trap). Used as the baseline
# to beat at matched volume.
INCUMBENT_EV_LO, INCUMBENT_EV_HI = 1.0, 5.0


# ───────────────────────── helpers ───────────────────────────────────

def american_to_decimal(a):
    return 1 + (a / 100 if a > 0 else 100 / -a)


def wilson_ci(wins, n, z=1.96):
    if n == 0:
        return (0.0, 0.0)
    p = wins / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = (z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / d
    return (max(0.0, c - h), min(1.0, c + h))


def ev_tier(ev):
    if ev is None:
        return "ev_na"
    if ev < 1:
        return "ev_0_1"
    if ev < 2:
        return "ev_1_2"
    if ev < 3:
        return "ev_2_3"
    if ev < 5:
        return "ev_3_5"
    return "ev_5p"


# ───────────────────────── load ──────────────────────────────────────

def load_rows():
    """Settled game-line WIN/LOSS picks with parseable EV, as dicts."""
    rows = []
    with open(CSV_PATH, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r["is_prop"] != "0" or r["won"] not in ("0", "1"):
                continue
            try:
                odds = int(r["odds"])
                imp = float(r["implied_prob"])
            except (ValueError, KeyError):
                continue
            ev = float(r["ev"]) if r["ev"] not in ("", None) else None
            lead = int(r["lead_time_days"]) if r["lead_time_days"] not in ("", None) else 0
            bet = float(r["bet"]) if r["bet"] not in ("", None) else 0.0
            pnl = float(r["pnl"]) if r["pnl"] not in ("", None) else 0.0
            clv = float(r["clv_pct"]) if r["clv_pct"] not in ("", None) else None
            rows.append({
                "game_date": date.fromisoformat(r["game_date"]),
                "sport": r["sport"], "market": r["market"], "book": r["book"],
                "odds": odds, "dec": american_to_decimal(odds), "imp": imp,
                "ev": ev, "lead": max(0, min(lead, 14)),
                "won": int(r["won"]),
                "bet": bet, "pnl": pnl,
                "unit_pnl": (american_to_decimal(odds) - 1) if r["won"] == "1" else -1.0,
                "clv": clv,
            })
    rows.sort(key=lambda x: x["game_date"])
    return rows


def collapse_rare(rows):
    """Map rare sports/books to OTHER (in place)."""
    for field in ("sport", "book"):
        counts = Counter(r[field] for r in rows)
        for r in rows:
            if counts[r[field]] < RARE_MIN:
                r[field] = "OTHER"


# ───────────────────────── features ──────────────────────────────────

def featurize(rows, mu, sd):
    """Row dicts -> list of feature dicts for DictVectorizer.
    Numerics standardized with provided train mean/std."""
    out = []
    for r in rows:
        out.append({
            "sport": r["sport"],
            "market": r["market"],
            "book": r["book"],
            "ev_tier": ev_tier(r["ev"]),
            "imp": (r["imp"] - mu["imp"]) / sd["imp"],
            "lead": (r["lead"] - mu["lead"]) / sd["lead"],
        })
    return out


def train_stats(rows):
    mu, sd = {}, {}
    for k in ("imp", "lead"):
        vals = np.array([r[k] for r in rows], float)
        mu[k] = float(vals.mean())
        sd[k] = float(vals.std()) or 1.0
    return mu, sd


def fit_fold(train_rows):
    mu, sd = train_stats(train_rows)
    dv = DictVectorizer(sparse=False)
    X = dv.fit_transform(featurize(train_rows, mu, sd))
    y = np.array([r["won"] for r in train_rows])
    clf = LogisticRegression(max_iter=2000, C=1.0, class_weight=None)
    clf.fit(X, y)
    return clf, dv, mu, sd


def predict(clf, dv, mu, sd, rows):
    X = dv.transform(featurize(rows, mu, sd))
    return clf.predict_proba(X)[:, 1]


# ───────────────────────── walk-forward ──────────────────────────────

def walk_forward(rows):
    d0, d1 = rows[0]["game_date"], rows[-1]["game_date"]
    span = (d1 - d0).days
    cutoff = d0 + timedelta(days=int(span * BURN_IN_FRAC))
    preds = []
    folds = 0
    while cutoff <= d1:
        nxt = cutoff + timedelta(days=STEP_DAYS)
        train = [r for r in rows if r["game_date"] < cutoff]
        test = [r for r in rows if cutoff <= r["game_date"] < nxt]
        if len(train) >= 200 and test:
            clf, dv, mu, sd = fit_fold(train)
            p = predict(clf, dv, mu, sd, test)
            for r, pw in zip(test, p):
                preds.append({**r, "p_win": float(pw),
                              "model_ev": float(pw) * r["dec"] - 1.0})
            folds += 1
        cutoff = nxt
    return preds, folds, cutoff


# ───────────────────────── evaluation ────────────────────────────────

def roi_block(picks):
    if not picks:
        return (0, 0, 0, 0.0, 0.0, None)
    w = sum(p["won"] for p in picks)
    n = len(picks)
    wager = sum(p["bet"] for p in picks)
    pnl = sum(p["pnl"] for p in picks)
    roi = pnl / wager * 100 if wager else 0.0
    flat = sum(p["unit_pnl"] for p in picks) / n * 100
    clvs = [p["clv"] for p in picks if p["clv"] is not None]
    avg_clv = sum(clvs) / len(clvs) if clvs else None
    return (n, w, n - w, roi, flat, avg_clv)


def show(label, picks, indent=0):
    n, w, l, roi, flat, clv = roi_block(picks)
    if n == 0:
        print(f"{' '*indent}{label:<28} (empty)")
        return
    c = f"  CLV {clv:+5.2f}%" if clv is not None else "  CLV   n/a"
    print(f"{' '*indent}{label:<28} n={n:>4}  W{w:>4}/L{l:<4}  "
          f"ROI {roi:>+6.1f}%  flat {flat:>+6.1f}%{c}")


def sec(t):
    print("\n" + "=" * 86 + f"\n{t}\n" + "=" * 86)


def evaluate(preds):
    sec("OUT-OF-SAMPLE CALIBRATION (predicted P(win) vs actual)")
    print("Well-calibrated => actual hit% tracks predicted, market anchor intact.\n")
    bins = [(0, .35), (.35, .45), (.45, .50), (.50, .55), (.55, .65), (.65, 1.01)]
    print(f'{"pred P(win)":<16}{"n":>6}{"actual":>9}{"pred":>9}{"95% CI":>20}')
    print("-" * 60)
    for lo, hi in bins:
        grp = [p for p in preds if lo <= p["p_win"] < hi]
        if not grp:
            continue
        n = len(grp)
        act = sum(p["won"] for p in grp) / n
        pred = sum(p["p_win"] for p in grp) / n
        cl, ch = wilson_ci(sum(p["won"] for p in grp), n)
        print(f'{lo*100:>4.0f}-{hi*100:<10.0f}{n:>6}{act*100:>8.1f}%{pred*100:>8.1f}%'
              f'   [{cl*100:>4.1f}%, {ch*100:>4.1f}%]')

    sec("THE MONEY METRIC — matched-volume decision comparison (OOS)")
    incumbent = [p for p in preds if p["ev"] is not None
                 and INCUMBENT_EV_LO <= p["ev"] < INCUMBENT_EV_HI]
    k = len(incumbent)
    by_score = sorted(preds, key=lambda p: -p["model_ev"])
    model_topk = by_score[:k]
    print(f"Incumbent gate proxy = EV in [{INCUMBENT_EV_LO},{INCUMBENT_EV_HI})%  "
          f"-> {k} bets. Model takes its top-{k} by score (same volume).\n")
    show("Play ALL (broad pool)", preds)
    show("Incumbent EV gate", incumbent)
    show(f"Model top-{k} (matched)", model_topk)
    show("Model EV>0 (natural)", [p for p in preds if p["model_ev"] > 0])
    show("Model EV>2% (strict)", [p for p in preds if p["model_ev"] > 0.02])

    # Lift curve: ROI as we keep only the best-scored fraction.
    sec("MODEL LIFT — OOS ROI by score decile kept (best first)")
    n = len(by_score)
    print(f'{"keep top":<12}{"n":>6}{"ROI":>9}{"flat":>9}{"CLV":>9}')
    print("-" * 46)
    for frac in (0.1, 0.2, 0.3, 0.5, 0.7, 1.0):
        k2 = max(1, int(n * frac))
        nn, w, l, roi, flat, clv = roi_block(by_score[:k2])
        cs = f"{clv:+.2f}%" if clv is not None else "n/a"
        print(f'top {int(frac*100):>3}%{"":<3}{nn:>6}{roi:>+8.1f}%{flat:>+8.1f}%{cs:>9}')


def interpret(rows):
    """Full-data fit purely for coefficient inspection (not for scoring)."""
    sec("WHAT THE MODEL LEARNED (full-fit L2 coefficients, log-odds of win)")
    mu, sd = train_stats(rows)
    dv = DictVectorizer(sparse=False)
    X = dv.fit_transform(featurize(rows, mu, sd))
    y = np.array([r["won"] for r in rows])
    clf = LogisticRegression(max_iter=2000, C=1.0)
    clf.fit(X, y)
    names = dv.get_feature_names_out()
    coef = clf.coef_[0]
    order = np.argsort(coef)
    print("Most NEGATIVE (model fades these):")
    for i in order[:8]:
        print(f"    {names[i]:<28} {coef[i]:+.3f}")
    print("Most POSITIVE (model backs these):")
    for i in order[::-1][:8]:
        print(f"    {names[i]:<28} {coef[i]:+.3f}")


def main():
    rows = load_rows()
    collapse_rare(rows)
    print(f"Loaded {len(rows)} settled game-line WIN/LOSS picks "
          f"({rows[0]['game_date']} .. {rows[-1]['game_date']})")
    clv_n = sum(1 for r in rows if r["clv"] is not None)
    print(f"CLV captured on {clv_n} ({clv_n/len(rows)*100:.0f}%) — used for OOS check only.")

    preds, folds, _ = walk_forward(rows)
    print(f"\nWalk-forward: {folds} weekly folds, {len(preds)} out-of-sample picks scored.")
    evaluate(preds)
    interpret(rows)


if __name__ == "__main__":
    main()
