"""
Human-approved gate overrides — the apply-side of the auto-calibration loop.

gate_autotune.py emits PROMOTE / EXCLUDE proposals where realized results
significantly disagree with the live _qualifies / _qualifies_prop gates. When
the admin approves one (via the Telegram /gate_approve command), it is recorded
here as a narrow, auditable override on a single (sport, market, EV-band) cell.
odds_compare._qualifies consults this store and lets an approved override flip
its base decision.

SAFETY PROPERTIES (by design):
* Empty store => decision_override() returns None for everything => the live
  gate behaves byte-identically to today. Overrides only ever exist because a
  human explicitly approved a specific, evidence-backed proposal.
* Kill switch: GATE_OVERRIDES_ENABLED=0 disables every override at once.
* EXCLUDE wins ties (prefer NOT betting over betting on conflict).
* Stdlib only, and this module must NOT import odds_compare (odds_compare
  imports it — keep the dependency one-way).
* Stored on the persistent volume (DATA_DIR) so approvals survive restarts.
"""
import json
import os
from datetime import datetime, timezone

_DATA_DIR = os.environ.get("DATA_DIR") or os.path.dirname(os.path.abspath(__file__))
STORE = os.path.join(_DATA_DIR, "gate_overrides.json")

VALID_KINDS = ("PROMOTE", "EXCLUDE")


def enabled() -> bool:
    """Master kill switch. Default ON; set GATE_OVERRIDES_ENABLED=0 to disable."""
    return os.environ.get("GATE_OVERRIDES_ENABLED", "1").strip().lower() \
        not in ("0", "false", "no", "off", "")


def make_id(kind: str, is_prop: bool, sport: str, market: str,
            ev_lo: float, ev_hi: float) -> str:
    scope = "prop" if is_prop else (sport or "?").upper()
    return f"{kind[:2].lower()}-{scope}-{market}-{ev_lo:g}-{ev_hi:g}"


def load() -> list[dict]:
    """Active override entries (empty list if missing/corrupt)."""
    try:
        with open(STORE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []
    entries = data.get("overrides", []) if isinstance(data, dict) else []
    return [e for e in entries if _valid(e)]


def _valid(e: dict) -> bool:
    try:
        return (e.get("kind") in VALID_KINDS
                and isinstance(e.get("is_prop"), bool)
                and isinstance(e.get("market"), str)
                and float(e["ev_lo"]) <= float(e["ev_hi"]))
    except (KeyError, TypeError, ValueError):
        return False


def save(entries: list[dict]) -> None:
    tmp = STORE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"overrides": entries}, f, indent=2)
    os.replace(tmp, STORE)   # atomic on POSIX + Windows


def add(entry: dict) -> dict:
    """Insert/replace an override (dedup by id). Returns the stored entry."""
    if not _valid(entry):
        raise ValueError(f"invalid override entry: {entry}")
    entry = dict(entry)
    entry.setdefault("approved_at", datetime.now(timezone.utc).isoformat())
    eid = entry.get("id") or make_id(
        entry["kind"], entry["is_prop"], entry.get("sport", ""),
        entry["market"], float(entry["ev_lo"]), float(entry["ev_hi"]))
    entry["id"] = eid
    entries = [e for e in load() if e.get("id") != eid]
    entries.append(entry)
    save(entries)
    return entry


def remove(eid: str) -> bool:
    """Delete an override by id. Returns True if something was removed."""
    entries = load()
    kept = [e for e in entries if e.get("id") != eid]
    if len(kept) == len(entries):
        return False
    save(kept)
    return True


def entry_from_proposal(p: dict) -> dict:
    """Build a store entry from a gate_autotune proposal dict."""
    return {
        "kind": p["kind"],
        "is_prop": bool(p["is_prop"]),
        "sport": "" if p["is_prop"] else (p.get("sport") or "").upper(),
        "market": p["market"],
        "ev_lo": float(p["ev_lo"]),
        "ev_hi": float(p["ev_hi"]),
        "evidence": {
            "n": p.get("n"), "flat_roi": p.get("flat_roi"),
            "ci": [p.get("ci_lo"), p.get("ci_hi")], "droi": p.get("droi"),
            "exact": p.get("exact"),
        },
    }


def decision_override(is_prop: bool, sport: str, market: str, ev) -> bool | None:
    """Return the forced gate decision for an edge, or None if no override.

    True  => force PLAY (an approved PROMOTE covers this cell)
    False => force SKIP  (an approved EXCLUDE covers this cell)
    None  => no opinion; caller keeps its base decision.

    EXCLUDE wins on conflict. Game-line matches require the same canonical
    sport; prop matches are sport-agnostic (entry sport is "").
    """
    if not enabled() or ev is None:
        return None
    try:
        ev = float(ev)
    except (TypeError, ValueError):
        return None
    sport = (sport or "").upper()
    market = (market or "").lower() if not is_prop else (market or "")
    force_skip = force_play = False
    for e in load():
        if bool(e["is_prop"]) != is_prop or e["market"] != market:
            continue
        if not is_prop and (e.get("sport", "").upper() != sport):
            continue
        if not (float(e["ev_lo"]) <= ev < float(e["ev_hi"])):
            continue
        if e["kind"] == "EXCLUDE":
            force_skip = True
        else:
            force_play = True
    if force_skip:
        return False
    if force_play:
        return True
    return None
