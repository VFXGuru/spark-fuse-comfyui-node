"""Render queue — run several workflows back to back on one warm instance.

Spark Fuse API §13 lets us pre-warm an instance (POST /instances/prepare), route
any number of sequential jobs to it by instanceHandle, then release it. This module
does that orchestration client-side: prepare once, submit each queued workflow (each
with its own batch count) in turn, download as each finishes, then release. When
Spark Fuse ships native automatic queuing on a prepared session, the per-item
submit-and-poll loop here collapses to a single load-all call; the prepare / handle
/ release scaffolding stays the same.
"""
from __future__ import annotations

import threading
import time
import traceback
import uuid

from spark_fuse.errors import NoWarmPoolCapacityError
from spark_fuse.models import LogEvent

from . import jobs
from .config import load_settings, make_client

_QUEUES: dict[str, dict] = {}
_LOCK = threading.Lock()
_MAX_LINES = 600
# Session idle-hold ceiling. The clock starts at 'ready' and re-arms after each
# job is submitted, so this is an IDLE ceiling between jobs, not a total-queue
# ceiling. 600s (10 min) is generous for the gaps between jobs while keeping
# billing exposure low on a crash or hard kill.
_HOLD_SECONDS = 600
_READY_TIMEOUT = 900   # max seconds to wait for the instance to report ready
_JOB_TIMEOUT = 7200    # per-job safety ceiling
_AFFINITY_RETRIES = 3  # attempts on NoWarmPoolCapacityError before fallback/abort
_AFFINITY_RETRY_SLEEP = 5  # seconds between capacity-retry attempts


def _clamp(value) -> int:
    try:
        return max(1, min(int(value or 1), 100))
    except (TypeError, ValueError):
        return 1


def _set(qid: str, **fields) -> None:
    with _LOCK:
        _QUEUES.setdefault(qid, {"lines": [], "items": []}).update(fields)


def _append(qid: str, line: str) -> None:
    with _LOCK:
        state = _QUEUES.setdefault(qid, {"lines": [], "items": []})
        state["lines"].append(line)
        if len(state["lines"]) > _MAX_LINES:
            state["lines"] = state["lines"][-_MAX_LINES:]


def _set_item(qid: str, index: int, **fields) -> None:
    with _LOCK:
        state = _QUEUES.get(qid)
        if not state:
            return
        for it in state["items"]:
            if it["index"] == index:
                it.update(fields)
                break


def _record_item_line(qid: str, index: int, line: str) -> None:
    with _LOCK:
        state = _QUEUES.get(qid)
        if not state:
            return
        for it in state["items"]:
            if it["index"] == index:
                buf = it.setdefault("_log", [])
                buf.append(line)
                if len(buf) > 200:
                    it["_log"] = buf[-200:]
                break


def _item_log(qid: str, index: int) -> list[str]:
    with _LOCK:
        state = _QUEUES.get(qid)
        if not state:
            return []
        for it in state["items"]:
            if it["index"] == index:
                return list(it.get("_log") or [])
    return []


def _cancelled(qid: str) -> bool:
    with _LOCK:
        state = _QUEUES.get(qid)
        return bool(state and state.get("cancel"))


def _item_active(qid: str, index: int) -> bool:
    with _LOCK:
        state = _QUEUES.get(qid)
        return bool(state and state.get("active_item") == index)


def get_queue_state(qid: str) -> dict | None:
    with _LOCK:
        state = _QUEUES.get(qid)
        if state is None:
            return None
        snapshot = dict(state)
        snapshot["lines"] = list(state["lines"][-250:])
        # Drop per-item internals (the raw _log buffer) from what the UI sees.
        snapshot["items"] = [
            {k: v for k, v in it.items() if not k.startswith("_")}
            for it in state["items"]
        ]
        return snapshot


def cancel_queue(qid: str) -> None:
    _set(qid, cancel=True)
    _append(qid, "[queue] cancel requested")


def submit_queue(items: list[dict], instance_type: str | None = None) -> str:
    """Start a render queue. items = [{workflow, batch_count, label}]. Returns queue id."""
    qid = uuid.uuid4().hex[:12]
    settings = load_settings()
    norm_items, state_items = [], []
    for i, it in enumerate(items):
        label = (it.get("label") or f"Job {i + 1}").strip() or f"Job {i + 1}"
        batch = _clamp(it.get("batch_count"))
        norm_items.append({"workflow": it["workflow"], "batch_count": batch, "label": label})
        state_items.append({"index": i, "label": label, "batch_count": batch,
                            "status": "queued", "job_id": None, "images": [], "error": None})
    _set(qid, status="preparing", instance_handle=None, image=None, error=None,
         cancel=False, active_item=None, items=state_items)
    threading.Thread(target=_run_queue, args=(qid, norm_items, instance_type, settings),
                     daemon=True).start()
    return qid


def _stream_item(client, job_id: str, qid: str, index: int, label: str) -> None:
    """Feed a running job's container log into the queue log, tagged by item. Stops
    once this item is no longer the active one, so a stream the idle-hold keeps open
    past terminal does not bleed into the next item."""
    try:
        for event in client.stream_logs(job_id):
            if not _item_active(qid, index):
                break
            if isinstance(event, LogEvent):
                _append(qid, f"  [{label}] {event.line}")
                _record_item_line(qid, index, event.line)
    except Exception:  # noqa: BLE001 - the log feed is non-essential
        pass


def _wait_job(client, job_id: str, qid: str, label: str):
    """Poll a job to terminal; heartbeat; honour queue cancel. Returns the Job, or
    None if the queue was cancelled (the job is cancelled too)."""
    job = client.get_job(job_id)
    waited = last_beat = 0
    while not job.is_terminal:
        if _cancelled(qid):
            try:
                client.cancel(job_id)
            except Exception:  # noqa: BLE001
                pass
            return None
        if waited >= _JOB_TIMEOUT:
            break
        time.sleep(5)
        waited += 5
        if waited - last_beat >= 30:
            _append(qid, f"[queue] {label}: running ({waited}s)")
            last_beat = waited
        job = client.get_job(job_id)
    return job


def _run_queue(qid: str, items: list[dict], instance_type: str | None, settings: dict) -> None:
    client = make_client(settings)
    handle = None
    try:
        client.login()
        sku = instance_type or settings["instance_type"]
        affinity = (settings.get("session_affinity") or "preferred").lower()

        # Attempt to prepare a warm session; retry on capacity pressure.
        session = None
        for attempt in range(1, _AFFINITY_RETRIES + 1):
            try:
                _append(qid, f"[queue] preparing a warm {sku} instance "
                             f"for {len(items)} job(s) (attempt {attempt}/{_AFFINITY_RETRIES})")
                session = client.prepare_instance(instance_type=sku, hold_seconds=_HOLD_SECONDS)
                break
            except NoWarmPoolCapacityError:
                _append(qid, f"[queue] no warm-pool capacity (attempt {attempt}/{_AFFINITY_RETRIES})")
                if attempt < _AFFINITY_RETRIES:
                    time.sleep(_AFFINITY_RETRY_SLEEP)

        if session is None:
            if affinity == "required":
                raise NoWarmPoolCapacityError(
                    f"No warm-pool capacity after {_AFFINITY_RETRIES} attempts; "
                    "session_affinity=required — queue aborted"
                )
            _append(qid, "[queue] no warm-pool capacity after retries; "
                         "running without a warm session (cold starts apply)")

        if session is not None:
            handle = session.instance_handle
            _set(qid, instance_handle=handle)

            # Wait until the session is ready (or fails) before routing jobs to it.
            # Keeps its own loop (not client.wait_until_ready) to honour _cancelled().
            waited = 0
            while not session.is_ready and not session.is_terminal:
                if _cancelled(qid):
                    _set(qid, status="cancelled")
                    return
                if waited >= _READY_TIMEOUT:
                    raise RuntimeError("instance did not become ready in time")
                time.sleep(5)
                waited += 5
                session = client.get_instance(handle)
            if not session.is_ready:
                raise RuntimeError(
                    f"instance prepare {session.status}: "
                    f"{session.error_code or ''} {session.error_message or ''}".strip())

            _append(qid, f"[queue] instance ready; running {len(items)} job(s) back to back")
        else:
            _append(qid, f"[queue] running {len(items)} job(s) without a warm session")

        _set(qid, status="running")

        for i, it in enumerate(items):
            if _cancelled(qid):
                _set_item(qid, i, status="cancelled")
                break
            label, batch, prompt = it["label"], it["batch_count"], it["workflow"]
            jobs._normalize_model_paths(prompt)
            _set_item(qid, i, status="running")
            _set(qid, active_item=i)
            _append(qid, f"[queue] {label}: submitting ({batch} image(s) per job)")
            try:
                resp = jobs._submit_job(client, prompt, instance_type=sku,
                                        settings=settings, batch_count=batch,
                                        instance_handle=handle)
            except Exception as exc:  # noqa: BLE001
                _set_item(qid, i, status="failed", error=str(exc))
                _append(qid, f"[queue] {label}: submit failed — {exc}")
                continue

            job_id = resp.job_id
            _set_item(qid, i, job_id=job_id)
            threading.Thread(target=_stream_item, args=(client, job_id, qid, i, label),
                             daemon=True).start()
            job = _wait_job(client, job_id, qid, label)
            _set(qid, active_item=None)  # let this item's lingering log stream stop

            if job is None:  # cancelled mid-job
                _set_item(qid, i, status="cancelled")
                break
            if job.status == "succeeded":
                try:
                    saved = jobs._download_images(client, job, log=lambda m: _append(qid, m))
                except Exception as exc:  # noqa: BLE001 - a download failure must not sink the queue
                    saved = []
                    _append(qid, f"[queue] {label}: download failed — {exc}")
                _set_item(qid, i, status="succeeded", images=saved)
                if saved:
                    _set(qid, image=saved[0])
                _append(qid, f"[queue] {label}: done ({len(saved)} image(s))")
            else:
                detail = (jobs._extract_validation_error(_item_log(qid, i))
                          or job.error_message or job.error_code or "job failed")
                _set_item(qid, i, status="failed", error=detail)
                _append(qid, f"[queue] {label}: FAILED — {detail}")

        if _cancelled(qid):
            _set(qid, status="cancelled")
        else:
            with _LOCK:
                fails = sum(1 for it in _QUEUES[qid]["items"] if it["status"] == "failed")
            _set(qid, status=("failed" if fails else "succeeded"))
            _append(qid, "[queue] all jobs finished" if not fails
                    else f"[queue] finished with {fails} failed job(s)")
    except Exception as exc:  # noqa: BLE001 - any failure should surface in the UI
        _append(qid, f"[queue error] {exc}")
        _set(qid, status="failed", error=str(exc))
        traceback.print_exc()
    finally:
        _set(qid, active_item=None)
        if handle:
            try:
                client.release_instance(handle)
                _append(qid, "[queue] instance released")
            except Exception as exc:  # noqa: BLE001
                _append(qid, f"[queue] release failed: {exc}")
        jobs._close(client)
