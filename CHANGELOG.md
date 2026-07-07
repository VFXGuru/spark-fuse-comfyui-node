# Changelog

## 0.2.1 — 2026-07-07

Bridge-only fixes from external tester feedback. Messenger is unchanged.

### Added

- **Version indicator** in the panel title, read from the bridge's own
  `__version__` via the existing `/settings` call — no new route.
- **Render queue collapsed by default**, matching the Credentials section, so
  the single "Render on Spark Fuse" button reads as the default action.
- **Live ShareSync path preview** under the assets field, and **leading-slash
  validation before submit**: a path missing the leading `/` is rejected in
  the panel with the same message Spark Fuse's API returns
  (`assetsShareSyncPath must start with '/'`), instead of failing only after
  the render or queue has already been submitted to the cloud.

### Dropped

- **Save-state (unsaved-changes) check before submit** — investigated and
  dropped. ComfyUI_frontend tracks workflow-modified state internally
  (`ComfyWorkflow.isModified`, behind a Pinia store), but it is not reachable
  from a legacy `scripts/app.js`-based extension without depending on
  undocumented internals. Blocked on an upstream ComfyUI frontend API; no
  documented, version-stable accessor exists.

## 0.2.0 — 2026-07-02

Pre-render model sync: the bridge now detects the models a workflow references,
checks ShareSync for them by name and exact size, and (only with your explicit
consent) uploads the missing ones before rendering.

### Added

- **Model detection** for checkpoints, LoRAs, VAEs, text encoders (CLIP),
  ControlNets, CLIP Vision, upscale models and GGUF files, covering the ComfyUI
  core loaders and ComfyUI-GGUF. Local paths resolve through ComfyUI's own
  `folder_paths`, so custom model directories are honoured.
- **Three-way consent** whenever uploadable models are found: upload then render;
  upload in the background without rendering (for next time); or cancel. Nothing
  uploads and nothing submits without consent. The queue gets one consolidated
  consent covering every queued workflow, checked before the warm instance is
  prepared.
- **Persistent upload badge** under the ⚡ button: percent, bytes transferred and
  estimated time remaining, with a cancel control. It outlives the panel being
  closed and re-attaches after a browser tab reload (transfers run in the ComfyUI
  server process).
- **Upload guard** (`"upload_guard_gb"` in `spark_fuse_settings.json`, default 50):
  files above it are routed to the ShareSync desktop app instead of an in-node
  upload. A practicality limit for non-resumable transfers, not a server limit
  (ShareSync accepts up to 2 TB per file); `0` disables it.
- **Hard stop with a clear message** when a referenced model exists neither
  locally nor on ShareSync.
- Uploads land at `{assets ShareSync path}/{model folder}/{name}`, preserving
  local subfolders, so the cloud library keeps mirroring your local layout.
- One automatic retry on transport errors during an upload; a same-name,
  different-size cloud copy is flagged and overwritten on an approved upload.

### Changed

- Requires `spark-fuse-messenger >= 0.5.0` (the ShareSync stat/upload layer).
- Version records aligned (`pyproject.toml` and `spark_fuse_bridge.__version__`
  both report 0.2.0).

## 0.1.2 — 2026-06-30

- Render queue: warm-instance affinity (`preferred`/`required`) via messenger
  0.4.0 sessions; tightened the idle backstop.
- Hardened input-file staging: the workflow is scanned for LoadImage references
  before upload.

## 0.1.1 — 2026-06-29

- Comfy Registry metadata (display name, repository URL, description); Python
  floor raised to 3.12; messenger dependency switched to the PyPI package.

## 0.1.0 — 2026-06-21

- Initial release: ⚡ panel, single render with live progress and image
  round-trip, batch count, render queue on one warm instance, sequential output
  naming.
