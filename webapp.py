"""
webapp.py — FastAPI service for the Odds Compare PWA.

Runs alongside the Telegram bot in the same asyncio loop (same Fly machine,
same process). Exposes a small JSON API + serves the PWA static files.

Auth model (v1, single-user):
  • On startup, read WEBAPP_PASSWORD env var (plaintext password).
  • POST /api/login with the password → returns an HMAC-signed token.
  • All other /api/* endpoints require Authorization: Bearer <token>.
  • Token = base64("<expiry_ts>:<hmac_sha256(secret, expiry_ts)>"), 30-day TTL.

Designed so that adding multi-user (per-user API keys, billing, etc.) later
is a strict superset — endpoints already accept a Depends(current_user) hook.

Endpoints:
  GET  /api/health                — public; basic liveness
  POST /api/login                 — public; exchange password for token
  POST /api/run                   — auth; full odds-compare run, returns JSON
  POST /api/run_live              — auth; live in-progress edges, returns JSON
  GET  /api/archives              — auth; list ev_edges_*.xlsx with metadata
  GET  /api/archives/{name}       — auth; download one archive
  GET  /api/backtest_archives     — auth; list backtest_*.xlsx with metadata
  GET  /api/backtest_archives/{name}          — auth; download one report
  GET  /api/backtest_archives/{name}/contents — auth; render report as JSON
  POST /api/backtest              — auth; run backtest, returns JSON

Static frontend:
  GET  /                          — index.html (PWA shell)
  GET  /manifest.json, /sw.js     — PWA wiring
  GET  /static/*                  — assets (css, js, icons)
"""
from __future__ import annotations

import base64
import glob
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import (Depends, FastAPI, HTTPException, Request, status)
from fastapi.responses import (FileResponse, JSONResponse, RedirectResponse)
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles


log = logging.getLogger("webapp")

# ── Config ─────────────────────────────────────────────────────────────

# Plaintext password set as a Fly secret (encrypted at rest by Fly). On
# login we constant-time compare against this and mint a session token.
# Single-user for v1 — to multi-user later, swap this for a per-user
# table keyed off (email, bcrypt_hash) and decode the user id from the token.
_WEBAPP_PASSWORD = os.environ.get("WEBAPP_PASSWORD", "")

# Used to sign session tokens. Auto-generated per-process if unset, which
# means tokens invalidate on restart — fine for v1, swap to a Fly secret
# (WEBAPP_TOKEN_SECRET) if you want sessions to survive deploys.
_TOKEN_SECRET = os.environ.get(
    "WEBAPP_TOKEN_SECRET", secrets.token_urlsafe(32)
).encode()

_TOKEN_TTL_SECONDS = 30 * 24 * 3600   # 30 days

# Where Excel archives live. Matches odds_compare.OUTPUT_DIR.
# Treat empty-string env values the same as unset (handles bare
# `OUTPUT_DIR=` lines in .env files).
_OUTPUT_DIR  = (
    os.environ.get("OUTPUT_DIR")
    or os.path.dirname(os.path.abspath(__file__))
)
_ARCHIVE_DIR = os.path.join(_OUTPUT_DIR, "archive")
# Older backtest_*.xlsx files get auto-moved here by run_full_backtest.
# The latest report sits at the top level alongside ev_edges_*.xlsx.
_BT_ARCHIVE_DIR = os.path.join(_OUTPUT_DIR, "Backtest Archives")

# Where static frontend files live.
_STATIC_DIR = Path(__file__).parent / "static"


# ── Token helpers ──────────────────────────────────────────────────────
# Two token formats accepted, one minted:
#
#   v2 format (minted by current code):
#       base64("v2:<user_id>:<expiry>:<sig>")
#       sig = hmac_sha256(secret, "<user_id>:<expiry>")
#
#   Legacy format (still accepted for backwards-compat with stale
#   localStorage tokens; treated as legacy_admin):
#       base64("<expiry>:<sig>")
#       sig = hmac_sha256(secret, "<expiry>")
#
# All login flows (signup, /api/login email+password, WEBAPP_PASSWORD
# fallback) mint v2 tokens. The legacy verify path only exists so users
# with an older token in localStorage don't get logged out by the
# auth-system upgrade.

import webapp_users


def _make_user_token(user_id: str, expiry_ts: int) -> str:
    """v2 token — embeds user_id so current_user can resolve the account."""
    payload = f"{user_id}:{expiry_ts}"
    sig = hmac.new(_TOKEN_SECRET, payload.encode(), hashlib.sha256).hexdigest()
    raw = f"v2:{payload}:{sig}".encode()
    return base64.urlsafe_b64encode(raw).decode()


def _verify_token(token: str) -> str | None:
    """Returns the authenticated user_id, or None if the token is bad/expired.

    Tries v2 format first (4 parts: 'v2', user_id, expiry, sig), falls
    back to legacy format (2 parts: expiry, sig → legacy_admin).
    """
    try:
        raw = base64.urlsafe_b64decode(token.encode()).decode()
    except (ValueError, TypeError, AttributeError):
        return None

    parts = raw.split(":")
    now = int(time.time())

    # v2 format: "v2:<user_id>:<expiry>:<sig>"
    if len(parts) == 4 and parts[0] == "v2":
        _, user_id, expiry_str, sig = parts
        try:
            expiry_ts = int(expiry_str)
        except ValueError:
            return None
        if expiry_ts < now:
            return None
        expected = hmac.new(
            _TOKEN_SECRET, f"{user_id}:{expiry_str}".encode(),
            hashlib.sha256,
        ).hexdigest()
        if hmac.compare_digest(sig, expected):
            return user_id
        return None

    # Legacy format: "<expiry>:<sig>" → treat as legacy_admin
    if len(parts) == 2:
        expiry_str, sig = parts
        try:
            expiry_ts = int(expiry_str)
        except ValueError:
            return None
        if expiry_ts < now:
            return None
        expected = hmac.new(
            _TOKEN_SECRET, expiry_str.encode(), hashlib.sha256
        ).hexdigest()
        if hmac.compare_digest(sig, expected):
            return webapp_users.LEGACY_ADMIN_ID
        return None

    return None


# ── Auth dependency ────────────────────────────────────────────────────

_bearer = HTTPBearer(auto_error=False)


def current_user(
    cred: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> dict:
    """Validates the Bearer token; returns the resolved user record.

    Returns a dict with keys: id, email, tier, created_at. The legacy
    admin (WEBAPP_PASSWORD shared login) returns a synthetic record
    with id='legacy_admin' and tier='admin'.
    """
    if cred is None or not cred.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    user_id = _verify_token(cred.credentials)
    if user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    user = webapp_users.find_by_id(user_id)
    if user is None:
        # Token validated but user record vanished (e.g., manual deletion
        # of webapp_users.json). Treat as auth failure.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Account not found — please sign in again",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return webapp_users.public(user)


def require_premium(user: dict = Depends(current_user)) -> dict:
    """Premium-gating dependency. Use on routes that require a paid tier.

    Currently no premium-only routes exist — this is the framework that
    item 5 wires up so future premium features are a one-liner away.
    """
    tier = user.get("tier")
    if tier in (webapp_users.TIER_PREMIUM, webapp_users.TIER_ADMIN):
        return user
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="This feature requires a Premium account.",
    )


# ── App factory ────────────────────────────────────────────────────────

def build_app() -> FastAPI:
    api = FastAPI(title="Odds Compare PWA",
                   docs_url=None, redoc_url=None)

    # Cache-busting for the PWA shell — applies to /static/* (StaticFiles
    # mount) plus any other text/JS responses we might add later. API
    # responses are unaffected (they're already volatile by nature).
    @api.middleware("http")
    async def no_cache_for_shell(request, call_next):
        response = await call_next(request)
        path = request.url.path
        if (path == "/" or path.startswith("/static/")
                or path in ("/sw.js", "/manifest.json")):
            response.headers["Cache-Control"] = (
                "no-store, no-cache, must-revalidate, max-age=0")
            response.headers["Pragma"]  = "no-cache"
            response.headers["Expires"] = "0"
        return response

    # ── Public ────────────────────────────────────────────────────────

    @api.get("/api/health")
    async def health() -> dict[str, Any]:
        # credit_status mirrors credit_tracker.get_status() so operators
        # can see the CLV pause-switch state without SSH'ing in. Lazy
        # import so a broken tracker file can't crash the health probe.
        try:
            import credit_tracker
            credit_status = credit_tracker.get_status()
        except Exception:  # pragma: no cover — defensive
            credit_status = None
        return {"status": "ok",
                "configured":    bool(_WEBAPP_PASSWORD),
                "credit_status": credit_status,
                "ts":            int(time.time())}

    @api.post("/api/login")
    async def login(req: Request) -> dict[str, Any]:
        """Two flows accepted:

        1. Email + password (per-user account from /api/signup)
        2. Password only — falls back to WEBAPP_PASSWORD legacy admin
           login. Existing users with the shared password keep working.

        Both mint a v2 user token; legacy admin gets user_id='legacy_admin'.
        """
        try:
            body = await req.json()
        except Exception:
            raise HTTPException(status_code=400, detail="JSON body required")
        body = body or {}
        email    = (body.get("email") or "").strip()
        password = body.get("password", "")
        if not isinstance(password, str) or not password:
            raise HTTPException(status_code=400, detail="Password required")

        user_id: str | None = None

        # Email + password → look up the user record and verify
        if email:
            user = webapp_users.find_by_email(email)
            if user and webapp_users.verify_password(user, password):
                user_id = user["id"]

        # No email OR email lookup failed → try the legacy admin password
        # (WEBAPP_PASSWORD env var). Skip if not configured.
        if user_id is None and _WEBAPP_PASSWORD:
            if hmac.compare_digest(password.encode(),
                                    _WEBAPP_PASSWORD.encode()):
                user_id = webapp_users.LEGACY_ADMIN_ID

        if user_id is None:
            # Fixed delay on failure: discourages online brute-force on
            # this public endpoint without leaking which path failed.
            time.sleep(0.5)
            raise HTTPException(status_code=401, detail="Invalid credentials")

        expiry_ts = int(time.time()) + _TOKEN_TTL_SECONDS
        token = _make_user_token(user_id, expiry_ts)
        return {"token": token, "expires_at": expiry_ts}

    @api.post("/api/signup")
    async def signup(req: Request) -> dict[str, Any]:
        """Create a new free-tier account. Returns a session token so
        the user is logged in immediately after signup."""
        try:
            body = await req.json()
        except Exception:
            raise HTTPException(status_code=400, detail="JSON body required")
        body = body or {}
        email             = body.get("email", "")
        password          = body.get("password", "")
        confirm_password  = body.get("confirm_password", "")

        if password != confirm_password:
            raise HTTPException(status_code=400,
                                 detail="Passwords do not match")

        try:
            user = webapp_users.create_user(email, password)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

        expiry_ts = int(time.time()) + _TOKEN_TTL_SECONDS
        token = _make_user_token(user["id"], expiry_ts)
        return {
            "token":      token,
            "expires_at": expiry_ts,
            "user":       user,
        }

    @api.get("/api/me")
    async def me(user: dict = Depends(current_user)) -> dict[str, Any]:
        """Return the authenticated user's account info — drives the
        Account tab. Excludes the password hash by virtue of going
        through current_user → webapp_users.public()."""
        return {"user": user}

    # ── Auth-required endpoints ───────────────────────────────────────

    @api.post("/api/run")
    async def run_scan(
        req: Request, _user: str = Depends(current_user)
    ) -> dict[str, Any]:
        """Trigger a full odds-compare run. Returns edges + summary as JSON.

        Body: {"dk_bankroll": float, "fd_bankroll": float}
        Defaults to the env var DEFAULT_DK_BANKROLL / DEFAULT_FD_BANKROLL.
        """
        try:
            body = await req.json()
        except Exception:
            body = {}
        body = body or {}

        dk = float(body.get("dk_bankroll") or
                    os.environ.get("DEFAULT_DK_BANKROLL", 100))
        fd = float(body.get("fd_bankroll") or
                    os.environ.get("DEFAULT_FD_BANKROLL", 100))

        # Parse the optional promos string (one promo per non-empty line)
        # using the bot's existing parser. Lets HR-applicable promos
        # unlock the batter_home_runs fetch via _promos_want_hr and
        # flow into compute_promo_picks for boost-aware picking.
        promos_raw = (body.get("promos") or "").strip()
        parsed_promos: list[dict[str, Any]] = []
        if promos_raw:
            from telegram_bot import parse_promo
            for line in promos_raw.splitlines():
                line = line.strip()
                if not line:
                    continue
                parsed = parse_promo(line)
                if parsed is not None:
                    parsed_promos.append(parsed)

        # Run the existing pipeline in a thread so we don't block the
        # asyncio loop (which is also serving Telegram polling).
        import asyncio
        from odds_compare import (run_headless_fetch, run_headless_finalize)

        def _do_run() -> dict[str, Any]:
            # When no promos are entered, pass promos=[] so the HR gate
            # fires and we save ~1 Odds API credit per MLB event. When
            # the user supplies HR-applicable promos, _promos_want_hr
            # unblocks the batter_home_runs fetch automatically.
            fetched = run_headless_fetch(dk, fd, promos=parsed_promos)
            return run_headless_finalize(fetched, promos=parsed_promos)

        result = await asyncio.to_thread(_do_run)
        return _shape_run_result(result)

    @api.post("/api/run_live")
    async def run_live(_user: str = Depends(current_user)) -> dict[str, Any]:
        import asyncio
        from odds_compare import run_headless_live

        edges = await asyncio.to_thread(run_headless_live)
        return {"edges": [_shape_edge(e) for e in edges],
                "count":  len(edges)}

    @api.get("/api/archives")
    async def list_archives(
        _user: str = Depends(current_user),
    ) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        for pat in (os.path.join(_ARCHIVE_DIR, "ev_edges_*.xlsx"),
                    os.path.join(_OUTPUT_DIR,  "ev_edges_*.xlsx")):
            for path in glob.glob(pat):
                name = os.path.basename(path)
                try:
                    st = os.stat(path)
                except OSError:
                    continue
                items.append({
                    "name":      name,
                    "size":      st.st_size,
                    "mtime":     int(st.st_mtime),
                    "an_only":   "_ANONLY" in name.upper(),
                })
        # Newest first
        items.sort(key=lambda x: x["mtime"], reverse=True)
        # Dedup by name (the archive/ + outputdir scan can overlap)
        seen: set[str] = set()
        uniq = []
        for it in items:
            if it["name"] in seen:
                continue
            seen.add(it["name"])
            uniq.append(it)
        return {"archives": uniq, "count": len(uniq)}

    @api.get("/api/archives/{name}")
    async def download_archive(
        name: str, _user: str = Depends(current_user),
    ) -> FileResponse:
        path = _resolve_archive(name)
        return FileResponse(
            path,
            filename=name,
            media_type=("application/vnd.openxmlformats-"
                        "officedocument.spreadsheetml.sheet"))

    @api.get("/api/archives/{name}/contents")
    async def archive_contents(
        name: str, _user: str = Depends(current_user),
    ) -> dict[str, Any]:
        """Render an archive's most useful tabs as JSON for in-app viewing.

        Pulled from the same readers backtest.py uses, so what shows here
        is exactly what the backtest grader sees — no parsing drift.
        """
        path = _resolve_archive(name)

        # Run synchronous openpyxl reads in a thread so we don't block
        # the asyncio loop while the bot is polling Telegram.
        import asyncio
        result = await asyncio.to_thread(_load_archive_contents, path, name)
        return result

    @api.get("/api/latest")
    async def latest_archive(
        _user: str = Depends(current_user),
    ) -> dict[str, Any]:
        """Return the most recent archive's contents — drives the dashboard.

        Identifies "most recent" by file mtime (matches /api/archives sort).
        Returns {"archive": null} if no archives exist yet.
        """
        # Discover the newest archive by mtime
        candidates: list[tuple[float, str, str]] = []  # (mtime, name, path)
        for parent in (_ARCHIVE_DIR, _OUTPUT_DIR):
            if not os.path.isdir(parent):
                continue
            for f in glob.glob(os.path.join(parent, "ev_edges_*.xlsx")):
                try:
                    candidates.append((os.path.getmtime(f),
                                        os.path.basename(f), f))
                except OSError:
                    continue
        if not candidates:
            return {"archive": None}

        # Newest by mtime (dedup by basename — archive/ + outputdir overlap)
        candidates.sort(key=lambda x: x[0], reverse=True)
        seen: set[str] = set()
        winner = None
        for mt, name, path in candidates:
            if name in seen:
                continue
            seen.add(name)
            winner = (mt, name, path)
            break

        if winner is None:
            return {"archive": None}

        _, name, path = winner
        import asyncio
        contents = await asyncio.to_thread(_load_archive_contents, path, name)
        return {"archive": contents}

    @api.get("/api/clv_today")
    async def clv_today(
        _user: str = Depends(current_user),
    ) -> dict[str, Any]:
        """Live CLV stats from today + yesterday's pending_closes_*.json.

        Surfaces what the closing-line worker has captured so far, refreshed
        on each hit. Cheap — just reads two small JSON files.
        """
        import asyncio
        return await asyncio.to_thread(_summarize_clv_today, _OUTPUT_DIR)

    @api.get("/api/backtest_archives")
    async def list_backtest_archives(
        _user: str = Depends(current_user),
    ) -> dict[str, Any]:
        """List backtest_*.xlsx reports from /data/output and Backtest Archives/.

        The 3 AM ET daily job writes one of these per day; ad-hoc /backtest
        runs add more. Newest first by mtime, dedup by basename.
        """
        items: list[dict[str, Any]] = []
        for pat in (os.path.join(_BT_ARCHIVE_DIR, "backtest_*.xlsx"),
                    os.path.join(_OUTPUT_DIR,     "backtest_*.xlsx")):
            for path in glob.glob(pat):
                name = os.path.basename(path)
                try:
                    st = os.stat(path)
                except OSError:
                    continue
                start_d, end_d = _bt_dates_from_name(name)
                # run_stamp comes from the filename when present (current
                # format); fall back to mtime so legacy files still show a
                # time. mtime is what `shutil.move` preserves on archive.
                run_stamp = _bt_run_stamp_from_name(name)
                if run_stamp is None:
                    run_stamp = datetime.fromtimestamp(
                        st.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
                items.append({
                    "name":     name,
                    "size":     st.st_size,
                    "mtime":    int(st.st_mtime),
                    "start":    start_d.isoformat() if start_d else None,
                    "end":      end_d.isoformat()   if end_d   else None,
                    "run_time": run_stamp,
                })
        items.sort(key=lambda x: x["mtime"], reverse=True)
        seen: set[str] = set()
        uniq = []
        for it in items:
            if it["name"] in seen:
                continue
            seen.add(it["name"])
            uniq.append(it)
        return {"archives": uniq, "count": len(uniq)}

    @api.get("/api/backtest_archives/{name}")
    async def download_backtest_archive(
        name: str, _user: str = Depends(current_user),
    ) -> FileResponse:
        path = _resolve_backtest_archive(name)
        return FileResponse(
            path,
            filename=name,
            media_type=("application/vnd.openxmlformats-"
                        "officedocument.spreadsheetml.sheet"))

    @api.get("/api/backtest_archives/{name}/contents")
    async def backtest_archive_contents(
        name: str, _user: str = Depends(current_user),
    ) -> dict[str, Any]:
        """Render a backtest report's Summary + All Picks tabs as JSON.

        Pure read of what's in the .xlsx — no re-grading, since the report
        was already graded against ESPN when written. That keeps the view
        cheap and means stale ESPN data can't change the historical numbers.
        """
        path = _resolve_backtest_archive(name)
        import asyncio
        return await asyncio.to_thread(_load_backtest_contents, path, name)

    @api.post("/api/backtest")
    async def backtest_run(
        req: Request, _user: str = Depends(current_user),
    ) -> dict[str, Any]:
        """Body:
            { "start": "YYYY-MM-DD",
              "end":   "YYYY-MM-DD",
              "an_only_mode": "exclude" | "include" | "only",
              "replay": bool }
        """
        try:
            body = await req.json()
        except Exception:
            body = {}
        body = body or {}

        try:
            start_dt = date.fromisoformat(body.get("start", ""))
            end_dt   = date.fromisoformat(body.get("end",   ""))
        except (ValueError, TypeError):
            raise HTTPException(
                status_code=400,
                detail="'start' and 'end' must be YYYY-MM-DD strings")

        an_mode = body.get("an_only_mode", "exclude")
        if an_mode not in ("exclude", "include", "only"):
            an_mode = "exclude"

        replay = bool(body.get("replay", False))

        import asyncio
        from backtest import run_full_backtest

        def _do_bt() -> dict[str, Any]:
            return run_full_backtest(start_dt, end_dt, an_only_mode=an_mode,
                                      replay=replay)

        result = await asyncio.to_thread(_do_bt)
        return _shape_backtest_result(result, start_dt, end_dt)

    # ── Static + PWA wiring ───────────────────────────────────────────

    # Headers that tell every layer (browser, any CDN/proxy, the SW
    # itself) to never serve a stale copy of the app shell. The PWA's
    # own SW is a no-op pass-through, but these belt-and-suspenders
    # headers protect against ALL caching layers — important for
    # rapid iteration during development.
    _NO_CACHE = {
        "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
        "Pragma":        "no-cache",
        "Expires":       "0",
    }

    if _STATIC_DIR.is_dir():
        api.mount("/static",
                   StaticFiles(directory=str(_STATIC_DIR)),
                   name="static")

        @api.get("/")
        async def root() -> FileResponse:
            return FileResponse(str(_STATIC_DIR / "index.html"),
                                headers=_NO_CACHE)

        @api.get("/manifest.json")
        async def manifest() -> FileResponse:
            return FileResponse(str(_STATIC_DIR / "manifest.json"),
                                headers=_NO_CACHE)

        @api.get("/sw.js")
        async def service_worker() -> FileResponse:
            return FileResponse(str(_STATIC_DIR / "sw.js"),
                                 media_type="application/javascript",
                                 headers=_NO_CACHE)

        # Escape hatch: nuke any stuck PWA state. Visit this URL once if
        # the app is showing stale UI after a deploy. Unregisters every
        # service worker, deletes every cache, then redirects to /.
        @api.get("/reset")
        async def reset_pwa() -> Any:
            from fastapi.responses import HTMLResponse
            html = """<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta http-equiv="Cache-Control" content="no-store">
<title>Arbyx — Reset</title>
<style>
body { background:#0F172A; color:#F1F5F9;
       font-family:-apple-system,sans-serif;
       padding:40px 20px; text-align:center; }
.spin { display:inline-block; width:20px; height:20px;
        border:3px solid #334155; border-top-color:#38BDF8;
        border-radius:50%; animation:s 0.7s linear infinite; }
@keyframes s { to { transform: rotate(360deg); } }
ul { text-align:left; max-width:400px; margin:20px auto; font-size:13px;
     color:#94A3B8; }
li { margin:4px 0; }
</style></head>
<body>
  <h2>Resetting Arbyx</h2>
  <p><span class="spin"></span></p>
  <ul id="log"></ul>
  <p id="done" style="display:none">All clear. Redirecting…</p>
  <script>
  const log = document.getElementById("log");
  const add = (m) => { const li = document.createElement("li");
    li.textContent = m; log.appendChild(li); };
  (async () => {
    try {
      if ("serviceWorker" in navigator) {
        const regs = await navigator.serviceWorker.getRegistrations();
        for (const r of regs) {
          await r.unregister();
          add("Unregistered SW: " + (r.scope || ""));
        }
        if (regs.length === 0) add("No service workers found.");
      }
      if ("caches" in window) {
        const keys = await caches.keys();
        for (const k of keys) {
          await caches.delete(k);
          add("Deleted cache: " + k);
        }
        if (keys.length === 0) add("No caches found.");
      }
      try { localStorage.clear(); add("Cleared localStorage."); }
      catch (_) {}
    } catch (e) { add("Error: " + e.message); }
    document.getElementById("done").style.display = "block";
    setTimeout(() => {
      // Cache-bust the redirect target so even the URL bar doesn't
      // pull a cached HTML response.
      window.location.replace("/?reset=" + Date.now());
    }, 1500);
  })();
  </script>
</body></html>"""
            return HTMLResponse(content=html, headers=_NO_CACHE)

    return api


# ── JSON shaping helpers ───────────────────────────────────────────────

def _edge_deep_link(e: dict[str, Any]) -> str | None:
    """Build a sportsbook event URL for this edge if the right book's
    event_id is present. Live edge dicts (from /api/run) carry these
    IDs directly — no sidecar lookup needed."""
    book = e.get("book")
    if book == "DraftKings" and e.get("dk_event_id"):
        return _DK_DEEP_LINK_BASE + str(e["dk_event_id"])
    if book == "FanDuel" and e.get("fd_event_id"):
        return _FD_DEEP_LINK_BASE + str(e["fd_event_id"])
    return None


def _shape_edge(e: dict[str, Any]) -> dict[str, Any]:
    """Trim an edge dict to a JSON-clean shape for the frontend."""
    return {
        "sport":         e.get("sport"),
        "game":          e.get("game"),
        "commence":      e.get("commence"),
        "book":          e.get("book"),
        "market":        e.get("market"),
        "outcome":       e.get("outcome"),
        "line":          e.get("line"),
        "odds":          e.get("odds"),
        "decimal":       e.get("odds_decimal"),
        "ev_pct":        e.get("ev_pct"),
        "fair_prob":     e.get("fair_prob"),
        "implied_prob":  e.get("implied_prob"),
        "kelly_pct":     e.get("kelly_pct"),
        "pinnacle_anchored": bool(e.get("pinnacle_anchored")),
        "deep_link":     _edge_deep_link(e),
    }


def _shape_promo_pick(p: dict[str, Any]) -> dict[str, Any]:
    """Shape one entry from compute_promo_picks() for the PWA.

    Each pick has shape {promo, edge, copy_idx, copy_of, reason,
    is_fallback}. When `edge` is None (no qualifying pick found), only
    the promo label + reason are surfaced so the UI can show the miss.
    """
    from odds_compare import apply_profit_boost, calc_ev, _draw_refund_ev_pct
    promo = p.get("promo") or {}
    edge  = p.get("edge")
    out: dict[str, Any] = {
        "promo_label":    promo.get("label"),
        "boost_pct":      promo.get("boost_pct"),
        "promo_type":     promo.get("promo_type"),
        "refund_on_draw": bool(promo.get("refund_on_draw")),
        "copy_idx":       p.get("copy_idx"),
        "copy_of":        p.get("copy_of"),
        "is_fallback":    bool(p.get("is_fallback")),
        "reason":         p.get("reason"),
    }
    if edge is None:
        return out
    out.update(_shape_edge(edge))
    # Add boosted fields when there's a profit-boost OR money-back-on-draw
    # promo. apply_profit_boost is a no-op for bonus_bet promos
    # (boost_pct=None), keeping the bonus-bet path on original odds; a pure
    # draw-insurance promo (no boost) still surfaces its EV via the refund.
    boost_pct   = promo.get("boost_pct")
    draw_refund = _draw_refund_ev_pct(promo, edge)
    if (boost_pct or draw_refund) and edge.get("odds_decimal"):
        boost_dec = apply_profit_boost(edge["odds_decimal"], boost_pct or 0)
        fair_prob = (edge.get("fair_prob") or 0) / 100
        out["boost_decimal"] = boost_dec
        out["boost_ev_pct"]  = round(calc_ev(fair_prob, boost_dec)
                                     + draw_refund, 2)
        if draw_refund:
            out["draw_refund_pct"] = round(draw_refund, 2)
    return out


def _shape_run_result(result: dict[str, Any]) -> dict[str, Any]:
    summary = result.get("summary", {})
    return {
        "summary": {
            "total_games":   summary.get("total_games", 0),
            "total_edges":   summary.get("total_edges", 0),
            "pinn_anchored": summary.get("pinn_anchored", 0),
            "an_only":       bool(summary.get("an_only", False)),
            "an_count":      summary.get("an_count", 0),
            "api_count":     summary.get("api_count", 0),
        },
        "play_edges":  [_shape_edge(e) for e in result.get("play_edges", [])],
        "all_edges":   [_shape_edge(e) for e in result.get("all_edges",  [])],
        "promo_picks": [_shape_promo_pick(p)
                        for p in result.get("promo_picks", [])],
        "output_path": (os.path.basename(result["output_path"])
                        if result.get("output_path") else None),
    }


def _shape_backtest_pick(p: dict[str, Any]) -> dict[str, Any]:
    """Bankroll-independent pick shape for the PWA — no dollar fields.

    Bet sizing is Kelly-fraction-of-bankroll, so absolute $ amounts vary
    per user but the W/L outcome and EV% don't. Per-pick unit return is
    derivable from odds + result if we ever want to display it.
    """
    return {
        "sport":      p.get("sport"),
        "game":       p.get("game"),
        "book":       p.get("book"),
        "market":     p.get("market"),
        "pick":       p.get("pick"),
        "odds":       p.get("odds"),
        "ev_pct":     p.get("ev_pct"),
        "result":     p.get("result"),
    }


# ── Archive in-app viewer helpers ──────────────────────────────────────

def _resolve_archive(name: str) -> str:
    """Validate name + return the absolute path. Raises 400/404."""
    if "/" in name or ".." in name or not name.endswith(".xlsx"):
        raise HTTPException(status_code=400, detail="Invalid archive name")
    for parent in (_ARCHIVE_DIR, _OUTPUT_DIR):
        cand = os.path.join(parent, name)
        if os.path.isfile(cand):
            return cand
    raise HTTPException(status_code=404, detail="Archive not found")


def _file_date_from_name(name: str) -> date | None:
    """Pull YYYYMMDD out of ev_edges_<date>_<time>[_ANONLY].xlsx."""
    import re
    m = re.search(r"ev_edges_(\d{8})_\d{4}", name)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y%m%d").date()
    except ValueError:
        return None


def _resolve_backtest_archive(name: str) -> str:
    """Validate name + return the absolute path. Raises 400/404."""
    if "/" in name or ".." in name or not name.endswith(".xlsx"):
        raise HTTPException(status_code=400, detail="Invalid archive name")
    if not name.startswith("backtest_"):
        raise HTTPException(status_code=400, detail="Invalid archive name")
    for parent in (_BT_ARCHIVE_DIR, _OUTPUT_DIR):
        cand = os.path.join(parent, name)
        if os.path.isfile(cand):
            return cand
    raise HTTPException(status_code=404, detail="Backtest report not found")


def _bt_dates_from_name(name: str) -> tuple[date | None, date | None]:
    """Pull (start, end) out of a backtest filename. Two shapes are accepted:
    - legacy: backtest_<start>_<end>.xlsx
    - current: backtest_<start>_<end>__<runstamp>.xlsx (each run gets its own
      file so re-running the same date range doesn't overwrite history)."""
    import re
    m = re.search(
        r"^backtest_(\d{4}-\d{2}-\d{2})_(\d{4}-\d{2}-\d{2})"
        r"(?:__\d{8}_\d{6})?\.xlsx$", name)
    if not m:
        return (None, None)
    try:
        return (date.fromisoformat(m.group(1)),
                date.fromisoformat(m.group(2)))
    except ValueError:
        return (None, None)


def _bt_run_stamp_from_name(name: str) -> str | None:
    """Pull the run timestamp out of a current-format backtest filename.
    Returns ISO 'YYYY-MM-DD HH:MM:SS' or None for legacy / unparseable."""
    import re
    m = re.search(
        r"^backtest_\d{4}-\d{2}-\d{2}_\d{4}-\d{2}-\d{2}"
        r"__(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})\.xlsx$", name)
    if not m:
        return None
    y, mo, d, hh, mm, ss = m.groups()
    return f"{y}-{mo}-{d} {hh}:{mm}:{ss}"


def _read_summary_tab(path: str) -> dict[str, Any]:
    """Read the Summary tab's known cells. Robust to the AN-only row shift
    (banner inserted at A1 pushes everything down by 1)."""
    import openpyxl
    out: dict[str, Any] = {
        "generated":      None,
        "dk_bankroll":    None,
        "fd_bankroll":    None,
        "kelly_fraction": None,
        "min_ev":         None,
        "total_edges":    None,
        "total_wagered":  None,
        "pinnacle":       None,
        "best_ev":        None,
    }
    try:
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    except Exception:
        return out
    if "Summary" not in wb.sheetnames:
        wb.close()
        return out
    ws = wb["Summary"]

    # Detect AN-only layout: A1 starts with the warning banner. Otherwise
    # A1 is "ODDS COMPARE — +EV EDGE FINDER".
    a1 = (ws["A1"].value or "")
    offset = 1 if isinstance(a1, str) and "ACTION NETWORK ONLY" in a1 else 0

    def at(addr: str) -> Any:
        return ws[addr].value

    # Generated timestamp lives one row below the title in either layout.
    out["generated"] = at(f"A{2 + offset}")  # "Generated: ..."

    # Bankrolls + config — labels in col A, values in col B.
    # Map by label search across rows 4..14 + offset (covers both layouts).
    label_map = {
        "DraftKings Bankroll:":     "dk_bankroll",
        "FanDuel Bankroll:":        "fd_bankroll",
        "Kelly Fraction:":          "kelly_fraction",
        "Min EV Threshold:":        "min_ev",
        "Total +EV Bets Found:":    "total_edges",
        "Total Capital Deployed:":  "total_wagered",
        "Pinnacle-Anchored Edges:": "pinnacle",
        "Best EV:":                 "best_ev",
    }
    for r in range(2, 18):
        a = at(f"A{r}")
        if not isinstance(a, str):
            continue
        a = a.strip()
        if a in label_map:
            out[label_map[a]] = at(f"B{r}")

    wb.close()
    return out


def _load_archive_contents(path: str, name: str) -> dict[str, Any]:
    """Reuse backtest.py's existing tab readers + graders.

    Picks are read from the .xlsx, then graded against ESPN results so
    the in-app view shows WIN/LOSS/PUSH/PENDING per pick and a daily ROI
    summary. Grading is wrapped in try/except so an ESPN outage degrades
    gracefully (results show "—" instead of breaking the page).
    """
    from backtest import (_read_play_these, _read_promo_picks,
                            _read_ev_edges,
                            _read_props_from_odds_comparison,
                            run_backtest, run_backtest_promo)

    file_dt = _file_date_from_name(name) or date.today()

    summary = _read_summary_tab(path)
    play    = _read_play_these(path, file_dt)
    all_ev  = _read_ev_edges(path, file_dt)   # broader "All Plays" pool
    promos  = _read_promo_picks(path, file_dt)
    props   = _read_props_from_odds_comparison(path, file_dt)

    # Bankrolls drive the kelly_pct back-calculation in shapers.
    bankrolls = {
        "DraftKings": _num(summary.get("dk_bankroll")),
        "FanDuel":    _num(summary.get("fd_bankroll")),
    }
    # The Kelly fraction the bot used when sizing the archived bets.
    # Default 0.5 (half-Kelly). Reading from odds_compare keeps us in sync
    # if the constant ever changes — divide stored kelly_pct by this to
    # recover the unscaled "full Kelly" used by the math.
    try:
        from odds_compare import KELLY_FRACTION as _kf
        kelly_fraction_runtime = float(_kf)
    except Exception:
        kelly_fraction_runtime = 0.5

    # Closing-line lookup — picks whose game has already hit its capture
    # window will get close_odds_american + clv_pct stamped onto them by
    # run_backtest. Range covers the file's date ± 1 day to absorb late
    # games that crossed midnight ET. Empty dict if the file isn't present
    # (legacy archive or no captures yet).
    try:
        from closing_lines import load_closing_lines_for_dates
        clv_lookup = load_closing_lines_for_dates(
            file_dt - timedelta(days=1), file_dt + timedelta(days=1))
    except Exception as e:
        log.warning(f"[archive] CLV lookup failed: {e}")
        clv_lookup = {}

    # Grade everything against ESPN. all_ev is graded too so "All Plays"
    # rows in the dashboard show their WIN/LOSS outcome. Passing clv_lookup
    # also stamps captured close odds + CLV % on each pick.
    from functools import partial
    _grade_with_clv = partial(run_backtest, closing_lines_lookup=clv_lookup)
    play   = _safe_grade(play,   _grade_with_clv,    "play_these")
    all_ev = _safe_grade(all_ev, _grade_with_clv,    "all_plays")
    promos = _safe_grade(promos, run_backtest_promo, "promo_picks")
    props  = _safe_grade(props,  _grade_with_clv,    "player_props")

    # Sportsbook deep links — restored from the sidecar that odds_compare
    # writes alongside the xlsx. xlsx round-trip drops the bookmaker IDs
    # from edge_dict, so the dashboard can't link to the event page
    # without this. {} when the sidecar is missing (older archives).
    book_links = _load_book_event_ids(_OUTPUT_DIR)

    return {
        "name":     name,
        "an_only":  "_ANONLY" in name.upper(),
        "file_date": file_dt.isoformat(),
        "kelly_fraction_runtime": kelly_fraction_runtime,
        "summary":  _shape_summary(summary),
        "play_these":  [_shape_archive_pick(p, bankrolls,
                                              kelly_fraction_runtime,
                                              book_links) for p in play],
        "all_plays":   [_shape_archive_pick(p, bankrolls,
                                              kelly_fraction_runtime,
                                              book_links) for p in all_ev],
        "promo_picks": [_shape_archive_promo(p) for p in promos],
        "player_props": [_shape_archive_prop(p) for p in props],
        "counts": {
            "play_these":   len(play),
            "all_plays":    len(all_ev),
            "promo_picks":  len(promos),
            "player_props": len(props),
        },
        # Daily results aggregate — bankroll-independent (ROI = pnl / wagered,
        # both scale linearly with bankroll so the ratio is constant).
        # Computed from Play These (the actionable filtered set).
        "results": _aggregate_archive_results(play + promos + props),
        # Same aggregate over the broader All Plays pool — lets the user
        # compare "what if I'd bet every +EV edge" against the filtered
        # Play These set, so the value of the filter is visible at a glance.
        "all_plays_results": _aggregate_archive_results(all_ev),
    }


def _num(v: Any) -> float | None:
    return v if isinstance(v, (int, float)) else None


def _safe_grade(picks: list[dict], grader, label: str) -> list[dict]:
    """Try to grade; on any failure, leave picks ungraded (frontend shows '—')."""
    if not picks:
        return picks
    try:
        return grader(picks)
    except Exception as e:
        log.warning(f"[archive] {label} grading failed: {e}")
        return picks


_PENDING_STATUSES = {"PENDING", "NOT_STARTED", "IN_PROGRESS"}


def _aggregate_archive_results(picks: list[dict]) -> dict[str, Any]:
    """W/L/P counts + bet-weighted ROI across all graded picks for one day."""
    settled = [p for p in picks
                if p.get("result") in ("WIN", "LOSS", "PUSH")]
    wins   = sum(1 for p in settled if p["result"] == "WIN")
    losses = sum(1 for p in settled if p["result"] == "LOSS")
    pushes = sum(1 for p in settled if p["result"] == "PUSH")
    not_started = sum(1 for p in picks if p.get("result") == "NOT_STARTED")
    in_progress = sum(1 for p in picks if p.get("result") == "IN_PROGRESS")
    legacy_pend = sum(1 for p in picks if p.get("result") == "PENDING")
    manual  = sum(1 for p in picks
                   if p.get("result") in ("MANUAL", "NO_RESULT"))
    pnl = sum(p.get("pnl") or 0 for p in settled)
    bet = sum(p.get("bet_amount") or 0 for p in settled)
    roi = (pnl / bet * 100) if bet else 0
    decided = wins + losses
    win_rate = (wins / decided * 100) if decided else 0
    return {
        "wins":        wins,
        "losses":      losses,
        "pushes":      pushes,
        "settled":     len(settled),
        # Backward-compat field summing all unfinished states
        "pending":     not_started + in_progress + legacy_pend,
        "not_started": not_started,
        "in_progress": in_progress,
        "manual":      manual,
        "roi_pct":     round(roi, 2),
        "win_rate":    round(win_rate, 1),
    }


def _unit_return_pct(p: dict[str, Any]) -> float | None:
    """Per-pick % return on the unit bet — bankroll-independent.

    WIN  -> (decimal - 1) * 100   (e.g. -110 winner = +90.9%)
    LOSS -> -100   (or 0 for free-bet promos where stake isn't at risk)
    PUSH -> 0
    Anything else -> None.
    """
    res = p.get("result")
    if res == "WIN":
        dec = p.get("decimal")
        if not isinstance(dec, (int, float)) or dec <= 1:
            return None
        return round((dec - 1) * 100, 2)
    if res == "LOSS":
        return 0.0 if p.get("is_free_bet") else -100.0
    if res == "PUSH":
        return 0.0
    return None


def _shape_summary(s: dict[str, Any]) -> dict[str, Any]:
    """Coerce openpyxl cell values to JSON-safe primitives."""
    def _num(v):
        if isinstance(v, (int, float)):
            return v
        return None
    def _str(v):
        if v is None:
            return None
        return str(v)
    gen = _str(s.get("generated"))
    if gen and gen.lower().startswith("generated:"):
        gen = gen.split(":", 1)[1].strip()
    return {
        "generated":      gen,
        "dk_bankroll":    _num(s.get("dk_bankroll")),
        "fd_bankroll":    _num(s.get("fd_bankroll")),
        "kelly_fraction": _str(s.get("kelly_fraction")),
        "min_ev":         _str(s.get("min_ev")),
        "total_edges":    _num(s.get("total_edges")),
        "total_wagered":  (round(s["total_wagered"], 2)
                           if isinstance(s.get("total_wagered"), (int, float))
                           else None),
        "pinnacle":       _str(s.get("pinnacle")),
        "best_ev":        _str(s.get("best_ev")),
    }


def _round(v: Any, n: int = 2) -> Any:
    if isinstance(v, (int, float)):
        return round(v, n)
    return v


# ── Sportsbook deep links ──────────────────────────────────────────────
# URL bases mirror telegram_bot._deep_link so both surfaces hit the same
# event pages. DK and FD redirect users to their state's sportsbook
# automatically, so a single base works for any legal jurisdiction.
_DK_DEEP_LINK_BASE = "https://sportsbook.draftkings.com/event/"
_FD_DEEP_LINK_BASE = "https://sportsbook.fanduel.com/sport/event/"


def _load_book_event_ids(output_dir: str) -> dict:
    """Load the latest_book_event_ids.json sidecar — pick_key →
    {dk_event_id, fd_event_id}. Returns {} if the sidecar doesn't
    exist yet (older archives, or scan hasn't run since deploy)."""
    path = os.path.join(output_dir, "latest_book_event_ids.json")
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
        return payload.get("picks", {}) or {}
    except (json.JSONDecodeError, OSError) as e:
        log.warning(f"[book-links] could not read {path}: {e}")
        return {}


def _resolve_deep_link(pick: dict[str, Any],
                        book_links: dict | None) -> str | None:
    """Look up the sportsbook event URL for this pick, or None if not
    available (no sidecar entry, or no event_id for this pick's book)."""
    if not book_links:
        return None
    # Lazy import — webapp.py is imported during cold start; pulling
    # backtest.py in unconditionally would chain in openpyxl/etc.
    from backtest import _backtest_pick_key
    entry = book_links.get(_backtest_pick_key(pick))
    if not entry:
        return None
    book = pick.get("book")
    if book == "DraftKings" and entry.get("dk_event_id"):
        return _DK_DEEP_LINK_BASE + str(entry["dk_event_id"])
    if book == "FanDuel" and entry.get("fd_event_id"):
        return _FD_DEEP_LINK_BASE + str(entry["fd_event_id"])
    return None


def _shape_archive_pick(p: dict[str, Any],
                          bankrolls: dict[str, float | None] | None = None,
                          kelly_fraction_runtime: float = 0.5,
                          book_links: dict | None = None,
                          ) -> dict[str, Any]:
    """Per-pick shape with bankroll-independent stake info.

    kelly_pct      — what the bot ACTUALLY staked at run time (= full Kelly
                     × the runtime fraction, default 0.5). This is the
                     ratio of bet_amount to bankroll, recovered from the
                     Summary tab's bankroll field.
    full_kelly_pct — the unscaled Kelly Criterion fraction (= kelly_pct /
                     runtime fraction). Frontend multiplies this by the
                     user's chosen Kelly fraction to recompute BR % live
                     without re-fetching.
    book_links     — optional sidecar lookup mapping pick_key →
                     {dk_event_id, fd_event_id}. When the right book's
                     ID is present, populates 'deep_link' so the frontend
                     can render the pick title as a sportsbook link.
    """
    bet  = p.get("bet_amount")
    book = p.get("book")
    bank = (bankrolls or {}).get(book)
    # Prefer the file-stored Kelly% (always present, even on /run_skip
    # archives where bankroll=0 makes bet/bankroll undefined). Fall back
    # to deriving it from bet/bankroll for older archives that didn't
    # round-trip the Kelly column through the readers.
    file_kelly = p.get("kelly_pct")
    kelly_pct      = None
    full_kelly_pct = None
    if isinstance(file_kelly, (int, float)) and file_kelly >= 0:
        kelly_pct = round(float(file_kelly), 3)
    elif (isinstance(bet, (int, float)) and bet > 0
            and isinstance(bank, (int, float)) and bank > 0):
        kelly_pct = round(bet / bank * 100, 3)
    if kelly_pct is not None and kelly_fraction_runtime > 0:
        full_kelly_pct = round(kelly_pct / kelly_fraction_runtime, 3)
    # Close odds — formatted with sign so frontend can render directly.
    close_am = p.get("close_odds_american")
    if isinstance(close_am, (int, float)):
        close_str = f"+{int(close_am)}" if close_am > 0 else str(int(close_am))
    else:
        close_str = None
    return {
        "sport":      p.get("sport"),
        "game":       p.get("game"),
        "game_time":  p.get("game_time"),
        "book":       book,
        "market":     p.get("market"),
        "pick":       p.get("pick"),
        "line":       p.get("line"),
        "odds":       p.get("odds"),
        "ev_pct":     _round(p.get("ev_pct"), 2),
        "kelly_pct":      kelly_pct,
        "full_kelly_pct": full_kelly_pct,
        "confidence": p.get("confidence"),
        "result":     p.get("result"),
        "unit_return_pct": _unit_return_pct(p),
        "deep_link":  _resolve_deep_link(p, book_links),
        # Closing line value — stamped by run_backtest when the closing
        # worker captured this pick. None for picks whose game hasn't
        # hit the 5-min-pre-tip-off capture window yet, or AN-fallback
        # picks (no Odds API event_id, so uncapturable).
        "close_odds": close_str,
        "clv_pct":    _round(p.get("clv_pct"), 2),
    }


def _shape_archive_promo(p: dict[str, Any]) -> dict[str, Any]:
    return {
        "sport":           p.get("sport"),
        "game":            p.get("game"),
        "book":            p.get("book"),
        "market":          p.get("market"),
        "pick":            p.get("pick"),
        "line":            p.get("line"),
        "orig_odds":       p.get("orig_odds"),
        "boost_odds":      p.get("odds"),
        "boost_odds_str":  p.get("boost_odds_str"),
        "orig_ev":         p.get("orig_ev"),
        "boost_ev":        p.get("boost_ev"),
        "ev_gain":         p.get("ev_gain"),
        "promo":           p.get("promo_desc") or p.get("promo"),
        "result":          p.get("result"),
        "unit_return_pct": _unit_return_pct(p),
    }


def _shape_archive_prop(p: dict[str, Any]) -> dict[str, Any]:
    return {
        "sport":     p.get("sport"),
        "game":      p.get("game"),
        "player":    p.get("player"),
        "market":    p.get("market"),
        "side":      p.get("side"),
        "line":      p.get("line"),
        "odds":      p.get("odds"),
        "book":      p.get("book"),
        "ev_pct":    p.get("ev_pct"),
        "result":    p.get("result"),
        "unit_return_pct": _unit_return_pct(p),
    }


# ── Backtest archive viewer helpers ────────────────────────────────────

_BT_SUMMARY_LABEL_MAP = {
    "Total Unique Picks":        "total_picks",
    "Settled (W / L / P)":       "settled",
    "Pending (future games)":    "pending",
    "Pending":                   "pending",
    "Manual / Unresolved":       "manual",
    "Wins":                      "wins",
    "Losses":                    "losses",
    "Pushes":                    "pushes",
    "Win Rate (excl. pushes)":   "win_rate",
    "Total Wagered":             "total_wagered",
    "Total P&L":                 "total_pnl",
    "ROI":                       "roi",
}

_BT_PT_ANCHOR = "▶ PLAY THESE"


def _empty_bt_summary() -> dict[str, Any]:
    return {k: None for k in (
        "generated", "date_range", "total_picks", "settled", "pending",
        "manual", "wins", "losses", "pushes", "win_rate",
        "total_wagered", "total_pnl", "roi",
    )}


def _read_bt_summary_tab(path: str) -> dict[str, Any]:
    """Read both summary blocks from a backtest report's Summary tab.

    The writer lays out two stat blocks under shared labels: an "All +EV
    picks" block at the top, then a "▶ PLAY THESE (Filtered Picks)"
    block lower down. Splits on the anchor row so each block's labels
    don't collide.

    Returns {"all_ev": {...}, "pt": {...}}. Either block may have all-None
    values if absent from the file.
    """
    import openpyxl
    out = {"all_ev": _empty_bt_summary(), "pt": _empty_bt_summary()}
    try:
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    except Exception:
        return out
    if "Summary" not in wb.sheetnames:
        wb.close()
        return out
    ws = wb["Summary"]

    def _at(addr: str) -> Any:
        return ws[addr].value

    out["all_ev"]["date_range"] = _at("A2")
    out["all_ev"]["generated"]  = _at("A3")
    out["pt"]["date_range"]     = out["all_ev"]["date_range"]
    out["pt"]["generated"]      = out["all_ev"]["generated"]

    section = "all_ev"
    for r in range(2, 120):
        a = _at(f"A{r}")
        if not isinstance(a, str):
            continue
        a = a.strip()
        if _BT_PT_ANCHOR in a:
            section = "pt"
            continue
        if a in _BT_SUMMARY_LABEL_MAP:
            out[section][_BT_SUMMARY_LABEL_MAP[a]] = _at(f"B{r}")

    wb.close()
    return out


def _read_bt_pt_by_sport(path: str) -> list[dict[str, Any]]:
    """Read the "PT By Sport" tab into the same shape as the computed
    by_sport breakdown so it can drop into the API response directly.

    Returns [] if the tab is missing (older reports). Skips the TOTAL row.
    """
    import openpyxl
    if not os.path.isfile(path):
        return []
    try:
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    except Exception:
        return []
    if "PT By Sport" not in wb.sheetnames:
        wb.close()
        return []
    ws = wb["PT By Sport"]
    rows: list[dict[str, Any]] = []
    for row in ws.iter_rows(min_row=2, values_only=True):
        if not row or row[0] is None:
            continue
        sport = str(row[0]).strip()
        if sport.upper() == "TOTAL":
            continue
        try:
            picks_n   = int(row[1]) if row[1] is not None else 0
            settled_n = int(row[2]) if row[2] is not None else 0
            wins      = int(row[3]) if row[3] is not None else 0
            losses    = int(row[4]) if row[4] is not None else 0
        except (TypeError, ValueError):
            continue
        wr_raw  = row[6] if len(row) > 6 else None
        roi_raw = row[9] if len(row) > 9 else None
        win_rate = (round(wr_raw  * 100, 1)
                    if isinstance(wr_raw,  (int, float)) else None)
        roi_pct  = (round(roi_raw * 100, 2)
                    if isinstance(roi_raw, (int, float)) else None)
        decided = wins + losses
        rows.append({
            "sport":         sport,
            "picks":         picks_n,
            "settled":       settled_n,
            "wins":          wins,
            "losses":        losses,
            "win_rate":      win_rate if decided else None,
            "roi_pct":       roi_pct,
            "avg_clv_pct":   None,
            "clv_captured":  0,
        })
    wb.close()
    rows.sort(key=lambda x: (x["roi_pct"] is None, -(x["roi_pct"] or 0)))
    return rows


def _read_bt_all_picks(path: str) -> list[dict[str, Any]]:
    """Read every row of the All Picks tab.

    Column layout (set by write_backtest_excel):
      1  #          5  Game     9  Line    13 Bet Amt
      2  File Date  6  Book     10 Odds    14 Seen
      3  Game Date  7  Market   11 EV %    15 Persistent
      4  Sport      8  Pick     12 Conf.   16 Result    17 P&L
                                                        18 Close   19 CLV %

    Pre-CLV xlsx files (written before 2026-05-05) only have 17 columns;
    we tolerate that by indexing defensively.
    """
    import openpyxl
    if not os.path.isfile(path):
        return []
    try:
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    except Exception:
        return []
    if "All Picks" not in wb.sheetnames:
        wb.close()
        return []
    ws = wb["All Picks"]
    picks: list[dict[str, Any]] = []
    for row in ws.iter_rows(min_row=2, values_only=True):
        if not row or row[0] is None:
            continue
        # Older xlsx pre-date the CLV columns — guard with len()
        close_odds = _bt_cell_str(row[17]) if len(row) > 17 else None
        clv_pct    = row[18]               if len(row) > 18 else None
        picks.append({
            "file_date":  _bt_cell_str(row[1]),
            "game_date":  _bt_cell_str(row[2]),
            "sport":      _bt_cell_str(row[3]),
            "game":       _bt_cell_str(row[4]),
            "book":       _bt_cell_str(row[5]),
            "market":     _bt_cell_str(row[6]),
            "pick":       _bt_cell_str(row[7]),
            "line":       row[8],
            "odds":       row[9],
            "ev_pct":     row[10],
            "confidence": _bt_cell_str(row[11]),
            "bet_amount": row[12],
            "result":     (_bt_cell_str(row[15]) or "").upper() or None,
            "pnl":        row[16],
            "close_odds": close_odds,
            "clv_pct":    clv_pct if isinstance(clv_pct, (int, float)) else None,
        })
    wb.close()
    return picks


def _bt_cell_str(v: Any) -> str | None:
    if v is None:
        return None
    return str(v).strip() or None


def _bt_unit_return_pct(p: dict[str, Any]) -> float | None:
    """% return on a unit stake — bankroll-independent.

    Re-derived from American odds since the .xlsx doesn't store decimals.
    """
    res = p.get("result")
    if res == "PUSH":
        return 0.0
    if res == "LOSS":
        return -100.0
    if res != "WIN":
        return None
    o = p.get("odds")
    if not isinstance(o, (int, float)):
        return None
    if o > 0:
        return round(o, 2) * 1.0
    if o < 0:
        return round(100.0 * 100.0 / abs(o), 2)
    return None


def _shape_bt_pick(p: dict[str, Any]) -> dict[str, Any]:
    return {
        "file_date":  p.get("file_date"),
        "game_date":  p.get("game_date"),
        "sport":      p.get("sport"),
        "game":       p.get("game"),
        "book":       p.get("book"),
        "market":     p.get("market"),
        "pick":       p.get("pick"),
        "line":       p.get("line"),
        "odds":       p.get("odds"),
        "ev_pct":     _round(p.get("ev_pct"), 2),
        "confidence": p.get("confidence"),
        "result":     p.get("result"),
        "unit_return_pct": _bt_unit_return_pct(p),
        "close_odds": p.get("close_odds"),
        "clv_pct":    _round(p.get("clv_pct"), 2),
    }


def _shape_bt_summary(s: dict[str, Any]) -> dict[str, Any]:
    """Coerce openpyxl cell values to JSON-safe primitives."""
    def _num(v):
        return v if isinstance(v, (int, float)) else None
    def _str(v):
        return None if v is None else str(v)
    gen = _str(s.get("generated"))
    if gen and gen.lower().startswith("generated:"):
        gen = gen.split(":", 1)[1].strip()
    rng = _str(s.get("date_range"))
    if rng and rng.lower().startswith("date range:"):
        rng = rng.split(":", 1)[1].strip()
    return {
        "generated":     gen,
        "date_range":    rng,
        "total_picks":   _num(s.get("total_picks")),
        "settled":       _num(s.get("settled")),
        "pending":       _num(s.get("pending")),
        "manual":        _num(s.get("manual")),
        "wins":          _num(s.get("wins")),
        "losses":        _num(s.get("losses")),
        "pushes":        _num(s.get("pushes")),
        "win_rate":      _str(s.get("win_rate")),
        "total_wagered": (round(s["total_wagered"], 2)
                          if isinstance(s.get("total_wagered"), (int, float))
                          else None),
        "total_pnl":     (round(s["total_pnl"], 2)
                          if isinstance(s.get("total_pnl"), (int, float))
                          else None),
        "roi":           _str(s.get("roi")),
    }


def _avg_clv(picks: list[dict[str, Any]]) -> tuple[float | None, int]:
    """Average CLV % across the subset of `picks` that have a captured CLV.
    Returns (avg_clv_pct, captured_count). avg is None if no captures."""
    captured = [p for p in picks if isinstance(p.get("clv_pct"), (int, float))]
    if not captured:
        return None, 0
    return round(sum(p["clv_pct"] for p in captured) / len(captured), 2), \
           len(captured)


def _summarize_clv_today(output_dir: str) -> dict[str, Any]:
    """Aggregate today + yesterday's pending_closes_*.json files for the
    live dashboard widget.

    Reads the same files the closing-line worker writes (today + yesterday
    ET, since a midnight-crossing capture window straddles two files).
    Returns a flat shape the frontend can render directly:
        {
            "tracked":           int   (total pending picks across files),
            "captured":          int   (subset whose close was logged),
            "avg_clv_pct":       float | None,
            "positive":          int,
            "negative":          int,
            "by_book":           [{book, captured, tracked, avg_clv_pct}, ...],
            "last_capture_iso":  str | None  (ISO ts of latest captured_at),
        }
    """
    from closing_lines import _list_recent_pending_files

    paths = _list_recent_pending_files(output_dir)
    tracked = 0
    captured: list[dict[str, Any]] = []
    by_book: dict[str, dict[str, Any]] = {}
    last_capture: str | None = None

    for path in paths:
        try:
            with open(path, "r", encoding="utf-8") as f:
                payload = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue
        for pick in (payload.get("picks") or {}).values():
            tracked += 1
            book = pick.get("book") or "—"
            slot = by_book.setdefault(
                book, {"book": book, "tracked": 0, "captured": 0, "_clvs": []})
            slot["tracked"] += 1
            close = pick.get("close") or {}
            clv = close.get("clv_pct")
            if isinstance(clv, (int, float)):
                captured.append(pick)
                slot["captured"] += 1
                slot["_clvs"].append(clv)
                ts = close.get("captured_at")
                if ts and (last_capture is None or ts > last_capture):
                    last_capture = ts

    if captured:
        all_clvs = [p["close"]["clv_pct"] for p in captured]
        avg_clv_pct = round(sum(all_clvs) / len(all_clvs), 2)
        positive = sum(1 for c in all_clvs if c > 0)
        negative = sum(1 for c in all_clvs if c < 0)
    else:
        avg_clv_pct, positive, negative = None, 0, 0

    by_book_out = []
    for slot in by_book.values():
        clvs = slot.pop("_clvs")
        slot["avg_clv_pct"] = (round(sum(clvs) / len(clvs), 2)
                                if clvs else None)
        by_book_out.append(slot)
    by_book_out.sort(key=lambda b: b["captured"], reverse=True)

    return {
        "tracked":          tracked,
        "captured":         len(captured),
        "avg_clv_pct":      avg_clv_pct,
        "positive":         positive,
        "negative":         negative,
        "by_book":          by_book_out,
        "last_capture_iso": last_capture,
    }


def _load_backtest_contents(path: str, name: str) -> dict[str, Any]:
    """Read a backtest report's Summary + All Picks tabs into JSON.

    Headline numbers (summary, by_sport) come from the Play These slice —
    that's what the bot actually recommends, so it's the meaningful read
    of the bot's edge. The All +EV slice (every detected pick, including
    filter-rejects) is exposed alongside as `summary_all_ev` and
    `by_sport_all_ev` for drill-down. Old reports without a PT block
    fall back to All +EV as the headline.

    No re-grading — results are read straight from the file as written.
    """
    summary_blocks = _read_bt_summary_tab(path)
    picks   = _read_bt_all_picks(path)
    start_d, end_d = _bt_dates_from_name(name)

    by_sport_all_ev = []
    for sport, sg in sorted(
        {((p.get("sport") or "").upper() or "OTHER"): None for p in picks}.items()
    ):
        sg = [p for p in picks
              if ((p.get("sport") or "").upper() or "OTHER") == sport]
        settled = [x for x in sg
                    if (x.get("result") or "") in ("WIN", "LOSS", "PUSH")]
        wins = sum(1 for x in settled if x["result"] == "WIN")
        loss = sum(1 for x in settled if x["result"] == "LOSS")
        pnl  = sum((x.get("pnl") or 0) for x in settled)
        bet  = sum((x.get("bet_amount") or 0) for x in settled)
        roi  = (pnl / bet * 100) if bet else 0
        decided = wins + loss
        avg_clv_pct, clv_n = _avg_clv(settled)
        by_sport_all_ev.append({
            "sport":            sport,
            "picks":            len(sg),
            "settled":          len(settled),
            "wins":             wins,
            "losses":           loss,
            "win_rate":         round(wins / decided * 100, 1) if decided else None,
            "roi_pct":          round(roi, 2) if bet else None,
            "avg_clv_pct":      avg_clv_pct,
            "clv_captured":     clv_n,
        })
    by_sport_all_ev.sort(key=lambda x: (x["roi_pct"] is None,
                                         -(x["roi_pct"] or 0)))

    settled_all = [p for p in picks
                   if (p.get("result") or "") in ("WIN", "LOSS", "PUSH")]
    avg_clv_pct, clv_captured_count = _avg_clv(settled_all)

    summary_all_ev = _shape_bt_summary(summary_blocks["all_ev"])
    # Avg CLV is re-derived from picks (the xlsx writes it as a formatted
    # string awkward to round-trip). Only the All +EV slice has a CLV
    # figure today — PT-only CLV would need PT-tagged picks, which the
    # report doesn't store. So PT headlines render without a CLV cell;
    # the All +EV drill-down keeps it.
    summary_all_ev["avg_clv_pct"]   = avg_clv_pct
    summary_all_ev["clv_captured"]  = clv_captured_count

    summary_pt = _shape_bt_summary(summary_blocks["pt"])
    summary_pt["avg_clv_pct"]  = None
    summary_pt["clv_captured"] = 0

    by_sport_pt = _read_bt_pt_by_sport(path)

    has_pt = summary_pt.get("settled") is not None
    headline_summary = summary_pt if has_pt else summary_all_ev
    headline_by_sport = by_sport_pt if has_pt and by_sport_pt else by_sport_all_ev

    return {
        "name":  name,
        "start": start_d.isoformat() if start_d else None,
        "end":   end_d.isoformat()   if end_d   else None,
        "summary":          headline_summary,
        "summary_all_ev":   summary_all_ev,
        "picks":            [_shape_bt_pick(p) for p in picks],
        "by_sport":         headline_by_sport,
        "by_sport_all_ev":  by_sport_all_ev,
        "headline_source":  "play_these" if has_pt else "all_ev",
        "counts": {
            "total":   len(picks),
            "settled": len(settled_all),
        },
    }


def _shape_backtest_result(result: dict[str, Any],
                             start_dt: date, end_dt: date) -> dict[str, Any]:
    """Bankroll-independent backtest summary.

    ROI = sum(pnl) / sum(wagered). Both numerator and denominator scale
    linearly with bankroll (bet size = Kelly% × bankroll), so the ratio is
    constant regardless of which user runs the backtest. Win rate, settled
    count, and per-pick result are also bankroll-independent.
    """
    picks = result.get("play_these_graded", []) or []
    settled = [p for p in picks
                if p.get("result") in ("WIN", "LOSS", "PUSH")]
    wins   = sum(1 for p in settled if p["result"] == "WIN")
    losses = sum(1 for p in settled if p["result"] == "LOSS")
    pushes = sum(1 for p in settled if p["result"] == "PUSH")
    decided = wins + losses
    win_rate = (wins / decided * 100) if decided else 0.0
    # ROI is computed from the underlying $ figures but only the ratio is
    # exposed — the absolute amounts depend on the running user's bankroll.
    pnl = sum(p.get("pnl") or 0 for p in settled)
    bet = sum(p.get("bet_amount") or 0 for p in settled)
    roi = (pnl / bet * 100) if bet else 0.0
    return {
        "start": start_dt.isoformat(),
        "end":   end_dt.isoformat(),
        "summary": {
            "settled":   len(settled),
            "pending":   len(picks) - len(settled),
            "wins":      wins,
            "losses":    losses,
            "pushes":    pushes,
            "win_rate":  round(win_rate, 1),
            "roi_pct":   round(roi, 2),
        },
        "picks":         [_shape_backtest_pick(p) for p in picks],
        "excel_path":    (os.path.basename(result["excel_path"])
                          if result.get("excel_path") else None),
    }


# Module-level instance for uvicorn import-string mode if ever used:
#   uvicorn webapp:api --host 0.0.0.0 --port 8080
api = build_app()
