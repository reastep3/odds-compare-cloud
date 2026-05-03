// Arbyx PWA — vanilla JS frontend.
// Talks to the FastAPI backend via fetch + Bearer token from localStorage.

// Bump on every meaningful release. Shown in the page footer so you can
// confirm at a glance that the cache evicted and you're on fresh code.
const BUILD = "2026-05-03 v14 (Backtest archives in Archives tab)";

const TOKEN_KEY = "arbyx_token";
const $  = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));

// ── Auth ──────────────────────────────────────────────────────────────

function token() { return localStorage.getItem(TOKEN_KEY) || ""; }
function setToken(t) { localStorage.setItem(TOKEN_KEY, t); }
function clearToken() { localStorage.removeItem(TOKEN_KEY); }

async function api(path, opts = {}) {
  const headers = Object.assign({}, opts.headers || {});
  if (token()) headers["Authorization"] = `Bearer ${token()}`;
  if (opts.body && !(opts.body instanceof FormData)) {
    headers["Content-Type"] = headers["Content-Type"] || "application/json";
  }
  const res = await fetch(path, Object.assign({}, opts, { headers }));
  if (res.status === 401) {
    clearToken();
    showLogin();
    throw new Error("Session expired — please sign in again.");
  }
  if (!res.ok) {
    let msg = `HTTP ${res.status}`;
    try { const j = await res.json(); if (j.detail) msg = j.detail; } catch (_) {}
    throw new Error(msg);
  }
  return res;
}

async function apiJSON(path, opts) {
  const res = await api(path, opts);
  return res.json();
}

function showLogin() {
  $("#login").hidden = false;
  $("#app").hidden = true;
  $("#logout-btn").hidden = true;
  $("#password").focus();
}

function showApp() {
  $("#login").hidden = true;
  $("#app").hidden = false;
  $("#logout-btn").hidden = false;
  // Default backtest dates: last 7 days
  const today = new Date();
  const week = new Date(today.getTime() - 7 * 86400 * 1000);
  $("#bt-start").value = week.toISOString().slice(0, 10);
  $("#bt-end").value   = today.toISOString().slice(0, 10);
  loadDashboard();
  loadArchives();
}

$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const pw = $("#password").value;
  $("#login-err").hidden = true;
  try {
    const res = await fetch("/api/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password: pw }),
    });
    if (!res.ok) {
      const j = await res.json().catch(() => ({}));
      throw new Error(j.detail || `HTTP ${res.status}`);
    }
    const j = await res.json();
    setToken(j.token);
    $("#password").value = "";
    showApp();
  } catch (err) {
    $("#login-err").textContent = err.message;
    $("#login-err").hidden = false;
  }
});

$("#logout-btn").addEventListener("click", () => { clearToken(); showLogin(); });

// ── Tabs ──────────────────────────────────────────────────────────────

$$(".tab").forEach((b) => {
  b.addEventListener("click", () => {
    $$(".tab").forEach((x) => x.classList.toggle("active", x === b));
    $$(".panel").forEach((p) => {
      p.classList.toggle("active", p.dataset.panel === b.dataset.tab);
    });
    if (b.dataset.tab === "archives") {
      // Always reset to list view on tab entry — feels more predictable
      // than landing back on a stale detail view.
      if (_archMode === "backtest") {
        btArchiveListView(true);
        loadBacktestArchives();
      } else {
        archiveListView(true);
        loadArchives();
      }
    }
  });
});

// ── Dashboard (left pane: most recent run) ───────────────────────────

// Cache so the Kelly selector can re-render the tables without a roundtrip.
let _dashCache = null;

// User's Kelly fraction. Persisted in localStorage. Default = 0.5
// (matches odds_compare's KELLY_FRACTION).
const KELLY_KEY = "arbyx_kelly_fraction";
function getKelly() {
  const v = parseFloat(localStorage.getItem(KELLY_KEY));
  return (Number.isFinite(v) && v > 0 && v <= 1) ? v : 0.5;
}
function setKelly(v) {
  localStorage.setItem(KELLY_KEY, String(v));
}

// Display timezone. Filenames encode UTC ('cause Fly machines run UTC),
// so we always parse as UTC and convert at render time. "auto" defers
// to the browser's resolved timezone.
const TZ_KEY = "arbyx_timezone";
function getTZ() {
  return localStorage.getItem(TZ_KEY) || "auto";
}
function setTZ(v) { localStorage.setItem(TZ_KEY, v); }
function tzOptions() {
  const tz = getTZ();
  return tz === "auto" ? {} : { timeZone: tz };
}

// User's per-book bankroll. Persisted in localStorage so it survives
// page reloads and is shared between the dashboard inputs (left pane)
// and the run-card inputs (right pane).
const DK_BR_KEY = "arbyx_dk_bankroll";
const FD_BR_KEY = "arbyx_fd_bankroll";
function _getBR(key) {
  const v = parseFloat(localStorage.getItem(key));
  return Number.isFinite(v) && v >= 0 ? v : 100;
}
function getDkBR() { return _getBR(DK_BR_KEY); }
function getFdBR() { return _getBR(FD_BR_KEY); }
function setDkBR(v) { localStorage.setItem(DK_BR_KEY, String(v)); }
function setFdBR(v) { localStorage.setItem(FD_BR_KEY, String(v)); }
// Pick the right bankroll for an edge based on which book it's on.
function bankrollFor(book) {
  if (book === "DraftKings") return getDkBR();
  if (book === "FanDuel")    return getFdBR();
  return 0;
}

async function loadDashboard() {
  const meta = $("#dashboard-meta");
  meta.innerHTML = `<span class="spinner"></span>Loading latest run + grading…`;
  $("#dashboard-empty").hidden = true;
  $("#dashboard-anonly-banner").hidden = true;
  $("#dashboard-summary").innerHTML = "";
  $("#dashboard-play-section").hidden = true;
  $("#dashboard-all-section").hidden = true;
  $("#kelly-bar").hidden = true;
  $("#bankroll-bar").hidden = true;

  try {
    const j = await apiJSON("/api/latest");
    if (!j.archive) {
      meta.textContent = "";
      $("#dashboard-empty").hidden = false;
      return;
    }
    _dashCache = j.archive;
    renderDashboard(_dashCache);
  } catch (e) {
    meta.innerHTML = `<span class="error">${e.message}</span>`;
  }
}

function renderDashboard(arc) {
  const s = arc.summary || {};
  const r = arc.results || {};
  const pretty = prettyArchiveName(arc.name);

  // Meta line: friendly date + raw filename caption
  $("#dashboard-meta").innerHTML =
    `<b>${pretty.primary}</b><br>` +
    `<span style="font-size:11px;opacity:0.5">${arc.name}</span>`;

  $("#dashboard-anonly-banner").hidden = !arc.an_only;

  // Summary grid: focus on actionable metrics
  let resultsHTML = "";
  if (r && (r.settled || r.pending || r.manual)) {
    const sgnCls  = r.roi_pct >= 0 ? "pos" : "neg";
    const wlpLine = `<b>${r.wins}</b> W / <b>${r.losses}</b> L / <b>${r.pushes}</b> P`;
    const extras  = [];
    if (r.in_progress) extras.push(`${r.in_progress} live`);
    if (r.not_started) extras.push(`${r.not_started} not started`);
    if (r.manual)      extras.push(`${r.manual} unresolved`);
    const extraStr = extras.length ? ` · ${extras.join(" · ")}` : "";
    const roiLine  = r.settled
      ? `ROI: <span class="${sgnCls}" style="font-size:1.3em">${fmtPct(r.roi_pct)}</span>
         · Win rate: <b>${fmtPct(r.win_rate)}</b>`
      : "<span class='muted'>No graded picks yet</span>";
    resultsHTML = `
      ${kvCell("Day's record", `${wlpLine}${extraStr}`, { full: true })}
      ${kvCell("Day's ROI", roiLine, { full: true })}`;
  }

  $("#dashboard-summary").innerHTML = [
    resultsHTML,
    kvCell("Total +EV edges", s.total_edges),
    kvCell("Pinnacle anchored", s.pinnacle),
    kvCell("Best EV", s.best_ev, { full: true }),
  ].join("");

  // Show the Kelly + bankroll selectors now that we have data
  const kelly = getKelly();
  $("#kelly-bar").hidden = false;
  $("#bankroll-bar").hidden = false;
  syncKellyControls(kelly);
  syncBankrollControls();

  // Play These (filtered actionable picks)
  const play = arc.play_these || [];
  $("#dashboard-play-count").textContent = `(${play.length})`;
  if (play.length) {
    $("#dashboard-play-section").hidden = false;
    $("#dashboard-play-table").innerHTML = renderDashboardTable(play, kelly);
  }

  // All Plays (every +EV edge — broader pool)
  const all = arc.all_plays || [];
  $("#dashboard-all-count").textContent = `(${all.length})`;
  if (all.length) {
    $("#dashboard-all-section").hidden = false;
    $("#dashboard-all-table").innerHTML = renderDashboardTable(all, kelly);
  }
}

// Recompute the stake column for both tables when the Kelly fraction
// or either bankroll changes. Doesn't refetch — operates on _dashCache.
function applyBettingChange() {
  if (!_dashCache) return;
  const kelly = getKelly();
  syncKellyControls(kelly);
  syncBankrollControls();
  const play = _dashCache.play_these || [];
  const all  = _dashCache.all_plays  || [];
  if (play.length) {
    $("#dashboard-play-table").innerHTML = renderDashboardTable(play, kelly);
  }
  if (all.length) {
    $("#dashboard-all-table").innerHTML = renderDashboardTable(all, kelly);
  }
}

function syncKellyControls(kelly) {
  $("#kelly-slider").value = Math.round(kelly * 100);
  $("#kelly-value").textContent = `${kelly.toFixed(2)}×`;
  document.querySelectorAll(".kelly-presets button").forEach((b) => {
    b.classList.toggle("active",
      Math.round(kelly * 100) === parseInt(b.dataset.kelly, 10));
  });
}

// Push the current localStorage bankrolls into all input fields. Used
// on dashboard render and after one input changes so the other input
// (run-card vs dashboard) stays in lockstep.
function syncBankrollControls() {
  const dk = getDkBR();
  const fd = getFdBR();
  for (const id of ["dash-dk-br", "dk-br"]) {
    const el = document.getElementById(id);
    if (el && document.activeElement !== el) el.value = dk;
  }
  for (const id of ["dash-fd-br", "fd-br"]) {
    const el = document.getElementById(id);
    if (el && document.activeElement !== el) el.value = fd;
  }
}

function fmtMoneyShort(x) {
  if (!Number.isFinite(x)) return "—";
  return x >= 100 ? `$${x.toFixed(0)}` : `$${x.toFixed(2)}`;
}

// Compact table for the left pane.
// Stake column: bankroll × Kelly% (per-book). Falls back to BR% only
// when the user has no bankroll set for that book, or when the archive
// predates the full_kelly_pct field.
function renderDashboardTable(picks, userKellyFraction) {
  const rows = picks.map((p) => {
    const ev = Number(p.ev_pct ?? 0);
    const br = (p.full_kelly_pct != null)
      ? p.full_kelly_pct * userKellyFraction
      : p.kelly_pct;
    const bankroll = bankrollFor(p.book);
    let stakeCell;
    if (br == null) {
      stakeCell = "—";
    } else if (bankroll > 0) {
      const dollars = bankroll * (br / 100);
      stakeCell = `<span class="stake-amt">${fmtMoneyShort(dollars)}</span>` +
                  `<span class="stake-pct">${br.toFixed(2)}%</span>`;
    } else {
      stakeCell = `${br.toFixed(2)}%`;
    }
    return `<tr>
      <td data-label="Sport" class="cell-sport">${(p.sport || "").toUpperCase()}</td>
      <td data-label="Game" class="cell-game">${p.game ?? ""}</td>
      <td data-label="Book" class="${bookCls(p.book)}">${shortBook(p.book)}</td>
      <td data-label="Pick">${p.pick ?? ""}${p.line != null ? ` (${p.line})` : ""}</td>
      <td data-label="Odds" class="num">${fmtAmerican(p.odds)}</td>
      <td data-label="Stake" class="num stake-cell">${stakeCell}</td>
      <td data-label="EV" class="num ${ev > 0 ? "pos" : ev < 0 ? "neg" : ""}">${fmtPct(ev, 2)}</td>
      ${resultCell(p)}
    </tr>`;
  }).join("");
  return `<thead><tr>
    <th>Sport</th><th>Game</th><th>Book</th><th>Pick</th>
    <th>Odds</th><th>Stake</th><th>EV</th><th>Result</th>
  </tr></thead><tbody>${rows}</tbody>`;
}

// Wire the Kelly slider + presets
const _kSlider = document.querySelector("#kelly-slider");
if (_kSlider) {
  _kSlider.addEventListener("input", () => {
    const k = parseInt(_kSlider.value, 10) / 100;
    setKelly(k);
    applyBettingChange();
  });
}
document.querySelectorAll(".kelly-presets button").forEach((b) => {
  b.addEventListener("click", () => {
    const k = parseInt(b.dataset.kelly, 10) / 100;
    setKelly(k);
    applyBettingChange();
  });
});

// Wire the bankroll inputs (both the dashboard pair and the run-card
// pair). Any change updates localStorage, then re-renders the tables
// and mirrors the value into the other input.
function _wireBR(inputId, setter) {
  const el = document.getElementById(inputId);
  if (!el) return;
  el.addEventListener("input", () => {
    const v = parseFloat(el.value);
    setter(Number.isFinite(v) && v >= 0 ? v : 0);
    applyBettingChange();
  });
}
_wireBR("dash-dk-br", setDkBR);
_wireBR("dash-fd-br", setFdBR);
_wireBR("dk-br",      setDkBR);
_wireBR("fd-br",      setFdBR);
// Populate the run-card inputs from localStorage on first load so they
// match whatever the user last entered (instead of the hardcoded 100).
{
  const _dkRun = document.getElementById("dk-br");
  const _fdRun = document.getElementById("fd-br");
  if (_dkRun) _dkRun.value = getDkBR();
  if (_fdRun) _fdRun.value = getFdBR();
}

function shortBook(book) {
  if (book === "DraftKings") return "DK";
  if (book === "FanDuel")    return "FD";
  return book ?? "";
}

// Wire the small refresh button on the dashboard card. Script runs after
// the body is parsed (it's at end of <body>) so the element exists.
const _dashRefresh = document.querySelector("#dashboard-refresh");
if (_dashRefresh) _dashRefresh.addEventListener("click", loadDashboard);

// Timezone dropdown — populate from localStorage and re-render the
// dashboard meta line + archive list when changed. Game-time strings
// inside picks are pre-baked in ET by the bot, so we only need to
// re-render the filename-derived headers.
{
  const _tz = document.getElementById("tz-select");
  if (_tz) {
    _tz.value = getTZ();
    _tz.addEventListener("change", () => {
      setTZ(_tz.value);
      if (_dashCache) renderDashboard(_dashCache);
      // Re-render archive list if it's been loaded already.
      const archList = document.getElementById("archives-list");
      if (archList && archList.childElementCount > 0) loadArchives();
    });
  }
}

// ── Run scan ─────────────────────────────────────────────────────────

function setStatus(el, msg, kind) {
  if (!msg) { el.hidden = true; el.textContent = ""; return; }
  el.hidden = false;
  el.className = "status" + (kind === "error" ? " error" : "");
  el.innerHTML = (kind === "loading"
    ? `<span class="spinner"></span>${msg}` : msg);
}

function fmtPct(x, digits = 1) {
  if (x === null || x === undefined || Number.isNaN(x)) return "—";
  return `${Number(x).toFixed(digits)}%`;
}

// Parse "ev_edges_YYYYMMDD_HHMM[_ANONLY].xlsx" into a friendly display.
// Returns { primary, secondary, isAnOnly } — falls back to the raw name
// if the pattern doesn't match.
function prettyArchiveName(filename, mtimeSec) {
  const m = filename.match(
    /^ev_edges_(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(?:_ANONLY)?\.xlsx$/i);
  if (!m) {
    return { primary: filename, secondary: "", isAnOnly: false };
  }
  const [, y, mo, d, hh, mm] = m;
  // Filenames are written by Fly's UTC-clock container, so parse the
  // timestamp as UTC and let toLocale*String handle the display TZ.
  const dt = new Date(Date.UTC(+y, +mo - 1, +d, +hh, +mm));
  const tzOpt = tzOptions();
  const sameYear = (
    new Date().toLocaleDateString("en-US", { ...tzOpt, year: "numeric" })
    === dt.toLocaleDateString("en-US", { ...tzOpt, year: "numeric" })
  );
  const dateOpts = sameYear
    ? { ...tzOpt, month: "short", day: "numeric" }
    : { ...tzOpt, month: "short", day: "numeric", year: "numeric" };
  const timeOpts = { ...tzOpt, hour: "numeric", minute: "2-digit",
                     timeZoneName: "short" };
  const datePart = dt.toLocaleDateString(undefined, dateOpts);
  const timePart = dt.toLocaleTimeString(undefined, timeOpts);
  return {
    primary:   `${datePart} · ${timePart}`,
    secondary: filename,    // raw name preserved as caption
    isAnOnly:  /_ANONLY/i.test(filename),
  };
}
function fmtMoney(x) {
  if (x === null || x === undefined || Number.isNaN(x)) return "—";
  const sgn = x >= 0 ? "+" : "−";
  return `${sgn}$${Math.abs(x).toFixed(2)}`;
}
function fmtAmerican(odds) {
  if (odds === null || odds === undefined) return "—";
  const n = Number(odds);
  return n > 0 ? `+${n}` : `${n}`;
}

function renderEdges(edges) {
  if (!edges || !edges.length) {
    $("#run-edges").hidden = true;
    return;
  }
  const rows = edges.map((e, i) => {
    const bookCls = e.book === "DraftKings" ? "book-DK"
                   : e.book === "FanDuel"   ? "book-FD" : "";
    const ev = Number(e.ev_pct ?? 0);
    return `<tr>
      <td data-label="#" class="num cell-rank">${i + 1}</td>
      <td data-label="Sport" class="cell-sport">${e.sport ?? ""}</td>
      <td data-label="Game" class="cell-game">${e.game ?? ""}</td>
      <td data-label="Book" class="${bookCls}">${e.book ?? ""}</td>
      <td data-label="Mkt">${e.market ?? ""}</td>
      <td data-label="Pick">${e.outcome ?? ""}${e.line != null ? ` (${e.line})` : ""}</td>
      <td data-label="Odds" class="num">${fmtAmerican(e.odds)}</td>
      <td data-label="EV" class="num ${ev > 0 ? "pos" : ev < 0 ? "neg" : ""}">${fmtPct(ev)}</td>
      <td data-label="Kelly" class="num">${fmtPct(e.kelly_pct, 2)}</td>
      <td data-label="Sharp">${e.pinnacle_anchored ? "📌 Pinnacle" : ""}</td>
    </tr>`;
  }).join("");
  $("#run-edges-table").innerHTML = `
    <thead><tr>
      <th>#</th><th>Sport</th><th>Game</th><th>Book</th>
      <th>Mkt</th><th>Pick</th><th>Odds</th><th>EV</th>
      <th>Kelly</th><th>📌</th>
    </tr></thead>
    <tbody>${rows}</tbody>`;
  $("#run-edges").hidden = false;
}

function renderRunSummary(s) {
  $("#run-summary-body").innerHTML = `
    <p><b>${s.total_games}</b> games scanned · <b>${s.total_edges}</b> +EV edges
    · <b>${s.pinn_anchored}</b> Pinnacle-anchored</p>
    ${s.an_count ? `<p class="muted">Sources: API ${s.api_count} sport(s)
      / AN fallback ${s.an_count} sport(s)</p>` : ""}`;
  $("#run-summary").hidden = false;
  $("#run-anonly-banner").hidden = !s.an_only;
}

$("#run-btn").addEventListener("click", async () => {
  const dk = parseFloat($("#dk-br").value || "100");
  const fd = parseFloat($("#fd-br").value || "100");
  $("#run-btn").disabled = true;
  $("#run-live-btn").disabled = true;
  setStatus($("#run-status"), "Running scan — this can take 60-90 seconds…",
            "loading");
  try {
    const j = await apiJSON("/api/run", {
      method: "POST",
      body: JSON.stringify({ dk_bankroll: dk, fd_bankroll: fd }),
    });
    setStatus($("#run-status"), "");
    renderRunSummary(j.summary);
    renderEdges(j.play_edges);
    if (j.output_path) {
      loadArchives();
      loadDashboard();   // refresh the left-pane board with the new run
    }
  } catch (e) {
    setStatus($("#run-status"), e.message, "error");
  } finally {
    $("#run-btn").disabled = false;
    $("#run-live-btn").disabled = false;
  }
});

$("#run-live-btn").addEventListener("click", async () => {
  $("#run-btn").disabled = true;
  $("#run-live-btn").disabled = true;
  setStatus($("#run-status"), "Pulling live in-progress edges…", "loading");
  try {
    const j = await apiJSON("/api/run_live", { method: "POST" });
    setStatus($("#run-status"), "");
    renderRunSummary({
      total_games: 0, total_edges: j.count, pinn_anchored: 0,
      an_only: false, an_count: 0, api_count: 0,
    });
    renderEdges(j.edges);
  } catch (e) {
    setStatus($("#run-status"), e.message, "error");
  } finally {
    $("#run-btn").disabled = false;
    $("#run-live-btn").disabled = false;
  }
});

// ── Archives ─────────────────────────────────────────────────────────

async function loadArchives() {
  const wrap = $("#archives-list");
  wrap.innerHTML = `<p class="muted"><span class="spinner"></span>Loading…</p>`;
  try {
    const j = await apiJSON("/api/archives");
    if (!j.archives.length) {
      wrap.innerHTML = `<p class="muted">No archives yet. Run a scan first.</p>`;
      return;
    }
    wrap.innerHTML = j.archives.map((a) => {
      const pretty = prettyArchiveName(a.name, a.mtime);
      const kb     = (a.size / 1024).toFixed(1);
      const cls    = a.an_only ? " an-only" : "";
      return `<a class="row${cls}" href="#" data-name="${a.name}">
        <div>
          <div class="row-title">${pretty.primary}</div>
          <div class="meta">${kb} KB${
            a.an_only ? ` · <span class="tag">AN-only fallback</span>` : ""}</div>
        </div>
        <div>›</div>
      </a>`;
    }).join("");

    wrap.querySelectorAll(".row").forEach((a) => {
      a.addEventListener("click", (e) => {
        e.preventDefault();
        showArchiveDetail(a.dataset.name);
      });
    });
  } catch (e) {
    wrap.innerHTML = `<p class="error">${e.message}</p>`;
  }
}

$("#archives-refresh").addEventListener("click", loadArchives);

// ── Archive detail view ──────────────────────────────────────────────

function archiveListView(showList) {
  $("#archives-list-view").hidden   = !showList;
  $("#archives-detail-view").hidden = showList;
  if (showList) window.scrollTo({ top: 0, behavior: "smooth" });
}

$("#archive-back").addEventListener("click", () => archiveListView(true));

async function showArchiveDetail(name) {
  archiveListView(false);
  // Reset visibility
  ["#archive-play-section", "#archive-promo-section",
   "#archive-props-section", "#archive-empty-state",
   "#archive-anonly-banner"].forEach((s) => $(s).hidden = true);
  const pretty = prettyArchiveName(name);
  $("#archive-title").textContent    = pretty.primary;
  $("#archive-subtitle").innerHTML   =
    `<span class="spinner"></span>Reading sheet + grading vs ESPN…`;
  $("#archive-summary-grid").innerHTML = "";

  try {
    const j = await apiJSON(`/api/archives/${encodeURIComponent(name)}/contents`);
    renderArchiveDetail(j);
  } catch (e) {
    $("#archive-subtitle").innerHTML = `<span class="error">${e.message}</span>`;
  }
}

function kvCell(label, value, opts = {}) {
  if (value === null || value === undefined || value === "") return "";
  const cls = opts.full ? "kv full" : "kv";
  return `<div class="${cls}">
    <span class="label">${label}</span>
    <span class="value">${value}</span>
  </div>`;
}

function renderArchiveDetail(j) {
  const s = j.summary || {};
  const r = j.results || {};
  // Subtitle = generated timestamp from the sheet (more precise than the
  // filename-derived time) plus the raw filename in muted small text.
  const ts = s.generated || j.file_date || "";
  $("#archive-subtitle").innerHTML =
    `${ts}<br><span style="font-size:11px;opacity:0.5">${j.name}</span>`;
  $("#archive-anonly-banner").hidden = !j.an_only;

  // Wire up download as the secondary option
  const dl = $("#archive-download-link");
  dl.href = `/api/archives/${encodeURIComponent(j.name)}`;
  dl.onclick = async (e) => {
    e.preventDefault();
    try {
      const res = await api(`/api/archives/${encodeURIComponent(j.name)}`);
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const tmp = document.createElement("a");
      tmp.href = url; tmp.download = j.name;
      document.body.appendChild(tmp); tmp.click(); tmp.remove();
      setTimeout(() => URL.revokeObjectURL(url), 30000);
    } catch (err) { alert(err.message); }
  };

  // Daily results card (only show if anything actually graded)
  let resultsHTML = "";
  if (r && (r.settled || r.pending || r.manual)) {
    const sgnCls   = r.roi_pct >= 0 ? "pos" : "neg";
    const wlpLine  = `<b>${r.wins}</b> W / <b>${r.losses}</b> L / <b>${r.pushes}</b> P`;
    const extras   = [];
    if (r.pending) extras.push(`${r.pending} pending`);
    if (r.manual)  extras.push(`${r.manual} unresolved`);
    const extraStr = extras.length ? ` · ${extras.join(" · ")}` : "";
    const roiLine  = r.settled
      ? `ROI: <span class="${sgnCls}" style="font-size:1.3em">${fmtPct(r.roi_pct)}</span>
         · Win rate: <b>${fmtPct(r.win_rate)}</b>`
      : "—";
    resultsHTML = `
      ${kvCell("Day's record", `${wlpLine}${extraStr}`, { full: true })}
      ${kvCell("Day's ROI", roiLine, { full: true })}`;
  }

  // Summary grid
  const fmtMoneyCell = (v) =>
    typeof v === "number" ? `$${v.toFixed(2)}` : "—";
  $("#archive-summary-grid").innerHTML = [
    resultsHTML,
    kvCell("Total +EV edges", s.total_edges),
    kvCell("Pinnacle anchored", s.pinnacle),
    kvCell("DK bankroll", fmtMoneyCell(s.dk_bankroll)),
    kvCell("FD bankroll", fmtMoneyCell(s.fd_bankroll)),
    kvCell("Capital deployed", fmtMoneyCell(s.total_wagered)),
    kvCell("Best EV", s.best_ev, { full: true }),
  ].join("");

  // Counts in section headers
  const counts = j.counts || {};
  $("#archive-play-count").textContent  = `(${counts.play_these || 0})`;
  $("#archive-promo-count").textContent = `(${counts.promo_picks || 0})`;
  $("#archive-props-count").textContent = `(${counts.player_props || 0})`;

  // Tables
  if ((j.play_these || []).length) {
    $("#archive-play-section").hidden = false;
    renderPlayTheseTable(j.play_these);
  }
  if ((j.promo_picks || []).length) {
    $("#archive-promo-section").hidden = false;
    renderPromoTable(j.promo_picks);
  }
  if ((j.player_props || []).length) {
    $("#archive-props-section").hidden = false;
    renderPropsTable(j.player_props);
  }
  if (!(j.play_these || []).length && !(j.promo_picks || []).length
      && !(j.player_props || []).length) {
    $("#archive-empty-state").hidden = false;
  }
}

function bookCls(book) {
  if (book === "DraftKings") return "book-DK";
  if (book === "FanDuel")    return "book-FD";
  return "";
}

function fmtBet(v) {
  return typeof v === "number" ? `$${v.toFixed(2)}` : "—";
}

function resultCell(p) {
  const r = p.result;
  if (!r) return `<td data-label="Result" class="muted">—</td>`;
  if (r === "NOT_STARTED") {
    return `<td data-label="Result" class="muted">📅 Not started</td>`;
  }
  if (r === "IN_PROGRESS" || r === "PENDING") {
    // PENDING is legacy (pre-status-split archives). Treat as live too.
    return `<td data-label="Result" class="live"><span class="live-dot"></span>Live</td>`;
  }
  if (r === "MANUAL" || r === "NO_RESULT" || r === "UNKNOWN") {
    return `<td data-label="Result" class="muted">—</td>`;
  }
  const cls = r === "WIN" ? "pos" : r === "LOSS" ? "neg" : "";
  const u   = p.unit_return_pct;
  const ret = (u !== null && u !== undefined)
    ? ` <span class="muted">${u >= 0 ? "+" : ""}${u.toFixed(1)}%</span>`
    : "";
  return `<td data-label="Result" class="${cls}"><b>${r}</b>${ret}</td>`;
}

function renderPlayTheseTable(picks) {
  const rows = picks.map((p) => {
    const ev = Number(p.ev_pct ?? 0);
    return `<tr>
      <td data-label="Sport" class="cell-sport">${(p.sport || "").toUpperCase()}</td>
      <td data-label="Game" class="cell-game">${p.game ?? ""}</td>
      <td data-label="Book" class="${bookCls(p.book)}">${p.book ?? ""}</td>
      <td data-label="Mkt">${p.market ?? ""}</td>
      <td data-label="Pick">${p.pick ?? ""}${p.line != null ? ` (${p.line})` : ""}</td>
      <td data-label="Odds" class="num">${fmtAmerican(p.odds)}</td>
      <td data-label="EV" class="num ${ev > 0 ? "pos" : ev < 0 ? "neg" : ""}">${fmtPct(ev, 2)}</td>
      ${resultCell(p)}
    </tr>`;
  }).join("");
  $("#archive-play-table").innerHTML = `
    <thead><tr>
      <th>Sport</th><th>Game</th><th>Book</th><th>Mkt</th>
      <th>Pick</th><th>Odds</th><th>EV</th><th>Result</th>
    </tr></thead>
    <tbody>${rows}</tbody>`;
}

function renderPromoTable(promos) {
  const rows = promos.map((p) => {
    const boostEv = Number(p.boost_ev ?? p.orig_ev ?? 0);
    return `<tr>
      <td data-label="Sport" class="cell-sport">${(p.sport || "").toUpperCase()}</td>
      <td data-label="Game" class="cell-game">${p.game ?? ""}</td>
      <td data-label="Book" class="${bookCls(p.book)}">${p.book ?? ""}</td>
      <td data-label="Pick">${p.pick ?? ""}${p.line != null ? ` (${p.line})` : ""}</td>
      <td data-label="Odds" class="num">${fmtAmerican(p.orig_odds)}</td>
      <td data-label="Boost" class="num">${p.boost_odds_str || fmtAmerican(p.boost_odds)}</td>
      <td data-label="EV" class="num ${boostEv > 0 ? "pos" : "neg"}">${fmtPct(boostEv, 1)}</td>
      ${resultCell(p)}
    </tr>`;
  }).join("");
  $("#archive-promo-table").innerHTML = `
    <thead><tr>
      <th>Sport</th><th>Game</th><th>Book</th><th>Pick</th>
      <th>Odds</th><th>Boost</th><th>EV</th><th>Result</th>
    </tr></thead>
    <tbody>${rows}</tbody>`;
}

function renderPropsTable(props) {
  const rows = props.map((p) => {
    const ev = Number(p.ev_pct ?? 0);
    return `<tr>
      <td data-label="Sport" class="cell-sport">${(p.sport || "").toUpperCase()}</td>
      <td data-label="Game" class="cell-game">${p.game ?? ""}</td>
      <td data-label="Player">${p.player ?? ""}</td>
      <td data-label="Mkt">${(p.market || "").toUpperCase()}</td>
      <td data-label="Side">${(p.side || "").toUpperCase()}${p.line != null ? ` ${p.line}` : ""}</td>
      <td data-label="Odds" class="num">${fmtAmerican(p.odds)}</td>
      <td data-label="EV" class="num ${ev > 0 ? "pos" : "neg"}">${fmtPct(ev, 1)}</td>
      ${resultCell(p)}
    </tr>`;
  }).join("");
  $("#archive-props-table").innerHTML = `
    <thead><tr>
      <th>Sport</th><th>Game</th><th>Player</th><th>Mkt</th>
      <th>Side</th><th>Odds</th><th>EV</th><th>Result</th>
    </tr></thead>
    <tbody>${rows}</tbody>`;
}

// ── Archives sub-toggle (Run vs Backtest reports) ───────────────────

let _archMode = "run";

$$("button[data-arch-mode]").forEach((b) => {
  b.addEventListener("click", () => {
    const mode = b.dataset.archMode;
    if (mode === _archMode) return;
    _archMode = mode;
    $$("button[data-arch-mode]").forEach((x) =>
      x.classList.toggle("active", x === b));
    $$("[data-arch-view]").forEach((v) =>
      v.hidden = v.dataset.archView !== mode);
    if (mode === "backtest") {
      btArchiveListView(true);
      loadBacktestArchives();
    } else {
      archiveListView(true);
      loadArchives();
    }
  });
});

// ── Backtest archives ────────────────────────────────────────────────

function prettyBacktestName(filename, startISO, endISO) {
  // Filename format: backtest_YYYY-MM-DD_YYYY-MM-DD.xlsx
  const m = filename.match(
    /^backtest_(\d{4})-(\d{2})-(\d{2})_(\d{4})-(\d{2})-(\d{2})\.xlsx$/i);
  if (!m && !(startISO && endISO)) {
    return { primary: filename, secondary: "" };
  }
  const [sy, sm, sd] = m
    ? [m[1], m[2], m[3]]
    : startISO.split("-");
  const [ey, em, ed] = m
    ? [m[4], m[5], m[6]]
    : endISO.split("-");
  // Anchor at noon UTC to dodge any TZ-rollover surprises in toLocaleDateString.
  const start = new Date(Date.UTC(+sy, +sm - 1, +sd, 12));
  const end   = new Date(Date.UTC(+ey, +em - 1, +ed, 12));
  const opts  = { month: "short", day: "numeric", year: "numeric",
                  timeZone: "UTC" };
  const sameDay = sy === ey && sm === em && sd === ed;
  if (sameDay) {
    return {
      primary:   start.toLocaleDateString(undefined, opts),
      secondary: filename,
    };
  }
  const sameYear = sy === ey;
  const startOpts = sameYear
    ? { month: "short", day: "numeric", timeZone: "UTC" }
    : opts;
  return {
    primary:   `${start.toLocaleDateString(undefined, startOpts)} → ${end.toLocaleDateString(undefined, opts)}`,
    secondary: filename,
  };
}

async function loadBacktestArchives() {
  const wrap = $("#bt-archives-list");
  wrap.innerHTML = `<p class="muted"><span class="spinner"></span>Loading…</p>`;
  try {
    const j = await apiJSON("/api/backtest_archives");
    if (!j.archives.length) {
      wrap.innerHTML = `<p class="muted">No backtest reports yet. The 3 AM ET daily job creates one each morning, or run <b>Backtest</b> manually.</p>`;
      return;
    }
    wrap.innerHTML = j.archives.map((a) => {
      const pretty = prettyBacktestName(a.name, a.start, a.end);
      const kb     = (a.size / 1024).toFixed(1);
      return `<a class="row" href="#" data-name="${a.name}">
        <div>
          <div class="row-title">${pretty.primary}</div>
          <div class="meta">${kb} KB</div>
        </div>
        <div>›</div>
      </a>`;
    }).join("");
    wrap.querySelectorAll(".row").forEach((a) => {
      a.addEventListener("click", (e) => {
        e.preventDefault();
        showBacktestArchiveDetail(a.dataset.name);
      });
    });
  } catch (e) {
    wrap.innerHTML = `<p class="error">${e.message}</p>`;
  }
}

$("#bt-archives-refresh").addEventListener("click", loadBacktestArchives);

function btArchiveListView(showList) {
  $("#bt-archives-list-view").hidden   = !showList;
  $("#bt-archives-detail-view").hidden = showList;
  if (showList) window.scrollTo({ top: 0, behavior: "smooth" });
}

$("#bt-archive-back").addEventListener("click",
                                        () => btArchiveListView(true));

async function showBacktestArchiveDetail(name) {
  btArchiveListView(false);
  ["#bt-archive-by-sport-section", "#bt-archive-picks-section",
   "#bt-archive-empty-state"].forEach((s) => $(s).hidden = true);
  const pretty = prettyBacktestName(name);
  $("#bt-archive-title").textContent  = pretty.primary;
  $("#bt-archive-subtitle").innerHTML =
    `<span class="spinner"></span>Reading report…`;
  $("#bt-archive-summary-grid").innerHTML = "";
  try {
    const j = await apiJSON(
      `/api/backtest_archives/${encodeURIComponent(name)}/contents`);
    renderBacktestArchiveDetail(j);
  } catch (e) {
    $("#bt-archive-subtitle").innerHTML = `<span class="error">${e.message}</span>`;
  }
}

function renderBacktestArchiveDetail(j) {
  const s = j.summary || {};
  const c = j.counts  || {};
  const ts = s.generated || j.start || "";
  $("#bt-archive-subtitle").innerHTML =
    `${ts}<br><span style="font-size:11px;opacity:0.5">${j.name}</span>`;

  const dl = $("#bt-archive-download-link");
  dl.href = `/api/backtest_archives/${encodeURIComponent(j.name)}`;
  dl.onclick = async (e) => {
    e.preventDefault();
    try {
      const res = await api(
        `/api/backtest_archives/${encodeURIComponent(j.name)}`);
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const tmp = document.createElement("a");
      tmp.href = url; tmp.download = j.name;
      document.body.appendChild(tmp); tmp.click(); tmp.remove();
      setTimeout(() => URL.revokeObjectURL(url), 30000);
    } catch (err) { alert(err.message); }
  };

  const wlpLine = `<b>${s.wins ?? 0}</b> W / <b>${s.losses ?? 0}</b> L / <b>${s.pushes ?? 0}</b> P`;
  const extras  = [];
  if (s.pending) extras.push(`${s.pending} pending`);
  if (s.manual)  extras.push(`${s.manual} unresolved`);
  const extraStr = extras.length ? ` · ${extras.join(" · ")}` : "";
  // ROI/win-rate come back as strings ("12.3%") because that's how the writer
  // formatted them. Pull the numeric to drive the +/- color class.
  const roiNum = parseFloat(s.roi || "0");
  const roiCls = roiNum >= 0 ? "pos" : "neg";
  const roiCell = s.settled
    ? `<span class="${roiCls}" style="font-size:1.3em">${s.roi || "—"}</span>
       · Win rate: <b>${s.win_rate || "—"}</b>`
    : "—";

  const fmtMoneyCell = (v) =>
    typeof v === "number"
      ? (v >= 0 ? "+" : "−") + "$" + Math.abs(v).toFixed(2)
      : "—";

  $("#bt-archive-summary-grid").innerHTML = [
    kvCell("Record", `${wlpLine}${extraStr}`, { full: true }),
    kvCell("ROI", roiCell, { full: true }),
    kvCell("Total picks", s.total_picks),
    kvCell("Settled", s.settled),
    kvCell("Wagered",
            typeof s.total_wagered === "number"
              ? `$${s.total_wagered.toFixed(2)}` : null),
    kvCell("P&L", fmtMoneyCell(s.total_pnl)),
  ].join("");

  // Per-sport breakdown
  const bs = j.by_sport || [];
  if (bs.length) {
    $("#bt-archive-by-sport-section").hidden = false;
    renderBacktestBySport(bs);
  }

  // All picks table
  $("#bt-archive-picks-count").textContent = `(${c.total || 0})`;
  if ((j.picks || []).length) {
    $("#bt-archive-picks-section").hidden = false;
    renderBacktestPicksTable(j.picks);
  } else {
    $("#bt-archive-empty-state").hidden = false;
  }
}

function renderBacktestBySport(rows) {
  const body = rows.map((r) => {
    const roi    = r.roi_pct;
    const roiCls = roi == null ? "muted" : roi >= 0 ? "pos" : "neg";
    const roiTxt = roi == null ? "—" : fmtPct(roi, 1);
    const wr     = r.win_rate == null ? "—" : fmtPct(r.win_rate, 0);
    return `<tr>
      <td data-label="Sport" class="cell-sport">${r.sport}</td>
      <td data-label="Picks" class="num">${r.picks}</td>
      <td data-label="Settled" class="num">${r.settled}</td>
      <td data-label="W-L" class="num">${r.wins}-${r.losses}</td>
      <td data-label="WR" class="num">${wr}</td>
      <td data-label="ROI" class="num ${roiCls}"><b>${roiTxt}</b></td>
    </tr>`;
  }).join("");
  $("#bt-archive-by-sport-table").innerHTML = `
    <thead><tr>
      <th>Sport</th><th>Picks</th><th>Settled</th>
      <th>W-L</th><th>WR</th><th>ROI</th>
    </tr></thead>
    <tbody>${body}</tbody>`;
}

function renderBacktestPicksTable(picks) {
  // Show settled first, then unsettled — within each, latest game-date first.
  const ordered = picks.slice().sort((a, b) => {
    const aSet = ["WIN", "LOSS", "PUSH"].includes(a.result) ? 0 : 1;
    const bSet = ["WIN", "LOSS", "PUSH"].includes(b.result) ? 0 : 1;
    if (aSet !== bSet) return aSet - bSet;
    return (b.game_date || "").localeCompare(a.game_date || "");
  });
  const rows = ordered.map((p) => {
    const ev  = Number(p.ev_pct ?? 0);
    return `<tr>
      <td data-label="Date" class="muted">${p.game_date || ""}</td>
      <td data-label="Sport" class="cell-sport">${(p.sport || "").toUpperCase()}</td>
      <td data-label="Game" class="cell-game">${p.game ?? ""}</td>
      <td data-label="Book" class="${bookCls(p.book)}">${p.book ?? ""}</td>
      <td data-label="Mkt">${p.market ?? ""}</td>
      <td data-label="Pick">${p.pick ?? ""}${p.line != null ? ` (${p.line})` : ""}</td>
      <td data-label="Odds" class="num">${fmtAmerican(p.odds)}</td>
      <td data-label="EV" class="num ${ev > 0 ? "pos" : ev < 0 ? "neg" : ""}">${fmtPct(ev, 1)}</td>
      ${resultCell(p)}
    </tr>`;
  }).join("");
  $("#bt-archive-picks-table").innerHTML = `
    <thead><tr>
      <th>Date</th><th>Sport</th><th>Game</th><th>Book</th>
      <th>Mkt</th><th>Pick</th><th>Odds</th><th>EV</th><th>Result</th>
    </tr></thead>
    <tbody>${rows}</tbody>`;
}

// ── Backtest ─────────────────────────────────────────────────────────

$$('button[data-bt-preset]').forEach((b) => {
  b.addEventListener("click", () => {
    const days = parseInt(b.dataset.btPreset, 10);
    const today = new Date();
    const start = new Date(today.getTime() - days * 86400 * 1000);
    $("#bt-start").value = start.toISOString().slice(0, 10);
    $("#bt-end").value   = today.toISOString().slice(0, 10);
  });
});

function renderBacktest(j) {
  const s   = j.summary;
  const sgn = s.roi_pct >= 0 ? "pos" : "neg";
  $("#bt-summary").innerHTML = `
    <p><b>${s.wins}</b> W / <b>${s.losses}</b> L / <b>${s.pushes}</b> P
       · ${s.settled} settled${s.pending ? ` · ${s.pending} pending` : ""}</p>
    <p>ROI: <span class="${sgn}" style="font-size:1.4em">${fmtPct(s.roi_pct)}</span>
       · Win rate: <b>${fmtPct(s.win_rate)}</b></p>
    <p class="muted" style="margin-top:6px;font-size:12px">
      ROI is bankroll-independent (Kelly bet sizing scales linearly with bankroll).
    </p>`;

  const settled = (j.picks || []).filter(
    (p) => p.result === "WIN" || p.result === "LOSS" || p.result === "PUSH");
  const rows = settled.map((p) => {
    const cls = p.result === "WIN"  ? "pos"
              : p.result === "LOSS" ? "neg" : "";
    const ev  = Number(p.ev_pct ?? 0);
    return `<tr>
      <td data-label="Sport" class="cell-sport">${(p.sport || "").toUpperCase()}</td>
      <td data-label="Game" class="cell-game">${p.game ?? ""}</td>
      <td data-label="Book" class="${bookCls(p.book)}">${p.book ?? ""}</td>
      <td data-label="Pick">${p.pick ?? ""}</td>
      <td data-label="Odds" class="num">${fmtAmerican(p.odds)}</td>
      <td data-label="EV" class="num ${ev > 0 ? "pos" : ""}">${fmtPct(ev, 1)}</td>
      <td data-label="Result" class="${cls}"><b>${p.result}</b></td>
    </tr>`;
  }).join("");
  $("#bt-table").innerHTML = `
    <thead><tr>
      <th>Sport</th><th>Game</th><th>Book</th><th>Pick</th>
      <th>Odds</th><th>EV</th><th>Result</th>
    </tr></thead>
    <tbody>${rows}</tbody>`;
  $("#bt-result").hidden = false;
}

$("#bt-run-btn").addEventListener("click", async () => {
  const start = $("#bt-start").value;
  const end   = $("#bt-end").value;
  if (!start || !end) {
    setStatus($("#bt-status"), "Pick start and end dates first.", "error");
    return;
  }
  const an_only_mode = $("#bt-anonly").value;
  $("#bt-run-btn").disabled = true;
  setStatus($("#bt-status"),
            "Grading picks against ESPN — can take a minute…", "loading");
  try {
    const j = await apiJSON("/api/backtest", {
      method: "POST",
      body: JSON.stringify({ start, end, an_only_mode }),
    });
    setStatus($("#bt-status"), "");
    renderBacktest(j);
  } catch (e) {
    setStatus($("#bt-status"), e.message, "error");
  } finally {
    $("#bt-run-btn").disabled = false;
  }
});

// ── Service worker (PWA) ──────────────────────────────────────────────
//
// Strategy: deliberately minimal — see sw.js. The page also auto-reloads
// when a new SW takes control, so deploys are visible after one refresh.

if ("serviceWorker" in navigator) {
  let reloading = false;
  navigator.serviceWorker.addEventListener("controllerchange", () => {
    if (reloading) return;
    reloading = true;
    window.location.reload();
  });
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("/sw.js").then((reg) => {
      // Force any waiting SW to take over now (happens after a deploy).
      if (reg.waiting) reg.waiting.postMessage({ type: "SKIP_WAITING" });
      reg.addEventListener("updatefound", () => {
        const sw = reg.installing;
        if (!sw) return;
        sw.addEventListener("statechange", () => {
          if (sw.state === "installed" && navigator.serviceWorker.controller) {
            // A new SW just installed and there's already a controller —
            // tell the new SW to take over so the controllerchange handler
            // above triggers the reload.
            sw.postMessage({ type: "SKIP_WAITING" });
          }
        });
      });
    }).catch((err) => console.warn("SW register failed:", err));
  });
}

// ── Build stamp (visible at bottom of every screen) ───────────────────

window.addEventListener("DOMContentLoaded", () => {
  const stamp = document.createElement("div");
  stamp.className = "build-stamp";
  stamp.textContent = `Build: ${BUILD}`;
  document.body.appendChild(stamp);
});

// ── Boot ──────────────────────────────────────────────────────────────

if (token()) showApp(); else showLogin();
