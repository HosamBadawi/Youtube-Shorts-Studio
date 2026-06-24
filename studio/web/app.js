"use strict";
// Daily Shorts Studio - mobile front-end. Plain JS, no build step.

const $ = (id) => document.getElementById(id);
const show = (id) => $(id).classList.remove("hidden");
const hide = (id) => $(id).classList.add("hidden");
const SECTIONS = ["login", "upload", "processing", "review", "results"];
const only = (id) => { SECTIONS.forEach(hide); show(id); };

let STATE = { platforms: [], job: null, poll: null };

async function api(path, opts = {}) {
  const res = await fetch(path, { credentials: "same-origin", ...opts });
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch (e) {}
    throw new Error(detail);
  }
  return res.status === 204 ? null : res.json();
}

// ---------- status / boot ----------
async function boot() {
  let s;
  try { s = await api("/api/status"); }
  catch (e) { only("login"); return; }
  renderStatus(s);
  STATE.platforms = s.platforms;
  if (s.needs_password && !s.authed) { only("login"); return; }
  if (s.today_job) { resumeJob(s.today_job); }
  else { only("upload"); }
}

function renderStatus(s) {
  const ai = s.ollama
    ? `<span class="dot on">● Ollama (${s.ollama_model})</span>`
    : `<span class="dot off">○ Ollama offline</span>`;
  const day = s.published_today
    ? `<span class="dot off">● Already published today</span>`
    : `<span class="dot on">● Ready for today</span>`;
  $("statusbar").innerHTML = ai + day;
}

// ---------- login ----------
$("loginBtn").onclick = async () => {
  $("loginErr").textContent = "";
  const fd = new FormData();
  fd.append("password", $("password").value);
  try { await api("/api/login", { method: "POST", body: fd }); boot(); }
  catch (e) { $("loginErr").textContent = "Wrong password"; }
};

// ---------- file pick ----------
$("file").onchange = () => {
  const f = $("file").files[0];
  if (f) {
    $("fileLabel").textContent = f.name + "  (" + (f.size / 1e6).toFixed(1) + " MB)";
    $("filepick").classList.add("has-file");
  }
};

// ---------- upload ----------
$("uploadBtn").onclick = async () => {
  $("uploadErr").textContent = "";
  const f = $("file").files[0];
  if (!f) { $("uploadErr").textContent = "Choose a video first"; return; }
  const fd = new FormData();
  fd.append("file", f);
  fd.append("auto_metadata", $("autoMeta").checked);
  fd.append("per_platform", $("perPlatform").checked);
  fd.append("niche", $("niche").value);
  fd.append("title", $("mTitle").value);
  fd.append("caption", $("mCaption").value);
  fd.append("hashtags", $("mTags").value);
  $("uploadBtn").disabled = true;
  try {
    const r = await api("/api/upload", { method: "POST", body: fd });
    only("processing");
    pollJob(r.job_id);
  } catch (e) { $("uploadErr").textContent = e.message; }
  finally { $("uploadBtn").disabled = false; }
};

// ---------- polling ----------
function pollJob(id) {
  clearInterval(STATE.poll);
  STATE.poll = setInterval(async () => {
    let job;
    try { job = await api("/api/job/" + id); } catch (e) { return; }
    STATE.job = job;
    if (job.status === "processing" || job.status === "new") {
      only("processing");
      $("stage").textContent = job.stage || "working…";
    } else if (job.status === "ready") {
      clearInterval(STATE.poll);
      fillReview(job);
    } else if (job.status === "publishing") {
      only("processing");
      $("stage").textContent = job.stage || "publishing…";
    } else if (job.status === "done") {
      clearInterval(STATE.poll);
      fillResults(job);
    } else if (job.status === "error") {
      clearInterval(STATE.poll);
      only("upload");
      $("uploadErr").textContent = "Failed: " + job.error;
    }
  }, 1500);
}

function resumeJob(job) {
  STATE.job = job;
  if (job.status === "ready") fillReview(job);
  else if (job.status === "done") fillResults(job);
  else { only("processing"); pollJob(job.id); }
}

// ---------- review ----------
function fillReview(job) {
  only("review");
  $("preview").src = "/api/preview/" + job.id + "?t=" + Date.now();
  $("rTitle").value = job.meta.title || "";
  $("rCaption").value = job.meta.caption || "";
  $("rTags").value = (job.meta.hashtags || []).join(" ");
  $("segNote").textContent = job.segment
    ? `AI picked ${job.segment[0].toFixed(0)}s–${job.segment[1].toFixed(0)}s from your clip.`
    : "";
  renderPlatforms();
}

function renderPlatforms() {
  $("platforms").innerHTML = STATE.platforms.map((p) => `
    <label class="pf"><input type="checkbox" value="${p}" checked />
      <span>${icon(p)} ${p}</span></label>`).join("");
}
function icon(p) {
  return { youtube: "▶️", instagram: "📸", tiktok: "🎵", facebook: "👍" }[p] || "•";
}

function currentMeta() {
  return {
    title: $("rTitle").value,
    caption: $("rCaption").value,
    hashtags: $("rTags").value,
    overrides: STATE.job?.meta?.overrides || {},
  };
}

$("saveMetaBtn").onclick = async () => {
  try {
    const r = await api("/api/job/" + STATE.job.id + "/meta", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(currentMeta()),
    });
    STATE.job.meta = r.meta;
    flash("saveMetaBtn", "Saved ✓");
  } catch (e) { $("publishErr").textContent = e.message; }
};

$("regenBtn").onclick = async () => {
  $("publishErr").textContent = "";
  $("regenBtn").disabled = true; $("regenBtn").textContent = "Thinking…";
  try {
    const r = await api("/api/job/" + STATE.job.id + "/generate", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ per_platform: $("perPlatform").checked, niche: $("niche").value }),
    });
    STATE.job.meta = r.meta;
    $("rTitle").value = r.meta.title || "";
    $("rCaption").value = r.meta.caption || "";
    $("rTags").value = (r.meta.hashtags || []).join(" ");
  } catch (e) { $("publishErr").textContent = e.message; }
  finally { $("regenBtn").disabled = false; $("regenBtn").textContent = "✨ Regenerate with AI"; }
};

$("publishBtn").onclick = async () => {
  $("publishErr").textContent = "";
  const platforms = [...document.querySelectorAll(".pf input:checked")].map((c) => c.value);
  if (!platforms.length) { $("publishErr").textContent = "Pick at least one platform"; return; }
  $("publishBtn").disabled = true;
  try {
    await api("/api/job/" + STATE.job.id + "/meta", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(currentMeta()),
    });
    await api("/api/job/" + STATE.job.id + "/publish", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ platforms }),
    });
    only("processing");
    pollJob(STATE.job.id);
  } catch (e) { $("publishErr").textContent = e.message; $("publishBtn").disabled = false; }
};

// ---------- results ----------
function fillResults(job) {
  only("results");
  const r = job.results || {};
  $("resultList").innerHTML = Object.keys(r).length
    ? Object.entries(r).map(([p, res]) => `
      <div class="result">
        <span class="name">${icon(p)} ${p}</span>
        ${res.ok
          ? `<span class="ok">✓ posted ${res.url ? `· <a href="${res.url}" target="_blank">view</a>` : ""}</span>`
          : `<span class="bad">✗ ${res.needs_login ? "login needed" : escapeHtml(res.error)}</span>`}
      </div>`).join("")
    : "<p class='hint'>No platforms ran.</p>";
}

$("newBtn").onclick = () => location.reload();

// ---------- utils ----------
function flash(id, text) {
  const el = $(id), old = el.textContent;
  el.textContent = text;
  setTimeout(() => { el.textContent = old; }, 1500);
}
function escapeHtml(s) {
  return String(s || "").replace(/[&<>"]/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

boot();
