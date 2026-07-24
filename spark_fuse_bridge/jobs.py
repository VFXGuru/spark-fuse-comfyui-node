"""Job orchestration: submit a workflow, stream progress, download the result.

The long-lived stream/poll/download runs in a daemon thread, recording state in an
in-memory registry the HTTP routes read from. This mirrors Spark Fuse's recommended
pattern: stream the log for progress, and on terminal `succeeded` the output path is
ready to list and download (no separate "output ready" signal).
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import tempfile
import threading
import time
import traceback
from pathlib import Path
from typing import NamedTuple

from spark_fuse.errors import ShareSyncError
from spark_fuse.models import LogEvent, QueueStatusEvent

from .config import load_settings, make_client

# job_id -> {status, lines[], image, error, image_cache_hit, image_affinity, exit_code}
_JOBS: dict[str, dict] = {}
_LOCK = threading.Lock()
_MAX_LINES = 400
_log = logging.getLogger(__name__)


def _update(job_id: str, **fields) -> None:
    with _LOCK:
        _JOBS.setdefault(job_id, {"lines": []}).update(fields)


def _append_line(job_id: str, line: str) -> None:
    with _LOCK:
        state = _JOBS.setdefault(job_id, {"lines": []})
        state["lines"].append(line)
        if len(state["lines"]) > _MAX_LINES:
            state["lines"] = state["lines"][-_MAX_LINES:]


def get_job_state(job_id: str) -> dict | None:
    with _LOCK:
        state = _JOBS.get(job_id)
        if state is None:
            return None
        snapshot = dict(state)
        snapshot["lines"] = list(state["lines"][-200:])
        return snapshot


_MODEL_EXTS = (".safetensors", ".ckpt", ".pt", ".pth", ".bin", ".gguf", ".sft", ".onnx")


def _normalize_model_paths(prompt: dict) -> None:
    """Convert Windows backslashes to forward slashes in model-path inputs.

    ComfyUI on Windows records sub-foldered model names with backslashes (e.g.
    'FLUX2\\model.safetensors'); the cloud runs Linux, where a backslash is a
    literal character, not a path separator. Rewrite those in place so a
    Windows-authored workflow resolves against the Linux /assets mount. The
    assets library must still mirror your local model folder layout.
    """
    if not isinstance(prompt, dict):
        return
    for node in prompt.values():
        inputs = node.get("inputs") if isinstance(node, dict) else None
        if not isinstance(inputs, dict):
            continue
        for key, value in inputs.items():
            if isinstance(value, str) and "\\" in value and value.lower().endswith(_MODEL_EXTS):
                inputs[key] = value.replace("\\", "/")


def _batch_count(settings: dict) -> int:
    try:
        return max(1, min(int(settings.get("batch_count") or 1), 100))
    except (TypeError, ValueError):
        return 1


def _build_env(settings: dict, batch_count: int) -> dict:
    return {
        "MODEL_BASE_DIR": settings["model_base_dir"],
        "BATCH_COUNT": str(batch_count),
    }


def _build_chunk_env(settings: dict) -> dict:
    """Env for a manifest-mode (queue chunk) job. batch_count travels per entry
    inside the manifest itself; the runner's manifest branch never reads
    BATCH_COUNT (spark_fuse_run.py's run_manifest() takes it from the parsed
    JSON, only the single-workflow fallback path reads the env var), so it is
    intentionally omitted here rather than sent as a value nothing will honour.
    """
    return {"MODEL_BASE_DIR": settings["model_base_dir"]}


# Add video/audio loaders here as one-line entries, e.g. "VHS_LoadVideo": ["video"].
INPUT_FILE_NODES: dict[str, list[str]] = {
    "LoadImage":     ["image"],
    "LoadImageMask": ["image"],
}


class _StagedFile(NamedTuple):
    node_id: str
    class_type: str
    field: str
    src: Path
    dest_rel: str


def _collect_input_files(workflow: dict, input_dir: Path) -> list[_StagedFile]:
    """Pure: scan workflow for input-file references; return planned copies.

    Checks each node whose class_type is in INPUT_FILE_NODES, reads the listed
    widget fields, and builds a _StagedFile record per string value. Non-string
    values (node links such as ["12", 0]) are silently skipped. Values annotated
    [output]/[temp] are skipped with a warning. Subfolder paths are preserved.
    Deduped on dest_rel. No filesystem access — fully unit-testable.
    """
    seen: set[str] = set()
    result: list[_StagedFile] = []
    for node_id, node in workflow.items():
        if not isinstance(node, dict):
            continue
        class_type = node.get("class_type")
        fields = INPUT_FILE_NODES.get(class_type)
        if not fields:
            continue
        inputs = node.get("inputs")
        if not isinstance(inputs, dict):
            continue
        for field in fields:
            value = inputs.get(field)
            if not isinstance(value, str):
                continue
            if value.endswith(" [output]") or value.endswith(" [temp]"):
                _log.warning(
                    "node %s (%s) field %r: %r has an [output]/[temp] annotation; skipping",
                    node_id, class_type, field, value,
                )
                continue
            filename = value[: -len(" [input]")] if value.endswith(" [input]") else value
            filename = filename.replace("\\", "/")
            if filename in seen:
                continue
            seen.add(filename)
            result.append(_StagedFile(
                node_id=node_id,
                class_type=class_type,
                field=field,
                src=input_dir / filename,
                dest_rel=filename,
            ))
    return result


def _stage_input_files(workflow: dict, staging_dir: Path) -> None:
    """Copy each planned input file into staging_dir, mirroring its relative path.

    Warns and skips files that do not exist locally; never raises. Calls
    folder_paths at runtime (ComfyUI-provided; not importable at module load time).
    """
    import folder_paths  # ComfyUI-provided; available at runtime
    planned = _collect_input_files(workflow, Path(folder_paths.get_input_directory()))
    staged = 0
    for sf in planned:
        if not sf.src.exists():
            _log.warning(
                "node %s (%s) field %r: %r not found locally; skipping",
                sf.node_id, sf.class_type, sf.field, str(sf.src),
            )
            continue
        dest = staging_dir / sf.dest_rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(sf.src, dest)
        _log.info(
            "staged node %s (%s) %s -> %s",
            sf.node_id, sf.class_type, sf.src, sf.dest_rel,
        )
        staged += 1
    _log.info("staged %d input file(s)", staged)


def _stage_chunk_input_files(entries: list[dict], staging_dir: Path, log=None) -> None:
    """Merge every entry's referenced input files into one staging pass for a
    queue chunk's single shared /input mount.

    A dest_rel referenced by more than one workflow in the chunk collapses to
    one copy: ComfyUI has one input directory (not one per workflow), so the
    same filename can only resolve to one physical file at the single moment
    this whole chunk is staged — restaging it twice would just copy the same
    source over itself. That said, this does remove a property the old
    one-job-per-item queue had: each item used to stage lazily, right before
    its own submission, so swapping a same-named input file between queue
    items (deliberately or not) would land the *new* file on the *later*
    item. Staging a whole chunk up front means every workflow in it now sees
    whatever was on disk at chunk-submit time, whichever one referenced it
    first or last. Not treated as an error — reusing one input image (e.g. a
    ControlNet pose) across every item in a batch is a normal, desirable
    pattern this cannot tell apart from an accidental collision — but always
    logged so it is visible.
    """
    if log is None:
        log = lambda _m: None  # noqa: E731
    import folder_paths  # ComfyUI-provided; available at runtime
    input_dir = Path(folder_paths.get_input_directory())

    planned: dict[str, _StagedFile] = {}
    referenced_by: dict[str, list[int]] = {}
    for idx, entry in enumerate(entries, start=1):
        for sf in _collect_input_files(entry["workflow"], input_dir):
            planned[sf.dest_rel] = sf
            referenced_by.setdefault(sf.dest_rel, []).append(idx)

    for dest_rel, item_numbers in referenced_by.items():
        if len(item_numbers) > 1:
            positions = ", ".join(str(n) for n in item_numbers)
            log(f"[bridge] note: {len(item_numbers)} workflows in this batch "
                f"(items {positions}) share input file {dest_rel!r}; each gets "
                "the same content")

    staged = 0
    for sf in planned.values():
        if not sf.src.exists():
            _log.warning(
                "node %s (%s) field %r: %r not found locally; skipping",
                sf.node_id, sf.class_type, sf.field, str(sf.src),
            )
            continue
        dest = staging_dir / sf.dest_rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(sf.src, dest)
        staged += 1
    _log.info("staged %d input file(s) for %d-workflow batch", staged, len(entries))


def _submit_job(client, api_prompt: dict, *, instance_type, settings, batch_count,
                instance_handle: str | None = None):
    """Submit one workflow job and stage its workflow.json; return CreateJobResponse.

    Shared by the single-render path and the render queue. The tiny workflow.json
    is pushed via auto-prepare; models come from the lazy read-only /assets mount;
    image affinity steers placement. Pass instance_handle to route the job onto a
    prepared warm session (§13).
    """
    resp = client.submit(
        image=settings["image"],
        command=settings["command"],
        instance_type=instance_type or settings["instance_type"],
        env=_build_env(settings, batch_count),
        input_push_mode="auto-prepare",
        assets_share_sync_path=settings.get("assets_share_sync_path") or None,
        assets_share_sync_space_name=settings.get("assets_share_sync_space_name") or None,
        image_affinity=settings.get("image_affinity") or None,
        instance_handle=instance_handle,
    )
    if not (resp.input and resp.input.upload_url):
        raise RuntimeError("No auto-prepare upload URL returned by Spark Fuse.")
    # Stage the workflow as /input/workflow.json via the one-shot upload URL.
    tmp = Path(tempfile.mkdtemp(prefix="spark-fuse-wf-"))
    (tmp / "workflow.json").write_text(json.dumps(api_prompt), encoding="utf-8")
    _stage_input_files(api_prompt, tmp)
    client.upload_input(tmp, resp.input.upload_url)
    return resp


def _submit_chunk_job(client, entries: list[dict], *, instance_type, settings,
                      instance_handle: str, log=None):
    """Submit one manifest-mode job covering up to queue_chunk_size() workflows,
    routed onto the prepared session via instance_handle. Mirrors _submit_job's
    auto-prepare push, but writes /input/spark_fuse_job.json (the runner's
    multi-workflow contract, already live in the published image) instead of a
    single workflow.json, and stages every entry's referenced input files in
    one merged pass (see _stage_chunk_input_files) rather than one per job.

    entries: [{"workflow": api-format graph, "batch_count": int}, ...], already
    normalised (jobs._normalize_model_paths applied) by the caller.
    """
    resp = client.submit(
        image=settings["image"],
        command=settings["command"],
        instance_type=instance_type or settings["instance_type"],
        env=_build_chunk_env(settings),
        input_push_mode="auto-prepare",
        assets_share_sync_path=settings.get("assets_share_sync_path") or None,
        assets_share_sync_space_name=settings.get("assets_share_sync_space_name") or None,
        image_affinity=settings.get("image_affinity") or None,
        instance_handle=instance_handle,
    )
    if not (resp.input and resp.input.upload_url):
        raise RuntimeError("No auto-prepare upload URL returned by Spark Fuse.")
    manifest = {"workflows": [
        {"workflow": e["workflow"], "batch_count": e["batch_count"]} for e in entries
    ]}
    tmp = Path(tempfile.mkdtemp(prefix="spark-fuse-chunk-"))
    (tmp / "spark_fuse_job.json").write_text(json.dumps(manifest), encoding="utf-8")
    _stage_chunk_input_files(entries, tmp, log=log)
    client.upload_input(tmp, resp.input.upload_url)
    return resp


def submit_workflow(api_prompt: dict, instance_type: str | None = None) -> str:
    """Submit an API-format workflow to Spark Fuse and return the job id.

    Models come from the read-only /assets mount; the small workflow.json is pushed
    via the auto-prepare upload URL. Progress is then tracked in a background thread.
    """
    _normalize_model_paths(api_prompt)
    settings = load_settings()
    client = make_client(settings)
    client.login()

    try:
        resp = _submit_job(client, api_prompt, instance_type=instance_type,
                           settings=settings, batch_count=_batch_count(settings))
    except Exception:
        _close(client)
        raise
    job_id = resp.job_id
    _update(job_id, status=resp.status, image=None, error=None)

    threading.Thread(target=_watch, args=(client, job_id), daemon=True).start()
    return job_id


def _extract_validation_error(lines: list[str]) -> str | None:
    """Pull a human-readable reason out of a ComfyUI prompt-validation rejection.

    The runner prints the raw /prompt 400 body, which carries node_errors with the
    offending input and value (for example a model name not present on /assets).
    """
    for line in lines:
        s = line.strip()
        if '"node_errors"' not in s:
            continue
        try:
            data = json.loads(s)
        except ValueError:
            continue
        msgs = []
        for node_id, info in (data.get("node_errors") or {}).items():
            ctype = info.get("class_type", "node")
            for err in info.get("errors", []):
                detail = err.get("details") or err.get("message") or "invalid"
                if err.get("type") == "value_not_in_list":
                    detail += (" — not found on the cloud. Make sure this model exists "
                               "under /assets at that path; your ShareSync assets must "
                               "mirror your local model folders.")
                msgs.append(f"{ctype} {node_id}: {detail}")
        if msgs:
            return " | ".join(msgs)
    return None


def _unique_output_name(out_dir: Path, src_name: str) -> str:
    """Return a filename that does not exist in out_dir, mimicking ComfyUI's
    sequential numbering. The cloud always names its file '<prefix>_00001_.png';
    pick the next free '<prefix>_NNNNN_.png' so renders accumulate locally."""
    m = re.match(r"^(?P<prefix>.+?)_(?P<num>\d+)_\.(?P<ext>\w+)$", src_name)
    if m:
        prefix, ext = m.group("prefix"), m.group("ext")
        rx = re.compile(rf"^{re.escape(prefix)}_(\d+)_\.{re.escape(ext)}$")
        nums = [int(rx.match(p.name).group(1)) for p in out_dir.iterdir()
                if p.is_file() and rx.match(p.name)]
        nxt = (max(nums) + 1) if nums else 1
        return f"{prefix}_{nxt:05d}_.{ext}"
    stem, suffix = Path(src_name).stem, Path(src_name).suffix
    candidate, i = src_name, 1
    while (out_dir / candidate).exists():
        candidate = f"{stem}_{i}{suffix}"
        i += 1
    return candidate


def _download_images(client, job, log=None) -> list[str]:
    """Download a succeeded job's images into ComfyUI's output dir under fresh
    sequential names; return the saved filenames. Shared by the single-render
    watcher and the render queue. The output URL can lag terminal, so retry briefly."""
    if log is None:
        log = lambda _m: None  # noqa: E731
    base_url = job.output.share_sync_base_url if job.output else None
    for _ in range(6):
        if base_url:
            break
        time.sleep(2)
        job = client.get_job(job.id)
        base_url = job.output.share_sync_base_url if job.output else None
    if not base_url:
        log("[bridge] job succeeded but no output path was returned after retries")
        return []
    import folder_paths  # ComfyUI-provided; available at runtime
    out_dir = Path(folder_paths.get_output_directory())
    out_dir.mkdir(parents=True, exist_ok=True)
    log(f"[bridge] downloading outputs from {base_url}")
    # Download to a temp dir, then move images into the output folder under the next
    # free sequential name so repeated renders accumulate (the cloud names _00001_).
    dl_dir = Path(tempfile.mkdtemp(prefix="spark-fuse-out-"))
    try:
        paths = client.download_outputs(base_url, dl_dir)
        images = [p for p in paths
                  if str(p).lower().endswith((".png", ".jpg", ".jpeg", ".webp"))]
        saved = []
        for p in images:
            name = _unique_output_name(out_dir, p.name)
            shutil.move(str(p), str(out_dir / name))
            saved.append(name)
    finally:
        shutil.rmtree(dl_dir, ignore_errors=True)
    if saved:
        log(f"[bridge] saved {len(saved)} image(s) to ComfyUI output: {', '.join(saved)}")
    else:
        log(f"[bridge] succeeded but found no image in {len(paths)} output file(s)")
    return saved


def _resolve_chunk_output_base_url(client, job, log=None) -> str | None:
    """Poll briefly for a chunk job's output.share_sync_base_url to populate (it
    can lag terminal by a few seconds — same lag _download_images already
    retries for on the single-render path). Kept separate from that function,
    rather than shared, so the single-render path is never touched by anything
    queue-related.
    """
    if log is None:
        log = lambda _m: None  # noqa: E731
    base_url = job.output.share_sync_base_url if job.output else None
    for _ in range(6):
        if base_url:
            return base_url
        time.sleep(2)
        job = client.get_job(job.id)
        base_url = job.output.share_sync_base_url if job.output else None
    log("[bridge] batch finished but no output path was returned after retries")
    return None


def _download_chunk_item_images(client, job, base_url: str, local_idx: int, log=None) -> list[str]:
    """Download one manifest entry's outputs from the job's /output/wf_{local_idx:02d}/
    subfolder, moving them into ComfyUI's output dir under fresh sequential names
    (same _unique_output_name convention as the single-render/fallback path).

    Uses the existing, unmodified client.download_outputs(), pointed directly at
    the subfolder's URL instead of the job's output root — that single existing
    method already recurses into subfolders and handles same-basename collisions,
    it just flattens everything by basename with no per-subfolder attribution, so
    calling it once per wf_NN/ (rather than once on the job root) is what gives
    per-item isolation, with no messenger changes needed.

    Returns [], not an error, when the subfolder was never created — an entry
    that failed with zero output does not get an empty wf_NN/ folder (see the
    runner's run_manifest()), so PROPFINDing it 404s; that is expected here,
    not a failure of the download step itself.
    """
    if log is None:
        log = lambda _m: None  # noqa: E731
    subfolder_url = f"{base_url.rstrip('/')}/wf_{local_idx:02d}/"
    import folder_paths  # ComfyUI-provided; available at runtime
    out_dir = Path(folder_paths.get_output_directory())
    out_dir.mkdir(parents=True, exist_ok=True)
    dl_dir = Path(tempfile.mkdtemp(prefix="spark-fuse-out-"))
    try:
        try:
            paths = client.download_outputs(subfolder_url, dl_dir)
        except ShareSyncError:
            return []
        images = [p for p in paths
                  if str(p).lower().endswith((".png", ".jpg", ".jpeg", ".webp"))]
        saved = []
        for p in images:
            name = _unique_output_name(out_dir, p.name)
            shutil.move(str(p), str(out_dir / name))
            saved.append(name)
    finally:
        shutil.rmtree(dl_dir, ignore_errors=True)
    if saved:
        log(f"[bridge] saved {len(saved)} image(s) to ComfyUI output: {', '.join(saved)}")
    return saved


def _stream_into_panel(client, job_id: str) -> None:
    """Best-effort live log feed for the panel. Completion is detected by polling
    the job status, not by this returning: the SSE stream can stay open well past
    the job's terminal state (the compute's idle-hold keeps it alive), so the
    download must not be blocked on it closing."""
    try:
        for event in client.stream_logs(job_id):
            if isinstance(event, LogEvent):
                _append_line(job_id, event.line)
            elif isinstance(event, QueueStatusEvent):
                _update(job_id, status=event.status)
    except Exception:  # noqa: BLE001 - the log feed is non-essential
        pass


def _watch(client, job_id: str) -> None:
    threading.Thread(target=_stream_into_panel, args=(client, job_id), daemon=True).start()
    try:
        # Poll the job status for the terminal state (Walt's recommended pattern)
        # rather than waiting for the log stream to close.
        job = client.get_job(job_id)
        waited = 0
        while not job.is_terminal and waited < 7200:
            time.sleep(5)
            waited += 5
            job = client.get_job(job_id)
            _update(job_id, status=job.status)

        common = dict(
            exit_code=job.exit_code,
            image_cache_hit=job.image_cache_hit,
            image_affinity=job.image_affinity,
        )

        if job.status == "succeeded":
            # Download BEFORE publishing the terminal status, so the UI does not stop
            # polling before the image lands.
            image_name = None
            try:
                saved = _download_images(client, job, log=lambda m: _append_line(job_id, m))
                image_name = saved[0] if saved else None
            except Exception as exc:  # noqa: BLE001 - a download failure must not mark a good job failed
                _append_line(job_id, f"[bridge] download failed: {exc}")
            _update(job_id, status=job.status, image=image_name, **common)
        else:
            state = get_job_state(job_id)
            detail = _extract_validation_error(state["lines"]) if state else None
            _update(job_id, status=job.status,
                    error=detail or job.error_message or job.error_code or "job failed",
                    **common)
    except Exception as exc:  # noqa: BLE001 - any failure should surface in the UI
        _append_line(job_id, f"[bridge error] {exc}")
        _update(job_id, status="failed", error=str(exc))
        traceback.print_exc()
    finally:
        _close(client)


def _close(client) -> None:
    close = getattr(client, "close", None)
    if callable(close):
        try:
            close()
        except Exception:  # noqa: BLE001
            pass
