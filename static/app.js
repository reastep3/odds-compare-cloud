// Arbyx PWA — vanilla JS frontend.
// Talks to the FastAPI backend via fetch + Bearer token from localStorage.

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
      archiveListView(true);
      loadArchives();
    }
  });
});

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
      <td class="num">${i + 1}</td>
      <td>${e.sport ?? ""}</td>
      <td>${e.game ?? ""}</td>
      <td class="${bookCls}">${e.book ?? ""}</td>
      <td>${e.market ?? ""}</td>
      <td>${e.outcome ?? ""}${e.line != null ? ` (${e.line})` : ""}</td>
      <td class="num">${fmtAmerican(e.odds)}</td>
      <td class="num ${ev > 0 ? "pos" : ev < 0 ? "neg" : ""}">${fmtPct(ev)}</td>
      <td class="num">${fmtPct(e.kelly_pct, 2)}</td>
      <td>${e.pinnacle_anchored ? "📌" : ""}</td>
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
    if (j.output_path) loadArchives();
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
      const date = new Date(a.mtime * 1000).toLocaleString();
      const kb   = (a.size / 1024).toFixed(1);
      const cls  = a.an_only ? " an-only" : "";
      return `<a class="row${cls}" href="#" data-name="${a.name}">
        <div>
          <div>${a.name}</div>
          <div class="meta">${date} · ${kb} KB${
            a.an_only ? ` · <span class="tag">AN-only</span>` : ""}</div>
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
  $("#archive-title").textContent = name;
  $("#archive-subtitle").innerHTML =
    `<span class="spinner"></span>Reading sheet…`;
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
  $("#archive-subtitle").textContent = s.generated || j.file_date || "";
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

  // Summary grid
  const fmtMoneyCell = (v) =>
    typeof v === "number" ? `$${v.toFixed(2)}` : "—";
  $("#archive-summary-grid").innerHTML = [
    kvCell("Generated", s.generated),
    kvCell("Total +EV edges", s.total_edges),
    kvCell("DK bankroll", fmtMoneyCell(s.dk_bankroll)),
    kvCell("FD bankroll", fmtMoneyCell(s.fd_bankroll)),
    kvCell("Capital deployed", fmtMoneyCell(s.total_wagered)),
    kvCell("Pinnacle anchored", s.pinnacle),
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

function renderPlayTheseTable(picks) {
  const rows = picks.map((p) => {
    const ev = Number(p.ev_pct ?? 0);
    return `<tr>
      <td>${(p.sport || "").toUpperCase()}</td>
      <td>${p.game ?? ""}</td>
      <td class="${bookCls(p.book)}">${p.book ?? ""}</td>
      <td>${p.market ?? ""}</td>
      <td>${p.pick ?? ""}${p.line != null ? ` (${p.line})` : ""}</td>
      <td class="num">${fmtAmerican(p.odds)}</td>
      <td class="num ${ev > 0 ? "pos" : ev < 0 ? "neg" : ""}">${fmtPct(ev, 2)}</td>
      <td class="num">${fmtBet(p.bet_amount)}</td>
    </tr>`;
  }).join("");
  $("#archive-play-table").innerHTML = `
    <thead><tr>
      <th>Sport</th><th>Game</th><th>Book</th><th>Mkt</th>
      <th>Pick</th><th>Odds</th><th>EV</th><th>Bet</th>
    </tr></thead>
    <tbody>${rows}</tbody>`;
}

function renderPromoTable(promos) {
  const rows = promos.map((p) => {
    const boostEv = Number(p.boost_ev ?? p.orig_ev ?? 0);
    return `<tr>
      <td>${(p.sport || "").toUpperCase()}</td>
      <td>${p.game ?? ""}</td>
      <td class="${bookCls(p.book)}">${p.book ?? ""}</td>
      <td>${p.pick ?? ""}${p.line != null ? ` (${p.line})` : ""}</td>
      <td class="num">${fmtAmerican(p.orig_odds)}</td>
      <td class="num">${p.boost_odds_str || fmtAmerican(p.boost_odds)}</td>
      <td class="num ${boostEv > 0 ? "pos" : "neg"}">${fmtPct(boostEv, 1)}</td>
      <td>${p.promo ?? ""}</td>
    </tr>`;
  }).join("");
  $("#archive-promo-table").innerHTML = `
    <thead><tr>
      <th>Sport</th><th>Game</th><th>Book</th><th>Pick</th>
      <th>Odds</th><th>Boost</th><th>EV</th><th>Promo</th>
    </tr></thead>
    <tbody>${rows}</tbody>`;
}

function renderPropsTable(props) {
  const rows = props.map((p) => {
    const ev = Number(p.ev_pct ?? 0);
    return `<tr>
      <td>${(p.sport || "").toUpperCase()}</td>
      <td>${p.game ?? ""}</td>
      <td>${p.player ?? ""}</td>
      <td>${(p.market || "").toUpperCase()}</td>
      <td>${(p.side || "").toUpperCase()}${p.line != null ? ` ${p.line}` : ""}</td>
      <td class="num">${fmtAmerican(p.odds)}</td>
      <td class="num ${ev > 0 ? "pos" : "neg"}">${fmtPct(ev, 1)}</td>
      <td class="${bookCls(p.book)}">${p.book ?? ""}</td>
    </tr>`;
  }).join("");
  $("#archive-props-table").innerHTML = `
    <thead><tr>
      <th>Sport</th><th>Game</th><th>Player</th><th>Mkt</th>
      <th>Side</th><th>Odds</th><th>EV</th><th>Book</th>
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
      <td>${(p.sport || "").toUpperCase()}</td>
      <td>${p.game ?? ""}</td>
      <td class="${bookCls(p.book)}">${p.book ?? ""}</td>
      <td>${p.pick ?? ""}</td>
      <td class="num">${fmtAmerican(p.odds)}</td>
      <td class="num ${ev > 0 ? "pos" : ""}">${fmtPct(ev, 1)}</td>
      <td class="${cls}"><b>${p.result}</b></td>
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

if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("/sw.js").catch(
      (err) => console.warn("SW register failed:", err));
  });
}

// ── Boot ──────────────────────────────────────────────────────────────

if (token()) showApp(); else showLogin();
