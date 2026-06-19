import { app } from "../../scripts/app.js";

// Spark Fuse bridge UI: a floating button that opens a panel to configure the
// job, send the current workflow to a Spark Fuse cloud GPU, watch progress, and
// show the returned image. A floating button is used so it works regardless of
// the ComfyUI menu version; move it into the menu later if you prefer.

const api = (path, opts) => fetch(`/spark_fuse${path}`, opts).then((r) => r.json());

let pollTimer = null;

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

  const saveBtn = el("button", { textContent: "Save settings", style: btnStyle("#3a3a3a"), onclick: saveSettings });
  const renderBtn = el("button", { id: "sf-render", textContent: "Render on Spark Fuse", style: btnStyle("#7c5cff"), onclick: onRender });

  const status = el("div", { id: "sf-status", style: "font-size:12px;margin:8px 0;min-height:16px;" });
  const log = el("pre", { id: "sf-log", style: `background:#111;border:1px solid #333;border-radius:4px;padding:6px;
            height:140px;overflow:auto;font-size:11px;white-space:pre-wrap;margin:0 0 8px;` });
  const preview = el("img", { id: "sf-preview", style: "max-width:100%;border:1px solid #333;border-radius:4px;display:none;" });

  panel.append(
    title,
    field("GPU", skuSelect), rateLabel,
    field("Assets ShareSync path (models)", assetsInput),
    field("Image affinity", affinitySelect),
    el("details", {}, el("summary", { textContent: "Credentials", style: "cursor:pointer;font-size:12px;margin-bottom:6px;" }),
       field("Host", hostInput), field("Email", emailInput), field("Password", passInput)),
    el("div", { style: "display:flex;gap:8px;margin:6px 0 10px;" }, saveBtn, renderBtn),
    status, log, preview,
  );
  document.body.append(panel);
  return panel;
}

const inputStyle = () => "padding:4px;background:#2a2a2a;color:#eee;border:1px solid #555;border-radius:3px;";
const btnStyle = (bg) => `flex:1;padding:8px;border:none;border-radius:4px;background:${bg};color:#fff;cursor:pointer;font-size:13px;`;

function setStatus(text, color = "#ddd") {
  const s = document.getElementById("sf-status");
  if (s) { s.textContent = text; s.style.color = color; }
}

async function loadSettings() {
  try {
    const s = await api("/settings");
    document.getElementById("sf-assets").value = s.assets_share_sync_path || "";
    document.getElementById("sf-affinity").value = s.image_affinity || "preferred";
    document.getElementById("sf-host").value = s.host || "";
    document.getElementById("sf-email").value = s.email || "";
    if (s.password_set) document.getElementById("sf-pass").placeholder = "(stored — leave blank to keep)";
  } catch (e) { setStatus(`Could not load settings: ${e}`, "#ff8888"); }
}

async function loadSkus() {
  const select = document.getElementById("sf-sku");
  try {
    const data = await api("/skus");
    if (data.error) { setStatus(`Could not list GPUs: ${data.error}`, "#ff8888"); return; }
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
  } catch (e) { setStatus(`Could not list GPUs: ${e}`, "#ff8888"); }
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
  renderBtn.disabled = true;
  setStatus("Exporting workflow and submitting...", "#ffd479");

  try {
    await saveSettings();
    const prompt = await app.graphToPrompt();
    const res = await api("/submit", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ workflow: prompt.output, instance_type: document.getElementById("sf-sku").value }),
    });
    if (res.error) { setStatus(`Submit failed: ${res.error}`, "#ff8888"); renderBtn.disabled = false; return; }
    setStatus(`Submitted job ${res.jobId}. Watching...`, "#ffd479");
    pollJob(res.jobId);
  } catch (e) {
    setStatus(`Error: ${e}`, "#ff8888");
    renderBtn.disabled = false;
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
      renderBtn.disabled = false;
      if (st.status === "failed" && st.error) setStatus(`Failed: ${st.error}`, "#ff8888");
    }
  }, 2500);
}

app.registerExtension({
  name: "SparkFuse.Bridge",
  async setup() {
    const panel = buildPanel();
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
