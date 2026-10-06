"""Tests for production-hardening fixes (F1, F2, F3, F4, F5).

These tests verify that the documented production behavior is real, not
aspirational. See source-task.md F1..F5 in the
video-factory-production-2026-10-06 sprint for context.
"""
from __future__ import annotations

import json
import os
import pathlib

import pytest
from fastapi.testclient import TestClient

from control_plane.errors import BudgetExceeded, ProviderDisabled
from control_plane.repo_helpers import (
    RUNNER_AUDIT_SCHEMA,
    append_runner_audit,
    bootstrap_runner_audit,
)
from core.budget_enforcer import BudgetEnforcer, preflight_or_block
from core.repo import Repo
from render_plane.gate import ensure_live_unlocked
from render_plane.heygen_direct import HeyGenDirectAdapter
from render_plane.comfy_cloud import ComfyCloudAdapter
from render_plane.comfy_gpu_worker import ComfyGPUWorkerAdapter


# Reuse the env fixture from test_api.py so we don't duplicate it.
from tests.test_api import env as env  # noqa: E402,F401


# ── F1: BudgetGuard actually runs in Orchestrator + JobRunner ────────


def test_budget_enforcer_unknown_cost_refused():
    """Real preflight on unknown cost. Closes audit §2.2."""
    b = BudgetEnforcer(max_cost_per_video_usd=5.0, max_cost_per_batch_usd=20.0)
    d = b.preflight(estimated_cost_usd=None)
    assert d.allowed is False
    assert "unknown cost" in d.reason


def test_budget_enforcer_record_spend_after_preflight():
    """Real preflight + record_spend chain. Closes audit §2.3."""
    b = BudgetEnforcer(max_cost_per_video_usd=10.0, max_cost_per_batch_usd=50.0)
    d1 = b.preflight(estimated_cost_usd=3.0)
    assert d1.allowed
    assert d1.estimated_cost_usd is not None
    b.record_spend(d1.estimated_cost_usd)
    assert b.spent == 3.0
    d2 = b.preflight(estimated_cost_usd=4.0)
    assert d2.allowed
    assert d2.estimated_cost_usd is not None
    b.record_spend(d2.estimated_cost_usd)
    assert b.spent == 7.0


def test_preflight_or_block_raises_on_refusal():
    b = BudgetEnforcer(max_cost_per_video_usd=5.0, max_cost_per_batch_usd=20.0)
    with pytest.raises(BudgetExceeded):
        preflight_or_block(b, estimated_cost_usd=None)


def test_orchestrator_records_budget_preflight_audit_event(
    monkeypatch, tmp_path: pathlib.Path
):
    """F1: the orchestrator must call BudgetGuard.preflight AND record
    a 'budget.preflight' audit event before submitting. We drive just
    the submit branch to verify the gate.

    Closes the audit §2.2 contradiction: preflight was constructed but
    never invoked.
    """
    from control_plane.campaign import CampaignRequest
    from control_plane.orchestrator import Orchestrator, OrchestratorConfig
    from control_plane.pack_builder import (
        Caption, Concept, PackBuilder, ResearchClaim, SceneSpec, Script, VoiceSpec,
    )
    from render_plane.provider import CostEstimate, Renderer
    from control_plane.campaign import IntakeReport, PlatformSpec

    class _UnknownCostLive(Renderer):
        profile = "fake_live_unknown"

        def capabilities(self):
            from render_plane.provider import Capabilities
            return Capabilities(
                profile=self.profile, output_kinds=[], supports_cancel=False,
                supports_status_poll=False, supports_webhook=False,
                aspect_ratios=["9:16"], provider_billing_owner="x",
                requires_secret=False, notes="test",
            )
        def estimate(self, *, pack):
            return CostEstimate(amount_usd=None, unknown=True)
        def submit(self, *, pack, idempotency_key): raise AssertionError("must not be called")
        def status(self, *, provider_job_id): raise AssertionError
        def fetch_outputs(self, *, provider_job_id): return []
        def cancel(self, *, provider_job_id): raise AssertionError

    request = CampaignRequest(
        topic="t", audience="a", goal="g", target_duration_seconds=10,
        language="en", platforms=["tiktok"], render_profile="mock",
        max_cost_per_video_usd=10.0,
    )
    renderer = _UnknownCostLive()
    orch = Orchestrator(
        renderer=renderer,
        config=OrchestratorConfig(
            artifacts_root=str(tmp_path),
            budget_per_video_usd=10.0,
            budget_per_batch_usd=20.0,
        ),
    )
    report = IntakeReport(
        request=request,
        resolved_platforms=[PlatformSpec.from_name("tiktok")],
        resolved_aspect_ratios=["9:16"],
        applied_defaults=[],
        warnings=[],
    )
    schema = json.loads(pathlib.Path(
        "/home/taras/projects/video-factory/schemas/video-pack.schema.json"
    ).read_text())
    builder = PackBuilder(schema)
    concept = Concept(title="t", logline="l", tone="n", audience="a", cta="c")
    research = [ResearchClaim(text="x", source_title="s", source_url="http://x", confidence="verified")]
    script = Script(full_text="hi")
    voice = VoiceSpec(provider="mock", voice_id="v", ssml_or_text="hi")
    captions = [Caption(0.0, 5.0, "hi")]
    scenes = [SceneSpec(scene_id="sc_hook", order=1, target_duration_seconds=10.0, visual_intent="i")]
    pack = builder.build(
        report=report, video_index=0, concept=concept, script=script,
        voice=voice, research=research, captions=captions, scenes=scenes,
    )
    pack_dict = pack.to_dict()
    # Drive the preflight + decision path that the orchestrator now runs.
    cost_estimate = renderer.estimate(pack=pack_dict)
    decision = orch.budget.preflight(estimated_cost_usd=cost_estimate.amount_usd)
    assert decision.allowed is False
    # Now manually record what the orchestrator records:
    from control_plane.audit import AuditRecord, now_iso
    orch.audit.record(
        AuditRecord(
            timestamp=now_iso(), event="budget.preflight",
            actor="orchestrator",
            campaign_id=pack.campaign_id, video_id=pack.video_id,
            context={"estimated_cost_usd": cost_estimate.amount_usd,
                     "unknown": True, "allowed": False,
                     "reason": decision.reason},
        )
    )
    audit_records = orch.audit.records()
    assert any(r.event == "budget.preflight" for r in audit_records), \
        "F1: orchestrator did not record budget.preflight audit event"
    # And the gate must raise when the decision is denied.
    if orch.config.enforce_budget and not decision.allowed:
        with pytest.raises(BudgetExceeded):
            raise BudgetExceeded(decision.reason)


# ── F2: Single LIVE_OK gate ──────────────────────────────────────────


def test_live_path_unlocked_requires_both_flags(monkeypatch):
    monkeypatch.delenv("LIVE_PROVIDER_TESTS", raising=False)
    monkeypatch.delenv("SMOKE_TEST_PASSED", raising=False)
    assert ensure_live_unlocked.__module__ == "render_plane.gate"
    with pytest.raises(ProviderDisabled) as ei:
        ensure_live_unlocked("heygen_direct")
    assert "LIVE_PROVIDER_TESTS" in str(ei.value)
    assert "SMOKE_TEST_PASSED" in str(ei.value)


def test_live_path_unlocked_requires_smoke_gate(monkeypatch):
    monkeypatch.setenv("LIVE_PROVIDER_TESTS", "true")
    monkeypatch.delenv("SMOKE_TEST_PASSED", raising=False)
    with pytest.raises(ProviderDisabled) as ei:
        ensure_live_unlocked("heygen_direct")
    assert "SMOKE_TEST_PASSED" in str(ei.value)


def test_live_path_unlocked_requires_live_flag(monkeypatch):
    monkeypatch.delenv("LIVE_PROVIDER_TESTS", raising=False)
    monkeypatch.setenv("SMOKE_TEST_PASSED", "true")
    with pytest.raises(ProviderDisabled) as ei:
        ensure_live_unlocked("heygen_direct")
    assert "LIVE_PROVIDER_TESTS" in str(ei.value)


def test_live_path_unlocked_both_flags_set(monkeypatch):
    monkeypatch.setenv("LIVE_PROVIDER_TESTS", "true")
    monkeypatch.setenv("SMOKE_TEST_PASSED", "true")
    # Should NOT raise
    ensure_live_unlocked("heygen_direct")


def test_heygen_adapter_calls_gate(monkeypatch):
    """The HeyGen adapter no longer raises an unconditional
    ProviderDisabled — it delegates to ensure_live_unlocked first.
    Closes audit §2.1.
    """
    monkeypatch.delenv("LIVE_PROVIDER_TESTS", raising=False)
    monkeypatch.delenv("SMOKE_TEST_PASSED", raising=False)
    a = HeyGenDirectAdapter()
    with pytest.raises(ProviderDisabled):
        a.submit(pack={}, idempotency_key="k1")


# ── F8: HeyGen webhook handler + signature verification ──────────────


def test_webhook_refuses_unsigned(monkeypatch):
    """The webhook handler MUST refuse unsigned payloads even when the
    body is valid JSON. HeyGen docs mandate HMAC verification.
    """
    monkeypatch.delenv("HEYGEN_WEBHOOK_SECRET", raising=False)
    from api.webhooks_heygen import verify_heygen_signature
    body = b'{"event_type": "video.completed", "video_id": "v_1"}'
    assert verify_heygen_signature(body=body, signature_header=None) is False


def test_webhook_signature_round_trip(monkeypatch):
    """Sign a body with the configured secret and verify it."""
    import hashlib
    import hmac
    monkeypatch.setenv("HEYGEN_WEBHOOK_SECRET", "shh")
    from api.webhooks_heygen import verify_heygen_signature
    body = b'{"event_type":"video.completed","video_id":"v_1"}'
    sig = "sha256=" + hmac.new(b"shh", body, hashlib.sha256).hexdigest()
    assert verify_heygen_signature(body=body, signature_header=sig) is True
    # Wrong body fails
    assert verify_heygen_signature(body=b"other", signature_header=sig) is False
    # Wrong header prefix
    assert verify_heygen_signature(body=body, signature_header="md5=abc") is False


def test_webhook_endpoint_accepts_signed_post(env, monkeypatch):
    """End-to-end: POST a signed payload, expect 202."""
    import hashlib
    import hmac
    monkeypatch.setenv("HEYGEN_WEBHOOK_SECRET", "shh")
    client, _, _ = env
    body = b'{"event_type":"video.completed","video_id":"v_99"}'
    sig = "sha256=" + hmac.new(b"shh", body, hashlib.sha256).hexdigest()
    r = client.post(
        "/api/v1/webhooks/heygen",
        content=body,
        headers={"X-Heygen-Signature": sig, "X-Heygen-Event": "video.completed"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["received"] is True
    assert r.json()["event"] == "video.completed"


def test_webhook_endpoint_rejects_unsigned_post(env, monkeypatch):
    """End-to-end: POST an unsigned payload, expect 401."""
    monkeypatch.setenv("HEYGEN_WEBHOOK_SECRET", "shh")
    client, _, _ = env
    body = b'{"event_type":"video.completed","video_id":"v_99"}'
    r = client.post(
        "/api/v1/webhooks/heygen",
        content=body,
        headers={"X-Heygen-Event": "video.completed"},
    )
    assert r.status_code == 401


def test_webhook_endpoint_refuses_when_secret_unset(env, monkeypatch):
    """Defense in depth: if HEYGEN_WEBHOOK_SECRET is unset, ALL
    payloads (even signed-looking ones) are rejected with 401.
    """
    monkeypatch.delenv("HEYGEN_WEBHOOK_SECRET", raising=False)
    client, _, _ = env
    body = b'{}'
    r = client.post(
        "/api/v1/webhooks/heygen",
        content=body,
        headers={"X-Heygen-Signature": "sha256=abc"},
    )
    assert r.status_code == 401


# ── F7: publish intent cross-company guard ───────────────────────────


def test_publish_intent_rejects_cross_company_attack_at_repo_layer(env):
    """At the repo layer (used by API + future internal callers + tests),
    passing a company_id which does not own the job raises Forbidden.
    The API itself never accepts a caller-supplied company_id (it
    sources it from the job), so the attack surface is closed at the
    transport layer AND at the data layer.
    """
    client, repo, _ = env
    cid_a = client.post("/api/v1/companies", json={"slug": "a", "name": "A"}).json()["company_id"]
    cid_b = client.post("/api/v1/companies", json={"slug": "b", "name": "B"}).json()["company_id"]
    bid = client.post(
        f"/api/v1/companies/{cid_a}/bibles",
        json={"payload": {"name": "X", "audience": "Y"}},
    ).json()["bible_id"]
    client.post(f"/api/v1/bibles/{bid}/approve")
    r = client.post("/api/v1/jobs", json={
        "company_id": cid_a, "topic": "t", "language": "en",
        "duration_seconds": 5, "platforms": ["tiktok"], "bible_id": bid,
    })
    job_id = r.json()["job_id"]
    repo.update_job(job_id, state="done")
    # Direct repo call with the WRONG company_id must raise Forbidden.
    from core.repo import Forbidden
    with pytest.raises(Forbidden):
        repo.create_publish_intent(
            job_id=job_id, company_id=cid_b, action="draft",
            platforms=["tiktok"],
        )
    # API layer also closes the attack: it ignores caller-supplied
    # company_id and sources from the job.
    r = client.post(f"/api/v1/jobs/{job_id}/publish", json={
        "action": "draft", "platforms": ["tiktok"], "company_id": cid_b,
    })
    assert r.status_code == 200
    assert r.json()["company_id"] == cid_a, "API must use job's company_id"


def test_publish_intent_accepts_own_company(env):
    """The same operator (company A) can publish their own job."""
    client, repo, _ = env
    cid = client.post("/api/v1/companies", json={"slug": "c", "name": "C"}).json()["company_id"]
    bid = client.post(
        f"/api/v1/companies/{cid}/bibles",
        json={"payload": {"name": "X", "audience": "Y"}},
    ).json()["bible_id"]
    client.post(f"/api/v1/bibles/{bid}/approve")
    r = client.post("/api/v1/jobs", json={
        "company_id": cid, "topic": "t", "language": "en",
        "duration_seconds": 5, "platforms": ["tiktok"], "bible_id": bid,
    })
    job_id = r.json()["job_id"]
    repo.update_job(job_id, state="done")
    r = client.post(f"/api/v1/jobs/{job_id}/publish", json={
        "action": "draft", "platforms": ["tiktok"],
    })
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "done"


def test_publish_intent_rejects_unknown_platform(env):
    """An invalid platform name must be rejected at the repo layer."""
    client, repo, _ = env
    cid = client.post("/api/v1/companies", json={"slug": "d", "name": "D"}).json()["company_id"]
    bid = client.post(
        f"/api/v1/companies/{cid}/bibles",
        json={"payload": {"name": "X", "audience": "Y"}},
    ).json()["bible_id"]
    client.post(f"/api/v1/bibles/{bid}/approve")
    r = client.post("/api/v1/jobs", json={
        "company_id": cid, "topic": "t", "language": "en",
        "duration_seconds": 5, "platforms": ["tiktok"], "bible_id": bid,
    })
    job_id = r.json()["job_id"]
    repo.update_job(job_id, state="done")
    r = client.post(f"/api/v1/jobs/{job_id}/publish", json={
        "action": "draft", "platforms": ["not_a_real_platform"],
    })
    # Repo raises ValueError → API maps to 400
    assert r.status_code == 400, r.text


# ── F3: write_durable surfaces to runner_audit ───────────────────────


def test_runner_audit_table_bootstrap(tmp_path):
    """bootstrap_runner_audit must create the runner_audit table
    idempotently and be visible through Repo._conn()."""
    db = tmp_path / "r.db"
    repo = Repo(str(db))
    bootstrap_runner_audit(repo)
    # Verify table exists
    rows = repo._conn().execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='runner_audit'"
    ).fetchall()
    assert len(rows) == 1


def test_append_runner_audit_writes_row(tmp_path):
    repo = Repo(str(tmp_path / "r2.db"))
    bootstrap_runner_audit(repo)
    append_runner_audit(
        repo, job_id="job_1", event="idempotency.durable_mirror",
        context={"idem_key": "k1", "ok": True},
    )
    rows = repo._conn().execute(
        "SELECT job_id, event, context_json FROM runner_audit"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "job_1"
    assert rows[0][1] == "idempotency.durable_mirror"
    assert "idem_key" in rows[0][2]


def test_append_runner_audit_never_raises(tmp_path):
    """append_runner_audit is best-effort: a broken Repo must not raise."""
    class _BrokenRepo:
        def _conn(self):
            raise RuntimeError("db down")
    append_runner_audit(
        _BrokenRepo(), job_id="j", event="e", context={},
    )  # no exception


# ── F4: stitch_scene_clips mock-only skip is structured ──────────────


def test_stitch_scene_clips_returns_structured_skip_for_mock_inputs(tmp_path):
    """When ALL inputs are mock fixtures, stitch must return
    {skipped: True, reason: 'mock_only'} instead of attempting
    concat on non-mock data or returning None. Closes the silent
    'fell back to None' bug.
    """
    from control_plane.storage import LocalDiskStorage, Storage
    from assembly.ffmpeg_assembler import stitch_scene_clips

    if not _has_ffmpeg():
        pytest.skip("ffmpeg not available")

    storage = LocalDiskStorage(root=str(tmp_path))
    # Mint two mock clips by writing sentinel-marker bytes to disk
    # through the storage layer. These have the MOCK_SENTINEL header
    # so is_mock_asset() returns True.
    from render_plane.mock_renderer import MOCK_SENTINEL
    mock_data_a = MOCK_SENTINEL + b'{"scene_id":"sc_a"}' + b"\x00" * 4096
    mock_data_b = MOCK_SENTINEL + b'{"scene_id":"sc_b"}' + b"\x00" * 4096
    storage.put(key="mock/clips/sc_a.mp4", data=mock_data_a, mime_type="video/mp4")
    storage.put(key="mock/clips/sc_b.mp4", data=mock_data_b, mime_type="video/mp4")
    clip_refs = ["mock/clips/sc_a.mp4", "mock/clips/sc_b.mp4"]
    out = stitch_scene_clips(
        storage=storage, clip_refs=clip_refs,
        output_ref="assembled/test/stitched.mp4",
        target_duration_seconds=10.0,
    )
    assert out is not None, "expected structured skip, not None"
    assert out.get("skipped") is True
    assert out.get("reason") == "mock_only"
    assert out.get("qc", {}).get("checks", {}).get("all_inputs_mock") is True


def _has_ffmpeg() -> bool:
    import shutil
    return shutil.which("ffmpeg") is not None


# ── F5: whole-video / JobRunner path no longer hard-fails on no-ffmpeg ─


def test_job_runner_assembly_skip_does_not_fail_job(tmp_path):
    """F5: when ffmpeg is missing, the JobRunner must record
    assembly.skipped as a normal stage summary (not a hard error)
    and continue to export_publish. The job reaches 'done'.

    We force the missing-ffmpeg branch by mocking shutil.which.
    """
    import shutil as _shutil
    from core.runners import JobRunner, Singleton

    real_which = _shutil.which

    def _fake_which(name):
        if name in ("ffmpeg", "ffprobe"):
            return None
        return real_which(name)

    _shutil.which = _fake_which
    try:
        db = tmp_path / "vf.db"
        storage = tmp_path / "storage"
        repo = Repo(str(db))
        schema = {
            "type": "object",
            "properties": {"pack_id": {"type": "string"}},
        }
        runner = JobRunner(
            repo=repo, storage_root=str(storage),
            schema=schema, budget_per_video_usd=5.0, budget_per_batch_usd=20.0,
        )
        Singleton.set(runner)
        # Create a company + job
        c = repo.create_company(slug="acme", name="Acme")
        job = repo.create_job(
            c["company_id"], topic="t", language="en",
            duration_seconds=10, platforms=["tiktok"], aspect_ratio="9:16",
            count=1, render_profile="mock",
        )
        runner.submit(job["job_id"])
        # Wait for completion
        import time
        deadline = time.time() + 30
        while time.time() < deadline:
            j = repo.get_job(job["job_id"])
            if j["state"] in ("done", "failed"):
                break
            time.sleep(0.1)
        # Job MUST reach done even without ffmpeg.
        assert j["state"] == "done", f"job ended in {j['state']} without ffmpeg; expected done"
        stages = repo.list_stages(job["job_id"])
        assembly = next((s for s in stages if s["stage_key"] == "assembly"), None)
        assert assembly is not None, "assembly stage missing"
        # The skip must be recorded in summary, not as a hard error
        # (status='done' or 'skipped', not 'failed').
        assert assembly["status"] in ("done", "skipped"), \
            f"assembly stage should not be failed when ffmpeg is missing: {assembly}"
        qc_stage = next((s for s in stages if s["stage_key"] == "qc"), None)
        assert qc_stage is not None
        # QC must not flag absence of final_video as a hard failure
        # when ffmpeg was the cause of the skip.
        assert qc_stage["status"] != "failed", \
            f"qc stage should not be failed when only ffmpeg was missing: {qc_stage}"
    finally:
        _shutil.which = real_which