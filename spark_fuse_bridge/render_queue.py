"""Render queue — run several workflows back to back on one warm instance.

Spark Fuse API §13 lets us pre-warm an instance (POST /instances/prepare) and
route jobs to it by instanceHandle. As of the published spark-fuse-comfyui
image (runner commits 28c449b + dfe12e3), a single job can also carry a
multi-workflow manifest (/input/spark_fuse_job.json) that runs several
workflows against ONE already-warm ComfyUI process, instead of one job per
workflow. This module chunks the queue into batches of up to
config.queue_chunk_size() workflows, submits one manifest job per batch
(still routed onto the same prepared instance), and derives per-workflow
status from the runner's own "[runner] workflow M/T ..." stdout markers,
since the platform only exposes one status per job — not one per workflow.

Item indices in _QUEUES[qid]["items"] are always GLOBAL (0-based position in
the whole queue, set once in submit_queue()). Each batch's runner reports
LOCAL indices that restart at 1 in every job/container; batch_offset
translates local -> global as global = batch_offset + local - 1.
"""
from __future__ import annotations

import re
import threading
import time
import traceback
import uuid

from spark_fuse.errors import NoWarmPoolCapacityError
from spark_fuse.models import LogEvent

from . import jobs
from .config import load_settings, make_client, queue_chunk_size

_QUEUES: dict[str, dict] = {}
_LOCK = threading.Lock()
_MAX_LINES = 600
# Session idle-hold ceiling. The clock starts at 'ready' and re-arms after each
# batch is submitted, so this is an IDLE ceiling between batches, not a total-
# queue ceiling. 600s (10 min) is generous for the gaps between batches while
# keeping billing exposure low on a crash or hard kill.
_HOLD_SECONDS = 600
_READY_TIMEOUT = 900   # max seconds to wait for the instance to report ready
_JOB_TIMEOUT = 7200    # per-batch safety ceiling
_AFFINITY_RETRIES = 3  # attempts on NoWarmPoolCapacityError before fallback/abort
_AFFINITY_RETRY_SLEEP = 5  # seconds between capacity-retry attempts
# Grace period after a batch's job goes terminal, before gap-filling and
# downloads: the SSE log stream can lag a terminal poll by a moment, so this
# gives any already-printed-but-not-yet-delivered "[runner] workflow ..."
# marker lines a chance to arrive before we conclude one was never sent.
_TERMINAL_GRACE_SECONDS = 3
# After cancelling a job that never confirmed terminal on its own (the
# _wait_job_to_terminal timeout path), how long to poll for it to actually
# reach a terminal state before giving up on releasing the instance. The API
# cancels via SIGTERM with a 30s grace then SIGKILL, so this must clear that
# window with margin, not just match it.
_CANCEL_GRACE_SECONDS = 45

# Matches the runner's exact marker strings (spark_fuse_run.py run_manifest()):
#   [runner] workflow 2/5 started
#   [runner] workflow 2/5 succeeded (3 file(s))
#   [runner] workflow 2/5 failed: <reason>
#   [runner] workflow 2/5 failed: <reason> (2 partial file(s))
_MARKER_RE = re.compile(
    r"^\[runner\] workflow (?P<local>\d+)/(?P<total>\d+) "
    r"(?P<event>started|succeeded|failed)"
    r"(?:: (?P<reason>.*?))?"
    r"(?: \((?P<count>\d+) (?:partial )?file\(s\)\))?$"
)


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


def _get_item(qid: str, index: int) -> dict | None:
    with _LOCK:
        state = _QUEUES.get(qid)
        if not state:
            return None
        for it in state["items"]:
            if it["index"] == index:
                return dict(it)
    return None


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


def _set_active_job(qid: str, job_id: str | None) -> None:
    _set(qid, active_job=job_id)


def _job_still_active(qid: str, job_id: str) -> bool:
    with _LOCK:
        state = _QUEUES.get(qid)
        return bool(state and state.get("active_job") == job_id)


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
    _append(qid, "[queue] cancel requested — the current batch will finish "
                  "and download normally; no further batches will be submitted")


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
         cancel=False, active_job=None, items=state_items)
    threading.Thread(target=_run_queue, args=(qid, norm_items, instance_type, settings),
                     daemon=True).start()
    return qid


def _stream_chunk(client, job_id: str, qid: str, batch_offset: int) -> None:
    """Feed a batch job's log into the queue log, parsing the runner's
    per-workflow markers to drive per-item status. One job's status/exit code
    covers up to queue_chunk_size() workflows, so job state alone cannot say
    which one is currently running or which one failed — only the runner's own
    "[runner] workflow M/T ..." lines can. Translates each marker's local index
    (restarts at 1 every job) to the correct global queue item via
    batch_offset. Stops once this batch is no longer the active one, mirroring
    the old per-item stream's past-terminal guard (idle-hold can keep the SSE
    stream open past the job's terminal state).
    """
    current_local = None
    try:
        for event in client.stream_logs(job_id):
            if not _job_still_active(qid, job_id):
                break
            if not isinstance(event, LogEvent):
                continue
            line = event.line
            _append(qid, f"  {line}")
            m = _MARKER_RE.match(line)
            if m:
                local = int(m.group("local"))
                global_idx = batch_offset + local - 1
                current_local = local
                event_kind = m.group("event")
                if event_kind == "started":
                    _set_item(qid, global_idx, status="running")
                elif event_kind == "succeeded":
                    _set_item(qid, global_idx, status="succeeded", _has_output=True)
                else:  # failed
                    reason = m.group("reason") or "workflow failed"
                    _set_item(qid, global_idx, status="failed", error=reason,
                              _has_output=m.group("count") is not None)
            elif current_local is not None:
                global_idx = batch_offset + current_local - 1
                _record_item_line(qid, global_idx, line)
    except Exception:  # noqa: BLE001 - the log feed is non-essential
        pass


def _wait_job_to_terminal(client, job_id: str, qid: str, label: str):
    """Poll a batch's job to terminal; heartbeat. Does NOT check queue-cancel:
    with chunking, a job now covers up to queue_chunk_size() workflows, so a
    queue cancel must let the in-flight batch finish and download normally
    rather than killing a job mid-batch — that would risk losing items in the
    batch that had already succeeded, and whether a cancelled job's outputs
    stay downloadable at all is unverified. Only the outer chunk loop in
    _run_queue consults the cancel flag, between batches.

    Returns (job, timed_out). timed_out=True means _JOB_TIMEOUT elapsed before
    the job reached a terminal state: job is a live snapshot, not a confirmed
    completion, and the caller must not treat it as one — see the timed_out
    branch in _run_queue, which skips the normal completion path entirely
    rather than reading exit_code/output off a job that may still be running.
    """
    job = client.get_job(job_id)
    waited = last_beat = 0
    while not job.is_terminal:
        if waited >= _JOB_TIMEOUT:
            return job, True
        time.sleep(5)
        waited += 5
        if waited - last_beat >= 30:
            _append(qid, f"[queue] {label}: running ({waited}s)")
            last_beat = waited
        job = client.get_job(job_id)
    return job, False


def _fill_marker_gaps(qid: str, batch_offset: int, count: int, reason: str) -> None:
    """After a batch's job goes terminal, any item in it that never received a
    terminal marker (never started, or started but no succeeded/failed line
    ever arrived — a dead ComfyUI process, or rarely a missed SSE line) must
    not be left stuck at queued/running in the panel forever."""
    for local in range(1, count + 1):
        global_idx = batch_offset + local - 1
        item = _get_item(qid, global_idx)
        if item and item.get("status") not in ("succeeded", "failed", "cancelled"):
            _set_item(qid, global_idx, status="failed", error=reason)


def _finalize_item_errors(qid: str, batch_offset: int, count: int) -> None:
    """Prefer the detailed ComfyUI rejection scraped from a failed item's own
    log slice over the runner marker's plain reason text, same precedence the
    single-job path used before chunking (_extract_validation_error(...) or
    the runner's own message)."""
    for local in range(1, count + 1):
        global_idx = batch_offset + local - 1
        item = _get_item(qid, global_idx)
        if not item or item.get("status") != "failed":
            continue
        detail = jobs._extract_validation_error(_item_log(qid, global_idx))
        if detail:
            _set_item(qid, global_idx, error=detail)


def _mark_batches_terminal(qid: str, start_offset: int, batches: list[list[dict]],
                            status: str, error: str | None = None) -> int:
    """Force every item across the given (not-yet-run) batches into a terminal
    status, starting at start_offset's global index. Used both for a queue
    cancel (marks not-yet-submitted batches 'cancelled') and for a bridge-side
    submit/manifest error (marks the rest 'failed'). Returns the offset just
    past the last batch marked, matching the running offset the caller would
    have reached had it kept going."""
    offset = start_offset
    for batch in batches:
        for local in range(1, len(batch) + 1):
            _set_item(qid, offset + local - 1, status=status, error=error)
        offset += len(batch)
    return offset


def _release_instance(client, qid: str, handle: str, stuck_job_id: str | None) -> None:
    """Release the session's instance from _run_queue's finally block.

    If stuck_job_id is set, the last batch's job never confirmed terminal (the
    _wait_job_to_terminal timeout path) and may still be running on this
    instance — releasing blind would risk a 409 (Spark Fuse's release endpoint
    refuses to tear down an instance with a job still running on it), which
    the messenger surfaces as SessionConflictError. Cancel that job first and
    poll briefly for it to actually go terminal before attempting release.

    Any failure here — cancel failing, the job never confirming terminal, or
    release itself failing (including an unexpected 409 on the normal path) —
    is reported as a queue-level failure, not just a scrollback log line: it
    means the instance is left allocated and billing, which the user needs to
    see without having to scroll the log.
    """
    if stuck_job_id:
        _append(qid, f"[queue] batch job {stuck_job_id} never confirmed finished; "
                     f"cancelling it before releasing instance {handle}")
        try:
            job = client.cancel(stuck_job_id)
        except Exception as exc:  # noqa: BLE001
            _append(qid, f"[queue] cancel failed: {exc}")
            _set(qid, status="failed",
                 error=f"instance {handle} may still be running a stuck job "
                       f"({stuck_job_id}); cancel failed: {exc}. Check it manually.")
            return

        waited = 0
        while not job.is_terminal and waited < _CANCEL_GRACE_SECONDS:
            time.sleep(3)
            waited += 3
            job = client.get_job(stuck_job_id)

        if not job.is_terminal:
            _append(qid, f"[queue] job {stuck_job_id} still not terminal "
                         f"{waited}s after cancel; leaving instance {handle} "
                         "running — check it manually")
            _set(qid, status="failed",
                 error=f"instance {handle} may still be running; job {stuck_job_id} "
                       "did not confirm terminal after cancel")
            return

    try:
        client.release_instance(handle)
        _append(qid, "[queue] instance released")
    except Exception as exc:  # noqa: BLE001
        _append(qid, f"[queue] release failed: {exc}")
        _set(qid, status="failed",
             error=f"instance {handle} may still be running; release failed: {exc}")


def _run_queue(qid: str, items: list[dict], instance_type: str | None, settings: dict) -> None:
    client = make_client(settings)
    handle = None
    # Set when a batch's job never confirmed terminal (see the timed_out branch
    # below), so `finally` knows to try cancelling it before releasing the
    # instance instead of releasing blind. None means release can proceed as
    # normal — either no batch ran, or every batch that did run confirmed terminal.
    stuck_job_id: str | None = None
    chunk_size = queue_chunk_size(settings)
    batches = [items[i:i + chunk_size] for i in range(0, len(items), chunk_size)]
    try:
        client.login()
        sku = instance_type or settings["instance_type"]
        affinity = (settings.get("session_affinity") or "preferred").lower()

        # Attempt to prepare a warm session; retry on capacity pressure.
        session = None
        for attempt in range(1, _AFFINITY_RETRIES + 1):
            try:
                _append(qid, f"[queue] preparing a warm {sku} instance "
                             f"for {len(items)} workflow(s) in {len(batches)} "
                             f"batch(es) (attempt {attempt}/{_AFFINITY_RETRIES})")
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
                    _mark_batches_terminal(qid, 0, batches, status="cancelled")
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

            _append(qid, f"[queue] instance ready; running {len(batches)} batch(es)")
        else:
            _append(qid, f"[queue] running {len(batches)} batch(es) without a warm session")

        _set(qid, status="running")

        offset = 0
        for batch_index, batch in enumerate(batches):
            if _cancelled(qid):
                _mark_batches_terminal(qid, offset, batches[batch_index:], status="cancelled")
                break

            count = len(batch)
            labels = ", ".join(it["label"] for it in batch)
            _append(qid, f"[queue] batch {batch_index + 1}/{len(batches)}: "
                         f"submitting {count} workflow(s) ({labels})")
            for it in batch:
                jobs._normalize_model_paths(it["workflow"])
            entries = [{"workflow": it["workflow"], "batch_count": it["batch_count"]} for it in batch]

            try:
                resp = jobs._submit_chunk_job(
                    client, entries, instance_type=sku, settings=settings,
                    instance_handle=handle, log=lambda m: _append(qid, m))
            except Exception as exc:  # noqa: BLE001
                _append(qid, f"[queue] batch {batch_index + 1}/{len(batches)}: "
                             f"submit failed — {exc}")
                _mark_batches_terminal(qid, offset, batches[batch_index:],
                                       status="failed", error=str(exc))
                break

            job_id = resp.job_id
            for local in range(1, count + 1):
                _set_item(qid, offset + local - 1, job_id=job_id)
            _set_active_job(qid, job_id)
            threading.Thread(target=_stream_chunk, args=(client, job_id, qid, offset),
                             daemon=True).start()
            job, timed_out = _wait_job_to_terminal(
                client, job_id, qid, f"batch {batch_index + 1}/{len(batches)}")

            if timed_out:
                _set_active_job(qid, None)
                _append(qid, f"[queue] batch {batch_index + 1}/{len(batches)}: gave up "
                             f"waiting after {_JOB_TIMEOUT}s; the job may still be "
                             "running. Stopping the queue.")
                _fill_marker_gaps(
                    qid, offset, count,
                    "bridge gave up waiting for this batch's job to finish (it may "
                    "still be running on Spark Fuse) — not a workflow failure")
                _finalize_item_errors(qid, offset, count)
                # Everything after this batch was never submitted, so the handle's
                # state is unknown until the stuck job is confirmed terminal; do not
                # risk a second job landing on a possibly still-busy instance.
                _mark_batches_terminal(
                    qid, offset + count, batches[batch_index + 1:], status="failed",
                    error="queue stopped: an earlier batch's job never confirmed finished")
                stuck_job_id = job_id
                break

            # Grace period for trailing SSE lines before we conclude a marker
            # was never sent, then stop this batch's stream from attributing
            # any further (unrelated) lines.
            time.sleep(_TERMINAL_GRACE_SECONDS)
            _set_active_job(qid, None)

            if job.exit_code == 2:
                reason = ("internal error: Spark Fuse rejected this batch's manifest "
                          "(exit code 2) — this indicates a bug in how the bridge built "
                          "the request, not a workflow problem")
                _append(qid, f"[queue] batch {batch_index + 1}/{len(batches)}: "
                             f"{reason}. Stopping the queue.")
                _mark_batches_terminal(qid, offset, batches[batch_index:],
                                       status="failed", error=reason)
                break

            gap_reason = (
                "batch aborted: the ComfyUI process died before this workflow could run"
                if job.exit_code == 6 else
                f"no result reported for this workflow before the batch ended "
                f"(job status: {job.status!r})"
            )
            _fill_marker_gaps(qid, offset, count, gap_reason)
            _finalize_item_errors(qid, offset, count)

            base_url = jobs._resolve_chunk_output_base_url(
                client, job, log=lambda m: _append(qid, m))
            if base_url:
                for local, it in enumerate(batch, start=1):
                    global_idx = offset + local - 1
                    item = _get_item(qid, global_idx)
                    if not item or item["status"] == "cancelled":
                        continue
                    if item["status"] == "failed" and not item.get("_has_output"):
                        continue  # marker already told us there is nothing to download
                    saved = jobs._download_chunk_item_images(
                        client, job, base_url, local, log=lambda m: _append(qid, m))
                    if saved:
                        _set_item(qid, global_idx, images=saved)
                        _set(qid, image=saved[0])

            _append(qid, f"[queue] batch {batch_index + 1}/{len(batches)}: done")
            offset += count

        if _cancelled(qid):
            _set(qid, status="cancelled")
        else:
            with _LOCK:
                fails = sum(1 for it in _QUEUES[qid]["items"] if it["status"] == "failed")
            _set(qid, status=("failed" if fails else "succeeded"))
            _append(qid, "[queue] all workflows finished" if not fails
                    else f"[queue] finished with {fails} failed workflow(s)")
    except Exception as exc:  # noqa: BLE001 - any failure should surface in the UI
        _append(qid, f"[queue error] {exc}")
        _set(qid, status="failed", error=str(exc))
        traceback.print_exc()
    finally:
        _set_active_job(qid, None)
        if handle:
            _release_instance(client, qid, handle, stuck_job_id)
        jobs._close(client)
