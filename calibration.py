"""
calibration.py — isotonic calibration of the model's fair probabilities.

WHY (2026-07-25): the 105-day review showed the model tracks actual win
rates within ~2 pts through the -250..+350 band but overstates longshots
~2×. The odds band cliff-cuts that tail; this layer learns the actual
predicted→realized mapping from graded history so probabilities can be
corrected CONTINUOUSLY instead. Runs in SHADOW MODE first: scans attach
cal_fair_prob / cal_ev_pct alongside the raw numbers, sheets record
them, and nothing in the gates or staking reads them until the shadow
data has earned the switch (same promote-on-evidence path as any cohort).

Method: pool-adjacent-violators (PAVA) isotonic regression, fitted
separately for Pinnacle-anchored and non-anchored game-line picks —
the two populations mis-calibrate differently. Pure stdlib: the fit
runs on the Fly box in the Sunday job (seconds at these sizes) and the
lookup is a binary search over a step function stored as JSON on the
volume. sklearn stays a local-only dev dependency.

Artifact: DATA_DIR/calibration.json
    {"version": 1, "fitted_at": ..., "window": [start, end],
     "n": {"pinn": ..., "nopin": ...},
     "segments": {"pinn": [[prob_from, calibrated_prob], ...],
                  "nopin": [...]}}
"""
from __future__ import annotations

import bisect
import json
import logging
import os
from datetime import date, datetime, timezone

log = logging.getLogger(__name__)

_DATA_DIR = os.environ.get("DATA_DIR") or os.path.dirname(
    os.path.abspath(__file__))
MODEL_PATH = os.path.join(_DATA_DIR, "calibration.json")

# A segment fitted on fewer settled picks than this stays identity —
# calibrating on thin data would just memorize noise.
MIN_SEGMENT_N = 300

_GAME_LINE_MARKETS = {"H2H", "SPREADS", "TOTALS"}

# Module-level cache: (mtime, model) so scans don't re-read the JSON on
# every edge. Reloads automatically after a Sunday refit.
_cache: tuple[float, dict] | None = None


def fit_isotonic(pairs: list[tuple[float, int]]) -> list[list[float]]:
    """PAVA over (predicted_prob, won∈{0,1}) pairs → non-decreasing step
    curve [[prob_from, calibrated_prob], ...] sorted by prob_from."""
    pts = sorted(pairs)
    blocks: list[list[float]] = []      # [sum_won, n, x_start]
    for x, y in pts:
        blocks.append([float(y), 1.0, float(x)])
        while (len(blocks) >= 2
               and blocks[-2][0] / blocks[-2][1]
                   >= blocks[-1][0] / blocks[-1][1]):
            s2, n2, _ = blocks.pop()
            s1, n1, x1 = blocks.pop()
            blocks.append([s1 + s2, n1 + n2, x1])
    return [[round(b[2], 4), round(b[0] / b[1], 4)] for b in blocks]


def _pairs_from_graded(picks: list[dict]) -> dict[str, list]:
    """Split settled game-line picks into pinn/nopin (prob, won) pairs.
    Predicted fair prob is recovered from the sheet's own numbers:
    ev = (fair × dec − 1) × 100  →  fair = (1 + ev/100) / dec."""
    out: dict[str, list] = {"pinn": [], "nopin": []}
    for p in picks:
        if p.get("result") not in ("WIN", "LOSS"):
            continue
        if (p.get("market") or "").upper() not in _GAME_LINE_MARKETS:
            continue
        dec = p.get("decimal") or 0
        if dec <= 1:
            continue
        fair = (1 + (p.get("ev_pct") or 0) / 100) / dec
        if not 0.01 <= fair <= 0.99:
            continue
        seg = "pinn" if p.get("pinnacle") else "nopin"
        out[seg].append((fair, 1 if p["result"] == "WIN" else 0))
    return out


def fit_from_history(start: date, end: date) -> dict:
    """Fit both segments from the graded broad pool (picks.db-backed
    loaders + grading). Heavy-ish (result fetches on a cold cache) —
    runs in the Sunday job right after the backtest warmed the caches."""
    import backtest                      # lazy — keeps import cheap
    raw = backtest.load_archive_ev_edges(start, end, "exclude")
    graded = backtest.run_backtest(raw, progress_label="calibration fit")
    pairs = _pairs_from_graded(graded)
    segments = {}
    n = {}
    for seg, ps in pairs.items():
        n[seg] = len(ps)
        segments[seg] = (fit_isotonic(ps) if len(ps) >= MIN_SEGMENT_N
                         else [])        # empty = identity
    return {
        "version": 1,
        "fitted_at": datetime.now(timezone.utc).isoformat(),
        "window": [start.isoformat(), end.isoformat()],
        "n": n,
        "segments": segments,
    }


def save_model(model: dict, path: str | None = None) -> str:
    path = path or MODEL_PATH
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(model, f)
    os.replace(tmp, path)
    return path


def load_model(path: str | None = None) -> dict | None:
    """Cached read; None when no model exists (calibrate() → identity)."""
    global _cache
    path = path or MODEL_PATH
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    if _cache and _cache[0] == mtime:
        return _cache[1]
    try:
        with open(path, "r", encoding="utf-8") as f:
            model = json.load(f)
    except Exception:
        log.exception("[calibration] unreadable model at %s", path)
        return None
    _cache = (mtime, model)
    return model


def calibrate(prob: float, pinnacle_anchored: bool,
              model: dict | None = None) -> float:
    """Map a predicted fair probability through the fitted curve.
    Identity when no model / empty segment / any trouble — the shadow
    layer must never be able to break a scan."""
    try:
        model = model if model is not None else load_model()
        if not model:
            return prob
        curve = model["segments"].get(
            "pinn" if pinnacle_anchored else "nopin") or []
        if not curve:
            return prob
        xs = [c[0] for c in curve]
        i = bisect.bisect_right(xs, prob) - 1
        if i < 0:
            i = 0
        return float(curve[i][1])
    except Exception:
        return prob
