"""HTTP routes registered on ComfyUI's server for the Spark Fuse bridge.

All blocking Spark Fuse calls run in a thread executor so they never block
ComfyUI's asyncio event loop.
"""
from __future__ import annotations

import asyncio

from aiohttp import web
from server import PromptServer

from . import __version__, config, jobs, model_sync, render_queue

routes = PromptServer.instance.routes


async def _run(func):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, func)


@routes.get("/spark_fuse/settings")
async def get_settings(request):
    data = config.public_settings()
    data["version"] = __version__
    return web.json_response(data)


@routes.post("/spark_fuse/settings")
async def post_settings(request):
    body = await request.json()
    # A blank password field must not wipe a stored credential.
    if not body.get("password"):
        body.pop("password", None)
    config.save_settings(body)
    return web.json_response(config.public_settings())


@routes.get("/spark_fuse/skus")
async def get_skus(request):
    def work():
        client = config.make_client()
        client.login()
        try:
            out = []
            for sku in client.list_skus():
                if isinstance(sku, dict):
                    out.append({
                        "instanceType": sku.get("instanceType"),
                        "gpuType": sku.get("gpuType"),
                        "gpuMemoryGb": sku.get("gpuMemoryGb"),
                    })
                else:
                    out.append({"instanceType": str(sku), "gpuType": None, "gpuMemoryGb": None})
            return out
        finally:
            jobs._close(client)

    try:
        skus = await _run(work)
    except Exception as exc:  # noqa: BLE001
        return web.json_response({"error": str(exc)}, status=400)
    return web.json_response({"skus": skus})


@routes.get("/spark_fuse/estimate")
async def get_estimate(request):
    name = request.query.get("instance_type", "")
    if not name:
        return web.json_response({"error": "instance_type required"}, status=400)

    def work():
        client = config.make_client()
        client.login()
        try:
            est = client.estimate(instance_type=name)
            return est.rate.billed_per_hour_usd
        finally:
            jobs._close(client)

    try:
        rate = await _run(work)
    except Exception as exc:  # noqa: BLE001
        # Unpriced SKUs return an error; surface it without failing the UI.
        return web.json_response({"instanceType": name, "ratePerHourUsd": None,
                                  "error": str(exc)})
    return web.json_response({"instanceType": name, "ratePerHourUsd": rate})


@routes.post("/spark_fuse/submit")
async def post_submit(request):
    body = await request.json()
    prompt = body.get("workflow") or body.get("prompt")
    instance_type = body.get("instance_type")
    if not prompt:
        return web.json_response({"error": "No workflow provided."}, status=400)

    try:
        job_id = await _run(lambda: jobs.submit_workflow(prompt, instance_type))
    except Exception as exc:  # noqa: BLE001
        return web.json_response({"error": str(exc)}, status=400)
    return web.json_response({"jobId": job_id})


@routes.get("/spark_fuse/job/{job_id}")
async def get_job(request):
    job_id = request.match_info["job_id"]
    state = jobs.get_job_state(job_id)
    if state is None:
        return web.json_response({"error": "unknown job"}, status=404)
    return web.json_response(state)


@routes.post("/spark_fuse/queue")
async def post_queue(request):
    body = await request.json()
    items = body.get("items") or []
    instance_type = body.get("instance_type")
    if not items:
        return web.json_response({"error": "No workflows in the queue."}, status=400)
    for it in items:
        wf = it.get("workflow") or it.get("prompt")
        if not wf:
            return web.json_response({"error": "A queued item is missing its workflow."}, status=400)
        it["workflow"] = wf

    try:
        queue_id = await _run(lambda: render_queue.submit_queue(items, instance_type))
    except Exception as exc:  # noqa: BLE001
        return web.json_response({"error": str(exc)}, status=400)
    return web.json_response({"queueId": queue_id})


@routes.get("/spark_fuse/queue/{queue_id}")
async def get_queue(request):
    queue_id = request.match_info["queue_id"]
    state = render_queue.get_queue_state(queue_id)
    if state is None:
        return web.json_response({"error": "unknown queue"}, status=404)
    return web.json_response(state)


@routes.post("/spark_fuse/queue/{queue_id}/cancel")
async def cancel_queue(request):
    queue_id = request.match_info["queue_id"]
    render_queue.cancel_queue(queue_id)
    return web.json_response({"ok": True})


# ---- Pre-render model sync ----------------------------------------------


@routes.post("/spark_fuse/models/check")
async def post_models_check(request):
    body = await request.json()
    workflows = body.get("workflows") or []
    if not workflows:
        return web.json_response({"error": "No workflows provided."}, status=400)
    try:
        result = await _run(lambda: model_sync.check_models(workflows))
    except Exception as exc:  # noqa: BLE001
        return web.json_response({"error": str(exc)}, status=400)
    return web.json_response(result)


@routes.post("/spark_fuse/models/upload")
async def post_models_upload(request):
    body = await request.json()
    files = body.get("files") or []
    if not files:
        return web.json_response({"error": "No files to upload."}, status=400)
    # The browser nominates {folder, name} pairs from a check result; local
    # paths, sizes and destinations are re-derived server-side.
    try:
        prepared = await _run(lambda: model_sync.prepare_upload_files(files))
    except ValueError as exc:
        return web.json_response({"error": str(exc)}, status=400)
    except Exception as exc:  # noqa: BLE001
        return web.json_response({"error": str(exc)}, status=400)
    upload_id = model_sync.start_upload(prepared)
    return web.json_response({"uploadId": upload_id})


@routes.get("/spark_fuse/models/upload/{upload_id}")
async def get_models_upload(request):
    state = model_sync.get_upload_state(request.match_info["upload_id"])
    if state is None:
        return web.json_response({"error": "unknown upload"}, status=404)
    return web.json_response(state)


@routes.get("/spark_fuse/models/uploads")
async def get_models_uploads(request):
    return web.json_response({"uploads": model_sync.list_uploads()})


@routes.post("/spark_fuse/models/upload/{upload_id}/cancel")
async def cancel_models_upload(request):
    model_sync.cancel_upload(request.match_info["upload_id"])
    return web.json_response({"ok": True})
