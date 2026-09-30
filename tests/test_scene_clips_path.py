"""Scene-clips path test.

Verifies that the mock path produces one OutputAsset per scene and
that the state machine reaches DONE.
"""

from __future__ import annotations

import pytest

from control_plane.campaign import CampaignRequest
from control_plane.orchestrator import Orchestrator, OrchestratorConfig
from control_plane.pack_builder import (
    Caption, Concept, ResearchClaim, SceneSpec, Script, VoiceSpec,
)
from render_plane.mock_renderer import MockRenderer, MockRendererConfig


def test_scene_clips_produces_per_scene_assets(tmp_path, schema):
    storage_root = tmp_path / "artifacts"
    storage_root.mkdir()
    renderer = MockRenderer()
    renderer.config = MockRendererConfig(output_duration_seconds=1.0)
    orch = Orchestrator(
        renderer=renderer,
        config=OrchestratorConfig(artifacts_root=str(storage_root)),
    )
    request = CampaignRequest(
        topic="Coffee altitude",
        audience="home baristas",
        goal="drive saves",
        target_duration_seconds=15,
        language="en-US",
        platforms=["tiktok", "reels"],
        render_profile="mock",
    )
    concept = Concept(
        title="Coffee altitude",
        logline="Brewing at altitude",
        tone="calm",
        audience="home baristas",
    )
    script = Script(full_text="Water boils cooler up here.")
    voice = VoiceSpec(provider="mock", voice_id="v1", ssml_or_text=script.full_text)
    research = [
        ResearchClaim(
            text="Water boils at lower temperature at altitude.",
            source_title="NIST",
            source_url="https://webbook.nist.gov/chemistry/",
        ),
    ]
    captions = [
        Caption(0.0, 5.0, "Water boils cooler up here."),
        Caption(5.0, 15.0, "Try a finer grind."),
    ]
    scenes = [
        SceneSpec(scene_id="sc_hook", order=1, target_duration_seconds=5.0, visual_intent="Hook"),
        SceneSpec(scene_id="sc_body", order=2, target_duration_seconds=5.0, visual_intent="Body"),
        SceneSpec(scene_id="sc_cta", order=3, target_duration_seconds=5.0, visual_intent="CTA"),
    ]
    result = orch.run_to_done(
        schema=schema,
        request=request,
        concept=concept,
        script=script,
        voice=voice,
        research=research,
        captions=captions,
        scenes=scenes,
    )
    assert result.state_machine.state.value == "DONE"
    assert len(result.outputs) == 3
    # Each asset has a scene_id attached.
    scene_ids = {a.scene_id for a in result.outputs}
    assert scene_ids == {"sc_hook", "sc_body", "sc_cta"}


def test_idempotent_retry_does_not_double_submit(tmp_path, schema):
    """A second run with the same content must NOT trigger a second provider submit.

    The orchestrator's IdempotencyStore must short-circuit on duplicate keys.
    We verify by asserting that the second run's provider_job_id matches
    the first run's, and that the audit log records a `submit.replay` event.
    """
    storage_root = tmp_path / "artifacts"
    storage_root.mkdir()
    renderer = MockRenderer()
    renderer.config = MockRendererConfig(output_duration_seconds=1.0)
    orch = Orchestrator(
        renderer=renderer,
        config=OrchestratorConfig(artifacts_root=str(storage_root)),
    )
    request = CampaignRequest(
        topic="Coffee altitude",
        audience="home baristas",
        goal="drive saves",
        target_duration_seconds=5,
        language="en-US",
        platforms=["tiktok"],
        render_profile="mock",
    )
    concept = Concept(
        title="Coffee altitude", logline="Brewing at altitude",
        tone="calm", audience="home baristas",
    )
    script = Script(full_text="Water boils cooler up here.")
    voice = VoiceSpec(provider="mock", voice_id="v1", ssml_or_text=script.full_text)
    research = [
        ResearchClaim(
            text="Water boils at lower temperature at altitude.",
            source_title="NIST",
            source_url="https://webbook.nist.gov/chemistry/",
        ),
    ]
    captions = [Caption(0.0, 5.0, "Water boils cooler up here.")]
    scenes = [SceneSpec(scene_id="sc_a", order=1, target_duration_seconds=5.0, visual_intent="hook")]

    # First run
    r1 = orch.run_to_done(
        schema=schema, request=request, concept=concept, script=script,
        voice=voice, research=research, captions=captions, scenes=scenes,
    )
    pj1 = r1.provider_job_id
    assert pj1 is not None

    # Second run with same content
    r2 = orch.run_to_done(
        schema=schema, request=request, concept=concept, script=script,
        voice=voice, research=research, captions=captions, scenes=scenes,
    )
    pj2 = r2.provider_job_id
    # Same content_hash -> same idem_key -> same provider_job_id.
    assert pj2 == pj1
    # The audit log on the second run records at least one `submit.replay`.
    recs = r2.audit_log.records()
    replay_events = [r for r in recs if r.event == "submit.replay"]
    assert len(replay_events) >= 1, "expected at least one submit.replay event"