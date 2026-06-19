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
2. Install the dependency into ComfyUI's Python (this pulls the messenger client
   from GitHub):
   ```
   pip install -r requirements.txt
   ```
3. Restart ComfyUI.

## Configure

Click the **⚡ Spark Fuse** button (top right), open **Credentials**, and set your
host, email and password. Alternatively provide `SPARK_HOST`, `SPARK_EMAIL` and
`SPARK_PASSWORD` in the environment. Set the **assets ShareSync path** (the folder
that holds your model subfolders, for example `/comfy-flux2-klein/models`), choose
a GPU, then **Save settings**. The image is digest-pinned in the settings file and
can be updated when you publish a new build.

## Use

1. Build or open a workflow as usual, with a Save Image node at the end.
2. Click **⚡ Spark Fuse**, choose a GPU (the hourly rate appears beside it), then
   click **Render on Spark Fuse**.
3. Watch progress in the panel. The image appears when the job finishes and is also
   saved in ComfyUI's output folder.

## Notes

- The button and panel are deliberately a floating overlay so they work across
  ComfyUI menu versions. If they do not appear, check the browser console and the
  ComfyUI log.
- Credentials, when set in the panel, are stored in `spark_fuse_settings.json`
  next to the extension. That file is gitignored. Prefer environment variables for
  shared machines.

## License

[MIT](LICENSE), Copyright (c) 2026 VFXGuru.
