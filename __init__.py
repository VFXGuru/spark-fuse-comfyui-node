"""Spark Fuse bridge for ComfyUI.

A ComfyUI extension that offloads the current workflow to a Spark Fuse cloud GPU
and brings the rendered image back into ComfyUI. Importing this package registers
the bridge's HTTP routes on ComfyUI's server; the web/ directory adds the UI.

This is an extension, not a graph node: there are no NODE_CLASS_MAPPINGS entries.
The whole workflow runs in the cloud, so a "Render on Spark Fuse" button ships the
current graph to Spark Fuse rather than executing it locally.
"""
if __package__:  # relative import only works inside a package (i.e. loaded by ComfyUI)
    from .spark_fuse_bridge import routes  # noqa: F401  (import registers the routes)

NODE_CLASS_MAPPINGS = {}
NODE_DISPLAY_NAME_MAPPINGS = {}
WEB_DIRECTORY = "./web"

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]
