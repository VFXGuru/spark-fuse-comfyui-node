"""Pre-render model sync: detect the models a workflow references, check ShareSync
by name+size, and upload missing ones after explicit consent.

Detection is the sibling of jobs.INPUT_FILE_NODES staging: a class_type -> field ->
model-folder map walked over API-format workflows, with local paths resolved via
ComfyUI's folder_paths. The ShareSync check (PROPFIND) and the upload transfer live
in the messenger (spark_fuse); this module owns the ComfyUI-specific parts and the
in-memory uploads registry the HTTP routes poll — the same pattern as jobs._JOBS.
"""
from __future__ import annotations

import logging
import threading
import time
import traceback
import uuid
from pathlib import Path
from typing import NamedTuple

import httpx

from spark_fuse.sharesync import DEFAULT_MAX_PUT_BYTES

from . import jobs
from .config import load_settings, make_client

_log = logging.getLogger(__name__)


class UploadCancelled(Exception):
    """Raised from inside the progress callback when a cancel is requested; it
    propagates through spark_fuse's upload_file and aborts the transfer."""


# Folder keys the cloud image registers under /assets — a hardcoded copy of
# MODEL_SUBDIRS in the runner (spark-fuse-comfyui repo, runner/spark_fuse_run.py).
# The two lists are maintained by hand in their two repos; keep them identical.
# test_model_sync.py asserts every folder in MODEL_FILE_NODES stays within this
# list, so a map entry can never point at a folder the cloud never searches.
CLOUD_MODEL_SUBDIRS = [
    "checkpoints", "diffusion_models", "unet", "text_encoders", "clip",
    "clip_vision", "vae", "loras", "controlnet", "upscale_models",
    "embeddings", "configs",
]

# class_type -> {input_field: folder_paths key}. Extend with one-line entries.
# Field names and folder keys verified against the installed ComfyUI core
# (nodes.py / comfy_extras) and ComfyUI-GGUF 1.1.10 on 2026-07-02. The GGUF
# loaders resolve through the legacy keys "unet"/"clip", which ComfyUI's
# folder_paths.map_legacy folds into "diffusion_models"/"text_encoders" — the
# canonical keys used here, so uploads land in the canonical subfolders
# (matching the live share layout) while local resolution still finds files
# under models/unet or models/clip.
MODEL_FILE_NODES: dict[str, dict[str, str]] = {
    "CheckpointLoaderSimple":  {"ckpt_name": "checkpoints"},
    "CheckpointLoader":        {"ckpt_name": "checkpoints"},
    "UNETLoader":              {"unet_name": "diffusion_models"},
    "VAELoader":               {"vae_name": "vae"},
    "LoraLoader":              {"lora_name": "loras"},
    "LoraLoaderModelOnly":     {"lora_name": "loras"},
    "CLIPLoader":              {"clip_name": "text_encoders"},
    "DualCLIPLoader":          {"clip_name1": "text_encoders", "clip_name2": "text_encoders"},
    "TripleCLIPLoader":        {"clip_name1": "text_encoders", "clip_name2": "text_encoders",
                                "clip_name3": "text_encoders"},
    "QuadrupleCLIPLoader":     {"clip_name1": "text_encoders", "clip_name2": "text_encoders",
                                "clip_name3": "text_encoders", "clip_name4": "text_encoders"},
    "ControlNetLoader":        {"control_net_name": "controlnet"},
    "DiffControlNetLoader":    {"control_net_name": "controlnet"},
    "CLIPVisionLoader":        {"clip_name": "clip_vision"},
    "UpscaleModelLoader":      {"model_name": "upscale_models"},
    "UnetLoaderGGUF":          {"unet_name": "diffusion_models"},
    "UnetLoaderGGUFAdvanced":  {"unet_name": "diffusion_models"},
    "CLIPLoaderGGUF":          {"clip_name": "text_encoders"},
    "DualCLIPLoaderGGUF":      {"clip_name1": "text_encoders", "clip_name2": "text_encoders"},
    "TripleCLIPLoaderGGUF":    {"clip_name1": "text_encoders", "clip_name2": "text_encoders",
                                "clip_name3": "text_encoders"},
    "QuadrupleCLIPLoaderGGUF": {"clip_name1": "text_encoders", "clip_name2": "text_encoders",
                                "clip_name3": "text_encoders", "clip_name4": "text_encoders"},
}


class _ModelRef(NamedTuple):
    node_id: str
    class_type: str
    field: str
    folder: str
    name: str  # forward-slash relative name, as the cloud workflow will reference it


def _collect_model_refs(workflow: dict) -> list[_ModelRef]:
    """Pure: scan an API-format workflow for model references; dedupe on
    (folder, name). Non-string values (node links such as ["12", 0]) are skipped,
    as are values without a model file extension (VAELoader's 'pixel_space' /
    taesd entries and other non-file combo options). No filesystem access."""
    seen: set[tuple[str, str]] = set()
    refs: list[_ModelRef] = []
    for node_id, node in workflow.items():
        if not isinstance(node, dict):
            continue
        class_type = node.get("class_type")
        fields = MODEL_FILE_NODES.get(class_type)
        if not fields:
            continue
        inputs = node.get("inputs")
        if not isinstance(inputs, dict):
            continue
        for field, folder in fields.items():
            value = inputs.get(field)
            if not isinstance(value, str):
                continue
            if not value.lower().endswith(jobs._MODEL_EXTS):
                continue
            name = value.replace("\\", "/").strip("/")
            key = (folder, name)
            if key in seen:
                continue
            seen.add(key)
            refs.append(_ModelRef(node_id, class_type, field, folder, name))
    return refs


def _warn_unknown_model_fields(workflow: dict) -> None:
    """Log-only heuristic: a string input that looks like a model file on a node
    the map does not cover. Never blocks — custom loaders are too varied for a
    hard gate; the cloud-side validation error remains the backstop."""
    for node_id, node in workflow.items():
        if not isinstance(node, dict):
            continue
        class_type = node.get("class_type")
        if class_type in MODEL_FILE_NODES or class_type in jobs.INPUT_FILE_NODES:
            continue
        inputs = node.get("inputs")
        if not isinstance(inputs, dict):
            continue
        for field, value in inputs.items():
            if isinstance(value, str) and value.lower().endswith(jobs._MODEL_EXTS):
                _log.warning(
                    "node %s (%s) field %r references %r but %s is not in "
                    "MODEL_FILE_NODES; it will not be checked or synced",
                    node_id, class_type, field, value, class_type,
                )


def _resolve_local(ref: _ModelRef) -> Path | None:
    """Resolve a model reference against the local install via folder_paths
    (runtime import — ComfyUI-provided, like jobs._stage_input_files)."""
    import folder_paths  # ComfyUI-provided; available at runtime
    try:
        path = folder_paths.get_full_path(ref.folder, ref.name)
    except Exception:  # noqa: BLE001 - an unknown folder key must not sink the check
        _log.warning("folder_paths could not resolve %s/%s", ref.folder, ref.name)
        return None
    return Path(path) if path else None


def _upload_guard_bytes(settings: dict) -> int | None:
    """The in-node single-upload guard in bytes, from the upload_guard_gb setting.

    A practicality guard against huge non-resumable transfers, not a server limit
    (ShareSync allows 2 TB per file). <= 0 disables the guard entirely."""
    raw = settings.get("upload_guard_gb")
    try:
        gb = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_MAX_PUT_BYTES
    if gb <= 0:
        return None
    return int(gb * 1024**3)


def check_models(workflows: list[dict], settings: dict | None = None) -> dict:
    """Classify every model referenced by *workflows* against ShareSync.

    Returns {"present", "uploadable", "over_limit", "missing", "uploading"} —
    lists of dicts the UI renders directly. Present = name exists at the expected
    remote path with a matching size (name alone suffices when there is no local
    copy to compare). A name match with a size mismatch is uploadable with
    size_mismatch=True (overwrite on upload). Files currently in flight from an
    earlier consent land in "uploading" so no second PUT is ever offered.
    """
    settings = settings or load_settings()
    refs: list[_ModelRef] = []
    seen: set[tuple[str, str]] = set()
    for wf in workflows:
        # Same normalization the submit path applies, so the checked remote path
        # is exactly what the cloud workflow will request.
        jobs._normalize_model_paths(wf)
        _warn_unknown_model_fields(wf)
        for ref in _collect_model_refs(wf):
            key = (ref.folder, ref.name)
            if key not in seen:
                seen.add(key)
                refs.append(ref)

    result: dict[str, list] = {
        "present": [], "uploadable": [], "over_limit": [], "missing": [],
        "uploading": [],
    }
    if not refs:
        return result

    assets_path = (settings.get("assets_share_sync_path") or "").rstrip("/")
    space = settings.get("assets_share_sync_space_name") or None
    guard = _upload_guard_bytes(settings)
    in_flight = {(f["folder"], f["name"]) for f in _active_upload_files()}

    client = make_client(settings)
    try:
        client.login()
        base = client.sharesync_dav_base(space)
        for ref in refs:
            remote_rel = f"{assets_path}/{ref.folder}/{ref.name}"
            local = _resolve_local(ref)
            local_size = local.stat().st_size if local else None
            item = {
                "node_id": ref.node_id, "class_type": ref.class_type,
                "field": ref.field, "folder": ref.folder, "name": ref.name,
                "remote_rel": remote_rel, "size": local_size,
            }
            if (ref.folder, ref.name) in in_flight:
                result["uploading"].append(item)
                continue
            entry = client.sharesync_stat(base, remote_rel)
            remote_size = entry.content_length if entry else None
            if entry is not None and (local_size is None or remote_size == local_size):
                item["size"] = remote_size if remote_size is not None else local_size
                result["present"].append(item)
            elif local is None:
                result["missing"].append(item)
            elif guard is not None and local_size > guard:
                item["guard_bytes"] = guard
                result["over_limit"].append(item)
            else:
                item["size_mismatch"] = entry is not None
                result["uploadable"].append(item)
    finally:
        jobs._close(client)
    return result


# ------------------------------------------------------------------
# Uploads registry — same shape as jobs._JOBS (dict + lock + daemon thread)
# ------------------------------------------------------------------

_UPLOADS: dict[str, dict] = {}
_UPLOAD_LOCK = threading.Lock()
_MAX_TRANSPORT_RETRIES = 1  # one automatic whole-file retry on transport error


def _set_upload(upload_id: str, **fields) -> None:
    with _UPLOAD_LOCK:
        state = _UPLOADS.get(upload_id)
        if state:
            state.update(fields)


def _set_file(upload_id: str, index: int, **fields) -> None:
    with _UPLOAD_LOCK:
        state = _UPLOADS.get(upload_id)
        if state:
            state["files"][index].update(fields)


def _upload_cancelled(upload_id: str) -> bool:
    with _UPLOAD_LOCK:
        state = _UPLOADS.get(upload_id)
        return bool(state and state.get("cancel"))


def get_upload_state(upload_id: str) -> dict | None:
    with _UPLOAD_LOCK:
        state = _UPLOADS.get(upload_id)
        if state is None:
            return None
        snapshot = dict(state)
        snapshot["files"] = [dict(f) for f in state["files"]]
        return snapshot


def list_uploads() -> dict[str, dict]:
    """All registry entries, keyed by id — lets a reloaded browser tab re-attach
    its progress badge to an upload that is still running server-side."""
    with _UPLOAD_LOCK:
        return {
            uid: {**state, "files": [dict(f) for f in state["files"]]}
            for uid, state in _UPLOADS.items()
        }


def cancel_upload(upload_id: str) -> None:
    _set_upload(upload_id, cancel=True)
    _log.info("upload %s: cancel requested", upload_id)


def _active_upload_files() -> list[dict]:
    """Files currently queued or transferring across all active uploads."""
    with _UPLOAD_LOCK:
        out: list[dict] = []
        for state in _UPLOADS.values():
            if state.get("status") in ("queued", "running"):
                out.extend(dict(f) for f in state["files"]
                           if f["status"] in ("queued", "uploading"))
        return out


def prepare_upload_files(files: list[dict], settings: dict | None = None) -> list[dict]:
    """Re-derive each requested {folder, name} server-side: local path via
    folder_paths, size, and the ShareSync-relative destination. The browser only
    nominates folder+name from a check result; it never dictates local paths.
    Raises ValueError when a nominated file cannot be resolved locally."""
    settings = settings or load_settings()
    assets_path = (settings.get("assets_share_sync_path") or "").rstrip("/")
    prepared: list[dict] = []
    for f in files:
        folder = str(f.get("folder") or "")
        name = str(f.get("name") or "").replace("\\", "/").strip("/")
        if folder not in CLOUD_MODEL_SUBDIRS or not name or ".." in name.split("/"):
            raise ValueError(f"Invalid upload request entry: {f!r}")
        ref = _ModelRef("", "", "", folder, name)
        local = _resolve_local(ref)
        if local is None or not local.is_file():
            raise ValueError(f"{folder}/{name} not found locally; re-run the model check")
        prepared.append({
            "folder": folder,
            "name": name,
            "local_path": str(local),
            "remote_rel": f"{assets_path}/{folder}/{name}",
            "size": local.stat().st_size,
        })
    return prepared


def _register_upload(files: list[dict]) -> str:
    upload_id = uuid.uuid4().hex[:12]
    state = {
        "status": "queued",
        "started_at": time.time(),
        "error": None,
        "cancel": False,
        "files": [{
            "folder": f["folder"], "name": f["name"], "local_path": f["local_path"],
            "remote_rel": f["remote_rel"], "size": f["size"],
            "sent": 0, "status": "queued", "error": None,
        } for f in files],
    }
    with _UPLOAD_LOCK:
        _UPLOADS[upload_id] = state
    return upload_id


def start_upload(files: list[dict], settings: dict | None = None) -> str:
    """Start uploading *prepared* files (see prepare_upload_files) in a daemon
    thread; returns the registry id the routes poll. The thread lives in the
    ComfyUI server process, so it survives the browser tab closing."""
    settings = settings or load_settings()
    upload_id = _register_upload(files)
    threading.Thread(target=_run_upload, args=(upload_id, settings), daemon=True).start()
    return upload_id


def _put_file(client, base: str, f: dict, progress) -> None:
    """One file, with one automatic whole-file retry on transport error (PUT
    overwrite is idempotent, so the retry simply restarts from zero).
    UploadCancelled is never retried."""
    for attempt in range(_MAX_TRANSPORT_RETRIES + 1):
        try:
            client.upload_file(
                Path(f["local_path"]), base, f["remote_rel"],
                progress=progress, max_bytes=None,  # guard already applied at check time
            )
            return
        except UploadCancelled:
            raise
        except httpx.TransportError as exc:
            if attempt >= _MAX_TRANSPORT_RETRIES:
                raise
            _log.warning("upload of %s hit a transport error (%s); retrying once",
                         f["name"], exc)


def _run_upload(upload_id: str, settings: dict) -> None:
    client = make_client(settings)
    try:
        client.login()
        base = client.sharesync_dav_base(settings.get("assets_share_sync_space_name") or None)
        _set_upload(upload_id, status="running")
        state = get_upload_state(upload_id)
        cancelled = False
        for i, f in enumerate(state["files"]):
            if _upload_cancelled(upload_id):
                cancelled = True
                _set_file(upload_id, i, status="cancelled")
                continue
            _set_file(upload_id, i, status="uploading")

            def progress(sent: int, total: int, _i: int = i) -> None:
                if _upload_cancelled(upload_id):
                    raise UploadCancelled()
                _set_file(upload_id, _i, sent=sent, size=total)

            try:
                _put_file(client, base, f, progress)
            except UploadCancelled:
                cancelled = True
                _set_file(upload_id, i, status="cancelled")
                _log.info("upload %s: %s cancelled mid-transfer", upload_id, f["name"])
                continue
            except Exception as exc:  # noqa: BLE001 - surface in the registry, keep going is wrong here
                _set_file(upload_id, i, status="failed", error=str(exc))
                _set_upload(upload_id, status="failed", error=f"{f['name']}: {exc}")
                _log.warning("upload %s: %s failed: %s", upload_id, f["name"], exc)
                return
            _set_file(upload_id, i, status="done", sent=f["size"])
            _log.info("upload %s: %s done (%d bytes)", upload_id, f["name"], f["size"])
        _set_upload(upload_id, status="cancelled" if cancelled else "done")
    except Exception as exc:  # noqa: BLE001 - any failure should surface in the UI
        _set_upload(upload_id, status="failed", error=str(exc))
        traceback.print_exc()
    finally:
        jobs._close(client)
