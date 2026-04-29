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
    """Reuse backtest.py's existing tab readers. Returns a JSON-clean dict."""
    from backtest import (_read_play_these, _read_promo_picks,
                            _read_props_from_odds_comparison)

    file_dt = _file_date_from_name(name) or date.today()

    summary = _read_summary_tab(path)
    play    = _read_play_these(path, file_dt)
    promos  = _read_promo_picks(path, file_dt)
    props   = _read_props_from_odds_comparison(path, file_dt)

    return {
        "name":     name,
        "an_only":  "_ANONLY" in name.upper(),
        "file_date": file_dt.isoformat(),
        "summary":  _shape_summary(summary),
        "play_these":  [_shape_archive_pick(p)  for p in play],
        "promo_picks": [_shape_archive_promo(p) for p in promos],
        "player_props": [_shape_archive_prop(p) for p in props],
        "counts": {
            "play_these":   len(play),
            "promo_picks":  len(promos),
            "player_props": len(props),
        },
    }


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


def _shape_archive_pick(p: dict[str, Any]) -> dict[str, Any]:
    return {
        "sport":      p.get("sport"),
        "game":       p.get("game"),
        "game_time":  p.get("game_time"),
        "book":       p.get("book"),
        "market":     p.get("market"),
        "pick":       p.get("pick"),
        "line":       p.get("line"),
        "odds":       p.get("odds"),
        "ev_pct":     _round(p.get("ev_pct"), 2),
        "confidence": p.get("confidence"),
        "bet_amount": _round(p.get("bet_amount"), 2),
        "potential":  _round(p.get("potential"), 2),
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
        "bet_amount": p.get("bet_amount"),
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
