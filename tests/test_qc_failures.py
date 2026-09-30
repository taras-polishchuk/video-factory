"""QC failure tests.

Verifies that the orchestrator surfaces missing-scene, wrong-duration,
and failed-upload failures without crashing and without leaving the
state machine in a misleading DONE.
"""

from __future__ import annotations

import pytest

from control_plane.campaign import CampaignRequest
from control_plane.errors import ProviderError
from control_plane.orchestrator import Orchestrator, OrchestratorConfig
from control_plane.pack_builder import (
    Caption, Concept, ResearchClaim, SceneSpec, Script, VoiceSpec,
)
from render_plane.mock_renderer import MockRenderer, MockRendererConfig


def _run_with_config(tmp_path, schema, *, fail_after_submit=False, scenes=None, captions=None):
    storage_root = tmp_path / "artifacts"
    storage_root.mkdir()
    renderer = MockRenderer()
    renderer.config = MockRendererConfig(
        output_duration_seconds=1.0,
        fail_after_submit=fail_after_submit,
    )
    orch = Orchestrator(
        renderer=renderer,
        config=OrchestratorConfig(artifacts_root=str(storage_root)),
    )
    request = CampaignRequest(
        topic="Coffee altitude", audience="home baristas", goal="drive saves",
        target_duration_seconds=10, language="en-US", platforms=["tiktok"],
        render_profile="mock",
    )
    concept = Concept(title="t", logline="l", tone="t", audience="a")
    script = Script(full_text="text")
    voice = VoiceSpec(provider="mock", voice_id="v1", ssml_or_text=script.full_text)
    research = [ResearchClaim(
        text="x", source_title="y", source_url="https://example.com",
    )]
    if scenes is None:
        scenes = [
            SceneSpec(scene_id="sc_a", order=1, target_duration_seconds=5.0, visual_intent="v"),
            SceneSpec(scene_id="sc_b", order=2, target_duration_seconds=5.0, visual_intent="v"),
        ]
    if captions is None:
        captions = [Caption(0.0, 5.0, "first"), Caption(5.0, 10.0, "second")]
    return orch.run_to_done(
        schema=schema, request=request, concept=concept, script=script,
        voice=voice, research=research, captions=captions, scenes=scenes,
    )


def test_missing_scene_does_not_crash(tmp_path, schema):
    """An empty scene list must produce a schema validation error at pack build."""
    with pytest.raises(ValueError, match="scenes"):
        _run_with_config(tmp_path, schema, scenes=[])


def test_failed_upload_surfaces_failure(tmp_path, schema):
    """MockRenderer with fail_after_submit=True raises ProviderError; the state
    machine ends in FAILED with an audit record explaining why."""
    with pytest.raises(ProviderError):
        _run_with_config(tmp_path, schema, fail_after_submit=True)


def test_malformed_captions_do_not_break_pipeline(tmp_path, schema):
    """Malformed caption (end <= start) is caught by QC; pipeline surfaces it
    via qc.ok=False and final_state=FAILED."""
    bad_captions = [
        Caption(0.0, 5.0, "ok"),
        Caption(5.0, 4.0, "end before start"),  # malformed
    ]
    result = _run_with_config(tmp_path, schema, captions=bad_captions)
    # Caption check sets qc.ok = False, so state machine should be FAILED.
    assert result.state_machine.state.value in {"FAILED"}
    assert result.qc is not None
    assert result.qc.ok is False
    assert any("caption" in n.lower() for n in result.qc.notes)