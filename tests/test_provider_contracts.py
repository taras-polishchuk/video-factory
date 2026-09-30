"""Renderer contract conformance tests.

Every adapter in the registry must satisfy the Renderer protocol:
  capabilities / estimate / submit / status / fetch_outputs / cancel
"""

from __future__ import annotations

import pytest

from render_plane.provider import JobState, OutputKind
from render_plane.registry import default_registry


@pytest.fixture
def reg():
    return default_registry()


def test_registry_has_all_profiles(reg):
    expected = {"mock", "heygen_direct", "comfy_cloud", "comfy_gpu_worker", "comfy_heygen_partner"}
    assert set(reg.known_profiles()) >= expected


def test_capabilities_shape(reg):
    for profile in reg.known_profiles():
        r = reg.resolve(profile)
        caps = r.capabilities()
        assert caps.profile == profile
        assert isinstance(caps.output_kinds, list)
        assert all(isinstance(k, OutputKind) for k in caps.output_kinds)


def test_estimate_shape(reg):
    for profile in reg.known_profiles():
        r = reg.resolve(profile)
        est = r.estimate(pack={"scenes": [{"scene_id": "sc_a"}]})
        # amount_usd is Optional[float]; for unknown it is None.
        assert est.currency == "USD"


def test_live_adapters_disabled_without_live_flag(reg, monkeypatch):
    monkeypatch.delenv("LIVE_PROVIDER_TESTS", raising=False)
    monkeypatch.delenv("MAX_COST_PER_BATCH_USD", raising=False)
    monkeypatch.setenv("LIVE_PROVIDER_TESTS", "false")

    for profile in ["heygen_direct", "comfy_cloud", "comfy_gpu_worker"]:
        r = reg.resolve(profile)
        with pytest.raises(Exception):
            r.submit(pack={"scenes": []}, idempotency_key="k")
        # The exception class can be ProviderDisabled or ProviderError;
        # we just assert the submit does NOT silently succeed.
        try:
            r.submit(pack={"scenes": []}, idempotency_key="k")
        except Exception as e:
            # Specifically: it must NOT return a real provider_job_id.
            assert "true" not in str(e).lower() or "live" in str(e).lower()


def test_mock_status_and_outputs(reg):
    r = reg.resolve("mock")
    submit = r.submit(pack={"scenes": [{"scene_id": "sc_a"}], "render_capabilities": {"output_kind": "scene_clips"}}, idempotency_key="k")
    assert submit.provider_job_id.startswith("mockjob_")
    status = r.status(provider_job_id=submit.provider_job_id)
    assert status.state in {JobState.PROCESSING, JobState.COMPLETED}
    # Idempotency replay: same key returns same job
    submit2 = r.submit(pack={"scenes": [{"scene_id": "sc_a"}], "render_capabilities": {"output_kind": "scene_clips"}}, idempotency_key="k")
    assert submit2.provider_job_id == submit.provider_job_id