"""get_skus tolerates a messenger whose list_skus has no gpu_only parameter.

routes.py imports ComfyUI's `server` at module level, so a stub stands in for
it here; no ComfyUI install is needed.
"""
from __future__ import annotations

import asyncio
import json
import sys
import types
from unittest.mock import MagicMock, patch

import pytest

GPU = {"instanceType": "g7e.2xlarge", "gpuType": "RTX PRO 6000", "gpuMemoryGb": 96,
       "coreCount": 8, "memoryGb": 64}
CPU = {"instanceType": "c5.large", "gpuType": None}


@pytest.fixture
def routes_mod():
    stub = types.ModuleType("server")
    passthrough = MagicMock()
    passthrough.get.return_value = passthrough.post.return_value = lambda f: f
    stub.PromptServer = MagicMock(instance=MagicMock(routes=passthrough))
    with patch.dict(sys.modules, {"server": stub}):
        sys.modules.pop("spark_fuse_bridge.routes", None)
        from spark_fuse_bridge import routes
        yield routes
    sys.modules.pop("spark_fuse_bridge.routes", None)


def _get_skus(routes, client):
    with patch.object(routes.config, "make_client", return_value=client), \
            patch.object(routes.jobs, "_close"):
        resp = asyncio.run(routes.get_skus(None))
    return json.loads(resp.text)


def test_uses_gpu_only_when_supported(routes_mod):
    client = MagicMock()
    client.list_skus.return_value = [GPU]
    assert _get_skus(routes_mod, client)["skus"][0]["instanceType"] == "g7e.2xlarge"
    client.list_skus.assert_called_once_with(gpu_only=True)


def test_falls_back_when_messenger_rejects_gpu_only(routes_mod):
    class OldClient:
        def login(self): pass
        def list_skus(self):  # messenger 0.5.0 signature
            return [GPU, CPU]

    out = _get_skus(routes_mod, OldClient())
    assert [s["instanceType"] for s in out["skus"]] == ["g7e.2xlarge"]
