import { app } from "../../scripts/app.js";

// Spark Fuse bridge UI: a floating button that opens a panel to configure the
// job, send the current workflow to a Spark Fuse cloud GPU, watch progress, and
// show the returned image. A floating button is used so it works regardless of
// the ComfyUI menu version; move it into the menu later if you prefer.

const api = (path, opts) => fetch(`/spark_fuse${path}`, opts).then((r) => r.json());

let pollTimer = null;
let queueItems = [];
let queueRunning = false;
let queueId = null;

function el(tag, props = {}, ...children) {
  const node = Object.assign(document.createElement(tag), props);
  for (const c of children) node.append(c);
  return node;
}

function field(labelText, input) {
  return el("label", { style: "display:flex;flex-direction:column;gap:2px;font-size:12px;margin-bottom:8px;" },
    el("span", { textContent: labelText, style: "opacity:0.8;" }), input);
}

function buildPanel() {
  const panel = el("div", {
    id: "spark-fuse-panel",
    style: `position:fixed;top:60px;right:16px;width:360px;max-height:80vh;overflow:auto;
            background:#1e1e1e;color:#eee;border:1px solid #444;border-radius:8px;padding:14px;
            z-index:10000;box-shadow:0 6px 24px rgba(0,0,0,0.5);font-family:sans-serif;display:none;`,
  });

  const title = el("div", { style: "display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;" },
    el("strong", { textContent: "Render on Spark Fuse" }),
    el("span", { textContent: "✕", style: "cursor:pointer;opacity:0.7;", onclick: () => (panel.style.display = "none") }));

  const skuSelect = el("select", { id: "sf-sku", style: "padding:4px;background:#2a2a2a;color:#eee;border:1px solid #555;" });
  const rateLabel = el("div", { id: "sf-rate", textContent: "", style: "font-size:12px;opacity:0.8;margin-bottom:8px;" });
  skuSelect.onchange = updateRate;

  const assetsInput = el("input", { id: "sf-assets", type: "text", style: inputStyle() });
  const affinitySelect = el("select", { id: "sf-affinity", style: inputStyle() });
  affinitySelect.append(el("option", { value: "preferred", textContent: "preferred" }),
                        el("option", { value: "required", textContent: "required" }));

  const hostInput = el("input", { id: "sf-host", type: "text", placeholder: "https://api.prod...", style: inputStyle() });
  const emailInput = el("input", { id: "sf-email", type: "text", style: inputStyle() });
  const passInput = el("input", { id: "sf-pass", type: "password", placeholder: "(unchanged)", style: inputStyle() });
  const batchInput = el("input", { id: "sf-batch", type: "number", min: "1", max: "100", value: "1", style: inputStyle() });

  const saveBtn = el("button", { textContent: "Save settings", style: btnStyle("#3a3a3a"), onclick: async () => { await saveSettings(); await loadSkus(); } });
  const renderBtn = el("button", { id: "sf-render", textContent: "Render on Spark Fuse", style: btnStyle("#7c5cff") + "opacity:0.5;cursor:not-allowed;", disabled: true, onclick: onRender });

  const addQueueBtn = el("button", { id: "sf-add-queue", textContent: "Add to queue", style: smallBtn("#3a3a3a"), onclick: addToQueue });
  const queueList = el("div", { id: "sf-queue-list", style: "display:flex;flex-direction:column;gap:3px;font-size:12px;margin:4px 0;" });
  const runQueueBtn = el("button", { id: "sf-run-queue", textContent: "Run queue", style: btnStyle("#7c5cff"), onclick: runQueue });
  const clearQueueBtn = el("button", { id: "sf-clear-queue", textContent: "Clear", style: btnStyle("#3a3a3a"), onclick: clearQueue });
  const cancelQueueBtn = el("button", { id: "sf-cancel-queue", textContent: "Cancel queue",
    style: "width:100%;padding:8px;border:none;border-radius:4px;background:#aa3333;color:#fff;cursor:pointer;font-size:13px;margin-top:6px;display:none;", onclick: cancelQueue });
  const queueSection = el("div", { style: "border-top:1px solid #333;margin-top:6px;padding-top:8px;" },
    el("div", { style: "display:flex;justify-content:space-between;align-items:center;margin-bottom:4px;" },
       el("strong", { textContent: "Render queue", style: "font-size:13px;" }), addQueueBtn),
    queueList,
    el("div", { style: "display:flex;gap:8px;" }, runQueueBtn, clearQueueBtn),
    cancelQueueBtn);

  const status = el("div", { id: "sf-status", style: "font-size:12px;margin:8px 0;min-height:16px;" });
  const log = el("pre", { id: "sf-log", style: `background:#111;border:1px solid #333;border-radius:4px;padding:6px;
            height:280px;min-height:120px;resize:vertical;overflow:auto;font-size:11px;white-space:pre-wrap;margin:0 0 8px;` });
  const preview = el("img", { id: "sf-preview", style: "max-width:100%;border:1px solid #333;border-radius:4px;display:none;" });

  panel.append(
    title,
    field("GPU", skuSelect), rateLabel,
    field("Assets ShareSync path (models)", assetsInput),
    field("Image affinity", affinitySelect),
    field("Batch count (images per job)", batchInput),
    el("details", {}, el("summary", { textContent: "Credentials", style: "cursor:pointer;font-size:12px;margin-bottom:6px;" }),
       field("Host", hostInput), field("Email", emailInput), field("Password", passInput)),
    el("div", { style: "display:flex;gap:8px;margin:6px 0 10px;" }, saveBtn, renderBtn),
    queueSection,
    status, log, preview,
  );
  document.body.append(panel);
  return panel;
}

const inputStyle = () => "padding:4px;background:#2a2a2a;color:#eee;border:1px solid #555;border-radius:3px;";
const btnStyle = (bg) => `flex:1;padding:8px;border:none;border-radius:4px;background:${bg};color:#fff;cursor:pointer;font-size:13px;`;
const smallBtn = (bg) => `padding:5px 10px;border:none;border-radius:4px;background:${bg};color:#fff;cursor:pointer;font-size:12px;`;

function setStatus(text, color = "#ddd") {
  const s = document.getElementById("sf-status");
  if (s) { s.textContent = text; s.style.color = color; }
}

function setRenderEnabled(on) {
  const b = document.getElementById("sf-render");
  if (!b) return;
  b.disabled = !on;
  b.style.opacity = on ? "1" : "0.5";
  b.style.cursor = on ? "pointer" : "not-allowed";
}

async function loadSettings() {
  try {
    const s = await api("/settings");
    document.getElementById("sf-assets").value = s.assets_share_sync_path || "";
    document.getElementById("sf-affinity").value = s.image_affinity || "preferred";
    document.getElementById("sf-batch").value = s.batch_count || 1;
    document.getElementById("sf-host").value = s.host || "";
    document.getElementById("sf-email").value = s.email || "";
    if (s.password_set) document.getElementById("sf-pass").placeholder = "(stored — leave blank to keep)";
  } catch (e) { setStatus(`Could not load settings: ${e}`, "#ff8888"); }
}

async function loadSkus() {
  const select = document.getElementById("sf-sku");
  try {
    const data = await api("/skus");
    if (data.error) { setStatus(`Could not list GPUs: ${data.error}`, "#ff8888"); setRenderEnabled(false); return; }
    const saved = (await api("/settings")).instance_type;
    select.innerHTML = "";
    for (const sku of data.skus) {
      if (!sku.instanceType) continue;
      const mem = sku.gpuMemoryGb ? ` ${sku.gpuMemoryGb}GB` : "";
      const gpu = sku.gpuType ? ` (${sku.gpuType}${mem})` : "";
      select.append(el("option", { value: sku.instanceType, textContent: `${sku.instanceType}${gpu}` }));
    }
    if (saved) select.value = saved;
    updateRate();
    setRenderEnabled(!!select.value);
  } catch (e) { setStatus(`Could not list GPUs: ${e}`, "#ff8888"); setRenderEnabled(false); }
}

async function updateRate() {
  const sku = document.getElementById("sf-sku").value;
  const rate = document.getElementById("sf-rate");
  rate.textContent = "checking price...";
  const data = await api(`/estimate?instance_type=${encodeURIComponent(sku)}`);
  rate.textContent = data.ratePerHourUsd ? `Rate: $${data.ratePerHourUsd}/hr` : "Rate: unavailable (SKU not priced)";
}

async function saveSettings() {
  const body = {
    instance_type: document.getElementById("sf-sku").value,
    assets_share_sync_path: document.getElementById("sf-assets").value,
    image_affinity: document.getElementById("sf-affinity").value,
    batch_count: parseInt(document.getElementById("sf-batch").value, 10) || 1,
    host: document.getElementById("sf-host").value,
    email: document.getElementById("sf-email").value,
  };
  const pass = document.getElementById("sf-pass").value;
  if (pass) body.password = pass;
  await api("/settings", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  setStatus("Settings saved.", "#88ff88");
}

async function onRender() {
  if (pollTimer) clearInterval(pollTimer);
  const renderBtn = document.getElementById("sf-render");
  const log = document.getElementById("sf-log");
  const preview = document.getElementById("sf-preview");
  preview.style.display = "none";
  log.textContent = "";
  const sku = document.getElementById("sf-sku").value;
  if (!sku) { setStatus("Pick a GPU first; the list may still be loading.", "#ff8888"); return; }
  if (queueRunning) { setStatus("A render queue is running; wait for it to finish.", "#ff8888"); return; }
  setRenderEnabled(false);
  setQueueButtonsEnabled(false);
  setStatus("Exporting workflow and submitting...", "#ffd479");

  try {
    await saveSettings();
    const prompt = await app.graphToPrompt();
    const res = await api("/submit", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ workflow: prompt.output, instance_type: sku }),
    });
    if (res.error) { setStatus(`Submit failed: ${res.error}`, "#ff8888"); setRenderEnabled(true); setQueueButtonsEnabled(true); return; }
    setStatus(`Submitted job ${res.jobId}. Watching...`, "#ffd479");
    pollJob(res.jobId);
  } catch (e) {
    setStatus(`Error: ${e}`, "#ff8888");
    setRenderEnabled(true);
    setQueueButtonsEnabled(true);
  }
}

function pollJob(jobId) {
  const log = document.getElementById("sf-log");
  const preview = document.getElementById("sf-preview");
  const renderBtn = document.getElementById("sf-render");
  pollTimer = setInterval(async () => {
    let st;
    try { st = await api(`/job/${jobId}`); } catch { return; }
    if (st.error && !st.status) return;
    log.textContent = (st.lines || []).join("\n");
    log.scrollTop = log.scrollHeight;
    const hit = st.image_cache_hit === true ? "  [image cache hit]" : st.image_cache_hit === false ? "  [cold pull]" : "";
    setStatus(`Status: ${st.status || "?"}${hit}`, st.status === "succeeded" ? "#88ff88" : st.status === "failed" ? "#ff8888" : "#ffd479");
    if (st.image) {
      preview.src = `/view?filename=${encodeURIComponent(st.image)}&type=output&t=${Date.now()}`;
      preview.style.display = "block";
    }
    if (st.status === "succeeded" || st.status === "failed" || st.status === "cancelled") {
      clearInterval(pollTimer);
      pollTimer = null;
      setRenderEnabled(true);
      setQueueButtonsEnabled(true);
      if (st.status === "failed" && st.error) setStatus(`Failed: ${st.error}`, "#ff8888");
    }
  }, 2500);
}

// ---- Render queue -------------------------------------------------------

function setQueueButtonsEnabled(on) {
  for (const id of ["sf-add-queue", "sf-run-queue", "sf-clear-queue"]) {
    const b = document.getElementById(id);
    if (b) { b.disabled = !on; b.style.opacity = on ? "1" : "0.5"; b.style.cursor = on ? "pointer" : "not-allowed"; }
  }
}

function renderQueueList() {
  const list = document.getElementById("sf-queue-list");
  const runBtn = document.getElementById("sf-run-queue");
  if (!list) return;
  list.innerHTML = "";
  if (!queueItems.length) {
    list.append(el("div", { textContent: "Queue is empty. Open a workflow, set a batch count, then Add to queue.", style: "opacity:0.6;" }));
  }
  queueItems.forEach((it, i) => {
    const c = { succeeded: "#88ff88", failed: "#ff8888", running: "#ffd479", cancelled: "#ffaa55" }[it.status] || "#aaa";
    const row = el("div", { style: "display:flex;justify-content:space-between;align-items:center;gap:6px;background:#181818;border:1px solid #333;border-radius:4px;padding:4px 6px;" },
      el("span", { textContent: `${i + 1}. ${it.label} — ${it.batch_count} img`, style: "overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" }),
      el("span", { style: "display:flex;align-items:center;gap:6px;flex:none;" },
        el("span", { textContent: it.status, style: `color:${c};font-size:11px;` }),
        el("span", { textContent: "✕", title: "Remove", style: queueRunning ? "display:none;" : "cursor:pointer;opacity:0.7;", onclick: () => removeQueueItem(i) })));
    list.append(row);
  });
  if (runBtn) runBtn.textContent = queueItems.length ? `Run queue (${queueItems.length})` : "Run queue";
}

async function addToQueue() {
  if (queueRunning) return;
  try {
    const prompt = await app.graphToPrompt();
    const batch = parseInt(document.getElementById("sf-batch").value, 10) || 1;
    queueItems.push({ workflow: prompt.output, batch_count: batch, label: `Job ${queueItems.length + 1}`, status: "queued" });
    renderQueueList();
    setStatus(`Added to queue (${queueItems.length} total). Open the next workflow and add it too.`, "#88ff88");
  } catch (e) {
    setStatus(`Could not add to queue: ${e}`, "#ff8888");
  }
}

function removeQueueItem(i) {
  if (queueRunning) return;
  queueItems.splice(i, 1);
  renderQueueList();
}

function clearQueue() {
  if (queueRunning) return;
  queueItems = [];
  renderQueueList();
  setStatus("Queue cleared.", "#ddd");
}

function setQueueRunning(on) {
  queueRunning = on;
  for (const id of ["sf-add-queue", "sf-run-queue", "sf-clear-queue", "sf-render"]) {
    const b = document.getElementById(id);
    if (b) { b.disabled = on; b.style.opacity = on ? "0.5" : "1"; b.style.cursor = on ? "not-allowed" : "pointer"; }
  }
  const cancel = document.getElementById("sf-cancel-queue");
  if (cancel) cancel.style.display = on ? "block" : "none";
  if (!on) setRenderEnabled(!!document.getElementById("sf-sku").value);
  renderQueueList();
}

async function runQueue() {
  if (queueRunning) return;
  if (!queueItems.length) { setStatus("Queue is empty.", "#ff8888"); return; }
  const sku = document.getElementById("sf-sku").value;
  if (!sku) { setStatus("Pick a GPU first; the list may still be loading.", "#ff8888"); return; }
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  const preview = document.getElementById("sf-preview");
  const log = document.getElementById("sf-log");
  preview.style.display = "none";
  log.textContent = "";
  queueItems.forEach((it) => (it.status = "queued"));
  setQueueRunning(true);
  setStatus(`Preparing a warm instance for ${queueItems.length} job(s)...`, "#ffd479");
  try {
    await saveSettings();
    const res = await api("/queue", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        items: queueItems.map((it) => ({ workflow: it.workflow, batch_count: it.batch_count, label: it.label })),
        instance_type: sku,
      }),
    });
    if (res.error) { setStatus(`Queue failed: ${res.error}`, "#ff8888"); setQueueRunning(false); return; }
    queueId = res.queueId;
    pollQueue(queueId);
  } catch (e) {
    setStatus(`Error: ${e}`, "#ff8888");
    setQueueRunning(false);
  }
}

function pollQueue(qid) {
  const log = document.getElementById("sf-log");
  const preview = document.getElementById("sf-preview");
  pollTimer = setInterval(async () => {
    let st;
    try { st = await api(`/queue/${qid}`); } catch { return; }
    if (st.error && !st.status) return;
    log.textContent = (st.lines || []).join("\n");
    log.scrollTop = log.scrollHeight;
    if (Array.isArray(st.items)) {
      st.items.forEach((sit) => { if (queueItems[sit.index]) queueItems[sit.index].status = sit.status; });
      renderQueueList();
    }
    const map = { preparing: "#ffd479", running: "#ffd479", succeeded: "#88ff88", failed: "#ff8888", cancelled: "#ffaa55" };
    setStatus(`Queue: ${st.status || "?"}`, map[st.status] || "#ddd");
    if (st.image) {
      preview.src = `/view?filename=${encodeURIComponent(st.image)}&type=output&t=${Date.now()}`;
      preview.style.display = "block";
    }
    if (["succeeded", "failed", "cancelled"].includes(st.status)) {
      clearInterval(pollTimer);
      pollTimer = null;
      setQueueRunning(false);
    }
  }, 2500);
}

async function cancelQueue() {
  if (!queueId) return;
  setStatus("Cancelling queue (current job will be stopped)...", "#ffd479");
  try { await api(`/queue/${queueId}/cancel`, { method: "POST" }); } catch (e) { /* best effort */ }
}

app.registerExtension({
  name: "SparkFuse.Bridge",
  async setup() {
    const panel = buildPanel();
    renderQueueList();
    const button = el("button", {
      textContent: "⚡ Spark Fuse",
      style: `position:fixed;top:16px;right:16px;z-index:10000;padding:8px 12px;border:none;border-radius:6px;
              background:#7c5cff;color:#fff;cursor:pointer;font-family:sans-serif;font-size:13px;box-shadow:0 2px 8px rgba(0,0,0,0.4);`,
      onclick: async () => {
        const showing = panel.style.display === "block";
        panel.style.display = showing ? "none" : "block";
        if (!showing) { await loadSettings(); await loadSkus(); }
      },
    });
    document.body.append(button);
  },
});
