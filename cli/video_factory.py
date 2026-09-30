"""Offline demo entrypoint.

Run with: python -m cli.video_factory [--output-dir artifacts/demo]

Produces one Video Pack in READY_FOR_RENDER + a mock-rendered artifact
set + a JSON report. No GPU, no API keys, no internet.

The same orchestrator path can be invoked from tests.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from control_plane.campaign import CampaignRequest
from control_plane.orchestrator import Orchestrator, OrchestratorConfig
from control_plane.pack_builder import (
    Caption,
    Concept,
    ResearchClaim,
    SceneSpec,
    Script,
    VoiceSpec,
)
from render_plane.registry import default_registry


def _schema_path() -> Path:
    return Path(__file__).resolve().parent.parent / "schemas" / "video-pack.schema.json"


def _build_request(args: argparse.Namespace) -> CampaignRequest:
    return CampaignRequest(
        topic=args.topic,
        audience=args.audience,
        goal=args.goal,
        target_duration_seconds=args.duration,
        language=args.language,
        platforms=[p.strip() for p in args.platforms.split(",") if p.strip()],
        count=1,
        render_profile=args.render_profile,
        max_cost_per_video_usd=args.max_cost_per_video,
        max_cost_per_batch_usd=args.max_cost_per_batch,
        approval_policy="auto",
    )


def _build_content():
    concept = Concept(
        title="Why your coffee tastes better at altitude",
        logline="A short explainer about extraction and water boiling point.",
        tone="curious, calm, factual",
        audience="curious home baristas aged 25-45",
        cta="Try it next morning. Link in bio.",
        negative_rules=[
            "Do not claim medical or health effects of caffeine.",
            "Do not disparage any brand of equipment.",
        ],
    )
    research = [
        ResearchClaim(
            text="Water boils at lower temperatures at high altitude.",
            source_title="NIST Chemistry WebBook",
            source_url="https://webbook.nist.gov/chemistry/",
            confidence="verified",
        ),
    ]
    script = Script(
        full_text=(
            "Coffee tastes different up here, and there is one reason. "
            "Water boils at a lower temperature, so your brew runs slightly cooler. "
            "Try a finer grind, the same ratio, and you taste the difference."
        ),
        approved_by="offline-demo",
    )
    voice = VoiceSpec(
        provider="mock",
        voice_id="mock_en_neutral",
        ssml_or_text=script.full_text,
    )
    captions = [
        Caption(0.0, 2.5, "Coffee tastes different up here."),
        Caption(2.5, 6.0, "And there is one reason."),
        Caption(6.0, 12.0, "Water boils at a lower temperature, so your brew runs slightly cooler."),
        Caption(12.0, 18.0, "Try a finer grind, the same ratio,"),
        Caption(18.0, 21.0, "and you taste the difference."),
    ]
    scenes = [
        SceneSpec(
            scene_id="sc_hook", order=1, target_duration_seconds=6.0,
            visual_intent="Mountain morning, hand holding mug, slow push-in.",
            motion_prompt="cinematic, soft light, shallow depth of field, gentle push-in",
        ),
        SceneSpec(
            scene_id="sc_explain", order=2, target_duration_seconds=12.0,
            visual_intent="Overhead shot of pour-over, kettle thermometer graphic overlay.",
            motion_prompt="overhead dolly, steam visible, calm pacing",
        ),
        SceneSpec(
            scene_id="sc_cta", order=3, target_duration_seconds=9.0,
            visual_intent="Cup close-up, brand mark subtly visible, CTA card overlay.",
            motion_prompt="macro, locked-off, warm color grade",
        ),
    ]
    return concept, research, script, voice, captions, scenes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Video Factory offline demo")
    parser.add_argument("--topic", default="Why your coffee tastes better at altitude")
    parser.add_argument("--audience", default="curious home baristas aged 25-45")
    parser.add_argument("--goal", default="drive saves + clicks to a brewing guide")
    parser.add_argument("--duration", type=int, default=30)
    parser.add_argument("--language", default="en-US")
    parser.add_argument("--platforms", default="tiktok,reels")
    parser.add_argument("--render-profile", default="mock",
                        choices=["mock", "heygen_direct", "comfy_cloud", "comfy_gpu_worker", "comfy_heygen_partner"])
    parser.add_argument("--max-cost-per-video", type=float, default=0.0)
    parser.add_argument("--max-cost-per-batch", type=float, default=0.0)
    parser.add_argument("--output-dir", default="artifacts/demo")
    args = parser.parse_args(argv)

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    reg = default_registry()
    try:
        renderer = reg.resolve(args.render_profile)
    except Exception as e:
        print(f"unknown render profile: {e}", file=sys.stderr)
        return 2

    if args.render_profile != "mock":
        print(f"render_profile={args.render_profile} is live-gated; switching to mock for offline demo.",
              file=sys.stderr)
        args.render_profile = "mock"

    schema = json.loads(_schema_path().read_text())
    request = _build_request(args)
    # Force render_profile back to mock if user tried a live one.
    object.__setattr__(request, "render_profile", "mock")
    renderer = reg.resolve("mock")

    orch = Orchestrator(
        renderer=renderer,
        config=OrchestratorConfig(
            artifacts_root=str(output_dir / "storage"),
            budget_per_video_usd=args.max_cost_per_video,
            budget_per_batch_usd=args.max_cost_per_batch,
        ),
    )
    concept, research, script, voice, captions, scenes = _build_content()

    result = orch.run_to_done(
        schema=schema,
        request=request,
        concept=concept,
        script=script,
        voice=voice,
        research=research,
        captions=captions,
        scenes=scenes,
        approval_required=False,
    )

    report = {
        "campaign_id": result.pack.campaign_id,
        "video_id": result.pack.video_id,
        "pack_id": result.pack.pack_id,
        "content_hash": result.pack.content_hash,
        "render_profile": result.pack.render_profile,
        "output_kind": result.pack.output_kind,
        "provider_job_id": result.provider_job_id,
        "final_state": result.state_machine.state.value,
        "qc_ok": (result.qc.ok if result.qc else None),
        "qc_notes": (result.qc.notes if result.qc else []),
        "assembled_ref": (result.assembled or {}).get("output_ref"),
        "output_assets": [a.ref for a in result.outputs],
        "audit_records": [r.to_dict() for r in result.audit_log.records()],
    }
    (output_dir / "report.json").write_text(json.dumps(report, indent=2))
    (output_dir / "video-pack.json").write_text(json.dumps(result.pack_dict, indent=2))

    print(f"final_state: {report['final_state']}")
    print(f"qc_ok: {report['qc_ok']}")
    print(f"pack_id: {report['pack_id']}")
    print(f"provider_job_id: {report['provider_job_id']}")
    print(f"output_assets: {len(report['output_assets'])}")
    print(f"output_dir: {output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())