"""PackBuilder: turns an IntakeReport + assets into a content-hashed VideoPack."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional

from jsonschema import Draft7Validator

from control_plane.campaign import IntakeReport, VideoSpec
from control_plane.idempotency import canonicalize_for_hash, sha256_hex


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_pack_id() -> str:
    return f"vp_{uuid.uuid4().hex[:16]}"


def _new_campaign_id() -> str:
    return f"cmp_{uuid.uuid4().hex[:12]}"


def _new_video_id() -> str:
    return f"vid_{uuid.uuid4().hex[:12]}"


def _pack_id_from_hash(content_hash: str) -> str:
    """Deterministic pack_id derived from content_hash. Enables safe retries.

    Idempotency contracts depend on stable, content-derived identifiers.
    Random pack_ids would force the orchestrator to compute and compare
    content_hash on every retry, but a content-derived pack_id removes
    that overhead AND aligns with downstream reconciliation.
    """
    return f"vp_{content_hash[:16]}"


def _campaign_id_from_hash(content_hash: str) -> str:
    return f"cmp_{content_hash[16:28]}"


def _video_id_from_hash(content_hash: str) -> str:
    return f"vid_{content_hash[28:40]}"


@dataclass
class ResearchClaim:
    text: str
    source_title: str
    source_url: str
    accessed_at: str = ""
    snippet: str = ""
    confidence: str = "verified"  # "verified" | "unresolved"

    def to_dict(self) -> dict:
        # accessed_at is omitted from canonical hashing when empty so
        # research claims without a known access time do not perturb
        # the content_hash. Callers must set it explicitly for audit.
        d: dict = {
            "text": self.text,
            "source_title": self.source_title,
            "source_url": self.source_url,
            "confidence": self.confidence,
        }
        if self.snippet:
            d["snippet"] = self.snippet
        if self.accessed_at:
            d["accessed_at"] = self.accessed_at
        return d


@dataclass
class Caption:
    start_seconds: float
    end_seconds: float
    text: str

    def to_dict(self) -> dict:
        return {
            "start_seconds": self.start_seconds,
            "end_seconds": self.end_seconds,
            "text": self.text,
        }


@dataclass
class SceneSpec:
    scene_id: str
    order: int
    target_duration_seconds: float
    visual_intent: str
    motion_prompt: str = ""
    continuity_notes: str = ""

    def to_dict(self) -> dict:
        d = {
            "scene_id": self.scene_id,
            "order": self.order,
            "target_duration_seconds": self.target_duration_seconds,
            "visual_intent": self.visual_intent,
        }
        if self.motion_prompt:
            d["motion_prompt"] = self.motion_prompt
        if self.continuity_notes:
            d["continuity_notes"] = self.continuity_notes
        return d


@dataclass
class Concept:
    title: str
    logline: str
    tone: str
    audience: str
    cta: Optional[str] = None
    negative_rules: Optional[List[str]] = None

    def to_dict(self) -> dict:
        d = {
            "title": self.title,
            "logline": self.logline,
            "tone": self.tone,
            "audience": self.audience,
        }
        if self.cta:
            d["cta"] = self.cta
        if self.negative_rules:
            d["negative_rules"] = list(self.negative_rules)
        return d


@dataclass
class Script:
    full_text: str
    approved_by: Optional[str] = None
    approved_at: Optional[str] = None

    def to_dict(self) -> dict:
        d = {"full_text": self.full_text}
        if self.approved_by:
            d["approved_by"] = self.approved_by
        if self.approved_at:
            d["approved_at"] = self.approved_at
        return d


@dataclass
class VoiceSpec:
    provider: str
    voice_id: str
    ssml_or_text: str

    def to_dict(self) -> dict:
        return {
            "provider": self.provider,
            "voice_id": self.voice_id,
            "ssml_or_text": self.ssml_or_text,
        }


@dataclass
class VideoPack:
    pack_id: str
    campaign_id: str
    video_id: str
    language: str
    target_duration_seconds: int
    platforms: List[str]
    aspect_ratios: List[str]
    concept: Concept
    research: List[ResearchClaim]
    approved_script: Script
    voice: VoiceSpec
    captions: List[Caption]
    scenes: List[SceneSpec]
    render_profile: str
    output_kind: str
    budget_max_per_video_usd: float
    budget_max_per_batch_usd: float
    approval_required: bool
    workflow_ref: Optional[str] = None
    provider_model_version: Optional[str] = None
    created_at: str = ""
    schema_version: str = "1.0.0"
    content_hash: str = ""

    def to_dict(self) -> dict:
        d = {
            "schema_version": self.schema_version,
            "pack_id": self.pack_id,
            "campaign_id": self.campaign_id,
            "video_id": self.video_id,
            "language": self.language,
            "target_duration_seconds": self.target_duration_seconds,
            "platforms": list(self.platforms),
            "aspect_ratios": list(self.aspect_ratios),
            "concept": self.concept.to_dict(),
            "research": {"claims": [c.to_dict() for c in self.research]},
            "approved_script": self.approved_script.to_dict(),
            "voice": self.voice.to_dict(),
            "captions": [c.to_dict() for c in self.captions],
            "scenes": [s.to_dict() for s in self.scenes],
            "render_profile": self.render_profile,
            "render_capabilities": {
                "output_kind": self.output_kind,
                "max_takes_per_scene": 1,
                "aspect_ratio_capabilities": list(self.aspect_ratios),
            },
            "budget": {
                "max_cost_per_video_usd": self.budget_max_per_video_usd,
                "max_cost_per_batch_usd": self.budget_max_per_batch_usd,
                "approval_required": self.approval_required,
            },
        }
        if self.workflow_ref:
            d["render_capabilities"]["workflow_ref"] = self.workflow_ref
        if self.provider_model_version:
            d["render_capabilities"]["provider_model_version"] = self.provider_model_version
        # content_hash is computed last; placeholder for canonicalization
        if self.content_hash:
            d["content_hash"] = self.content_hash
        else:
            d["content_hash"] = compute_content_hash(d)
        # created_at is recorded on the dict but excluded from content_hash
        # so retries with the same content yield the same pack_id.
        d["created_at"] = self.created_at or _now_iso()
        return d


def compute_content_hash(d: dict) -> str:
    """SHA-256 of canonical JSON excluding pack_id, content_hash, created_at, and metadata that varies per call.

    created_at is wall-clock and must not perturb retries.
    campaign_id and video_id are derived from the hash itself, so they
    must also be excluded from the input that produced the hash.
    """
    d = dict(d)
    d.pop("pack_id", None)
    d.pop("campaign_id", None)
    d.pop("video_id", None)
    d.pop("content_hash", None)
    d.pop("created_at", None)
    return sha256_hex(d)


class PackBuilder:
    """Builds a VideoPack from intake + content sources."""

    DEFAULT_WORKFLOW_BY_PROFILE = {
        "mock": "workflows/scene-clip-mock.json",
        "comfy_cloud": None,  # tenant-managed
        "comfy_gpu_worker": None,
        "comfy_heygen_partner": None,
        "heygen_direct": None,  # no workflow JSON needed; whole-video
    }

    OUTPUT_KIND_BY_PROFILE = {
        "mock": "scene_clips",
        "heygen_direct": "whole_video",
        "comfy_cloud": "scene_clips",
        "comfy_gpu_worker": "scene_clips",
        "comfy_heygen_partner": "avatar_video",
    }

    def __init__(self, schema: dict) -> None:
        self._validator = Draft7Validator(schema)

    def build(
        self,
        *,
        report: IntakeReport,
        video_index: int,
        concept: Concept,
        script: Script,
        voice: VoiceSpec,
        research: List[ResearchClaim],
        captions: List[Caption],
        scenes: List[SceneSpec],
        approval_required: bool = False,
        provider_model_version: Optional[str] = None,
        workflow_ref: Optional[str] = None,
        pack_id: Optional[str] = None,
    ) -> VideoPack:
        req = report.request
        render_profile = req.render_profile
        output_kind = self.OUTPUT_KIND_BY_PROFILE.get(render_profile, "scene_clips")
        if workflow_ref is None:
            workflow_ref = self.DEFAULT_WORKFLOW_BY_PROFILE.get(render_profile)
        # Generate a temporary pack with a random pack_id to compute
        # content_hash, then re-issue with a content-derived pack_id.
        # This makes retries converge on the same pack_id.
        temp_pack_id = _new_pack_id()
        temp_pack = VideoPack(
            pack_id=temp_pack_id,
            campaign_id=_new_campaign_id(),
            video_id=_new_video_id(),
            language=req.language,
            target_duration_seconds=req.target_duration_seconds,
            platforms=req.platforms,
            aspect_ratios=report.resolved_aspect_ratios,
            concept=concept,
            research=research,
            approved_script=script,
            voice=voice,
            captions=captions,
            scenes=scenes,
            render_profile=render_profile,
            output_kind=output_kind,
            budget_max_per_video_usd=req.max_cost_per_video_usd,
            budget_max_per_batch_usd=req.max_cost_per_batch_usd,
            approval_required=approval_required,
            workflow_ref=workflow_ref,
            provider_model_version=provider_model_version,
        )
        temp_dict = temp_pack.to_dict()
        # The temp_pack.pack_id was random. Strip it from the dict so
        # content_hash is computed deterministically across retries.
        temp_dict.pop("pack_id", None)
        content_hash = compute_content_hash(temp_dict)
        # Override ids with content-derived values for stable retries.
        temp_dict["pack_id"] = _pack_id_from_hash(content_hash)
        temp_dict["campaign_id"] = _campaign_id_from_hash(content_hash)
        temp_dict["video_id"] = _video_id_from_hash(content_hash)
        # Lock the computed content_hash so to_dict() does not recompute.
        temp_dict["content_hash"] = content_hash
        # Validate with deterministic ids in place.
        errors = list(self._validator.iter_errors(temp_dict))
        if errors:
            msg = "; ".join(
                f"{'/'.join(str(p) for p in e.absolute_path)}: {e.message}" for e in errors
            )
            raise ValueError(f"VideoPack schema validation failed: {msg}")
        # Build the final VideoPack instance with the deterministic ids.
        pack = VideoPack(
            pack_id=temp_dict["pack_id"],
            campaign_id=temp_dict["campaign_id"],
            video_id=temp_dict["video_id"],
            language=temp_pack.language,
            target_duration_seconds=temp_pack.target_duration_seconds,
            platforms=temp_pack.platforms,
            aspect_ratios=temp_pack.aspect_ratios,
            concept=temp_pack.concept,
            research=temp_pack.research,
            approved_script=temp_pack.approved_script,
            voice=temp_pack.voice,
            captions=temp_pack.captions,
            scenes=temp_pack.scenes,
            render_profile=temp_pack.render_profile,
            output_kind=temp_pack.output_kind,
            budget_max_per_video_usd=temp_pack.budget_max_per_video_usd,
            budget_max_per_batch_usd=temp_pack.budget_max_per_batch_usd,
            approval_required=temp_pack.approval_required,
            workflow_ref=temp_pack.workflow_ref,
            provider_model_version=temp_pack.provider_model_version,
            content_hash=content_hash,
        )
        return pack