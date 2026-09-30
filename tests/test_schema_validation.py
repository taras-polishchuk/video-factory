"""Schema validation tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from control_plane.campaign import CampaignRequest, IntakeValidator
from control_plane.pack_builder import (
    Caption, Concept, PackBuilder, ResearchClaim, SceneSpec, Script, VoiceSpec,
)


def test_intake_rejects_bad_language():
    v = IntakeValidator()
    req = CampaignRequest(
        topic="t", audience="a", goal="g", target_duration_seconds=10,
        language="not_a_tag", platforms=["tiktok"],
    )
    with pytest.raises(ValueError, match="BCP-47"):
        v.validate(req)


def test_intake_rejects_unknown_platform():
    v = IntakeValidator()
    req = CampaignRequest(
        topic="t", audience="a", goal="g", target_duration_seconds=10,
        language="en-US", platforms=["myspace"],
    )
    with pytest.raises(ValueError, match="unknown platform"):
        v.validate(req)


def test_intake_rejects_bad_duration():
    v = IntakeValidator()
    req = CampaignRequest(
        topic="t", audience="a", goal="g", target_duration_seconds=2,
        language="en-US", platforms=["tiktok"],
    )
    with pytest.raises(ValueError, match="target_duration_seconds"):
        v.validate(req)


def test_intake_resolves_aspects_from_platforms():
    v = IntakeValidator()
    req = CampaignRequest(
        topic="t", audience="a", goal="g", target_duration_seconds=10,
        language="en-US", platforms=["linkedin"],
    )
    report = v.validate(req)
    # LinkedIn resolves to 16:9 + 1:1
    assert "16:9" in report.resolved_aspect_ratios
    assert "1:1" in report.resolved_aspect_ratios


def test_pack_builder_validates_schema(tmp_path, schema):
    """A complete concept+script+voice+captions+scenes build passes schema."""
    builder = PackBuilder(schema)
    v = IntakeValidator()
    req = CampaignRequest(
        topic="t", audience="a", goal="g", target_duration_seconds=10,
        language="en-US", platforms=["tiktok"],
    )
    report = v.validate(req)
    pack = builder.build(
        report=report,
        video_index=0,
        concept=Concept(title="t", logline="l", tone="t", audience="a"),
        script=Script(full_text="text"),
        voice=VoiceSpec(provider="mock", voice_id="v1", ssml_or_text="text"),
        research=[ResearchClaim(
            text="x", source_title="y", source_url="https://example.com",
        )],
        captions=[Caption(0.0, 5.0, "ok"), Caption(5.0, 10.0, "ok2")],
        scenes=[
            SceneSpec(scene_id="sc_a", order=1, target_duration_seconds=5.0, visual_intent="v"),
            SceneSpec(scene_id="sc_b", order=2, target_duration_seconds=5.0, visual_intent="v"),
        ],
    )
    d = pack.to_dict()
    assert d["schema_version"] == "1.0.0"
    assert d["content_hash"]  # non-empty after build
    assert len(d["content_hash"]) == 64  # sha256 hex