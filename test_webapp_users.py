"""Unit tests for the webapp signup/login flow.

Covers webapp_users (the JSON-backed user db with bcrypt hashing) and
the v2-token mint/verify cycle in webapp.py — including legacy-token
backwards compatibility for the WEBAPP_PASSWORD shared admin login.
"""
import base64
import hashlib
import hmac
import importlib
import os
import time

import pytest


@pytest.fixture
def fresh_users_module(tmp_path, monkeypatch):
    """Re-import webapp_users with a clean DATA_DIR so each test starts
    with no users.json. Yields the freshly-loaded module."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import webapp_users
    importlib.reload(webapp_users)
    yield webapp_users


# ── webapp_users.create_user — validation ─────────────────────────────

def test_create_user_returns_public_record(fresh_users_module):
    u = fresh_users_module.create_user("alice@example.com", "longerpassword")
    assert u["email"] == "alice@example.com"
    assert u["tier"] == "free"
    assert "id" in u and "created_at" in u
    # Critically — never leak the hash through the public surface
    assert "password_hash" not in u

def test_create_user_normalizes_email_case(fresh_users_module):
    fresh_users_module.create_user("Alice@Example.COM", "longerpassword")
    # Lookup with different casing should still hit
    found = fresh_users_module.find_by_email("alice@example.com")
    assert found is not None
    assert found["email"] == "alice@example.com"

def test_create_user_rejects_short_password(fresh_users_module):
    with pytest.raises(ValueError, match="at least 8"):
        fresh_users_module.create_user("alice@example.com", "short1")

def test_create_user_rejects_invalid_email(fresh_users_module):
    with pytest.raises(ValueError, match="valid email"):
        fresh_users_module.create_user("notanemail", "longerpassword")
    with pytest.raises(ValueError, match="valid email"):
        fresh_users_module.create_user("missing@dotcom", "longerpassword")

def test_create_user_rejects_duplicate_email(fresh_users_module):
    fresh_users_module.create_user("alice@example.com", "longerpassword")
    with pytest.raises(ValueError, match="already exists"):
        fresh_users_module.create_user("alice@example.com", "differentpass1")

def test_create_user_rejects_bad_tier(fresh_users_module):
    with pytest.raises(ValueError, match="Invalid tier"):
        fresh_users_module.create_user(
            "alice@example.com", "longerpassword", tier="super-vip")


# ── verify_password ────────────────────────────────────────────────────

def test_verify_password_accepts_correct(fresh_users_module):
    fresh_users_module.create_user("alice@example.com", "hunter2foo")
    user = fresh_users_module.find_by_email("alice@example.com")
    # find_by_email returns the FULL record (including hash) — that's
    # the internal call path verify_password uses
    assert fresh_users_module.verify_password(user, "hunter2foo") is True

def test_verify_password_rejects_wrong(fresh_users_module):
    fresh_users_module.create_user("alice@example.com", "hunter2foo")
    user = fresh_users_module.find_by_email("alice@example.com")
    assert fresh_users_module.verify_password(user, "wrongpassword") is False

def test_verify_password_handles_missing_user(fresh_users_module):
    assert fresh_users_module.verify_password(None, "anything") is False
    assert fresh_users_module.verify_password({}, "anything") is False

def test_verify_password_empty_password(fresh_users_module):
    fresh_users_module.create_user("alice@example.com", "hunter2foo")
    user = fresh_users_module.find_by_email("alice@example.com")
    assert fresh_users_module.verify_password(user, "") is False


# ── tier management ────────────────────────────────────────────────────

def test_set_tier_upgrades_user(fresh_users_module):
    u = fresh_users_module.create_user("alice@example.com", "longerpassword")
    assert fresh_users_module.set_tier(u["id"], "premium") is True
    assert fresh_users_module.find_by_id(u["id"])["tier"] == "premium"

def test_set_tier_rejects_invalid(fresh_users_module):
    u = fresh_users_module.create_user("alice@example.com", "longerpassword")
    with pytest.raises(ValueError):
        fresh_users_module.set_tier(u["id"], "platinum-elite")

def test_set_tier_unknown_user_returns_false(fresh_users_module):
    assert fresh_users_module.set_tier("nonexistent-id", "premium") is False


# ── legacy admin synthetic record ──────────────────────────────────────

def test_legacy_admin_has_admin_tier(fresh_users_module):
    rec = fresh_users_module.legacy_admin_record()
    assert rec["id"] == "legacy_admin"
    assert rec["tier"] == "admin"

def test_find_by_id_resolves_legacy_admin(fresh_users_module):
    # Even with no users in the file, legacy_admin id always resolves
    rec = fresh_users_module.find_by_id("legacy_admin")
    assert rec is not None
    assert rec["tier"] == "admin"


# ── Token round-trip (v2 + legacy compat) ──────────────────────────────

@pytest.fixture
def webapp_module(tmp_path, monkeypatch):
    """Import webapp with deterministic env so tokens are reproducible."""
    monkeypatch.setenv("DATA_DIR",         str(tmp_path))
    monkeypatch.setenv("WEBAPP_PASSWORD",  "shared-admin-pw")
    monkeypatch.setenv("ODDS_API_KEY",     "test")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test")
    monkeypatch.setenv("PINNACLE_GUEST_KEY", "test")
    monkeypatch.setenv(
        "WEBAPP_TOKEN_SECRET",
        "fixed-secret-for-tests-only-do-not-use-in-prod")
    import webapp_users
    importlib.reload(webapp_users)
    import webapp
    importlib.reload(webapp)
    yield webapp


def test_v2_token_round_trip(webapp_module):
    user_id = "abc123-test-user"
    token = webapp_module._make_user_token(user_id, int(time.time()) + 3600)
    assert webapp_module._verify_token(token) == user_id

def test_v2_expired_token_rejected(webapp_module):
    token = webapp_module._make_user_token("abc123", int(time.time()) - 1)
    assert webapp_module._verify_token(token) is None

def test_legacy_token_resolves_to_legacy_admin(webapp_module):
    # Legacy format: base64('<expiry>:<sig>'), no user_id embedded.
    # Existing tokens in users' localStorage from the old shared-login
    # era should keep working — they map to the synthetic admin user.
    expiry = str(int(time.time()) + 3600)
    sig = hmac.new(
        webapp_module._TOKEN_SECRET, expiry.encode(), hashlib.sha256
    ).hexdigest()
    legacy = base64.urlsafe_b64encode(f"{expiry}:{sig}".encode()).decode()
    assert webapp_module._verify_token(legacy) == "legacy_admin"

def test_legacy_token_with_bad_signature_rejected(webapp_module):
    # Same shape as legacy, but the sig won't match — must be rejected
    expiry = str(int(time.time()) + 3600)
    legacy = base64.urlsafe_b64encode(
        f"{expiry}:{'0' * 64}".encode()).decode()
    assert webapp_module._verify_token(legacy) is None

def test_garbage_token_rejected(webapp_module):
    assert webapp_module._verify_token("not-a-real-token") is None
    assert webapp_module._verify_token("") is None

def test_v2_token_with_tampered_user_id_rejected(webapp_module):
    """If an attacker swaps the user_id portion of a valid v2 token,
    the HMAC sig won't match and it must be rejected."""
    real = webapp_module._make_user_token("alice", int(time.time()) + 3600)
    raw = base64.urlsafe_b64decode(real.encode()).decode()
    parts = raw.split(":")
    parts[1] = "bob"   # swap user_id
    tampered_raw = ":".join(parts).encode()
    tampered = base64.urlsafe_b64encode(tampered_raw).decode()
    assert webapp_module._verify_token(tampered) is None
