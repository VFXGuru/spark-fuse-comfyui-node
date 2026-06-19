"""Settings and Spark Fuse client construction for the bridge.

Settings live in spark_fuse_settings.json next to this package (gitignored, since
it can hold credentials). Credentials fall back to the SPARK_HOST / SPARK_EMAIL /
SPARK_PASSWORD environment variables when not set in the panel.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from spark_fuse import SparkFuseClient

_ROOT = Path(__file__).resolve().parent.parent
SETTINGS_PATH = _ROOT / "spark_fuse_settings.json"

# The pinned, digest-referenced image is the most reliable for cached-image
# affinity. Update this when you publish a new image build.
DEFAULTS = {
    "image": "ghcr.io/vfxguru/spark-fuse-comfyui@sha256:08530e3e48924017bd6242b1ff6051fd5f0e81848e20d90745605c071182602d",
    "command": ["python3.13", "/runner/spark_fuse_run.py"],
    "instance_type": "g7e.2xlarge",
    "assets_share_sync_path": "/comfy-flux2-klein/models",
    "assets_share_sync_space_name": "",
    "model_base_dir": "/assets",
    "image_affinity": "required",
    # Credentials (optional here; env vars are the fallback)
    "host": "",
    "email": "",
    "password": "",
}

# Keys that are safe to send back to the browser (never the password).
PUBLIC_KEYS = [
    "image", "instance_type", "assets_share_sync_path",
    "assets_share_sync_space_name", "model_base_dir", "image_affinity",
    "host", "email",
]


def load_settings() -> dict:
    data = dict(DEFAULTS)
    if SETTINGS_PATH.is_file():
        try:
            data.update(json.loads(SETTINGS_PATH.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass
    return data


def save_settings(values: dict) -> dict:
    current = load_settings()
    for key in DEFAULTS:
        if key in values and values[key] is not None:
            current[key] = values[key]
    SETTINGS_PATH.write_text(json.dumps(current, indent=2), encoding="utf-8")
    return current


def public_settings() -> dict:
    s = load_settings()
    out = {k: s.get(k, "") for k in PUBLIC_KEYS}
    out["password_set"] = bool(s.get("password") or os.environ.get("SPARK_PASSWORD"))
    return out


def _credentials(settings: dict) -> tuple[str, str, str]:
    host = settings.get("host") or os.environ.get("SPARK_HOST", "")
    email = settings.get("email") or os.environ.get("SPARK_EMAIL", "")
    password = settings.get("password") or os.environ.get("SPARK_PASSWORD", "")
    return host, email, password


def make_client(settings: dict | None = None) -> SparkFuseClient:
    settings = settings or load_settings()
    host, email, password = _credentials(settings)
    if not (host and email and password):
        raise RuntimeError(
            "Spark Fuse credentials are missing. Set them in the Spark Fuse panel, "
            "or provide SPARK_HOST, SPARK_EMAIL and SPARK_PASSWORD in the environment."
        )
    return SparkFuseClient(host=host, email=email, password=password)
