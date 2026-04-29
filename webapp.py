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
_OUTPUT_DIR  = os.environ.get("OUTPUT_DIR", os.path.dirname(
    os.path.abspath(__file__)))
_ARCHIVE_DIR = os.path.join(_OUTPUT_DIR, "archive")

# Where static frontend files live.
_STATIC_DIR = Path(__file__).parent / "static"


# ── Token helpers ──────────────────────────────────────────────────────

def _make_token(expiry_ts: int) -> str:
    """HMAC-signed token: base64('<expiry>:<sig>')."""
    msg = str(expiry_ts).encode()
    sig = hmac.new(_TOKEN_SECRET, msg, hashlib.sha256).hexdigest()
    raw = f"{expiry_ts}:{sig}".encode()
    return base64.urlsafe_b64encode(raw).decode()


def _verify_token(token: str) -> bool:
    try:
        raw = base64.urlsafe_b64decode(token.encode()).decode()
        expiry_str, sig = raw.split(":", 1)
        expiry_ts = int(expiry_str)
        if expiry_ts < int(time.time()):
            return False
        expected = hmac.new(
            _TOKEN_SECRET, expiry_str.encode(), hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(sig, expected)
    except (ValueError, TypeError, AttributeError):
        return False


# ── Auth dependency ────────────────────────────────────────────────────

_bearer = HTTPBearer(auto_error=False)


def current_user(
    cred: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> str:
    """Validates the Bearer token. Returns a user id ('owner' for v1)."""
    if cred is None or not cred.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not _verify_token(cred.credentials):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    # Single-user for now. Multi-user later: decode user id from token.
    return "owner"


# ── App factory ────────────────────────────────────────────────────────

def build_app() -> FastAPI:
    api = FastAPI(title="Odds Compare PWA",
                   docs_url=None, redoc_url=None)

    # ── Public ────────────────────────────────────────────────────────

    @api.get("/api/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok",
                "configured": bool(_WEBAPP_PASSWORD),
                "ts": int(time.time())}

    @api.post("/api/login")
    async def login(req: Request) -> dict[str, Any]:
        if not _WEBAPP_PASSWORD:
            raise HTTPException(
                status_code=503,
                detail=("WEBAPP_PASSWORD not set. "
                        "Run: fly secrets set WEBAPP_PASSWORD=<your-pw>"))
        try:
            body = await req.json()
        except Exception:
            raise HTTPException(status_code=400, detail="JSON body required")
        password = (body or {}).get("password", "")
        if not isinstance(password, str) or not password:
            raise HTTPException(status_code=400, detail="Password required")

        # Constant-time compare to avoid leaking length / first-mismatch
        # via timing. Plus a fixed delay on wrong attempts to discourage
        # online brute force on a public endpoint.
        if not hmac.compare_digest(password.encode(),
                                    _WEBAPP_PASSWORD.encode()):
            time.sleep(0.5)
            raise HTTPException(status_code=401, detail="Invalid password")

        expiry_ts = int(time.time()) + _TOKEN_TTL_SECONDS
        token = _make_token(expiry_ts)
        return {"token": token, "expires_at": expiry_ts}

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

        # Run the existing pipeline in a thread so we don't block the
        # asyncio loop (which is also serving Telegram polling).
        import asyncio
        from odds_compare import (run_headless_fetch, run_headless_finalize)

        def _do_run() -> dict[str, Any]:
            fetched = run_headless_fetch(dk, fd)
            return run_headless_finalize(fetched, promos=None)

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
        # Defense-in-depth: only allow simple basenames.
        if "/" in name or ".." in name or not name.endswith(".xlsx"):
            raise HTTPException(status_code=400, detail="Invalid archive name")
        for parent in (_ARCHIVE_DIR, _OUTPUT_DIR):
            cand = os.path.join(parent, name)
            if os.path.isfile(cand):
                return FileResponse(
                    cand,
                    filename=name,
                    media_type=("application/vnd.openxmlformats-"
                                "officedocument.spreadsheetml.sheet"))
        raise HTTPException(status_code=404, detail="Archive not found")

    @api.post("/api/backtest")
    async def backtest_run(
        req: Request, _user: str = Depends(current_user),
    ) -> dict[str, Any]:
        """Body:
            { "start": "YYYY-MM-DD",
              "end":   "YYYY-MM-DD",
              "an_only_mode": "exclude" | "include" | "only" }
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

        import asyncio
        from backtest import run_full_backtest

        def _do_bt() -> dict[str, Any]:
            return run_full_backtest(start_dt, end_dt, an_only_mode=an_mode)

        result = await asyncio.to_thread(_do_bt)
        return _shape_backtest_result(result, start_dt, end_dt)

    # ── Static + PWA wiring ───────────────────────────────────────────

    if _STATIC_DIR.is_dir():
        api.mount("/static",
                   StaticFiles(directory=str(_STATIC_DIR)),
                   name="static")

        @api.get("/")
        async def root() -> FileResponse:
            return FileResponse(str(_STATIC_DIR / "index.html"))

        @api.get("/manifest.json")
        async def manifest() -> FileResponse:
            return FileResponse(str(_STATIC_DIR / "manifest.json"))

        @api.get("/sw.js")
        async def service_worker() -> FileResponse:
            return FileResponse(str(_STATIC_DIR / "sw.js"),
                                 media_type="application/javascript")

    return api


# ── JSON shaping helpers ───────────────────────────────────────────────

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
    }


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
        "play_edges": [_shape_edge(e) for e in result.get("play_edges", [])],
        "all_edges":  [_shape_edge(e) for e in result.get("all_edges",  [])],
        "promo_picks": result.get("promo_picks", []),
        "output_path": (os.path.basename(result["output_path"])
                        if result.get("output_path") else None),
    }


def _shape_backtest_pick(p: dict[str, Any]) -> dict[str, Any]:
    return {
        "sport":      p.get("sport"),
        "game":       p.get("game"),
        "book":       p.get("book"),
        "market":     p.get("market"),
        "pick":       p.get("pick"),
        "odds":       p.get("odds"),
        "ev_pct":     p.get("ev_pct"),
        "result":     p.get("result"),
        "pnl":        p.get("pnl"),
        "bet_amount": p.get("bet_amount"),
    }


def _shape_backtest_result(result: dict[str, Any],
                             start_dt: date, end_dt: date) -> dict[str, Any]:
    picks = result.get("play_these_graded", []) or []
    settled = [p for p in picks
                if p.get("result") in ("WIN", "LOSS", "PUSH")]
    wins   = sum(1 for p in settled if p["result"] == "WIN")
    losses = sum(1 for p in settled if p["result"] == "LOSS")
    pushes = sum(1 for p in settled if p["result"] == "PUSH")
    pnl    = sum(p["pnl"] for p in settled) if settled else 0.0
    bet    = sum(p["bet_amount"] for p in settled) if settled else 0.0
    roi    = (pnl / bet * 100) if bet else 0.0
    return {
        "start": start_dt.isoformat(),
        "end":   end_dt.isoformat(),
        "summary": {
            "settled":  len(settled),
            "pending":  len(picks) - len(settled),
            "wins":     wins,
            "losses":   losses,
            "pushes":   pushes,
            "pnl":      round(pnl, 2),
            "wagered":  round(bet, 2),
            "roi_pct":  round(roi, 2),
        },
        "picks":         [_shape_backtest_pick(p) for p in picks],
        "excel_path":    (os.path.basename(result["excel_path"])
                          if result.get("excel_path") else None),
    }


# Module-level instance for uvicorn import-string mode if ever used:
#   uvicorn webapp:api --host 0.0.0.0 --port 8080
api = build_app()
