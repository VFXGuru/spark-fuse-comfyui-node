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
// Pre-render model sync: the active background upload (id) and its poll loop.
// The upload itself runs server-side; these only drive the progress badge.
let uploadPollTimer = null;
let activeUploadId = null;
let uploadBadge = null;

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
    el("span", {},
      el("strong", { textContent: "Render on Spark Fuse" }),
      el("span", { id: "sf-version", style: "opacity:0.6;font-size:11px;margin-left:6px;" })),
    el("span", { textContent: "✕", style: "cursor:pointer;opacity:0.7;", onclick: () => (panel.style.display = "none") }));

  const skuSelect = el("select", { id: "sf-sku", style: "padding:4px;background:#2a2a2a;color:#eee;border:1px solid #555;" });
  const rateLabel = el("div", { id: "sf-rate", textContent: "", style: "font-size:12px;opacity:0.8;margin-bottom:8px;" });
  skuSelect.onchange = updateRate;

  const assetsInput = el("input", { id: "sf-assets", type: "text", style: inputStyle(),
    oninput: updateAssetsPreview });
  const assetsPreview = el("div", { id: "sf-assets-preview", style: "font-size:11px;opacity:0.7;margin:-4px 0 8px;" });
  const affinitySelect = el("select", { id: "sf-affinity", style: inputStyle() });
  affinitySelect.append(el("option", { value: "preferred", textContent: "preferred" }),
                        el("option", { value: "required", textContent: "required" }));

  const hostInput = el("input", { id: "sf-host", type: "text", placeholder: "https://api.prod...", style: inputStyle() });
  const emailInput = el("input", { id: "sf-email", type: "text", style: inputStyle() });
  const passInput = el("input", { id: "sf-pass", type: "password", placeholder: "(unchanged)", style: inputStyle() });
  const batchInput = el("input", { id: "sf-batch", type: "number", min: "1", max: "100", value: "1", style: inputStyle() });
  const guardInput = el("input", { id: "sf-guard", type: "number", min: "1", value: "50", style: inputStyle() });

  const saveBtn = el("button", { textContent: "Save settings", style: btnStyle("#3a3a3a"),
    onclick: async () => { try { await saveSettings(); await loadSkus(); } catch (e) { setStatus(`Could not save settings: ${e}`, "#ff8888"); } } });
  const renderBtn = el("button", { id: "sf-render", textContent: "Render on Spark Fuse", style: btnStyle("#7c5cff") + "opacity:0.5;cursor:not-allowed;", disabled: true, onclick: onRender });

  const addQueueBtn = el("button", { id: "sf-add-queue", textContent: "Add to queue", style: smallBtn("#3a3a3a"), onclick: addToQueue });
  const queueList = el("div", { id: "sf-queue-list", style: "display:flex;flex-direction:column;gap:3px;font-size:12px;margin:4px 0;" });
  const runQueueBtn = el("button", { id: "sf-run-queue", textContent: "Run queue", style: btnStyle("#7c5cff"), onclick: runQueue });
  const clearQueueBtn = el("button", { id: "sf-clear-queue", textContent: "Clear", style: btnStyle("#3a3a3a"), onclick: clearQueue });
  const cancelQueueBtn = el("button", { id: "sf-cancel-queue", textContent: "Cancel queue (finishes current batch)",
    style: "width:100%;padding:8px;border:none;border-radius:4px;background:#aa3333;color:#fff;cursor:pointer;font-size:13px;margin-top:6px;display:none;", onclick: cancelQueue });
  const queueSection = el("details", { style: "border-top:1px solid #333;margin-top:6px;padding-top:8px;" },
    el("summary", { textContent: "Render queue — queue several workflows to run back to back", style: "cursor:pointer;font-size:13px;margin-bottom:4px;" }),
    el("div", { textContent: "Long queues run in batches of up to 10 workflows per job.",
                style: "font-size:11px;opacity:0.7;margin-bottom:4px;" }),
    el("div", { style: "display:flex;justify-content:flex-end;margin-bottom:4px;" }, addQueueBtn),
    queueList,
    el("div", { style: "display:flex;gap:8px;" }, runQueueBtn, clearQueueBtn),
    cancelQueueBtn);

  // Model-sync consent section — hidden until a check finds models to resolve.
  const consentSection = el("div", { id: "sf-consent",
    style: "display:none;border-top:1px solid #333;margin-top:6px;padding-top:8px;" });

  const status = el("div", { id: "sf-status", style: "font-size:12px;margin:8px 0;min-height:16px;" });
  const log = el("pre", { id: "sf-log", style: `background:#111;border:1px solid #333;border-radius:4px;padding:6px;
            height:280px;min-height:120px;resize:vertical;overflow:auto;font-size:11px;white-space:pre-wrap;margin:0 0 8px;` });
  const preview = el("img", { id: "sf-preview", style: "max-width:100%;border:1px solid #333;border-radius:4px;display:none;" });

  panel.append(
    title,
    field("GPU", skuSelect), rateLabel,
    field("Assets ShareSync path (models)", assetsInput),
    assetsPreview,
    field("Image affinity", affinitySelect),
    field("Batch count (images per job)", batchInput),
    el("details", {}, el("summary", { textContent: "Credentials", style: "cursor:pointer;font-size:12px;margin-bottom:6px;" }),
       field("Host", hostInput), field("Email", emailInput), field("Password", passInput),
       field("Upload guard (GB, max single-file auto-upload)", guardInput)),
    el("div", { style: "display:flex;gap:8px;margin:6px 0 10px;" }, saveBtn, renderBtn),
    status,
    queueSection,
    consentSection,
    log, preview,
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
    document.getElementById("sf-version").textContent = s.version ? `v${s.version}` : "";
    document.getElementById("sf-assets").value = s.assets_share_sync_path || "";
    updateAssetsPreview();
    document.getElementById("sf-affinity").value = s.image_affinity || "preferred";
    document.getElementById("sf-batch").value = s.batch_count || 1;
    document.getElementById("sf-guard").value = s.upload_guard_gb || 50;
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

// Mirrors the Spark Fuse API's own rejection (spark-fuse-api-v124.md:1134 in the
// SPARK FUSE MESSENGER repo: "assetsShareSyncPath must start with '/' ...") so the
// panel fails the same way, before submit, instead of after a 400 from the cloud.
const ASSETS_PATH_ERROR = "assetsShareSyncPath must start with '/' — add a leading slash.";

function updateAssetsPreview() {
  const preview = document.getElementById("sf-assets-preview");
  if (!preview) return;
  const raw = document.getElementById("sf-assets").value.trim();
  if (!raw) { preview.textContent = ""; return; }
  if (!raw.startsWith("/")) {
    preview.textContent = `⚠ ${ASSETS_PATH_ERROR}`;
    preview.style.color = "#ffaa55";
  } else {
    preview.textContent = `Resolves to: ${raw}`;
    preview.style.color = "";
  }
}

async function saveSettings() {
  const assetsPath = document.getElementById("sf-assets").value.trim();
  if (assetsPath && !assetsPath.startsWith("/")) throw new Error(ASSETS_PATH_ERROR);
  // An empty or invalid entry falls back to the 50 GB default rather than
  // erroring, since this field is easy to clear by accident.
  const guardRaw = parseInt(document.getElementById("sf-guard").value, 10);
  const uploadGuardGb = (Number.isFinite(guardRaw) && guardRaw > 0) ? guardRaw : 50;
  document.getElementById("sf-guard").value = uploadGuardGb;
  const body = {
    instance_type: document.getElementById("sf-sku").value,
    assets_share_sync_path: assetsPath,
    image_affinity: document.getElementById("sf-affinity").value,
    batch_count: parseInt(document.getElementById("sf-batch").value, 10) || 1,
    upload_guard_gb: uploadGuardGb,
    host: document.getElementById("sf-host").value,
    email: document.getElementById("sf-email").value,
  };
  const pass = document.getElementById("sf-pass").value;
  if (pass) body.password = pass;
  await api("/settings", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  setStatus("Settings saved.", "#88ff88");
}

// ---- Seed control_after_generate ----------------------------------------
// The bridge calls app.graphToPrompt() and submits directly, bypassing
// ComfyUI's own Queue Prompt path — so control_after_generate never fires
// and every submission carries the same seed. This mimics ComfyUI's own
// order of operations: the prompt is captured first with the CURRENT seed
// (this submission is unaffected), then the widget is advanced so the NEXT
// submission differs.
//
// Seed widgets are identified by an immediately adjacent control_after_
// generate widget (widgets[i+1].name === "control_after_generate"), not a
// name whitelist, so a custom loader using a different input name is still
// covered. Confirmed live: the control widget's value is one of fixed /
// increment / decrement / randomize, and the seed widget's own
// options.{min,max} carry the true bounds (max observed as
// 18446744073709552000 — a float above Number.MAX_SAFE_INTEGER, so
// randomising up to it verbatim would risk an imprecise, possibly
// exponential-notation value; the effective upper bound is clamped to
// Number.MAX_SAFE_INTEGER and every generated value is verified with
// Number.isSafeInteger() before being assigned).
//
// Subgraphs: confirmed live that a promoted seed widget on a subgraph
// wrapper node and the inner KSampler widget it is promoted from are
// linked — only one seed entry appears in the flattened prompt
// ("<wrapperId>:<innerId>"), not two. A promoted widget lives in the
// wrapper node's own .widgets array, so walking app.graph._nodes at the
// top level only already reaches it — no separate recursion into
// node.subgraph._nodes is done here, both because it is unnecessary for a
// promoted+linked widget and because, without live proof the two are
// always the exact same underlying reference, recursing in as well would
// risk advancing one linked seed twice with two different values (exactly
// what must not happen). The one gap this leaves: a seed widget that was
// never promoted out of a subgraph at all (no wrapper-level representation)
// is not reachable this way and will not be advanced. See the commit
// report for why this trade-off was chosen over risking a double-advance.

function advanceSeeds() {
  const nodes = app.graph?._nodes || [];
  const counts = { randomize: 0, increment: 0, decrement: 0 };
  for (const node of nodes) {
    const widgets = node.widgets;
    if (!Array.isArray(widgets)) continue;
    for (let i = 0; i < widgets.length - 1; i++) {
      const w = widgets[i];
      const control = widgets[i + 1];
      if (!control || control.name !== "control_after_generate") continue;
      if (typeof w.value !== "number") continue;
      const mode = control.value;
      if (mode === "fixed") continue;
      const lower = Number.isFinite(w.options?.min) ? w.options.min : 0;
      const upper = Math.min(
        Number.isFinite(w.options?.max) ? w.options.max : Number.MAX_SAFE_INTEGER,
        Number.MAX_SAFE_INTEGER);
      let next;
      if (mode === "randomize") {
        next = Math.floor(Math.random() * (upper - lower + 1)) + lower;
      } else if (mode === "increment") {
        next = Math.min(w.value + 1, upper);
      } else if (mode === "decrement") {
        next = Math.max(w.value - 1, lower);
      } else {
        continue; // unrecognised control mode; never guess, leave it untouched
      }
      if (!Number.isSafeInteger(next)) continue;
      w.value = next;
      counts[mode] = (counts[mode] || 0) + 1;
    }
  }
  return counts;
}

function logSeedAdvance(counts) {
  const parts = Object.entries(counts).filter(([, n]) => n).map(([mode, n]) => `${n} ${mode}`);
  if (!parts.length) return;
  const log = document.getElementById("sf-log");
  if (log) log.textContent += (log.textContent ? "\n" : "") + `[bridge] seeds advanced: ${parts.join(", ")}`;
}

async function onRender() {
  if (pollTimer) clearInterval(pollTimer);
  const log = document.getElementById("sf-log");
  const preview = document.getElementById("sf-preview");
  preview.style.display = "none";
  log.textContent = "";
  const sku = document.getElementById("sf-sku").value;
  if (!sku) { setStatus("Pick a GPU first; the list may still be loading.", "#ff8888"); return; }
  if (queueRunning) { setStatus("A render queue is running; wait for it to finish.", "#ff8888"); return; }
  setRenderEnabled(false);
  setQueueButtonsEnabled(false);

  try {
    // saveSettings first so the model check reads the assets path submit will use.
    await saveSettings();
    const prompt = await app.graphToPrompt();
    const workflow = prompt.output;
    logSeedAdvance(advanceSeeds());
    await modelGate([workflow], () => submitRender(workflow, sku));
  } catch (e) {
    setStatus(`Error: ${e}`, "#ff8888");
    setRenderEnabled(true);
    setQueueButtonsEnabled(true);
  }
}

async function submitRender(workflow, sku) {
  setStatus("Submitting...", "#ffd479");
  const res = await api("/submit", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ workflow, instance_type: sku }),
  });
  if (res.error) { setStatus(`Submit failed: ${res.error}`, "#ff8888"); setRenderEnabled(true); setQueueButtonsEnabled(true); return; }
  setStatus(`Submitted job ${res.jobId}. Watching...`, "#ffd479");
  pollJob(res.jobId);
}

// ---- Pre-render model sync ----------------------------------------------
// Gate every render/queue submit behind a ShareSync model check. Nothing
// uploads without explicit consent; nothing submits while models are missing.

function fmtBytes(n) {
  if (n == null) return "?";
  if (n >= 1024 ** 3) return (n / 1024 ** 3).toFixed(1) + " GB";
  if (n >= 1024 ** 2) return (n / 1024 ** 2).toFixed(1) + " MB";
  if (n >= 1024) return (n / 1024).toFixed(1) + " KB";
  return n + " B";
}

function fmtEta(secs) {
  if (!isFinite(secs) || secs < 0) return "?";
  if (secs < 60) return Math.ceil(secs) + "s";
  if (secs < 3600) return Math.ceil(secs / 60) + "m";
  return Math.floor(secs / 3600) + "h " + Math.ceil((secs % 3600) / 60) + "m";
}

function reenableSubmitButtons() {
  setRenderEnabled(!!document.getElementById("sf-sku").value);
  setQueueButtonsEnabled(true);
}

async function modelGate(workflows, proceed) {
  setStatus("Checking models on ShareSync...", "#ffd479");
  let res;
  try {
    res = await api("/models/check", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ workflows }),
    });
  } catch (e) {
    setStatus(`Model check failed: ${e}. Nothing was submitted.`, "#ff8888");
    reenableSubmitButtons();
    return;
  }
  if (res.error) {
    setStatus(`Model check failed: ${res.error}. Nothing was submitted.`, "#ff8888");
    reenableSubmitButtons();
    return;
  }
  if (res.missing && res.missing.length) {
    const names = res.missing.map((m) => `${m.folder}/${m.name}`).join(", ");
    setStatus(`Model(s) not found locally or on ShareSync: ${names}. Nothing was submitted.`, "#ff8888");
    reenableSubmitButtons();
    return;
  }
  if (res.uploading && res.uploading.length) {
    setStatus("A model upload is still in progress (see the badge under the ⚡ button). " +
              "Wait for it to finish, then render again.", "#ffd479");
    reenableSubmitButtons();
    return;
  }
  const uploadable = res.uploadable || [];
  const overLimit = res.over_limit || [];
  if (uploadable.length || overLimit.length) {
    showModelConsent(uploadable, overLimit, proceed);
    return;
  }
  await proceed();
}

function showModelConsent(uploadable, overLimit, proceed) {
  const box = document.getElementById("sf-consent");
  box.innerHTML = "";
  const hide = () => { box.style.display = "none"; box.innerHTML = ""; };
  const total = uploadable.reduce((s, m) => s + (m.size || 0), 0);
  const rowStyle = "display:flex;justify-content:space-between;gap:6px;background:#181818;" +
                   "border:1px solid #333;border-radius:4px;padding:4px 6px;font-size:12px;";

  box.append(el("strong", { textContent: "These models are not on ShareSync yet",
                            style: "font-size:13px;" }));
  const list = el("div", { style: "display:flex;flex-direction:column;gap:3px;margin:6px 0;" });
  for (const m of uploadable) {
    list.append(el("div", { style: rowStyle },
      el("span", { textContent: `${m.folder}/${m.name}`,
                   style: "overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" }),
      el("span", { textContent: fmtBytes(m.size) + (m.size_mismatch ? " (will overwrite the cloud copy)" : ""),
                   style: "flex:none;color:#ffd479;" })));
  }
  for (const m of overLimit) {
    list.append(el("div", { style: rowStyle + "border-color:#7a5a2a;" },
      el("span", { textContent:
        `${m.folder}/${m.name} — ${fmtBytes(m.size)}; exceeds the configured ` +
        `${fmtBytes(m.guard_bytes)} upload guard. Stage via the ShareSync desktop app, ` +
        `or raise upload_guard_gb in settings.`,
        style: "color:#ffaa55;white-space:normal;" })));
  }
  box.append(list);
  if (uploadable.length) {
    box.append(el("div", {
      textContent: `Total upload: ${fmtBytes(total)}. Uploads continue while ComfyUI stays open; ` +
                   "closing ComfyUI itself stops them.",
      style: "font-size:11px;opacity:0.7;margin-bottom:6px;" }));
  }

  const files = uploadable.map((m) => ({ folder: m.folder, name: m.name }));
  const buttons = el("div", { style: "display:flex;flex-direction:column;gap:6px;" });
  if (uploadable.length) {
    buttons.append(el("button", {
      textContent: `Upload ${uploadable.length} model(s) (${fmtBytes(total)}), then render`,
      style: btnStyle("#7c5cff"),
      onclick: async () => { hide(); await startModelUpload(files, proceed); },
    }));
    buttons.append(el("button", {
      textContent: "Upload for next time (render cancelled)",
      style: btnStyle("#3a3a3a"),
      onclick: async () => {
        hide();
        await startModelUpload(files, null);
        reenableSubmitButtons();
      },
    }));
  } else {
    buttons.append(el("button", {
      textContent: "Render anyway (models must be staged another way)",
      style: btnStyle("#7c5cff"),
      onclick: async () => { hide(); await proceed(); },
    }));
  }
  buttons.append(el("button", {
    textContent: "Cancel",
    style: btnStyle("#aa3333"),
    onclick: () => {
      hide();
      reenableSubmitButtons();
      setStatus("Cancelled. Nothing was uploaded or submitted.", "#ddd");
    },
  }));
  box.append(buttons);
  box.style.display = "block";
  setStatus(`${uploadable.length + overLimit.length} model(s) need attention before rendering.`, "#ffd479");
}

async function startModelUpload(files, thenRender) {
  try {
    const res = await api("/models/upload", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ files }),
    });
    if (res.error) {
      setStatus(`Upload failed to start: ${res.error}`, "#ff8888");
      reenableSubmitButtons();
      return;
    }
    activeUploadId = res.uploadId;
    setStatus(thenRender ? "Uploading models, then rendering..." :
                           "Uploading models in the background. Render was not submitted.", "#ffd479");
    pollUploads(res.uploadId, thenRender);
  } catch (e) {
    setStatus(`Upload failed to start: ${e}`, "#ff8888");
    reenableSubmitButtons();
  }
}

function pollUploads(uploadId, thenRender) {
  if (uploadPollTimer) clearInterval(uploadPollTimer);
  const samples = [];
  uploadPollTimer = setInterval(async () => {
    let st;
    try { st = await api(`/models/upload/${uploadId}`); } catch { return; }
    if (st.error && !st.status) return;
    updateUploadBadge(st, samples);
    if (["done", "failed", "cancelled"].includes(st.status)) {
      clearInterval(uploadPollTimer);
      uploadPollTimer = null;
      activeUploadId = null;
      if (st.status === "done") {
        setUploadBadge("✓ model sync complete", "#274a27");
        setTimeout(hideUploadBadge, 8000);
        if (thenRender) { await thenRender(); }
        else { setStatus("Model upload complete.", "#88ff88"); }
      } else if (st.status === "failed") {
        setUploadBadge("✕ model upload failed", "#5a2727");
        setStatus(`Model upload failed: ${st.error || "unknown error"}. ` +
                  "It will be re-offered on the next render.", "#ff8888");
        if (thenRender) reenableSubmitButtons();
      } else {
        hideUploadBadge();
        setStatus("Model upload cancelled. Nothing was submitted.", "#ffaa55");
        if (thenRender) reenableSubmitButtons();
      }
    }
  }, 2000);
}

// The badge is a fixed element under the floating ⚡ button — deliberately NOT
// inside the panel, so background-upload progress survives the panel closing.
function ensureUploadBadge() {
  if (uploadBadge) return uploadBadge;
  uploadBadge = el("div", {
    id: "spark-fuse-upload-badge",
    style: `position:fixed;top:54px;right:16px;z-index:10000;display:none;align-items:center;gap:8px;
            padding:6px 10px;border-radius:6px;background:#2a2a44;color:#fff;font-family:sans-serif;
            font-size:12px;box-shadow:0 2px 8px rgba(0,0,0,0.4);max-width:420px;`,
  },
    el("span", { id: "sf-badge-text", textContent: "",
                 style: "overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" }),
    el("span", {
      textContent: "✕", title: "Cancel upload",
      style: "cursor:pointer;opacity:0.7;flex:none;",
      onclick: async () => {
        if (!activeUploadId) return;
        try { await api(`/models/upload/${activeUploadId}/cancel`, { method: "POST" }); } catch { /* best effort */ }
      },
    }));
  document.body.append(uploadBadge);
  return uploadBadge;
}

function setUploadBadge(text, bg) {
  const badge = ensureUploadBadge();
  badge.style.display = "flex";
  if (bg) badge.style.background = bg;
  const t = document.getElementById("sf-badge-text");
  if (t) t.textContent = text;
}

function hideUploadBadge() {
  if (uploadBadge) uploadBadge.style.display = "none";
}

function updateUploadBadge(st, samples) {
  const files = st.files || [];
  const total = files.reduce((s, f) => s + (f.size || 0), 0);
  const sent = files.reduce((s, f) => s + (f.status === "done" ? (f.size || 0) : (f.sent || 0)), 0);
  const active = files.find((f) => f.status === "uploading");
  samples.push({ t: Date.now(), sent });
  while (samples.length > 15) samples.shift();
  let eta = "";
  if (samples.length >= 2) {
    const first = samples[0], last = samples[samples.length - 1];
    const rate = (last.sent - first.sent) / Math.max((last.t - first.t) / 1000, 0.001);
    if (rate > 0) eta = `, ~${fmtEta((total - sent) / rate)} left`;
  }
  const pct = total ? Math.floor((sent / total) * 100) : 0;
  const name = active ? active.name.split("/").pop() : "models";
  const text = `⬆ ${name}: ${pct}% (${fmtBytes(sent)} / ${fmtBytes(total)}${eta})`;
  setUploadBadge(text, "#2a2a44");
  // Mirror into the panel status line when it is open (harmless when hidden).
  if (document.getElementById("spark-fuse-panel")?.style.display === "block") {
    setStatus(`Model upload: ${text}`, "#ffd479");
  }
}

// After a tab reload the server-side upload keeps running; re-attach the badge.
async function reattachUploads() {
  try {
    const data = await api("/models/uploads");
    const entries = Object.entries(data.uploads || {});
    const active = entries.find(([, st]) => st.status === "queued" || st.status === "running");
    if (active) {
      activeUploadId = active[0];
      pollUploads(active[0], null);
    }
  } catch { /* best effort */ }
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
    logSeedAdvance(advanceSeeds());
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
  setRenderEnabled(false);
  setQueueButtonsEnabled(false);
  try {
    await saveSettings();
    // One consolidated check + consent for every queued workflow, before the
    // warm instance is prepared (its idle-hold clock must not run during
    // consent clicks or multi-GB uploads).
    const workflows = queueItems.map((it) => it.workflow);
    await modelGate(workflows, () => submitQueue(sku));
  } catch (e) {
    setStatus(`Error: ${e}`, "#ff8888");
    reenableSubmitButtons();
  }
}

async function submitQueue(sku) {
  setQueueRunning(true);
  setStatus(`Preparing a warm instance for ${queueItems.length} job(s)...`, "#ffd479");
  try {
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
  setStatus("Cancelling after the current batch finishes...", "#ffd479");
  try { await api(`/queue/${queueId}/cancel`, { method: "POST" }); } catch (e) { /* best effort */ }
}

app.registerExtension({
  name: "SparkFuse.Bridge",
  async setup() {
    const panel = buildPanel();
    renderQueueList();
    reattachUploads();  // a server-side upload may have outlived a tab reload
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
