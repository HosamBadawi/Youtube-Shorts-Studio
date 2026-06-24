"use strict";
// Shorts Studio - mobile/desktop front-end. Plain JS, no build step.

const $ = (s) => document.querySelector(s);
const $$ = (s) => [...document.querySelectorAll(s)];
const api = async (path, opts = {}) => {
  const res = await fetch(path, { credentials: "same-origin", ...opts });
  if (!res.ok) {
    let d = res.statusText;
    try { d = (await res.json()).detail || d; } catch (e) {}
    throw new Error(d);
  }
  return res.status === 204 ? null : res.json();
};
const esc = (s) => String(s || "").replace(/[&<>"]/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const icon = (p) => ({ youtube: "▶️", instagram: "📸", tiktok: "🎵", facebook: "👍" }[p] || "•");

let STATE = { platforms: [], src: "local", batchId: null, batchPoll: null, dlPoll: null, cards: {} };

function toast(msg) {
  const t = $("#toast"); t.textContent = msg; t.classList.remove("hidden");
  clearTimeout(t._t); t._t = setTimeout(() => t.classList.add("hidden"), 2200);
}

// ---------- navigation ----------
function go(screen) {
  $$(".screen").forEach((s) => s.classList.toggle("hidden", s.dataset.screen !== screen));
  $$(".tab").forEach((t) => t.classList.toggle("active", t.dataset.go === screen));
  if (screen === "download") loadVideos();
  if (screen === "shorts") loadShorts();
  if (screen === "create") loadLocalOptions();
  if (screen === "connections") { loadHealth(); loadConnections(); }
}
$$(".tab").forEach((t) => (t.onclick = () => go(t.dataset.go)));

// ---------- boot ----------
async function boot() {
  let s;
  try { s = await api("/api/status"); } catch (e) { showLogin(); return; }
  STATE.platforms = s.platforms;
  if (s.needs_password && !s.authed) { showLogin(); return; }
  $("#login").classList.add("hidden");
  $("#app").classList.remove("hidden");
  $("#statuschip").innerHTML = s.ollama
    ? `<span class="on">●</span> ${esc(s.ollama_model)} · ${esc(s.reframe_mode)}`
    : `<span class="off">●</span> Ollama offline`;
  $("#count").value = s.default_count || 3;
  if (s.length_min) $("#lenMin").value = Math.round(s.length_min);
  if (s.length_max) $("#lenMax").value = Math.round(s.length_max);
  STATE.vaultEnabled = s.vault_enabled;
  if (s.needs_password_change) $("#pwBanner").classList.remove("hidden");
  go("download");
}
function showLogin() { $("#app").classList.add("hidden"); $("#login").classList.remove("hidden"); }

$("#loginBtn").onclick = async () => {
  $("#loginErr").textContent = "";
  const fd = new FormData(); fd.append("password", $("#password").value);
  try { await api("/api/login", { method: "POST", body: fd }); boot(); }
  catch (e) { $("#loginErr").textContent = "Wrong password"; }
};
$("#password").addEventListener("keydown", (e) => { if (e.key === "Enter") $("#loginBtn").click(); });

// ====================================================================
// DOWNLOAD
// ====================================================================
$("#dlRefresh").onclick = loadVideos;
async function loadVideos() {
  try {
    const r = await api("/api/library");
    const el = $("#videoList");
    el.innerHTML = r.videos.length
      ? r.videos.map((v) => `
        <div class="vrow">
          <div class="vico">🎞️</div>
          <div class="vmeta"><div class="vname">${esc(v.name)}</div><div class="muted small">${v.size_mb} MB</div></div>
          <button class="btn ghost tiny" data-cut="${esc(v.name)}">✂️ Cut</button>
        </div>`).join("")
      : `<div class="empty">${esc(r.dir)}<br>No videos yet — download one above.</div>`;
    el.querySelectorAll("[data-cut]").forEach((b) => b.onclick = () => {
      go("create"); selectSource("local");
      const opt = [...$("#localPick").options].find((o) => o.value === b.dataset.cut);
      if (opt) $("#localPick").value = b.dataset.cut;
    });
  } catch (e) {}
}

$("#dlBtn").onclick = async () => {
  $("#dlErr").textContent = "";
  const url = $("#dlUrl").value.trim();
  if (!url) { $("#dlErr").textContent = "Paste a URL"; return; }
  const fd = new FormData(); fd.append("url", url);
  $("#dlBtn").disabled = true;
  try {
    const r = await api("/api/download", { method: "POST", body: fd });
    $("#dlProgress").classList.remove("hidden");
    pollDownload(r.download_id);
  } catch (e) { $("#dlErr").textContent = e.message; $("#dlBtn").disabled = false; }
};
function pollDownload(id) {
  clearInterval(STATE.dlPoll);
  STATE.dlPoll = setInterval(async () => {
    let d; try { d = await api("/api/download/" + id); } catch (e) { return; }
    $("#dlStage").textContent = d.error || d.stage;
    const m = (d.stage || "").match(/([\d.]+)%/);
    $("#dlBar").style.width = m ? m[1] + "%" : (d.done ? "100%" : "40%");
    if (d.done) {
      clearInterval(STATE.dlPoll); $("#dlBtn").disabled = false;
      if (d.error) { $("#dlErr").textContent = d.error; }
      else { toast("Downloaded " + d.file + " ✓"); $("#dlUrl").value = ""; loadVideos();
             setTimeout(() => $("#dlProgress").classList.add("hidden"), 1500); }
    }
  }, 1000);
}

// ====================================================================
// CREATE (make shorts)
// ====================================================================
$$("#srcSeg .seg-btn").forEach((b) => b.onclick = () => selectSource(b.dataset.src));
function selectSource(src) {
  STATE.src = src;
  $$("#srcSeg .seg-btn").forEach((b) => b.classList.toggle("active", b.dataset.src === src));
  $$(".srcpane").forEach((p) => p.classList.toggle("hidden", p.dataset.pane !== src));
  if (src === "local") loadLocalOptions();
}
async function loadLocalOptions() {
  try {
    const r = await api("/api/library");
    $("#localPick").innerHTML = r.videos.length
      ? r.videos.map((v) => `<option value="${esc(v.name)}">${esc(v.name)} · ${v.size_mb} MB</option>`).join("")
      : `<option disabled>(no videos — download one first)</option>`;
  } catch (e) {}
}
$("#cMinus").onclick = () => $("#count").value = Math.max(1, (+$("#count").value || 1) - 1);
$("#cPlus").onclick = () => $("#count").value = Math.min(20, (+$("#count").value || 1) + 1);
$("#file").onchange = () => {
  const f = $("#file").files[0];
  if (f) { $("#fileLabel").innerHTML = `<b>${esc(f.name)}</b><br><span class="muted small">${(f.size/1e6).toFixed(0)} MB</span>`; $("#filepick").classList.add("has-file"); }
};

$("#genBtn").onclick = async () => {
  $("#genErr").textContent = "";
  const fd = new FormData();
  fd.append("source_type", STATE.src);
  fd.append("count", $("#count").value || "3");
  fd.append("niche", $("#niche").value);
  fd.append("min_seconds", $("#lenMin").value || "0");
  fd.append("max_seconds", $("#lenMax").value || "0");
  if (STATE.src === "upload") {
    const f = $("#file").files[0];
    if (!f) { $("#genErr").textContent = "Choose a video"; return; }
    fd.append("file", f);
  } else if (STATE.src === "url") {
    if (!$("#srcUrl").value.trim()) { $("#genErr").textContent = "Paste a URL"; return; }
    fd.append("url", $("#srcUrl").value.trim());
  } else {
    if (!$("#localPick").value) { $("#genErr").textContent = "Pick a video"; return; }
    fd.append("name", $("#localPick").value);
  }
  $("#genBtn").disabled = true;
  try {
    const r = await api("/api/generate", { method: "POST", body: fd });
    STATE.batchId = r.batch_id; STATE.cards = {};
    $("#createList").innerHTML = "";
    $("#genProgress").classList.remove("hidden");
    pollBatch();
  } catch (e) { $("#genErr").textContent = e.message; }
  finally { $("#genBtn").disabled = false; }
};

function pollBatch() {
  clearInterval(STATE.batchPoll);
  STATE.batchPoll = setInterval(async () => {
    let b; try { b = await api("/api/batch/" + STATE.batchId); } catch (e) { return; }
    $("#genStage").textContent = b.error || b.stage || "working…";
    (b.shorts || []).forEach((j) => renderCard(j, $("#createList")));
    if (b.done) {
      const busy = (b.shorts || []).some((j) => ["processing", "new", "publishing"].includes(j.status));
      if (!busy) { clearInterval(STATE.batchPoll); $("#genProgress").classList.add("hidden"); }
    }
  }, 2000);
}

// ====================================================================
// SHORTS LIBRARY
// ====================================================================
$("#shRefresh").onclick = loadShorts;
async function loadShorts() {
  try {
    const r = await api("/api/shorts");
    $("#shortsCount").textContent = `All shorts (${r.shorts.length})`;
    const el = $("#shortsList");
    if (!r.shorts.length) { el.innerHTML = `<div class="empty">No shorts yet — make some in Create.</div>`; return; }
    STATE.cards = {}; el.innerHTML = "";
    r.shorts.forEach((j) => renderCard(j, el));
  } catch (e) {}
}

// ====================================================================
// SHARED SHORT CARD
// ====================================================================
function renderCard(job, container) {
  let card = STATE.cards[job.id];
  if (!card) {
    card = document.createElement("div");
    card.className = "card short";
    card.innerHTML = cardHtml(job);
    container.appendChild(card);
    STATE.cards[job.id] = card;
    wireCard(card, job.id);
  }
  updateCard(card, job);
}
function cardHtml(job) {
  return `
    <div class="short-head">
      <span class="badge">${esc(job.topic || "short")}</span>
      <span class="seg">${job.segment ? job.segment[0].toFixed(0)+"–"+job.segment[1].toFixed(0)+"s" : ""}</span>
    </div>
    <video class="prev" controls playsinline preload="none"></video>
    <div class="status-line"></div>
    <div class="meta hidden">
      <label class="field"><span class="lbl">Title</span><input class="t-title" type="text"></label>
      <label class="field"><span class="lbl">Caption</span><textarea class="t-cap" rows="3"></textarea></label>
      <label class="field"><span class="lbl">Hashtags</span><input class="t-tags" type="text"></label>
      <div class="row" style="margin-top:12px">
        <button class="btn ghost b-regen">✨ AI</button>
        <button class="btn ghost b-save">Save</button>
      </div>
      <div class="pf-row"></div>
      <button class="btn primary block b-pub">🚀 Publish</button>
      <div class="results"></div>
    </div>`;
}
function wireCard(card, id) {
  const q = (s) => card.querySelector(s);
  q(".b-save").onclick = async () => { try { await saveMeta(id, card); toast("Saved ✓"); } catch (e) { setStatus(card, e.message, true); } };
  q(".b-regen").onclick = async () => {
    const b = q(".b-regen"); b.disabled = true; b.textContent = "…";
    try { const r = await api(`/api/job/${id}/generate`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ niche: $("#niche").value }) }); fillMeta(card, r.meta); toast("Rewrote with AI"); }
    catch (e) { setStatus(card, e.message, true); }
    finally { b.disabled = false; b.textContent = "✨ AI"; }
  };
  q(".b-pub").onclick = async () => {
    const platforms = [...card.querySelectorAll(".pf.on")].map((p) => p.dataset.p);
    if (!platforms.length) { setStatus(card, "Pick a platform", true); return; }
    try {
      await saveMeta(id, card);
      await api(`/api/job/${id}/publish`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ platforms }) });
      setStatus(card, "publishing…", false); q(".b-pub").disabled = true;
      pollJob(id, card);
    } catch (e) { setStatus(card, e.message, true); }
  };
}
function updateCard(card, job) {
  const q = (s) => card.querySelector(s);
  if (job.status === "processing" || job.status === "new") { setStatus(card, (job.stage || "rendering…"), false, true); return; }
  if (job.status === "error") { setStatus(card, "failed: " + job.error, true); return; }
  const v = q(".prev"); if (!v.src) v.src = "/api/preview/" + job.id;
  q(".meta").classList.remove("hidden");
  if (!card.dataset.loaded) {
    fillMeta(card, job.meta);
    q(".pf-row").innerHTML = STATE.platforms.map((p) =>
      `<span class="pf on" data-p="${p}">${icon(p)} ${p}</span>`).join("");
    q(".pf-row").querySelectorAll(".pf").forEach((pf) => pf.onclick = () => pf.classList.toggle("on"));
    card.dataset.loaded = "1";
  }
  const uploaded = Object.values(job.results || {}).some((r) => r.ok);
  if (uploaded && !q(".badge.up")) q(".short-head").insertAdjacentHTML("beforeend", `<span class="badge up">✓ uploaded</span>`);
  if (job.status === "done") setStatus(card, "", false);
  renderResults(card, job);
}
function pollJob(id, card) {
  const t = setInterval(async () => {
    let j; try { j = await api("/api/job/" + id); } catch (e) { return; }
    updateCard(card, j);
    if (["done", "error", "ready"].includes(j.status) && j.status !== "publishing") {
      if (Object.keys(j.results || {}).length || j.status === "error") { clearInterval(t); card.querySelector(".b-pub").disabled = false; }
    }
  }, 2000);
}
async function saveMeta(id, card) {
  const meta = { title: card.querySelector(".t-title").value, caption: card.querySelector(".t-cap").value, hashtags: card.querySelector(".t-tags").value };
  const r = await api(`/api/job/${id}/meta`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(meta) });
  return r.meta;
}
function fillMeta(card, meta) {
  card.querySelector(".t-title").value = meta.title || "";
  card.querySelector(".t-cap").value = meta.caption || "";
  card.querySelector(".t-tags").value = (meta.hashtags || []).join(" ");
}
function renderResults(card, job) {
  const r = job.results || {}; if (!Object.keys(r).length) return;
  card.querySelector(".results").innerHTML = Object.entries(r).map(([p, res]) =>
    `<div class="result"><span>${icon(p)} ${p}</span>${res.ok
      ? `<span class="ok">✓${res.url ? ` <a href="${res.url}" target="_blank">view</a>` : ""}</span>`
      : `<span class="bad">✗ ${res.needs_login ? "login needed" : esc(res.error)}</span>`}</div>`).join("");
}
function setStatus(card, text, bad, spin) {
  const el = card.querySelector(".status-line");
  el.innerHTML = (spin ? `<span class="spinner"></span>` : "") + esc(text);
  el.className = "status-line" + (bad ? " bad" : "");
}

// ====================================================================
// CONNECTIONS
// ====================================================================
$("#healthRefresh").onclick = loadHealth;
async function loadHealth() {
  try {
    const h = await api("/api/health");
    $("#healthChecks").innerHTML = h.checks.map((c) =>
      `<div class="vrow"><div class="vico">${c.ok ? "✅" : (c.critical ? "❌" : "⚠️")}</div>
        <div class="vmeta"><div class="vname">${esc(c.name)}</div>
        <div class="muted small">${esc(c.detail || "")}</div></div></div>`).join("");
  } catch (e) {}
}

async function loadConnections() {
  let data;
  try { data = await api("/api/connections"); } catch (e) { return; }
  STATE.strategies = data.strategies;
  $("#connList").innerHTML = data.platforms.map((p) => connCard(p, data.vault_enabled)).join("");
  data.platforms.forEach((p) => wireConn(p.platform));
}

function chip(p) {
  // Prefer the last health-check result (it reflects edge_profile too).
  if (p.health) {
    return p.health.ok
      ? `<span class="cc ok">connected${p.health.strategy ? " · " + p.health.strategy : ""}</span>`
      : `<span class="cc bad">not connected</span>`;
  }
  if (p.is_api) return p.has_session ? `<span class="cc ok">connected</span>` : `<span class="cc warn">not set up</span>`;
  if (p.has_session) return `<span class="cc ok">session ✓</span>`;
  if (p.edge_configured) return `<span class="cc warn">via Edge · tap Check</span>`;
  if (p.credentials.has_credentials) return `<span class="cc warn">credentials only</span>`;
  return `<span class="cc bad">not connected</span>`;
}

function connCard(p, vaultEnabled) {
  const opts = (STATE.strategies || []).map((s) =>
    `<option value="${s}" ${s === p.strategy ? "selected" : ""}>${s}</option>`).join("");
  const credBlock = p.is_api
    ? `<p class="muted small">Uses the YouTube API. Run <code>python -m studio.login_setup youtube</code> on the PC once to authorize.</p>`
    : (vaultEnabled ? `
      <details class="credbox"><summary>Credentials ${p.credentials.has_password ? "✓" : ""}</summary>
        <label class="field"><span class="lbl">Username</span><input class="c-user" type="text" value="${esc(p.credentials.username || "")}"></label>
        <label class="field"><span class="lbl">Password</span><input class="c-pass" type="password" placeholder="${p.credentials.has_password ? "•••••• (stored)" : ""}"></label>
        <label class="field"><span class="lbl">2FA secret <span class="muted small">(optional, base32)</span></span><input class="c-totp" type="text" placeholder="${p.credentials.has_totp ? "•••••• (stored)" : ""}"></label>
        <div class="row"><button class="btn ghost c-save">Save</button><button class="btn ghost c-clear">Clear</button></div>
        <p class="muted small">Best-effort. Prefer Edge profile / saved session; credential login can trip 2FA.</p>
      </details>` : `<p class="muted small">Credential storage disabled (cryptography not installed).</p>`);
  return `<div class="card conn" data-p="${p.platform}">
    <div class="short-head"><span class="badge">${icon(p.platform)} ${p.platform}</span>${chip(p)}</div>
    <label class="field"><span class="lbl">Login strategy</span>
      <select class="c-strat select">${opts}</select></label>
    ${credBlock}
    <div class="row" style="margin-top:12px">
      <button class="btn ghost c-check">🩺 Check</button>
      <button class="btn primary c-connect">🔗 Connect</button>
    </div>
    <div class="status-line"></div>
  </div>`;
}

function wireConn(platform) {
  const card = document.querySelector(`.conn[data-p="${platform}"]`);
  if (!card) return;
  const q = (s) => card.querySelector(s);
  q(".c-strat").onchange = async () => {
    try { await api(`/api/connections/${platform}/strategy`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ strategy: q(".c-strat").value }) }); toast("Strategy saved"); }
    catch (e) { toast(e.message); }
  };
  if (q(".c-save")) q(".c-save").onclick = async () => {
    const body = { username: q(".c-user").value, password: q(".c-pass").value, totp_secret: q(".c-totp").value };
    if (!body.username || !body.password) { setLine(card, "username + password required", true); return; }
    try { await api(`/api/connections/${platform}/credentials`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }); toast("Credentials saved 🔒"); loadConnections(); }
    catch (e) { setLine(card, e.message, true); }
  };
  if (q(".c-clear")) q(".c-clear").onclick = async () => {
    try { await api(`/api/connections/${platform}/credentials`, { method: "DELETE" }); toast("Cleared"); loadConnections(); }
    catch (e) { setLine(card, e.message, true); }
  };
  q(".c-check").onclick = () => runConn(platform, card, "health");
  q(".c-connect").onclick = () => runConn(platform, card, "login");
}

async function runConn(platform, card, kind) {
  setLine(card, kind === "health" ? "checking…" : "connecting…", false, true);
  try {
    const r = await api(`/api/connections/${platform}/${kind}`, { method: "POST" });
    const poll = setInterval(async () => {
      let run; try { run = await api("/api/connections/run/" + r.run_id); } catch (e) { return; }
      if (!run.done) return;
      clearInterval(poll);
      if (run.error) { setLine(card, run.error, true); return; }
      const res = run.result || {};
      setLine(card, (res.ok ? "✅ " : "❌ ") + (res.detail || "") + (res.strategy ? ` (${res.strategy})` : ""), !res.ok);
      loadConnections();
    }, 1500);
  } catch (e) { setLine(card, e.message, true); }
}

function setLine(card, text, bad, spin) {
  const el = card.querySelector(".status-line");
  el.innerHTML = (spin ? `<span class="spinner"></span>` : "") + esc(text);
  el.className = "status-line" + (bad ? " bad" : "");
}

boot();
