# Changelog

## 0.3.2 — 2026-07-27

A single reliability fix for job submission. No user-facing or documented
behaviour change; the manual is unchanged. Messenger is unchanged.

### Changed

- **`maxWallClockSeconds` is now set explicitly to 3600 on every job
  submission**, both single renders and batched queue jobs, rather than
  being omitted, which on Spark Fuse means never kill. The
  container-inactivity detector set explicitly in 0.3.1 only fires when
  stdout, CPU and GPU are all quiet simultaneously, so it catches a stalled
  container but never a busy one: a workflow spinning the GPU in a loop
  would have run, and billed, indefinitely. 3600 seconds gives roughly 8x
  headroom over a warm batch of ten and about 4.6x over our worst observed
  cold single render, while capping a runaway inside one billing hour.
  Deliberately kept out of `DEFAULTS`/`PUBLIC_KEYS`, so it cannot be
  changed from the panel or a raw POST.

## 0.3.1 — 2026-07-27

Reliability fixes for the render queue and job submission. No user-facing or
documented behaviour changes; the manual is unchanged. Messenger is unchanged.

### Fixed

- **The queue's per-batch job wait no longer fails silently on timeout.**
  When the 2-hour per-batch ceiling was reached, the wait broke out with no
  log line and handed back a job that was still running, which downstream
  code treated as finished: the queue could report success while the
  instance was still running a job, and the subsequent release call would
  hit a 409 that was swallowed into a single log line. The timeout is now
  logged loudly; affected items are marked failed with a reason that
  distinguishes the bridge giving up waiting from an actual workflow
  failure; the queue stops rather than submitting further batches onto a
  handle whose state is uncertain; the still-running job is cancelled and
  confirmed terminal before the instance is released; and a release failure
  now surfaces as a queue-level error rather than a single scrollback line.

### Changed

- **`containerInactivitySeconds` is now set explicitly to 3600 on every job
  submission**, both single renders and batched queue jobs, rather than
  inheriting the platform's 1800-second default. Spark Fuse's
  container-inactivity detector fires when stdout, CPU and GPU are all
  simultaneously quiet at once, and the exposure grew once the bridge
  started batching up to 10 workflows into a single job. Deliberately kept
  out of `DEFAULTS`/`PUBLIC_KEYS`, so it cannot be changed from the panel
  or a raw POST.

## 0.3.0 — 2026-07-25

Multi-workflow render queue, backed by the already-published spark-fuse-comfyui
image's manifest mode, plus a seed-handling fix that changes visible render
behaviour. Messenger is unchanged.

### Added

- **The render queue now runs several workflows in a single job against one
  already-warm ComfyUI process**, instead of one job per workflow. Only the
  first workflow in a batch pays the cold start, model load and plugin
  initialisation; each one after that costs roughly its own sampling time.
  Measured on a queue of 3: total time fell from roughly 570s to 281s, and the
  marginal cost of each additional queued workflow fell from about 190s to
  about 25s. Long queues are split into batches of up to 10 workflows per job
  (a queue of 12 ran as two batches), all still routed onto the same prepared
  instance. Per-workflow status, output attribution and error reporting within
  a batched job are driven by structured progress markers the runner now
  prints per workflow; a failed workflow no longer stops the rest of the
  batch.

### Fixed

- **Seed `control_after_generate` (`fixed`, `increment`, `decrement`,
  `randomize`) is now honoured on both single renders and queued items.**
  This changes visible render behaviour: previously the bridge captured the
  workflow's prompt and submitted it directly, bypassing ComfyUI's own Queue
  Prompt path entirely, so `control_after_generate` never fired and every
  submission carried the same seed regardless of its setting. In practice
  this meant repeated single renders produced identical images, and, more
  seriously, queued workflows sharing one ComfyUI process could fail
  outright, because ComfyUI's execution cache treated the repeated identical
  prompt as already computed, executed it in 0.00 seconds, and wrote no
  output.

### Changed

- **Cancel queue no longer kills the running job.** It now lets the batch
  already in flight finish and download normally, so nothing already
  rendered is lost, and only skips batches that have not been submitted yet.
  The button is relabelled "Cancel queue (finishes current batch)" to say so.
- **The collapsed "Render queue" panel header is now bold and light blue**,
  so the multi-workflow queue reads as a distinct, discoverable feature
  rather than blending into the rest of the panel.

### Docs

- Rewrote manual section 6.2 (Render queue) for the batched, single-process
  queue model, and added a short note on `control_after_generate` alongside
  the batch-render section.
- Regenerated the manual PDF.

## 0.2.4 — 2026-07-22

Bridge-only discoverability and documentation pass. Messenger is unchanged.

### Changed

- **The collapsed "Render queue" panel header now reads "Render queue — queue
  several workflows to run back to back"**, so the multi-workflow queue is
  discoverable and clearly distinct from the single-workflow batch count.

### Docs

- Brought the user manual up to date with the 0.2.1 to 0.2.3 UI changes:
  settings now live in ComfyUI's user directory and survive updates and
  reinstalls; the upload guard is editable in the panel; the GPU dropdown
  shows GPU instances only, grouped by family and size; the ShareSync path is
  previewed and validated in the panel; added ComfyUI-Manager as the primary
  install method; and clarified that `session_affinity` is a separate,
  panel-unexposed setting from image affinity.
- Regenerated the manual PDF.

## 0.2.3 — 2026-07-18

Bridge-only UI fixes from partner feedback. Messenger is unchanged.

### Fixed

- **The GPU dropdown no longer lists non-GPU SKUs.** `c3.*`, `r6a.*`, `m6a.*`
  and any other SKU without a GPU are filtered out server-side, keyed on the
  `gpuType` field the API already returns; only instances that can actually
  run a render are shown.
- **GPU dropdown ordering is now sensible.** Entries are grouped by family
  (`g6.*`, `g7e.*`, ...) and ordered by size within each family, so
  `g6.xlarge` sorts back in with the rest of the `g6` family instead of
  landing wherever the API's allow-list happened to place it. Sorting is
  computed from the instance type string itself, not a hardcoded family
  list, so new families and sizes sort correctly automatically.
- **"Settings saved." is now visible.** The confirmation was already firing
  on a successful save, but the status line rendered near the bottom of the
  panel, below the fold. It now sits directly under the Save/Render button
  row, where the click happened.

## 0.2.2 — 2026-07-12

Bridge-only fixes from a partner tester report: a Manager update wiped saved
settings and left no way back in. Messenger is unchanged.

### Fixed

- **Settings now survive an uninstall or a delete-and-reinstall.** They used
  to live inside the installed node folder, which some install paths (a
  Registry install via `comfy node install`, or Manager's own Uninstall)
  remove outright. They now live under ComfyUI's per-installation user
  directory (`folder_paths.get_system_user_directory`, with a fallback for
  older ComfyUI), which no install, update or uninstall path touches. An
  existing settings file is migrated automatically on first run; the old copy
  is renamed aside as `spark_fuse_settings.json.migrated`, never deleted.

### Added

- **Upload guard is now editable in the panel**, under Credentials, defaulting
  to 50 GB. Previously it could only be changed by hand-editing the settings
  file, which testers could not find.

### Changed

- **The published archive no longer ships `scripts/` or `tests/`** (added a
  `.comfyignore`). `docs/` still ships, so the manual PDF is unaffected.

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
