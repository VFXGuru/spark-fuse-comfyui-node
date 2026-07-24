"""Settings and Spark Fuse client construction for the bridge.

Settings live outside the installed node folder, under ComfyUI's per-installation
user directory (see _settings_path), so they survive a Manager uninstall or a
delete-and-reinstall. Credentials fall back to the SPARK_HOST / SPARK_EMAIL /
SPARK_PASSWORD environment variables when not set in the panel.

Known limitation: the settings file still stores email and password in plain
text. The new location is not exposed over HTTP, which is no worse than the
old in-folder location, but real encryption at rest is a future improvement.
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

from spark_fuse import SparkFuseClient

_ROOT = Path(__file__).resolve().parent.parent
_LEGACY_SETTINGS_PATH = _ROOT / "spark_fuse_settings.json"

# Cache so _settings_path() resolves (and migrates) only once per process.
_settings_path_cache: Path | None = None

# The production API host is effectively constant for now; default to it so users
# only need to supply their email and password.
DEFAULT_HOST = "https://api.prod.aapse1.sparkcloud.studio"

# image and command are node-version constants (always taken from here, never from
# a stale saved settings file). ':latest' so users get image updates (such as batch
# render) automatically; cached-image affinity still resolves it to a digest at
# submit time.
DEFAULTS = {
    "image": "ghcr.io/vfxguru/spark-fuse-comfyui:latest",
    "command": ["python3.13", "/runner/spark_fuse_run.py"],
    "instance_type": "g7e.2xlarge",
    "assets_share_sync_path": "/comfy-flux2-klein/models",
    "assets_share_sync_space_name": "",
    "model_base_dir": "/assets",
    "image_affinity": "required",
    "batch_count": 1,
    # Pre-render model sync: largest single model the bridge will upload in-node,
    # in GB. A practicality guard against huge non-resumable transfers, not a
    # server limit (ShareSync allows 2 TB per file). 0 disables the guard.
    "upload_guard_gb": 50,
    # Credentials (optional here; env vars are the fallback)
    "host": DEFAULT_HOST,
    "email": "",
    "password": "",
}

# Keys that are safe to send back to the browser (never the password).
PUBLIC_KEYS = [
    "image", "instance_type", "assets_share_sync_path",
    "assets_share_sync_space_name", "model_base_dir", "image_affinity",
    "batch_count", "host", "email", "upload_guard_gb",
]

# Queue chunking: the maximum number of workflows per manifest job (see
# render_queue.py and spark_fuse_run.py's multi-workflow mode). Deliberately
# NOT in DEFAULTS/PUBLIC_KEYS, read the same way as session_affinity below:
# save_settings() writes any key present in DEFAULTS regardless of PUBLIC_KEYS,
# so keeping this out entirely — not just out of PUBLIC_KEYS — is the only way
# to guarantee it can't be changed by a raw POST to /spark_fuse/settings, only
# by hand-editing the settings file. Not a panel control: a user raising it
# risks losing a larger batch's worth of completed work at once if its job is
# killed.
DEFAULT_QUEUE_CHUNK_SIZE = 10


def queue_chunk_size(settings: dict) -> int:
    try:
        return max(1, int(settings.get("queue_chunk_size") or DEFAULT_QUEUE_CHUNK_SIZE))
    except (TypeError, ValueError):
        return DEFAULT_QUEUE_CHUNK_SIZE


def _settings_path() -> Path:
    """Resolve where the settings file lives, migrating a legacy copy on first use.

    Settings used to live at _LEGACY_SETTINGS_PATH, inside the installed node
    folder. That location does not survive a Manager uninstall or a
    delete-and-reinstall, so settings now live under ComfyUI's
    per-installation user directory instead, which no node install, update or
    uninstall path touches.
    """
    global _settings_path_cache
    if _settings_path_cache is not None:
        return _settings_path_cache

    import folder_paths  # ComfyUI-provided; available at runtime

    if hasattr(folder_paths, "get_system_user_directory"):
        # System User directory: internal-only, never exposed through
        # ComfyUI's HTTP /userdata endpoints. Mirrors ComfyUI-Manager's own
        # move from user/default/ComfyUI-Manager to user/__manager.
        settings_dir = Path(folder_paths.get_system_user_directory("spark_fuse_bridge"))
    else:
        # Older ComfyUI without the System User Protection API: fall back to
        # the plain per-user directory, matching Manager's legacy path
        # convention (user/default/<name>).
        settings_dir = Path(folder_paths.get_user_directory()) / "default" / "spark_fuse_bridge"

    settings_dir.mkdir(parents=True, exist_ok=True)
    new_path = settings_dir / "spark_fuse_settings.json"
    _migrate_legacy_settings(new_path)

    _settings_path_cache = new_path
    return new_path


def _migrate_legacy_settings(new_path: Path) -> None:
    """Move a pre-0.2.2 in-folder settings file into place, once.

    If the new location already has a file, it wins and the legacy file is
    left untouched. Otherwise the legacy file is copied to the new location
    and renamed aside (never deleted), so nothing is lost if this goes wrong.
    """
    if new_path.is_file() or not _LEGACY_SETTINGS_PATH.is_file():
        return
    try:
        shutil.copyfile(_LEGACY_SETTINGS_PATH, new_path)
        backup_path = _LEGACY_SETTINGS_PATH.with_name(_LEGACY_SETTINGS_PATH.name + ".migrated")
        _LEGACY_SETTINGS_PATH.rename(backup_path)
    except OSError:
        pass


def load_settings() -> dict:
    data = dict(DEFAULTS)
    settings_path = _settings_path()
    if settings_path.is_file():
        try:
            data.update(json.loads(settings_path.read_text(encoding="utf-8-sig")))
        except (OSError, ValueError):
            pass
    # image/command are node-version constants; never honour a stale saved value.
    data["image"] = DEFAULTS["image"]
    data["command"] = DEFAULTS["command"]
    return data


def save_settings(values: dict) -> dict:
    current = load_settings()
    for key in DEFAULTS:
        if key in values and values[key] is not None:
            current[key] = values[key]
    _settings_path().write_text(json.dumps(current, indent=2), encoding="utf-8")
    return current


def public_settings() -> dict:
    s = load_settings()
    out = {k: s.get(k, "") for k in PUBLIC_KEYS}
    if not out.get("host"):
        out["host"] = DEFAULT_HOST
    out["password_set"] = bool(s.get("password") or os.environ.get("SPARK_PASSWORD"))
    return out


def _credentials(settings: dict) -> tuple[str, str, str]:
    host = settings.get("host") or os.environ.get("SPARK_HOST") or DEFAULT_HOST
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
