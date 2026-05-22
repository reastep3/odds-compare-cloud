"""
The Odds API monthly credit tracker — CLV pause-switch.

Reads `x-requests-remaining` from every Odds API response, persists the
running state to $DATA_DIR/credit_state.json, and exposes
`should_pause_clv()` for the closing-line worker so we don't burn the
monthly quota on CLV captures (a nice-to-have validator) when we still
need credits for active scanning (mission-critical).

Threshold rationale
-------------------
Default: pause CLV captures when the running tally hits 85% of monthly
quota used (i.e., remaining < 15% of observed peak). CLV captures rank
last among API consumers — they confirm whether the model has real
edge but never produce a new +EV pick. A 15% reserve translates to
~4-5 days of active-scan headroom at typical late-month rates, which
keeps the bot operational through the end of the billing window even
if we trip the gate early. Configurable via `CLV_PAUSE_AT_USAGE_PCT`
(0.0–1.0) — set lower to be more conservative, higher to claw back
late-month CLV coverage at the cost of a tighter reserve.

Plan-size detection
-------------------
Odds API responses tell us `x-requests-remaining` but not the plan
total. We infer it from the highest observed `remaining` (= the
post-rollover ceiling). Set `ODDS_API_MONTHLY_LIMIT` env var to
override (useful for new installs where the peak hasn't been seen
yet — we'd otherwise gate on the first-observed remaining, which may
be mid-month).

Rollover handling
-----------------
When `remaining` jumps upward by ≥50% in a single update, treat it as
a month-rollover (or a plan upgrade) and update the peak watermark so
the percentage math stays calibrated to the new ceiling. CLV resumes
automatically on the next call because `should_pause_clv()` re-reads
state every time.
"""
import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger(__name__)

# Threshold: fraction of monthly quota used before CLV pauses. Default
# 0.85 (pause when remaining < 15% of peak). Tunable via env var.
def _parse_pct(raw: str, default: float) -> float:
    try:
        v = float(raw)
        return v if 0.0 < v < 1.0 else default
    except (TypeError, ValueError):
        return default

CLV_PAUSE_AT_USAGE_PCT = _parse_pct(
    os.environ.get("CLV_PAUSE_AT_USAGE_PCT", ""), 0.85
)

# Optional explicit monthly limit. When set, it overrides the inferred
# peak — useful before we've observed a full-month ceiling.
def _parse_int(raw: str, default: Optional[int]) -> Optional[int]:
    try:
        v = int(raw)
        return v if v > 0 else default
    except (TypeError, ValueError):
        return default

EXPLICIT_MONTHLY_LIMIT = _parse_int(
    os.environ.get("ODDS_API_MONTHLY_LIMIT", ""), None
)

# A jump of ≥ROLLOVER_RATIO× the previous `remaining` value signals a
# month rollover (quota refilled) or a plan upgrade.
ROLLOVER_RATIO = 1.5

# Persist state to DATA_DIR so the count survives container restarts.
# Default mirrors odds_compare.OUTPUT_DIR's pattern.
def _state_path() -> str:
    data_dir = os.environ.get("DATA_DIR") or "/data/state"
    return os.path.join(data_dir, "credit_state.json")


def _load_state() -> dict:
    try:
        with open(_state_path(), "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _save_state(state: dict) -> None:
    path = _state_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, path)
    except OSError as e:
        log.warning("[credit-tracker] could not persist state: %s", e)


def record(remaining) -> None:
    """Persist the latest `x-requests-remaining` value from an Odds API
    response. Idempotent — safe to call from every API caller.

    `remaining` may be a str (the raw header), int, or None. Invalid
    values are silently ignored so this never causes a fetch to fail.
    """
    if remaining is None:
        return
    try:
        remaining = int(remaining)
    except (TypeError, ValueError):
        return
    if remaining < 0:
        return

    state = _load_state()
    prev_remaining = state.get("remaining")
    peak = int(state.get("peak_remaining") or 0)
    rolled_over = False

    # Rollover or plan upgrade — remaining jumped substantially upward.
    if (prev_remaining is not None
            and prev_remaining > 0
            and remaining > prev_remaining * ROLLOVER_RATIO):
        rolled_over = True
        log.info(
            "[credit-tracker] rollover detected: remaining %d -> %d "
            "(previous peak %d)", prev_remaining, remaining, peak
        )

    # Track the high-water mark — peak grows monotonically except on
    # rollover, where we accept the new ceiling if it exceeds the old.
    if remaining > peak:
        peak = remaining

    state["remaining"]      = remaining
    state["peak_remaining"] = peak
    state["last_updated"]   = datetime.now(timezone.utc).isoformat()
    if rolled_over:
        state["last_rollover_at"] = state["last_updated"]
    _save_state(state)


def get_status() -> dict:
    """Snapshot of current credit budget state.

    Returns a dict with:
      - known         (bool) — whether we have enough data to decide
      - remaining     (int|None) — most recent x-requests-remaining
      - peak          (int|None) — plan-size proxy
      - usage_pct     (float|None) — 0.0–1.0
      - threshold_pct (float) — pause threshold
      - clv_paused    (bool) — whether CLV is currently gated off
      - last_updated  (str|None) — UTC ISO timestamp
      - source        ('explicit' | 'observed') — peak source
    """
    state = _load_state()
    remaining = state.get("remaining")
    observed_peak = int(state.get("peak_remaining") or 0)
    peak = EXPLICIT_MONTHLY_LIMIT or observed_peak
    source = "explicit" if EXPLICIT_MONTHLY_LIMIT else "observed"

    if remaining is None or not peak:
        return {
            "known":            False,
            "remaining":        remaining,
            "peak":             peak or None,
            "usage_pct":        None,
            "threshold_pct":    CLV_PAUSE_AT_USAGE_PCT,
            "clv_paused":       False,
            "last_updated":     state.get("last_updated"),
            "last_rollover_at": state.get("last_rollover_at"),
            "source":           source,
        }

    used = max(0, peak - remaining)
    usage_pct = used / peak if peak else 0.0
    paused = usage_pct >= CLV_PAUSE_AT_USAGE_PCT
    return {
        "known":            True,
        "remaining":        remaining,
        "peak":             peak,
        "usage_pct":        round(usage_pct, 3),
        "threshold_pct":    CLV_PAUSE_AT_USAGE_PCT,
        "clv_paused":       paused,
        "last_updated":     state.get("last_updated"),
        "last_rollover_at": state.get("last_rollover_at"),
        "source":           source,
    }


def should_pause_clv() -> bool:
    """Return True if the CLV worker should skip its API calls this tick.

    Decision is based solely on the most recent `x-requests-remaining`
    observed vs the peak (or explicit) plan size. When we don't have
    enough data to decide (no API call has been recorded yet, or no
    peak inferred), returns False — never block CLV on missing data.
    """
    return get_status()["clv_paused"]
