"""Unit tests for spark_fuse_bridge.model_sync.

_collect_model_refs is pure (no filesystem). check_models and the uploads
registry are exercised with a fake folder_paths module (sys.modules patch, as
in test_staging.py) and a mocked messenger client, so no ComfyUI install and
no network are needed. _run_upload is called synchronously — start_upload's
thread wrapper is trivial and untested here.
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest

from spark_fuse_bridge import model_sync
from spark_fuse_bridge.model_sync import (
    CLOUD_MODEL_SUBDIRS,
    MODEL_FILE_NODES,
    UploadCancelled,
    _collect_model_refs,
    _register_upload,
    _run_upload,
    _upload_guard_bytes,
    check_models,
    get_upload_state,
    prepare_upload_files,
)

SETTINGS = {
    "assets_share_sync_path": "/comfy-flux2-klein/models",
    "assets_share_sync_space_name": "",
    "upload_guard_gb": 50,
}


@pytest.fixture(autouse=True)
def _clean_registry():
    """The uploads registry is module state; isolate every test."""
    with model_sync._UPLOAD_LOCK:
        model_sync._UPLOADS.clear()
    yield
    with model_sync._UPLOAD_LOCK:
        model_sync._UPLOADS.clear()


def _wf(*nodes):
    """Build a minimal API-format prompt from (node_id, class_type, inputs) triples."""
    return {nid: {"class_type": ct, "inputs": inp} for nid, ct, inp in nodes}


def _fake_fp(paths: dict[tuple[str, str], Path | None]) -> MagicMock:
    """folder_paths stand-in: get_full_path looks up (folder, name)."""
    fp = MagicMock()
    fp.get_full_path.side_effect = (
        lambda folder, name: str(p) if (p := paths.get((folder, name))) else None
    )
    return fp


# ── the map itself ───────────────────────────────────────────────────────────

def test_map_folders_stay_within_cloud_subdirs():
    # A map entry pointing at a folder the runner image does not register would
    # upload to a location cloud ComfyUI never searches. CLOUD_MODEL_SUBDIRS is
    # the hand-maintained copy of the runner's MODEL_SUBDIRS.
    for class_type, fields in MODEL_FILE_NODES.items():
        for field, folder in fields.items():
            assert folder in CLOUD_MODEL_SUBDIRS, (
                f"{class_type}.{field} -> {folder!r} is not a cloud model subdir"
            )


# ── _collect_model_refs — pure, zero filesystem ──────────────────────────────

def test_collects_checkpoint_ref():
    refs = _collect_model_refs(
        _wf(("1", "CheckpointLoaderSimple", {"ckpt_name": "sd_xl_base_1.0.safetensors"})))
    assert len(refs) == 1
    assert refs[0].folder == "checkpoints"
    assert refs[0].name == "sd_xl_base_1.0.safetensors"
    assert refs[0].node_id == "1"


def test_node_link_skipped():
    assert _collect_model_refs(
        _wf(("1", "LoraLoader", {"lora_name": ["12", 0]}))) == []


def test_non_file_value_skipped():
    # VAELoader offers special non-file entries such as 'pixel_space' / taesd
    # names; anything without a model extension must be ignored.
    assert _collect_model_refs(
        _wf(("1", "VAELoader", {"vae_name": "pixel_space"}))) == []


def test_backslashes_normalized():
    refs = _collect_model_refs(
        _wf(("1", "UNETLoader", {"unet_name": "FLUX2\\flux2-dev.safetensors"})))
    assert refs[0].name == "FLUX2/flux2-dev.safetensors"
    assert refs[0].folder == "diffusion_models"


def test_dedupes_same_model_across_nodes_and_fields():
    refs = _collect_model_refs(_wf(
        ("1", "DualCLIPLoader", {"clip_name1": "clip_l.safetensors",
                                 "clip_name2": "clip_l.safetensors"}),
        ("2", "CLIPLoader", {"clip_name": "clip_l.safetensors"}),
    ))
    assert len(refs) == 1
    assert refs[0].folder == "text_encoders"


def test_unmapped_class_type_ignored():
    assert _collect_model_refs(
        _wf(("9", "KSampler", {"seed": 42}))) == []


def test_gguf_loader_uses_canonical_folder():
    # ComfyUI-GGUF resolves via legacy keys ("unet"/"clip"); the map targets the
    # canonical folders so uploads match the live share layout.
    refs = _collect_model_refs(
        _wf(("1", "UnetLoaderGGUF", {"unet_name": "flux2-Q8_0.gguf"})))
    assert refs[0].folder == "diffusion_models"


# ── _upload_guard_bytes ──────────────────────────────────────────────────────

def test_guard_from_settings_gb():
    assert _upload_guard_bytes({"upload_guard_gb": 50}) == 50 * 1024**3


def test_guard_zero_disables():
    assert _upload_guard_bytes({"upload_guard_gb": 0}) is None


def test_guard_garbage_falls_back_to_default():
    from spark_fuse.sharesync import DEFAULT_MAX_PUT_BYTES
    assert _upload_guard_bytes({"upload_guard_gb": "lots"}) == DEFAULT_MAX_PUT_BYTES


# ── check_models classification ──────────────────────────────────────────────

def _stat_map(client: MagicMock, sizes: dict[str, int | None]) -> None:
    """sharesync_stat -> ShareSyncEntry-like (only content_length is read) or None."""
    def stat(base, remote_rel):
        if remote_rel not in sizes:
            return None
        entry = MagicMock()
        entry.content_length = sizes[remote_rel]
        return entry
    client.sharesync_stat.side_effect = stat


def _run_check(workflows, fp, sizes, settings=SETTINGS):
    client = MagicMock()
    client.sharesync_dav_base.return_value = "https://h/dav/spaces/s1"
    _stat_map(client, sizes)
    with patch.dict(sys.modules, {"folder_paths": fp}):
        with patch.object(model_sync, "make_client", return_value=client):
            return check_models(workflows, settings)


def test_present_when_name_and_size_match(tmp_path):
    local = tmp_path / "ae.safetensors"
    local.write_bytes(b"\x00" * 100)
    fp = _fake_fp({("vae", "ae.safetensors"): local})
    result = _run_check(
        [_wf(("1", "VAELoader", {"vae_name": "ae.safetensors"}))],
        fp,
        {"/comfy-flux2-klein/models/vae/ae.safetensors": 100},
    )
    assert [m["name"] for m in result["present"]] == ["ae.safetensors"]
    assert result["uploadable"] == [] and result["missing"] == []


def test_present_when_remote_only():
    # No local copy: name existence alone suffices (nothing to compare).
    fp = _fake_fp({})
    result = _run_check(
        [_wf(("1", "VAELoader", {"vae_name": "ae.safetensors"}))],
        fp,
        {"/comfy-flux2-klein/models/vae/ae.safetensors": 999},
    )
    assert len(result["present"]) == 1
    assert result["missing"] == []


def test_uploadable_when_local_only(tmp_path):
    local = tmp_path / "style.safetensors"
    local.write_bytes(b"\x00" * 64)
    fp = _fake_fp({("loras", "style.safetensors"): local})
    result = _run_check(
        [_wf(("1", "LoraLoader", {"lora_name": "style.safetensors"}))],
        fp, {},
    )
    item = result["uploadable"][0]
    assert item["name"] == "style.safetensors"
    assert item["size"] == 64
    assert item["size_mismatch"] is False
    assert item["remote_rel"] == "/comfy-flux2-klein/models/loras/style.safetensors"


def test_size_mismatch_is_uploadable_with_flag(tmp_path):
    local = tmp_path / "ae.safetensors"
    local.write_bytes(b"\x00" * 100)
    fp = _fake_fp({("vae", "ae.safetensors"): local})
    result = _run_check(
        [_wf(("1", "VAELoader", {"vae_name": "ae.safetensors"}))],
        fp,
        {"/comfy-flux2-klein/models/vae/ae.safetensors": 50},  # differs from 100
    )
    assert result["present"] == []
    assert result["uploadable"][0]["size_mismatch"] is True


def test_over_limit_when_local_exceeds_guard(tmp_path):
    local = tmp_path / "huge.safetensors"
    local.write_bytes(b"\x00" * 4096)
    fp = _fake_fp({("checkpoints", "huge.safetensors"): local})
    tiny_guard = dict(SETTINGS, upload_guard_gb=1024 / 1024**3)  # 1 KiB guard
    result = _run_check(
        [_wf(("1", "CheckpointLoaderSimple", {"ckpt_name": "huge.safetensors"}))],
        fp, {}, settings=tiny_guard,
    )
    assert result["uploadable"] == []
    item = result["over_limit"][0]
    assert item["size"] == 4096
    assert item["guard_bytes"] == 1024


def test_missing_both_places():
    fp = _fake_fp({})
    result = _run_check(
        [_wf(("1", "CheckpointLoaderSimple", {"ckpt_name": "nowhere.safetensors"}))],
        fp, {},
    )
    assert [m["name"] for m in result["missing"]] == ["nowhere.safetensors"]


def test_in_flight_upload_reported_as_uploading(tmp_path):
    local = tmp_path / "style.safetensors"
    local.write_bytes(b"\x00" * 64)
    _register_upload([{
        "folder": "loras", "name": "style.safetensors",
        "local_path": str(local), "remote_rel": "/x/loras/style.safetensors",
        "size": 64,
    }])
    fp = _fake_fp({("loras", "style.safetensors"): local})
    result = _run_check(
        [_wf(("1", "LoraLoader", {"lora_name": "style.safetensors"}))],
        fp, {},
    )
    assert len(result["uploading"]) == 1
    assert result["uploadable"] == []


def test_union_deduped_across_queue_workflows(tmp_path):
    local = tmp_path / "ae.safetensors"
    local.write_bytes(b"\x00" * 8)
    fp = _fake_fp({("vae", "ae.safetensors"): local})
    wf = lambda: _wf(("1", "VAELoader", {"vae_name": "ae.safetensors"}))  # noqa: E731
    result = _run_check([wf(), wf(), wf()], fp, {})
    assert len(result["uploadable"]) == 1


# ── prepare_upload_files ─────────────────────────────────────────────────────

def test_prepare_resolves_server_side(tmp_path):
    local = tmp_path / "style.safetensors"
    local.write_bytes(b"\x00" * 32)
    fp = _fake_fp({("loras", "style.safetensors"): local})
    with patch.dict(sys.modules, {"folder_paths": fp}):
        prepared = prepare_upload_files(
            [{"folder": "loras", "name": "style.safetensors"}], SETTINGS)
    assert prepared == [{
        "folder": "loras",
        "name": "style.safetensors",
        "local_path": str(local),
        "remote_rel": "/comfy-flux2-klein/models/loras/style.safetensors",
        "size": 32,
    }]


def test_prepare_rejects_unknown_folder():
    with pytest.raises(ValueError, match="Invalid upload request"):
        prepare_upload_files([{"folder": "evil", "name": "x.safetensors"}], SETTINGS)


def test_prepare_rejects_path_traversal():
    with pytest.raises(ValueError, match="Invalid upload request"):
        prepare_upload_files([{"folder": "loras", "name": "../secrets.bin"}], SETTINGS)


def test_prepare_rejects_locally_missing_file():
    fp = _fake_fp({})
    with patch.dict(sys.modules, {"folder_paths": fp}):
        with pytest.raises(ValueError, match="not found locally"):
            prepare_upload_files([{"folder": "loras", "name": "gone.safetensors"}], SETTINGS)


# ── the uploads registry worker ──────────────────────────────────────────────

def _file_entry(tmp_path, name="m.safetensors", size=100) -> dict:
    local = tmp_path / name
    local.write_bytes(b"\x00" * size)
    return {"folder": "loras", "name": name, "local_path": str(local),
            "remote_rel": f"/x/loras/{name}", "size": size}


def _worker_client(upload_side_effect) -> MagicMock:
    client = MagicMock()
    client.sharesync_dav_base.return_value = "https://h/dav/spaces/s1"
    client.upload_file.side_effect = upload_side_effect
    return client


def test_run_upload_success(tmp_path):
    f = _file_entry(tmp_path)
    upload_id = _register_upload([f])

    def fake_upload(local_path, base, remote_rel, *, progress=None, max_bytes=None):
        progress(0, 100)
        progress(100, 100)

    client = _worker_client(fake_upload)
    with patch.object(model_sync, "make_client", return_value=client):
        _run_upload(upload_id, SETTINGS)

    state = get_upload_state(upload_id)
    assert state["status"] == "done"
    assert state["files"][0]["status"] == "done"
    assert state["files"][0]["sent"] == 100


def test_run_upload_retries_once_on_transport_error(tmp_path):
    f = _file_entry(tmp_path)
    upload_id = _register_upload([f])
    calls = {"n": 0}

    def flaky_upload(local_path, base, remote_rel, *, progress=None, max_bytes=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("boom")
        progress(100, 100)

    client = _worker_client(flaky_upload)
    with patch.object(model_sync, "make_client", return_value=client):
        _run_upload(upload_id, SETTINGS)

    assert calls["n"] == 2
    assert get_upload_state(upload_id)["status"] == "done"


def test_run_upload_transport_error_twice_fails(tmp_path):
    f = _file_entry(tmp_path)
    upload_id = _register_upload([f])

    def always_fails(*args, **kwargs):
        raise httpx.ConnectError("still down")

    client = _worker_client(always_fails)
    with patch.object(model_sync, "make_client", return_value=client):
        _run_upload(upload_id, SETTINGS)

    state = get_upload_state(upload_id)
    assert state["status"] == "failed"
    assert "still down" in state["error"]
    assert state["files"][0]["status"] == "failed"


def test_run_upload_cancel_mid_transfer(tmp_path):
    f = _file_entry(tmp_path)
    upload_id = _register_upload([f])

    def fake_upload(local_path, base, remote_rel, *, progress=None, max_bytes=None):
        progress(0, 100)                       # fine — not cancelled yet
        model_sync.cancel_upload(upload_id)    # user clicks ✕ on the badge
        progress(50, 100)                      # progress callback must raise

    client = _worker_client(fake_upload)
    with patch.object(model_sync, "make_client", return_value=client):
        _run_upload(upload_id, SETTINGS)

    state = get_upload_state(upload_id)
    assert state["status"] == "cancelled"
    assert state["files"][0]["status"] == "cancelled"
    # A cancel must never be retried as if it were a transport error.
    assert client.upload_file.call_count == 1


def test_run_upload_cancel_before_start_skips_all(tmp_path):
    f1 = _file_entry(tmp_path, "a.safetensors")
    f2 = _file_entry(tmp_path, "b.safetensors")
    upload_id = _register_upload([f1, f2])
    model_sync.cancel_upload(upload_id)

    client = _worker_client(lambda *a, **k: pytest.fail("must not upload after cancel"))
    with patch.object(model_sync, "make_client", return_value=client):
        _run_upload(upload_id, SETTINGS)

    state = get_upload_state(upload_id)
    assert state["status"] == "cancelled"
    assert all(fs["status"] == "cancelled" for fs in state["files"])


def test_run_upload_sharesync_error_fails_without_retry(tmp_path):
    from spark_fuse.errors import ShareSyncError
    f = _file_entry(tmp_path)
    upload_id = _register_upload([f])

    def http_error(*args, **kwargs):
        raise ShareSyncError("PUT /x returned HTTP 507")

    client = _worker_client(http_error)
    with patch.object(model_sync, "make_client", return_value=client):
        _run_upload(upload_id, SETTINGS)

    assert get_upload_state(upload_id)["status"] == "failed"
    assert client.upload_file.call_count == 1  # only transport errors retry
