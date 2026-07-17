"""
progress.py — process-global progress reporting for long-running jobs.

The scan and backtest pipelines run for minutes inside worker threads;
the webapp's job-status endpoints poll this module so the browser can
render a real progress bar. Workers call report() at cheap checkpoints
(a sport fetched, a batch of result-days graded); readers get a snapshot.

One writer per kind at a time in practice — the webapp's job endpoints
enforce a single run and a single backtest — so a lock and a dict are
all this needs. Stdlib-only; report() must never raise into a pipeline.
"""
import threading
import time

_lock = threading.Lock()
_state: dict[str, dict] = {}

KINDS = ("run", "backtest")


def report(kind: str, phase: str, done: int | None = None,
           total: int | None = None, note: str = "") -> None:
    """Record where a job is. Safe to sprinkle inside hot loops —
    exception-proof and O(1)."""
    try:
        with _lock:
            _state[kind] = {"phase": phase, "done": done, "total": total,
                            "note": note, "ts": time.time()}
    except Exception:
        pass


def get(kind: str) -> dict | None:
    """Snapshot of the latest report for a kind, or None."""
    with _lock:
        s = _state.get(kind)
        return dict(s) if s else None


def clear(kind: str) -> None:
    with _lock:
        _state.pop(kind, None)
