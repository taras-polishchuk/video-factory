"""hyperframes_local adapter: contract + real local render (skipped without the CLI)."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from control_plane.errors import ProviderError
from control_plane.storage import LocalDiskStorage
from render_plane.hyperframes_local import HyperFramesLocalAdapter, hyperframes_binary
from render_plane.provider import JobState, OutputKind
from render_plane.registry import default_registry


def test_registered_free_and_secretless():
    r = default_registry().resolve("hyperframes_local")
    caps = r.capabilities()
    assert caps.output_kinds == [OutputKind.WHOLE_VIDEO]
    assert caps.requires_secret is False
    assert "9:16" in caps.aspect_ratios and "16:9" in caps.aspect_ratios
    assert r.estimate(pack={}).amount_usd == 0.0


def test_project_dir_outside_allowed_roots_refused(tmp_path, monkeypatch):
    monkeypatch.delenv("VIDEO_FACTORY_HYPERFRAMES_ROOTS", raising=False)
    if not hyperframes_binary():
        monkeypatch.setenv("VIDEO_FACTORY_HYPERFRAMES_BIN", "/bin/true")
    r = HyperFramesLocalAdapter(storage=LocalDiskStorage(str(tmp_path / "store")))
    with pytest.raises(ProviderError, match="outside allowed roots"):
        r.submit(pack={"hyperframes": {"project_dir": "/etc"}}, idempotency_key="k-escape")


def test_bad_quality_refused(tmp_path, monkeypatch):
    if not hyperframes_binary():
        monkeypatch.setenv("VIDEO_FACTORY_HYPERFRAMES_BIN", "/bin/true")
    r = HyperFramesLocalAdapter(storage=LocalDiskStorage(str(tmp_path)))
    with pytest.raises(ProviderError):
        r.submit(pack={"hyperframes": {"quality": "ultra"}}, idempotency_key="k-q")


@pytest.mark.skipif(not hyperframes_binary() or not shutil.which("ffprobe"),
                    reason="hyperframes CLI / ffprobe not installed")
def test_real_vertical_render(tmp_path):
    r = HyperFramesLocalAdapter(storage=LocalDiskStorage(str(tmp_path)))
    pack = {"pack_id": "vp_hf_test", "aspect_ratio": "9:16",
            "scenes": [{"scene_id": "sc_1", "on_screen_text": "Regression render"}],
            "hyperframes": {"quality": "draft", "variables": {"cta": "Test CTA"}}}
    sub = r.submit(pack=pack, idempotency_key="k-real")
    assert r.submit(pack=pack, idempotency_key="k-real").provider_job_id == sub.provider_job_id
    st = r.wait(sub.provider_job_id, timeout=600)
    assert st.state == JobState.COMPLETED, st.error
    out = st.outputs[0]
    assert (out.width, out.height) == (1080, 1920)
    assert 5.5 <= (out.duration_seconds or 0) <= 6.5
    path = tmp_path / out.ref
    probe = json.loads(subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_name", "-of", "json", str(path)],
        capture_output=True, text=True, check=True).stdout)
    assert probe["streams"][0]["codec_name"] == "h264"
