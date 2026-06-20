"""Job orchestration: submit a workflow, stream progress, download the result.

The long-lived stream/poll/download runs in a daemon thread, recording state in an
in-memory registry the HTTP routes read from. This mirrors Spark Fuse's recommended
pattern: stream the log for progress, and on terminal `succeeded` the output path is
ready to list and download (no separate "output ready" signal).
"""
from __future__ import annotations

import json
import tempfile
import threading
import time
import traceback
from pathlib import Path

from spark_fuse.models import LogEvent, QueueStatusEvent

from .config import load_settings, make_client

# job_id -> {status, lines[], image, error, image_cache_hit, image_affinity, exit_code}
_JOBS: dict[str, dict] = {}
_LOCK = threading.Lock()
_MAX_LINES = 400


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


def submit_workflow(api_prompt: dict, instance_type: str | None = None) -> str:
    """Submit an API-format workflow to Spark Fuse and return the job id.

    Models come from the read-only /assets mount; the small workflow.json is pushed
    via the auto-prepare upload URL. Progress is then tracked in a background thread.
    """
    _normalize_model_paths(api_prompt)
    settings = load_settings()
    client = make_client(settings)
    client.login()

    resp = client.submit(
        image=settings["image"],
        command=settings["command"],
        instance_type=instance_type or settings["instance_type"],
        env={"MODEL_BASE_DIR": settings["model_base_dir"]},
        input_push_mode="auto-prepare",
        assets_share_sync_path=settings.get("assets_share_sync_path") or None,
        assets_share_sync_space_name=settings.get("assets_share_sync_space_name") or None,
        image_affinity=settings.get("image_affinity") or None,
    )
    job_id = resp.job_id
    _update(job_id, status=resp.status, image=None, error=None)

    if not (resp.input and resp.input.upload_url):
        _update(job_id, status="failed",
                error="No auto-prepare upload URL returned by Spark Fuse.")
        _close(client)
        return job_id

    # Stage the workflow as /input/workflow.json via the one-shot upload URL.
    tmp = Path(tempfile.mkdtemp(prefix="spark-fuse-wf-"))
    (tmp / "workflow.json").write_text(json.dumps(api_prompt), encoding="utf-8")
    client.upload_input(tmp, resp.input.upload_url)

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
            # The resolved output URL can lag the terminal status by a moment, so
            # retry briefly rather than skip the download. Download BEFORE publishing
            # the terminal status, so the UI does not stop polling before the image.
            base_url = job.output.share_sync_base_url if job.output else None
            for _ in range(6):
                if base_url:
                    break
                time.sleep(2)
                job = client.get_job(job_id)
                base_url = job.output.share_sync_base_url if job.output else None

            image_name = None
            try:
                if base_url:
                    import folder_paths  # ComfyUI-provided; available at runtime
                    out_dir = Path(folder_paths.get_output_directory())
                    _append_line(job_id, f"[bridge] downloading outputs from {base_url}")
                    paths = client.download_outputs(base_url, out_dir)
                    images = [
                        p for p in paths
                        if str(p).lower().endswith((".png", ".jpg", ".jpeg", ".webp"))
                    ]
                    if images:
                        image_name = Path(images[0]).name
                        _append_line(job_id, f"[bridge] downloaded {len(images)} image(s) to ComfyUI output")
                    else:
                        _append_line(job_id, f"[bridge] succeeded but found no image in {len(paths)} output file(s)")
                else:
                    _append_line(job_id, "[bridge] job succeeded but no output path was returned after retries")
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
