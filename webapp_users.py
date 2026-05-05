"""
User storage for the webapp signup/login flow.

JSON-file-backed (one process writes, single asyncio loop reads/writes
under a thread lock so concurrent requests can't corrupt the file).
Lives next to other state in DATA_DIR; for Fly that's the persistent
volume mount, so accounts survive deploys + machine restarts.

Schema (webapp_users.json):
    {
      "users": {
        "<email_lowercase>": {
          "id":            "<urlsafe-token>",
          "email":         "<email_lowercase>",
          "password_hash": "$2b$...",
          "tier":          "free" | "premium",
          "created_at":    "<iso-8601 utc>"
        }
      }
    }

Tiers:
    "free"     — default for newly signed-up users. Full read access.
    "premium"  — paid tier. require_premium() gates premium-only routes.
    "admin"    — reserved id "legacy_admin" — the WEBAPP_PASSWORD login.
                 Treated as premium for gating purposes.

Passwords use bcrypt (cost factor 12). For comparison: never trust
user-supplied bytes — always pass through bcrypt.checkpw which is
constant-time.
"""
from __future__ import annotations

import json
import os
import secrets
import threading
from datetime import datetime, timezone
from typing import Optional

import bcrypt

# Storage path: webapp_users.json in DATA_DIR (Fly persistent volume).
# Distinct from the Telegram bot's users.json — different concept
# (chat IDs vs. web account credentials).
# `.env` files with bare `DATA_DIR=` lines load as empty strings — treat
# those the same as unset so we fall through to the script directory.
_DATA_DIR = (
    os.environ.get("DATA_DIR")
    or os.path.dirname(os.path.abspath(__file__))
)
USERS_FILE_PATH = os.path.join(_DATA_DIR, "webapp_users.json")

# Single-process app, but we still serialize file writes — concurrent
# /api/signup requests could otherwise read-modify-write race-condition.
_lock = threading.Lock()

LEGACY_ADMIN_ID = "legacy_admin"
TIER_FREE       = "free"
TIER_PREMIUM    = "premium"
TIER_ADMIN      = "admin"
ALLOWED_TIERS   = {TIER_FREE, TIER_PREMIUM, TIER_ADMIN}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _norm_email(email: str) -> str:
    return (email or "").strip().lower()


def _load() -> dict:
    """Read users.json — returns empty skeleton if file missing/corrupt."""
    if not os.path.exists(USERS_FILE_PATH):
        return {"users": {}}
    try:
        with open(USERS_FILE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or "users" not in data:
            return {"users": {}}
        return data
    except (json.JSONDecodeError, OSError):
        return {"users": {}}


def _save(data: dict) -> None:
    """Atomic write — tmp file then rename so a crash mid-write can't
    leave a half-written file on disk."""
    os.makedirs(_DATA_DIR, exist_ok=True)
    tmp = USERS_FILE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, USERS_FILE_PATH)


def find_by_email(email: str) -> Optional[dict]:
    return _load()["users"].get(_norm_email(email))


def find_by_id(user_id: str) -> Optional[dict]:
    """Linear scan — fine at this scale (file-based DB, small user count)."""
    if user_id == LEGACY_ADMIN_ID:
        return legacy_admin_record()
    for user in _load()["users"].values():
        if user.get("id") == user_id:
            return user
    return None


def legacy_admin_record() -> dict:
    """Synthetic user for the WEBAPP_PASSWORD shared-password admin.
    Lets the rest of the auth code treat the legacy login uniformly
    instead of having a special "is this an admin token" branch."""
    return {
        "id":         LEGACY_ADMIN_ID,
        "email":      None,
        "tier":       TIER_ADMIN,
        "created_at": None,
    }


def create_user(email: str, password: str,
                 tier: str = TIER_FREE) -> dict:
    """Create a new user account. Raises ValueError on validation failures.

    Returns the new user record (without password_hash).
    """
    if tier not in ALLOWED_TIERS:
        raise ValueError(f"Invalid tier: {tier!r}")

    email_n = _norm_email(email)
    # Cheap structural email check — full RFC validation would reject too
    # many oddballs (plus-addressing, unicode TLDs). "@ + dot + nonempty"
    # is enough to filter typos and matches what the frontend enforces.
    if "@" not in email_n or "." not in email_n.split("@", 1)[1]:
        raise ValueError("Please enter a valid email address")
    if len(email_n) > 254:
        raise ValueError("Email is too long")
    if not isinstance(password, str) or len(password) < 8:
        raise ValueError("Password must be at least 8 characters")
    if len(password) > 128:
        raise ValueError("Password is too long")

    with _lock:
        data = _load()
        if email_n in data["users"]:
            raise ValueError("An account with that email already exists")

        record = {
            "id":            secrets.token_urlsafe(16),
            "email":         email_n,
            "password_hash": bcrypt.hashpw(
                password.encode("utf-8"), bcrypt.gensalt(12)
            ).decode("utf-8"),
            "tier":          tier,
            "created_at":    _now_iso(),
        }
        data["users"][email_n] = record
        _save(data)
    return _public_view(record)


def verify_password(user: dict, password: str) -> bool:
    """Constant-time check of the supplied password against the user's hash."""
    if not user or not password:
        return False
    h = user.get("password_hash")
    if not h:
        return False
    try:
        return bcrypt.checkpw(password.encode("utf-8"), h.encode("utf-8"))
    except ValueError:
        return False


def set_tier(user_id: str, new_tier: str) -> bool:
    """Change a user's tier (e.g., "free" → "premium" after upgrade).
    Returns True if found + updated."""
    if new_tier not in ALLOWED_TIERS:
        raise ValueError(f"Invalid tier: {new_tier!r}")
    with _lock:
        data = _load()
        for record in data["users"].values():
            if record.get("id") == user_id:
                record["tier"] = new_tier
                _save(data)
                return True
    return False


def _public_view(user: dict) -> dict:
    """Strip the password hash before returning a user record to callers."""
    return {k: v for k, v in user.items() if k != "password_hash"}


def public(user: dict) -> dict:
    """Public alias of _public_view for callers."""
    return _public_view(user)
