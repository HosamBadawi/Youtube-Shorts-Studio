"use strict";
// YouTube Shorts Studio — mobile/desktop front-end. Plain JS, no build step.

const $ = (s) => document.querySelector(s);
const $$ = (s) => [...document.querySelectorAll(s)];
const api = async (path, opts = {}) => {
  const res = await fetch(path, { credentials: "same-origin", ...opts });
  if (res.status === 401) { showLogin(); throw new Error("not authenticated"); }
  if (!res.ok) {
    let d = res.statusText;
    try { d = (await res.json()).detail || d; } catch (e) {}
    throw new Error(d);
  }
  return res.status === 204 ? null : res.json();
};
const jpost = (path, body) => api(path, {
  method: "POST", headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body || {}),
});
const esc = (s) => String(s || "").replace(/[&<>"]/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

const STATE = {
  status: {}, src: "url", cards: {}, jobPolls: {}, batchPolls: {},
  dlPoll: null, uploadQueue: [], models: null,
};

function toast(msg) {
  const t = $("#toast"); t.textContent = msg; t.classList.remove("hidden");
  clearTimeout(t._t); t._t = setTimeout(() => t.classList.add("hidden"), 2400);
}

// ---------- tabs ----------
function go(tab) {
  $$(".screen").forEach((s) => s.classList.toggle("hidden", s.dataset.screen !== tab));
  $$(".tabbar button").forEach((b) => b.classList.toggle("on", b.dataset.tab === tab));
  if (tab === "create") { loadLibrary(); loadActiveBatches(); }
  if (tab === "shorts") loadShorts();
  if (tab === "settings") { loadModels(); loadYouTube(); loadHealth(); }
}
$$(".tabbar button").forEach((b) => (b.onclick = () => go(b.dataset.tab)));

// ---------- boot / auth ----------
function showLogin() { $("#login").classList.remove("hidden"); }

async function boot() {
  let s;
  try { s = await api("/api/status"); } catch (e) { showLogin(); return; }
  if (s.needs_password && !s.authed) { showLogin(); return; }
  $("#login").classList.add("hidden");
  STATE.status = s;
  const pill = $("#model-pill");
  pill.classList.toggle("on", !!s.ollama);
  $("#model-name").textContent = s.ollama
    ? (s.ollama_model || s.llm_provider)
    : (s.llm_provider === "ollama" ? "Ollama offline" : s.llm_provider);
  $("#gen-count").value = s.default_count || 3;
  if (s.length_min) $("#gen-min").value = Math.round(s.length_min);
  if (s.length_max) $("#gen-max").value = Math.round(s.length_max);
  $("#pass-banner").classList.toggle("hidden", !s.needs_password_change);
  // deep link: /#shorts or /#settings opens that tab directly
  const tab = location.hash.slice(1);
  go(["create", "shorts", "settings"].includes(tab) ? tab : "create");
}

$("#login-form").onsubmit = async (e) => {
  e.preventDefault();
  $("#login-err").textContent = "";
  const fd = new FormData();
  fd.append("password", $("#login-pass").value);
  try {
    await api("/api/login", { method: "POST", body: fd });
    $("#login-pass").value = "";
    boot();
  } catch (err) { $("#login-err").textContent = "Wrong password"; }
};

$("#logout").onclick = async () => {
  try { await api("/api/logout", { method: "POST" }); } catch (e) {}
  location.reload();
};

// ====================================================================
// CREATE
// ====================================================================
$("#src-seg").onclick = (e) => {
  const b = e.target.closest("button"); if (!b) return;
  STATE.src = b.dataset.src;
  $$("#src-seg button").forEach((x) => x.classList.toggle("on", x === b));
  $("#src-url").classList.toggle("hidden", STATE.src !== "url");
  $("#src-upload").classList.toggle("hidden", STATE.src !== "upload");
  $("#src-local").classList.toggle("hidden", STATE.src !== "local");
};
$$(".stepper button").forEach((b) => (b.onclick = () => {
  const inp = $("#gen-count");
  inp.value = Math.min(10, Math.max(1, (+inp.value || 3) + (+b.dataset.step)));
}));
$("#gen-file").onchange = () => {
  const f = $("#gen-file").files[0];
  $("#gen-file-label").textContent = f ? f.name : "Choose a video…";
};

$("#gen-btn").onclick = async () => {
  $("#gen-err").textContent = "";
  const fd = new FormData();
  fd.append("source_type", STATE.src);
  if (STATE.src === "url") {
    const u = $("#gen-url").value.trim();
    if (!u) { $("#gen-err").textContent = "Paste a YouTube link first."; return; }
    fd.append("url", u);
  } else if (STATE.src === "upload") {
    const f = $("#gen-file").files[0];
    if (!f) { $("#gen-err").textContent = "Choose a video file first."; return; }
    fd.append("file", f);
  } else {
    const n = $("#gen-local").value;
    if (!n) { $("#gen-err").textContent = "The library is empty."; return; }
    fd.append("name", n);
  }
  fd.append("count", $("#gen-count").value || "3");
  fd.append("niche", $("#gen-niche").value.trim());
  fd.append("min_seconds", $("#gen-min").value || "0");
  fd.append("max_seconds", $("#gen-max").value || "0");
  fd.append("face_tracking", $("#gen-face").checked ? "1" : "0");

  const btn = $("#gen-btn");
  btn.disabled = true;
  try {
    const r = await api("/api/generate", { method: "POST", body: fd });
    toast("Generation started");
    $("#gen-url").value = "";
    watchBatch(r.batch_id);
  } catch (e) {
    $("#gen-err").textContent = e.message;
  } finally { btn.disabled = false; }
};

// --- batch progress panels ---
async function loadActiveBatches() {
  try {
    const r = await api("/api/batches");
    (r.batches || []).filter((b) => !b.done).forEach((b) => watchBatch(b.id));
  } catch (e) {}
}

function batchPanel(id) {
  let el = $(`[data-batch="${id}"]`);
  if (!el) {
    el = document.createElement("div");
    el.className = "card";
    el.dataset.batch = id;
    el.innerHTML = `
      <div class="row spread"><h3>Generating…</h3>
        <span class="muted small" data-role="pct"></span></div>
      <div class="progress"><div class="bar"><i style="width:0%"></i></div>
        <div class="plabel"><span data-role="stage">starting…</span></div></div>
      <div data-role="note"></div>
      <div class="batch-shorts" data-role="shorts"></div>`;
    $("#active-batches").prepend(el);
  }
  return el;
}

function watchBatch(id) {
  if (STATE.batchPolls[id]) return;
  const el = batchPanel(id);
  const tick = async () => {
    let b;
    try { b = await api(`/api/batch/${id}`); }
    catch (e) { stop(); return; }
    el.querySelector("[data-role=stage]").textContent = b.stage || "…";
    el.querySelector("[data-role=pct]").textContent =
      b.percent ? `${Math.round(b.percent)}%` : "";
    el.querySelector(".bar i").style.width = `${b.percent || 0}%`;
    el.querySelector("[data-role=note]").innerHTML = b.error
      ? `<div class="callout-err">${esc(b.error)}</div>`
      : (b.note ? `<div class="note">${esc(b.note)}</div>` : "");
    el.querySelector("[data-role=shorts]").innerHTML = (b.shorts || []).map(
      (j) => `<div class="mini ${esc(j.status)}"><span class="st"></span>
         <span dir="auto">${esc(j.meta.title || j.topic || j.id)}</span>
         <span class="muted">· ${esc(j.stage || j.status)}</span></div>`).join("");
    updateBadge(b.shorts || []);
    if (b.done) {
      stop();
      el.querySelector("h3").textContent = b.error ? "Generation failed" : "Done";
      if (!b.error) {
        toast("Shorts are ready for review");
        setTimeout(() => { el.remove(); }, 4500);
        go("shorts");
      }
    }
  };
  const h = setInterval(tick, 2000);
  const stop = () => { clearInterval(h); delete STATE.batchPolls[id]; };
  STATE.batchPolls[id] = h;
  tick();
}

function updateBadge(shorts) {
  const n = shorts.filter((j) => j.status === "ready").length;
  const b = $("#tab-badge");
  b.textContent = n;
  b.classList.toggle("hidden", n === 0);
}

// --- standalone download ---
$("#dl-btn").onclick = async () => {
  const u = $("#dl-url").value.trim();
  if (!u) return;
  const fd = new FormData(); fd.append("url", u);
  const box = $("#dl-progress");
  try {
    const r = await api("/api/download", { method: "POST", body: fd });
    $("#dl-url").value = "";
    box.innerHTML = `<div class="progress"><div class="bar"><i style="width:0%"></i></div>
      <div class="plabel"><span data-role="st">starting…</span><span data-role="pc"></span></div></div>`;
    clearInterval(STATE.dlPoll);
    STATE.dlPoll = setInterval(async () => {
      let d;
      try { d = await api(`/api/download/${r.download_id}`); }
      catch (e) { clearInterval(STATE.dlPoll); return; }
      box.querySelector("[data-role=st]").textContent = d.error || d.stage;
      box.querySelector("[data-role=pc]").textContent =
        d.percent ? `${Math.round(d.percent)}%` : "";
      box.querySelector(".bar i").style.width = `${d.percent || 0}%`;
      if (d.done) {
        clearInterval(STATE.dlPoll);
        if (!d.error) { toast(`Saved: ${d.file}`); loadLibrary(); }
        setTimeout(() => (box.innerHTML = ""), 3000);
      }
    }, 1000);
  } catch (e) { toast(e.message); }
};

// --- library ---
$("#lib-refresh").onclick = () => loadLibrary();
async function loadLibrary() {
  const el = $("#library");
  try {
    const r = await api("/api/library");
    const sel = $("#gen-local");
    sel.innerHTML = r.videos.map(
      (v) => `<option value="${esc(v.name)}">${esc(v.name)} (${v.size_mb} MB)</option>`).join("");
    el.innerHTML = r.videos.length
      ? r.videos.map((v) => `
        <div class="vrow">
          <div class="grow"><div class="vname">${esc(v.name)}</div>
            <div class="muted small">${v.size_mb} MB</div></div>
          <button class="btn ghost small" data-cut="${esc(v.name)}">Cut into Shorts</button>
        </div>`).join("")
      : `<div class="muted small">Nothing downloaded yet.</div>`;
    el.querySelectorAll("[data-cut]").forEach((b) => (b.onclick = () => {
      STATE.src = "local";
      $$("#src-seg button").forEach((x) =>
        x.classList.toggle("on", x.dataset.src === "local"));
      $("#src-url").classList.add("hidden");
      $("#src-upload").classList.add("hidden");
      $("#src-local").classList.remove("hidden");
      $("#gen-local").value = b.dataset.cut;
      window.scrollTo({ top: 0, behavior: "smooth" });
    }));
  } catch (e) { el.innerHTML = `<div class="muted small">${esc(e.message)}</div>`; }
}

// ====================================================================
// SHORTS (review + upload)
// ====================================================================
async function loadShorts() {
  const list = $("#shorts-list");
  if (!list.children.length) list.innerHTML = `<div class="skel"></div><div class="skel"></div>`;
  let r;
  try { r = await api("/api/shorts"); }
  catch (e) {
    // transient failure: keep existing cards; only clear the skeletons
    list.querySelectorAll(".skel").forEach((s) => s.remove());
    return;
  }
  const jobs = r.shorts || [];
  list.querySelectorAll(".skel").forEach((s) => s.remove());
  $("#shorts-empty").classList.toggle("hidden", jobs.length > 0);
  const seen = new Set();
  jobs.forEach((j) => { seen.add(j.id); upsertCard(j); });
  Object.keys(STATE.cards).forEach((id) => {
    if (!seen.has(id)) { STATE.cards[id].remove(); delete STATE.cards[id]; }
  });
  updateUploadAll(jobs);
  updateBadge(jobs);
}

function upsertCard(j) {
  let card = STATE.cards[j.id];
  if (!card) {
    card = document.createElement("div");
    card.className = "short-card";
    card.dataset.id = j.id;
    card.innerHTML = cardHtml(j);
    $("#shorts-list").appendChild(card);
    STATE.cards[j.id] = card;
    wireCard(card, j);
  }
  updateCard(card, j);
  if (["processing", "publishing", "new"].includes(j.status) || j.stage) pollJob(j.id);
}

function cardHtml(j) {
  return `
  <div class="media">
    <video controls playsinline preload="none" src="/api/preview/${j.id}"></video>
    <div>
      <div class="thumb-box" data-role="thumb"><span>no thumbnail yet</span></div>
      <div class="thumb-actions">
        <button class="btn small" data-act="regen">↻ Regenerate</button>
        <button class="btn small" data-act="frames">🖼 Frame</button>
        <button class="btn small" data-act="headline">✎ Headline</button>
        <button class="btn small" data-act="upthumb">⬆ Upload</button>
        <a class="btn small disabled" data-act="dl"
           href="/api/job/${j.id}/thumbnail?download=1">⬇ Save</a>
        <input type="file" accept="image/*" data-role="thumbfile" hidden>
      </div>
      <div data-role="framestrip"></div>
    </div>
  </div>
  <div class="badge-row" data-role="badges"></div>
  <div data-role="why"></div>
  <div class="fields">
    <label>Title
      <input data-f="title" type="text" maxlength="100" dir="auto">
      <span class="charcount" data-role="tcount"></span>
    </label>
    <label>Description
      <textarea data-f="description" rows="4" dir="auto"></textarea>
    </label>
    <label>Hashtags <span class="hint">(space separated)</span>
      <input data-f="hashtags" type="text" dir="auto">
    </label>
    <label class="hidden" data-role="headline-row">Thumbnail headline
      <input data-f="headline" type="text" dir="auto">
    </label>
    <div class="row">
      <button class="btn small" data-act="save">Save</button>
      <button class="btn small" data-act="ai">AI rewrite</button>
    </div>
  </div>
  <div class="upload-row">
    <select data-role="privacy">
      <option value="public">Public</option>
      <option value="unlisted">Unlisted</option>
      <option value="private">Private</option>
    </select>
    <button class="btn primary" data-act="upload">Upload to YouTube</button>
  </div>
  <div data-role="status"></div>`;
}

function wireCard(card, j0) {
  const id = j0.id;
  const F = (n) => card.querySelector(`[data-f="${n}"]`);

  F("title").addEventListener("input", () => {
    card.querySelector("[data-role=tcount]").textContent =
      `${F("title").value.length}/100`;
  });

  const saveMeta = () => jpost(`/api/job/${id}/meta`, {
    title: F("title").value,
    description: F("description").value,
    hashtags: F("hashtags").value.split(/\s+/).filter(Boolean),
    thumbnail_headline: F("headline").value,
  });
  card._saveMeta = saveMeta;

  card.querySelector("[data-act=save]").onclick = async () => {
    try { await saveMeta(); toast("Saved"); }
    catch (e) { toast(e.message); }
  };

  card.querySelector("[data-act=ai]").onclick = async (e) => {
    const b = e.target; b.disabled = true; b.textContent = "Writing…";
    try {
      const r = await jpost(`/api/job/${id}/generate`, { niche: "" });
      fillFields(card, r.meta);
      toast("Rewritten — review then Save");
    } catch (err) { toast(err.message); }
    b.disabled = false; b.textContent = "AI rewrite";
  };

  card.querySelector("[data-act=regen]").onclick = () =>
    regenThumb(card, id, {});

  card.querySelector("[data-act=headline]").onclick = () => {
    const row = card.querySelector("[data-role=headline-row]");
    if (row.classList.contains("hidden")) { row.classList.remove("hidden"); return; }
    regenThumb(card, id, { headline: F("headline").value });
  };

  card.querySelector("[data-act=frames]").onclick = async () => {
    const strip = card.querySelector("[data-role=framestrip]");
    if (strip.innerHTML) { strip.innerHTML = ""; return; }
    try {
      const r = await api(`/api/job/${id}/frames`);
      if (!(r.frames || []).length) { toast("No candidate frames stored"); return; }
      strip.innerHTML = `<div class="frame-strip">` + r.frames.map(
        (f) => `<img src="/api/job/${id}/frames/${esc(f.name)}"
                     data-t="${f.t}" title="${f.t}s">`).join("") + `</div>`;
      strip.querySelectorAll("img").forEach((im) => (im.onclick = () => {
        strip.querySelectorAll("img").forEach((x) => x.classList.remove("sel"));
        im.classList.add("sel");
        regenThumb(card, id, { frame_t: +im.dataset.t });
      }));
    } catch (e) { toast(e.message); }
  };

  // custom thumbnail: pick a photo from the phone, it replaces the composed
  // one everywhere (Save button + first-frame embed on upload)
  const tfile = card.querySelector("[data-role=thumbfile]");
  card.querySelector("[data-act=upthumb]").onclick = () => tfile.click();
  tfile.onchange = async () => {
    const f = tfile.files[0];
    if (!f) return;
    const fd = new FormData();
    fd.append("file", f);
    const b = card.querySelector("[data-act=upthumb]");
    b.disabled = true;
    try {
      await api(`/api/job/${id}/thumbnail/upload`, { method: "POST", body: fd });
      card.querySelector("[data-role=thumb]").innerHTML =
        `<img src="/api/job/${id}/thumbnail?t=${Date.now()}">`;
      card.querySelector("[data-act=dl]").classList.remove("disabled");
      toast("Custom thumbnail set");
    } catch (e) { toast(e.message); }
    b.disabled = false;
    tfile.value = "";
  };

  card.querySelector("[data-act=upload]").onclick = () => uploadJob(card, id);
}

async function regenThumb(card, id, body) {
  try {
    await jpost(`/api/job/${id}/thumbnail`, body);
    toast("Rebuilding thumbnail…");
    pollJob(id);
  } catch (e) { toast(e.message); }
}

async function uploadJob(card, id) {
  const btn = card.querySelector("[data-act=upload]");
  const privacy = card.querySelector("[data-role=privacy]").value;
  btn.disabled = true;
  try {
    // save edits FIRST (awaited) so what you see is what uploads
    await card._saveMeta();
    await jpost(`/api/job/${id}/upload`, {
      privacy, embed_thumb: STATE.status.embed_thumb,
    });
    toast("Upload started");
    pollJob(id);
  } catch (e) {
    toast(e.message);
    btn.disabled = false;
    // a failed POST must not stall the upload-all queue
    if (STATE.uploadQueue.length && STATE.uploadQueue[0] === id) {
      STATE.uploadQueue.shift();
      runUploadQueue();
    }
  }
}

function fillFields(card, meta) {
  const F = (n) => card.querySelector(`[data-f="${n}"]`);
  F("title").value = meta.title || "";
  F("description").value = meta.description || "";
  F("hashtags").value = (meta.hashtags || []).join(" ");
  F("headline").value = meta.thumbnail_headline || "";
  card.querySelector("[data-role=tcount]").textContent =
    `${(meta.title || "").length}/100`;
}

function updateCard(card, j) {
  // fields: fill only when untouched by the user (avoid clobbering edits)
  if (!card.dataset.filled && (j.meta.title || j.meta.description)) {
    fillFields(card, j.meta);
    card.dataset.filled = "1";
  }
  // set the privacy select ONCE (stored value, else the server default) —
  // re-setting on every poll tick would clobber an in-progress selection
  if (!card.dataset.privSet) {
    card.querySelector("[data-role=privacy]").value =
      j.privacy || STATE.status.default_privacy || "public";
    card.dataset.privSet = "1";
  }

  // badges
  const badges = [];
  if (j.score > 0) badges.push(`<span class="score-badge">${Math.round(j.score)}</span>`);
  if (j.topic) badges.push(`<span class="topic-chip" dir="auto">${esc(j.topic)}</span>`);
  if (j.duration && j.segment)
    badges.push(`<span class="topic-chip">${Math.round(j.segment[0])}s → ${Math.round(j.segment[1])}s</span>`);
  if (j.reason)
    badges.push(`<button class="linkish" data-act="why">Why this clip?</button>`);
  const brow = card.querySelector("[data-role=badges]");
  if (brow.innerHTML !== badges.join("")) {
    brow.innerHTML = badges.join("");
    const why = brow.querySelector("[data-act=why]");
    if (why) why.onclick = () => {
      const w = card.querySelector("[data-role=why]");
      w.innerHTML = w.innerHTML ? "" : `<div class="why" dir="auto">${esc(j.reason)}</div>`;
    };
  }

  // thumbnail: initial load here; refreshes after a rebuild are handled by
  // pollJob's settled-state hook (cache-busted)
  const tb = card.querySelector("[data-role=thumb]");
  if (j.has_thumb && !tb.querySelector("img")) {
    tb.innerHTML = `<img src="/api/job/${j.id}/thumbnail?t=${Date.now()}">`;
  }
  card.querySelector("[data-act=dl]").classList.toggle("disabled", !j.has_thumb);

  // status line
  const st = card.querySelector("[data-role=status]");
  const yt = (j.results || {}).youtube;
  let html = "";
  if (j.status === "processing" || j.status === "publishing" || j.stage) {
    html = `<div class="stage-line"><span class="spinner"></span>${esc(j.stage || j.status)}…</div>`;
  } else if (j.status === "error") {
    html = `<div class="result-err">${esc(j.error)}</div>`;
  } else if (yt && yt.ok && !yt.dry_run) {
    const url = yt.url || `https://youtube.com/shorts/${j.youtube_id}`;
    html = `<div class="result-ok">✓ Uploaded — <a href="${esc(url)}" target="_blank" rel="noopener">watch on YouTube</a></div>`;
    if (j.thumb_api) html += `<div class="thumb-api-note">API thumbnail: ${esc(j.thumb_api)}</div>`;
  } else if (yt && !yt.ok) {
    html = `<div class="result-err">${esc(yt.error)}</div>`;
    if (yt.needs_login) html += `<div class="thumb-api-note">Re-auth on the PC: <code>python -m studio.login_setup</code></div>`;
  }
  if (st.innerHTML !== html) st.innerHTML = html;

  const upBtn = card.querySelector("[data-act=upload]");
  const busy = ["processing", "publishing"].includes(j.status) || !!j.stage;
  upBtn.disabled = busy || !j.has_output;
  if (yt && yt.ok && !yt.dry_run) upBtn.textContent = "Upload again";
}

function pollJob(id) {
  if (STATE.jobPolls[id]) return;
  const tick = async () => {
    let j;
    try { j = await api(`/api/job/${id}`); }
    catch (e) { stop(); return; }
    const card = STATE.cards[id];
    if (card) updateCard(card, j);
    const settled = ["ready", "done", "error"].includes(j.status) && !j.stage;
    if (settled) {
      stop();
      // one final thumbnail refresh after a rebuild finished
      if (card && j.has_thumb) {
        const tb = card.querySelector("[data-role=thumb]");
        tb.innerHTML = `<img src="/api/job/${id}/thumbnail?t=${Date.now()}">`;
      }
      if (STATE.uploadQueue.length && STATE.uploadQueue[0] === id) {
        STATE.uploadQueue.shift();
        runUploadQueue();
      }
    }
  };
  const h = setInterval(tick, 2000);
  const stop = () => { clearInterval(h); delete STATE.jobPolls[id]; };
  STATE.jobPolls[id] = h;
}

// --- upload all ---
function updateUploadAll(jobs) {
  const ready = jobs.filter((j) => j.status === "ready" && j.meta.title);
  const bar = $("#upload-all-bar");
  bar.classList.toggle("hidden", ready.length < 2);
  $("#upload-all-btn").textContent = `Upload all (${ready.length})`;
  $("#upload-all-btn").onclick = () => {
    if (!confirm(`Upload ${ready.length} shorts to YouTube?`)) return;
    STATE.uploadQueue = ready.map((j) => j.id);
    runUploadQueue();
  };
}

function runUploadQueue() {
  const id = STATE.uploadQueue[0];
  if (!id) { toast("All uploads started/finished"); loadShorts(); return; }
  const card = STATE.cards[id];
  if (card) uploadJob(card, id);
  else STATE.uploadQueue.shift();
}

// ====================================================================
// SETTINGS
// ====================================================================
async function loadModels() {
  let m;
  try { m = await api("/api/models"); } catch (e) { return; }
  STATE.models = m;
  const prov = $("#m-provider");
  prov.innerHTML = m.providers.map(
    (p) => `<option value="${p}" ${p === m.provider ? "selected" : ""}>${p}</option>`).join("");
  renderModelList();
  prov.onchange = renderModelList;
  $("#m-status").textContent = m.active_available ? "active model reachable ✓" : "";
}

function renderModelList() {
  const m = STATE.models;
  const p = $("#m-provider").value;
  const list = p === "ollama" ? (m.ollama_models || []) : (m.cloud_models[p] || []);
  $("#m-model").innerHTML = list.map(
    (x) => `<option ${x === m.model ? "selected" : ""}>${esc(x)}</option>`).join("")
    || `<option value="">(none found)</option>`;
  const needsKey = p !== "ollama";
  $("#m-key-row").classList.toggle("hidden", !needsKey);
  if (needsKey && m.keys[p]) $("#m-key").placeholder = "key saved ✓ (write to replace)";
}

$("#m-apply").onclick = async () => {
  const provider = $("#m-provider").value;
  const model = $("#m-custom").value.trim() || $("#m-model").value;
  try {
    const key = $("#m-key").value.trim();
    if (key && provider !== "ollama") {
      await jpost("/api/models/key", { provider, key });
      $("#m-key").value = "";
    }
    const r = await jpost("/api/models/select", { provider, model });
    $("#m-status").textContent = r.available ? "applied ✓" : "applied — not reachable yet";
    toast("Model applied");
    boot();
  } catch (e) { $("#m-status").textContent = e.message; }
};

$("#m-test").onclick = async () => {
  $("#m-status").textContent = "testing…";
  try {
    const r = await jpost("/api/models/test", {
      provider: $("#m-provider").value,
      model: $("#m-custom").value.trim() || $("#m-model").value,
    });
    $("#m-status").textContent = r.ok ? `✓ ${r.reply}` : `✗ ${r.error}`;
  } catch (e) { $("#m-status").textContent = e.message; }
};

async function loadYouTube() {
  try {
    const r = await api("/api/connections");
    const y = r.youtube || {};
    $("#yt-status").innerHTML = `
      <div class="hrow"><span class="st ${y.has_token ? "ok" : "bad"}"></span>
        <div class="grow">Authorized token</div>
        <span class="muted small">${y.has_token ? "saved" : "missing"}</span></div>
      <div class="hrow"><span class="st ${y.has_client_secret ? "ok" : "warn"}"></span>
        <div class="grow">OAuth client secret</div>
        <span class="muted small">${y.has_client_secret ? "found" : "missing"}</span></div>`;
  } catch (e) {}
}

$("#yt-check").onclick = async () => {
  const out = $("#yt-check-out");
  out.textContent = "checking…";
  try {
    const r = await jpost("/api/connections/youtube/health", {});
    let ticks = 0;
    const poll = setInterval(async () => {
      try {
        const s = await api(`/api/connections/run/${r.run_id}`);
        if (!s.done && ++ticks < 40) return;
        clearInterval(poll);
        const res = s.result || {};
        out.textContent = s.error ? `✗ ${s.error}`
          : (res.ok ? `✓ ${res.detail}` : `✗ ${res.detail || "timed out"}`);
        loadYouTube();
      } catch (e) {
        clearInterval(poll);   // never leak the interval on 401/network errors
        out.textContent = e.message;
      }
    }, 1500);
  } catch (e) { out.textContent = e.message; }
};

$("#health-refresh").onclick = () => loadHealth();
async function loadHealth() {
  const el = $("#health-list");
  try {
    const h = await api("/api/health");
    el.innerHTML = h.checks.map((c) => `
      <div class="hrow">
        <span class="st ${c.ok ? "ok" : (c.critical ? "bad" : "warn")}"></span>
        <div class="grow">${esc(c.name)}
          ${c.detail ? `<div class="detail">${esc(c.detail)}</div>` : ""}</div>
      </div>`).join("");
  } catch (e) { el.innerHTML = `<div class="muted small">${esc(e.message)}</div>`; }
}

// ---------- go ----------
boot();
