# Spark Fuse ComfyUI Node

A ComfyUI extension that offloads the current workflow to a [Spark Fuse](https://sparkcloud.studio)
cloud GPU and brings the rendered image back into ComfyUI. It is the desktop
companion to the [spark-fuse-comfyui](https://github.com/VFXGuru/spark-fuse-comfyui)
image and the [spark-fuse-messenger](https://github.com/VFXGuru/spark-fuse-messenger)
client, which it reuses as its API layer.

## How it works

It is an extension, not a graph node. The whole workflow runs in the cloud, so a
"Render on Spark Fuse" button ships your current graph (in ComfyUI API format) to
Spark Fuse, which runs it on the published ComfyUI image with your model library
mounted lazily at `/assets`. Progress streams back to a panel, and the finished
image is downloaded into ComfyUI's output folder and shown.

```
Local ComfyUI (authoring)                Spark Fuse cloud GPU
  click "Render on Spark Fuse"  ──┐
  current graph as API JSON       ▼
                             workflow.json pushed to /input
  panel shows live progress  ◄──── log stream
                                   models read lazily from /assets (cached on node)
  image appears in the panel ◄──── downloaded from /output
```

Models are not uploaded per render. They live on ShareSync, staged once, and are
mounted read-only and lazily at `/assets`, cached on the compute node across jobs.
Only the small `workflow.json` is sent each time. Image affinity steers repeated
runs onto a node that already cached the image, so warm runs skip the image pull.

## Requirements

- A local ComfyUI install.
- A Spark Fuse account with API credentials.
- Your model library staged once on ShareSync. See the
  [image repo's USER-GUIDE](https://github.com/VFXGuru/spark-fuse-comfyui/blob/main/USER-GUIDE.md).

## Install

1. Clone into ComfyUI's `custom_nodes` folder:
   ```
   git clone https://github.com/VFXGuru/spark-fuse-comfyui-node
   ```
   so it sits at `ComfyUI/custom_nodes/spark-fuse-comfyui-node`.
2. Install the dependency (the messenger client) into **the same Python that runs
   your ComfyUI**.

   Standard install (a venv or system Python):
   ```
   pip install -r requirements.txt
   ```

   Portable or desktop ComfyUI (embedded Python): the portable build ships its own
   Python in a `python_embeded` folder, and its isolated build step cannot fetch
   the messenger's build backend, failing with `Cannot import 'hatchling.build'`.
   Install the backend first and skip isolation. From the `python_embeded` folder:
   ```
   .\python.exe -m pip install hatchling
   .\python.exe -m pip install --no-build-isolation -r "..\ComfyUI\custom_nodes\spark-fuse-comfyui-node\requirements.txt"
   ```
   Confirm with `.\python.exe -c "import spark_fuse; print('spark_fuse OK')"`.
3. Restart ComfyUI.

## Configure

Click the **⚡ Spark Fuse** button (top right), open **Credentials**, and set your
host, email and password. Alternatively provide `SPARK_HOST`, `SPARK_EMAIL` and
`SPARK_PASSWORD` in the environment. Set the **assets ShareSync path** (the folder
that holds your model subfolders, for example `/comfy-flux2-klein/models`), choose
a GPU, optionally set a **batch count** (see below), then **Save settings**. The
runner image tracks the published `:latest` build, so image updates reach you
automatically; image affinity still resolves it to a specific digest at submit time.

## Use

1. Build or open a workflow as usual, with a Save Image node at the end.
2. Click **⚡ Spark Fuse**, choose a GPU (the hourly rate appears beside it), then
   click **Render on Spark Fuse**.
3. Watch progress in the panel. The image appears when the job finishes and is also
   saved in ComfyUI's output folder.

## Batch render

Set **Batch count** to render several images from one job. The job pays the cold
start and loads the model once, then renders that many images in sequence, giving
each a fresh seed so they differ. Because the renders run one after another rather
than as a single large batch, VRAM use stays at one image's worth, and every image
after the first costs only its sampling time rather than another full cold start.
All the images are downloaded into ComfyUI's output folder, numbered so they do not
overwrite earlier renders. The count is limited to between 1 and 100.

## Render queue

The queue runs several different workflows back to back on one warm instance.
Spark Fuse pre-warms a single instance, every queued job runs on it with no cold
start or image pull between jobs, and the instance is released when the queue ends.

1. Open a workflow, set its **Batch count**, and click **Add to queue**. Repeat for
   each workflow: open the next one, set its batch count, add it. Each item snapshots
   the graph as it is when you add it.
2. Click **Run queue**. The panel prepares the instance, then runs each item in turn,
   downloading its images as it finishes and showing live progress. Per-item status
   (queued / running / succeeded / failed) is shown in the list.
3. **Cancel queue** stops after the current job and releases the instance.

All queued workflows draw their models from the one shared library mounted at
`/assets`, so sync every model any of them use into that single ShareSync folder,
mirroring your local folder structure. The prepared instance is billed for the whole
session, including the short gaps between jobs, so the queue is most economical run
back to back rather than left idle. Each job is a fresh container that reloads its
model from the node's local cache (fast, no network); batching within a single
workflow still amortises model load best, while the queue removes provisioning and
image pull between different workflows.

## Notes

- The button and panel are deliberately a floating overlay so they work across
  ComfyUI menu versions. If they do not appear, check the browser console and the
  ComfyUI log.
- Credentials, when set in the panel, are stored in `spark_fuse_settings.json`
  next to the extension. That file is gitignored. Prefer environment variables for
  shared machines.

## License

[MIT](LICENSE), Copyright (c) 2026 VFXGuru.
