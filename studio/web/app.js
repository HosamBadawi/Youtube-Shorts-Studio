"use strict";
// Daily Shorts Studio - mobile front-end. Plain JS, no build step.

const $ = (id) => document.getElementById(id);
const show = (id) => $(id).classList.remove("hidden");
const hide = (id) => $(id).classList.add("hidden");
const SECTIONS = ["login", "source", "generating", "shorts"];
const only = (...ids) => { SECTIONS.forEach(hide); ids.forEach(show); };

let STATE = { platforms: [], tab: "upload", batchId: null, poll: null, cards: {} };

async function api(path, opts = {}) {
  const res = await fetch(path, { credentials: "same-origin", ...opts });
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch (e) {}
    throw new Error(detail);
  }
  return res.status === 204 ? null : res.json();
}

// ---------- boot ----------
async function boot() {
  let s;
  try { s = await api("/api/status"); } catch (e) { only("login"); return; }
  STATE.platforms = s.platforms;
  renderStatus(s);
  if (s.needs_password && !s.authed) { only("login"); return; }
  $("count").value = s.default_count || 3;
  only("source");
  loadLibrary();
}

function renderStatus(s) {
  const ai = s.ollama
    ? `<span class="dot on">● ${s.ollama_model}</span>`
    : `<span class="dot off">○ Ollama offline</span>`;
  $("statusbar").innerHTML = ai + `<span class="dot">▦ ${s.reframe_mode}</span>`;
}

// ---------- login ----------
$("loginBtn").onclick = async () => {
  $("loginErr").textContent = "";
  const fd = new FormData(); fd.append("password", $("password").value);
  try { await api("/api/login", { method: "POST", body: fd }); boot(); }
  catch (e) { $("loginErr").textContent = "Wrong password"; }
};

// ---------- tabs ----------
document.querySelectorAll(".tab").forEach((t) => {
  t.onclick = () => {
    STATE.tab = t.dataset.tab;
    document.querySelectorAll(".tab").forEach((x) => x.classList.toggle("active", x === t));
    document.querySelectorAll(".tabpane").forEach((p) =>
      p.classList.toggle("hidden", p.dataset.pane !== STATE.tab));
    if (STATE.tab === "local") loadLibrary();
  };
});

// ---------- file pick ----------
$("file").onchange = () => {
  const f = $("file").files[0];
  if (f) {
    $("fileLabel").textContent = f.name + "  (" + (f.size / 1e6).toFixed(0) + " MB)";
    $("filepick").classList.add("has-file");
  }
};

// ---------- local library ----------
$("refreshLib").onclick = loadLibrary;
async function loadLibrary() {
  try {
    const r = await api("/api/library");
    $("libDir").textContent = "Folder: " + r.dir;
    const sel = $("localPick");
    sel.innerHTML = r.videos.length
      ? r.videos.map((v) => `<option value="${escapeHtml(v.name)}">${escapeHtml(v.name)} · ${v.size_mb} MB</option>`).join("")
      : `<option disabled>(no videos found in this folder)</option>`;
  } catch (e) {}
}

// ---------- generate ----------
$("genBtn").onclick = async () => {
  $("genErr").textContent = "";
  const fd = new FormData();
  fd.append("source_type", STATE.tab);
  fd.append("count", $("count").value || "3");
  fd.append("niche", $("niche").value);
  if (STATE.tab === "upload") {
    const f = $("file").files[0];
    if (!f) { $("genErr").textContent = "Choose a video first"; return; }
    fd.append("file", f);
  } else if (STATE.tab === "url") {
    if (!$("url").value.trim()) { $("genErr").textContent = "Paste a URL"; return; }
    fd.append("url", $("url").value.trim());
  } else {
    const name = $("localPick").value;
    if (!name) { $("genErr").textContent = "Pick a video from the list"; return; }
    fd.append("name", name);
  }
  $("genBtn").disabled = true;
  try {
    const r = await api("/api/generate", { method: "POST", body: fd });
    STATE.batchId = r.batch_id;
    STATE.cards = {};
    $("shortList").innerHTML = "";
    only("generating", "shorts");
    pollBatch();
  } catch (e) { $("genErr").textContent = e.message; }
  finally { $("genBtn").disabled = false; }
};

$("newBatch").onclick = () => { clearInterval(STATE.poll); only("source"); };

// ---------- poll batch ----------
function pollBatch() {
  clearInterval(STATE.poll);
  STATE.poll = setInterval(async () => {
    let b;
    try { b = await api("/api/batch/" + STATE.batchId); } catch (e) { return; }
    $("genStage").textContent = b.error || b.stage || "working…";
    (b.shorts || []).forEach(renderCard);
    $("shortsTitle").textContent = b.shorts.length
      ? `Your shorts (${b.shorts.length})` : "Your shorts";
    if (b.done) {
      hide("generating");
      const busy = (b.shorts || []).some((j) => ["processing", "new", "publishing"].includes(j.status));
      if (!busy) clearInterval(STATE.poll);
    }
  }, 2000);
}

// ---------- short card ----------
function renderCard(job) {
  let card = STATE.cards[job.id];
  if (!card) {
    card = document.createElement("div");
    card.className = "card short";
    card.innerHTML = cardHtml(job);
    $("shortList").appendChild(card);
    STATE.cards[job.id] = card;
    wireCard(card, job.id);
  }
  updateCard(card, job);
}

function cardHtml(job) {
  return `
    <div class="short-head"><span class="badge">${escapeHtml(job.topic || "short")}</span>
      <span class="seg">${job.segment ? job.segment[0].toFixed(0)+"s–"+job.segment[1].toFixed(0)+"s" : ""}</span></div>
    <div class="short-body">
      <video class="prev" controls playsinline preload="none"></video>
      <div class="status-line"></div>
      <label class="field">Title <input class="t-title" type="text" /></label>
      <label class="field">Caption <textarea class="t-cap" rows="3"></textarea></label>
      <label class="field">Hashtags <input class="t-tags" type="text" /></label>
      <div class="row">
        <button class="ghost b-regen">✨ AI</button>
        <button class="ghost b-save">Save</button>
      </div>
      <div class="platforms"></div>
      <button class="primary b-pub">🚀 Publish this short</button>
      <div class="results"></div>
    </div>`;
}

function wireCard(card, id) {
  const q = (c) => card.querySelector(c);
  q(".b-save").onclick = async () => {
    try { await saveMeta(id, card); flash(q(".b-save"), "Saved ✓"); }
    catch (e) { setStatus(card, e.message, true); }
  };
  q(".b-regen").onclick = async () => {
    const btn = q(".b-regen"); btn.disabled = true; btn.textContent = "…";
    try {
      const r = await api(`/api/job/${id}/generate`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ niche: $("niche").value }),
      });
      fillMeta(card, r.meta);
    } catch (e) { setStatus(card, e.message, true); }
    finally { btn.disabled = false; btn.textContent = "✨ AI"; }
  };
  q(".b-pub").onclick = async () => {
    const platforms = [...card.querySelectorAll(".platforms input:checked")].map((c) => c.value);
    if (!platforms.length) { setStatus(card, "Pick a platform", true); return; }
    try {
      await saveMeta(id, card);
      await api(`/api/job/${id}/publish`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ platforms }),
      });
      setStatus(card, "publishing…", false);
      pollBatch();
    } catch (e) { setStatus(card, e.message, true); }
  };
}

function updateCard(card, job) {
  const q = (c) => card.querySelector(c);
  if (job.status === "processing" || job.status === "new") {
    setStatus(card, job.stage || "rendering…", false);
    return;
  }
  if (job.status === "error") { setStatus(card, "failed: " + job.error, true); return; }
  // ready / publishing / done
  const v = q(".prev");
  if (!v.src) v.src = "/api/preview/" + job.id;
  if (!card.dataset.metaLoaded) {
    fillMeta(card, job.meta); card.dataset.metaLoaded = "1";
    q(".platforms").innerHTML = STATE.platforms.map((p) =>
      `<label class="pf"><input type="checkbox" value="${p}" checked><span>${icon(p)} ${p}</span></label>`).join("");
  }
  renderResults(card, job);
}

async function saveMeta(id, card) {
  const meta = {
    title: card.querySelector(".t-title").value,
    caption: card.querySelector(".t-cap").value,
    hashtags: card.querySelector(".t-tags").value,
  };
  const r = await api(`/api/job/${id}/meta`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(meta),
  });
  return r.meta;
}

function fillMeta(card, meta) {
  card.querySelector(".t-title").value = meta.title || "";
  card.querySelector(".t-cap").value = meta.caption || "";
  card.querySelector(".t-tags").value = (meta.hashtags || []).join(" ");
}

function renderResults(card, job) {
  const r = job.results || {};
  if (!Object.keys(r).length) return;
  card.querySelector(".results").innerHTML = Object.entries(r).map(([p, res]) =>
    `<div class="result"><span class="name">${icon(p)} ${p}</span>${res.ok
      ? `<span class="ok">✓${res.url ? ` <a href="${res.url}" target="_blank">view</a>` : ""}</span>`
      : `<span class="bad">✗ ${res.needs_login ? "login needed" : escapeHtml(res.error)}</span>`}</div>`).join("");
}

function setStatus(card, text, bad) {
  const el = card.querySelector(".status-line");
  el.textContent = text; el.className = "status-line" + (bad ? " bad" : "");
}

// ---------- utils ----------
function icon(p) { return { youtube: "▶️", instagram: "📸", tiktok: "🎵", facebook: "👍" }[p] || "•"; }
function flash(el, text) { const o = el.textContent; el.textContent = text; setTimeout(() => (el.textContent = o), 1500); }
function escapeHtml(s) {
  return String(s || "").replace(/[&<>"]/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

boot();
