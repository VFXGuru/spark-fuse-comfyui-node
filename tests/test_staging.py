"""Unit tests for the input-file staging helpers in spark_fuse_bridge.jobs.

_collect_input_files is pure (no filesystem); all tests for it run without
tmp_path. _stage_input_files is filesystem-bound; those tests use tmp_path
and patch sys.modules so folder_paths does not need a live ComfyUI install.
"""
import logging
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# spark_fuse_bridge.jobs defers `import folder_paths` to function-call time, so
# the module imports cleanly here even without ComfyUI present.
from spark_fuse_bridge.jobs import _collect_input_files, _stage_input_files


# ── helpers ─────────────────────────────────────────────────────────────────

def _wf(*nodes):
    """Build a minimal API-format prompt from (node_id, class_type, inputs) triples."""
    return {nid: {"class_type": ct, "inputs": inp} for nid, ct, inp in nodes}


def _fake_fp(input_dir: Path) -> MagicMock:
    fp = MagicMock()
    fp.get_input_directory.return_value = str(input_dir)
    return fp


# ── _collect_input_files — pure, zero filesystem ─────────────────────────────

def test_bare_filename():
    result = _collect_input_files(
        _wf(("1", "LoadImage", {"image": "photo.png"})),
        Path("/fake/input"),
    )
    assert len(result) == 1
    sf = result[0]
    assert sf.node_id == "1"
    assert sf.class_type == "LoadImage"
    assert sf.field == "image"
    assert sf.src == Path("/fake/input/photo.png")
    assert sf.dest_rel == "photo.png"


def test_subfolder_preserved():
    result = _collect_input_files(
        _wf(("5", "LoadImageMask", {"image": "subdir/mask.png"})),
        Path("/fake/input"),
    )
    assert len(result) == 1
    assert result[0].src == Path("/fake/input/subdir/mask.png")
    assert result[0].dest_rel == "subdir/mask.png"


def test_node_link_skipped():
    # A list value is a node link — must not appear in results.
    assert _collect_input_files(
        _wf(("3", "LoadImage", {"image": ["12", 0]})),
        Path("/fake/input"),
    ) == []


def test_output_annotation_skipped_with_warning(caplog):
    with caplog.at_level(logging.WARNING, logger="spark_fuse_bridge.jobs"):
        result = _collect_input_files(
            _wf(("7", "LoadImage", {"image": "result.png [output]"})),
            Path("/fake/input"),
        )
    assert result == []
    assert caplog.records, "expected a warning log for [output] annotation"


def test_temp_annotation_skipped_with_warning(caplog):
    with caplog.at_level(logging.WARNING, logger="spark_fuse_bridge.jobs"):
        result = _collect_input_files(
            _wf(("8", "LoadImage", {"image": "tmp.png [temp]"})),
            Path("/fake/input"),
        )
    assert result == []
    assert caplog.records, "expected a warning log for [temp] annotation"


def test_input_annotation_stripped():
    result = _collect_input_files(
        _wf(("2", "LoadImage", {"image": "photo.png [input]"})),
        Path("/fake/input"),
    )
    assert len(result) == 1
    assert result[0].src == Path("/fake/input/photo.png")
    assert result[0].dest_rel == "photo.png"


def test_dedupes_same_file():
    # Two nodes referencing the same file → only one planned copy.
    result = _collect_input_files(
        _wf(
            ("1", "LoadImage",     {"image": "photo.png"}),
            ("2", "LoadImageMask", {"image": "photo.png"}),
        ),
        Path("/fake/input"),
    )
    assert len(result) == 1


def test_no_matching_nodes():
    assert _collect_input_files(
        _wf(("9", "KSampler", {"seed": 42})),
        Path("/fake/input"),
    ) == []


# ── _stage_input_files — filesystem ─────────────────────────────────────────

def test_missing_file_warns_does_not_raise(tmp_path, caplog):
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    staging = tmp_path / "staging"
    staging.mkdir()
    wf = _wf(("1", "LoadImage", {"image": "missing.png"}))
    with patch.dict(sys.modules, {"folder_paths": _fake_fp(input_dir)}):
        with caplog.at_level(logging.WARNING, logger="spark_fuse_bridge.jobs"):
            _stage_input_files(wf, staging)  # must not raise
    assert not (staging / "missing.png").exists()
    assert caplog.records, "expected a warning for the missing file"


def test_copies_bare_file(tmp_path):
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    (input_dir / "photo.png").write_bytes(b"\x89PNG")
    staging = tmp_path / "staging"
    staging.mkdir()
    wf = _wf(("1", "LoadImage", {"image": "photo.png"}))
    with patch.dict(sys.modules, {"folder_paths": _fake_fp(input_dir)}):
        _stage_input_files(wf, staging)
    assert (staging / "photo.png").read_bytes() == b"\x89PNG"


def test_copies_subfolder_and_creates_parents(tmp_path):
    input_dir = tmp_path / "input"
    (input_dir / "sub").mkdir(parents=True)
    (input_dir / "sub" / "mask.png").write_bytes(b"\x89PNG")
    staging = tmp_path / "staging"
    staging.mkdir()
    wf = _wf(("2", "LoadImageMask", {"image": "sub/mask.png"}))
    with patch.dict(sys.modules, {"folder_paths": _fake_fp(input_dir)}):
        _stage_input_files(wf, staging)
    assert (staging / "sub" / "mask.png").read_bytes() == b"\x89PNG"
