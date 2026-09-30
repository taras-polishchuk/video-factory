"""Campaign intake validation.

The intake layer rejects incompatible combinations BEFORE generation.
Defaults are documented and surfaced in the manifest so the operator
can see what was filled in for them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional


_LANG_RE = re.compile(r"^[a-z]{2,3}(-[A-Z]{2})?$")


@dataclass(frozen=True)
class PlatformSpec:
    """Target platform + its implied aspect ratios."""
    name: str
    aspect_ratios: tuple[str, ...]

    @classmethod
    def from_name(cls, name: str) -> "PlatformSpec":
        n = name.lower().strip()
        if n == "tiktok":
            return cls(name="tiktok", aspect_ratios=("9:16",))
        if n == "reels":
            return cls(name="reels", aspect_ratios=("9:16",))
        if n == "shorts":
            return cls(name="shorts", aspect_ratios=("9:16",))
        if n == "youtube_shorts":
            return cls(name="youtube_shorts", aspect_ratios=("9:16",))
        if n == "linkedin":
            return cls(name="linkedin", aspect_ratios=("16:9", "1:1"))
        if n == "x":
            return cls(name="x", aspect_ratios=("16:9", "1:1"))
        if n == "web":
            return cls(name="web", aspect_ratios=("16:9",))
        raise ValueError(f"unknown platform: {name}")


@dataclass(frozen=True)
class VideoSpec:
    topic: str
    audience: str
    target_duration_seconds: int
    language: str
    platforms: List[str]
    tone: str = "neutral"
    cta: Optional[str] = None
    goal: Optional[str] = None


@dataclass(frozen=True)
class CampaignRequest:
    topic: str
    audience: str
    goal: str
    target_duration_seconds: int
    language: str
    platforms: List[str]
    count: int = 1
    tone: str = "neutral"
    cta: Optional[str] = None
    render_profile: str = "mock"
    quality_preset: str = "balanced"
    takes_per_scene: int = 1
    max_cost_per_video_usd: float = 0.0
    max_cost_per_batch_usd: float = 0.0
    brand_profile_id: Optional[str] = None
    approval_policy: str = "auto"  # "auto" | "needs_review" | "always"
    aspect_ratios: Optional[List[str]] = None

    def to_videos(self) -> List[VideoSpec]:
        return [
            VideoSpec(
                topic=self.topic,
                audience=self.audience,
                target_duration_seconds=self.target_duration_seconds,
                language=self.language,
                platforms=self.platforms,
                tone=self.tone,
                cta=self.cta,
                goal=self.goal,
            )
            for _ in range(self.count)
        ]


@dataclass(frozen=True)
class IntakeDefaults:
    """Documented defaults. Anything filled in by the validator shows up in the manifest."""
    language: str = "en-US"
    render_profile: str = "mock"
    quality_preset: str = "balanced"
    takes_per_scene: int = 1
    approval_policy: str = "auto"
    tone: str = "neutral"


@dataclass(frozen=True)
class IntakeReport:
    request: CampaignRequest
    resolved_platforms: List[PlatformSpec]
    resolved_aspect_ratios: List[str]
    applied_defaults: List[str]  # human-readable notes about what the validator filled in
    warnings: List[str] = field(default_factory=list)


class IntakeValidator:
    """Validates a CampaignRequest and resolves platforms/aspects + documents defaults."""

    VALID_RENDER_PROFILES = {
        "mock", "heygen_direct", "comfy_cloud", "comfy_gpu_worker", "comfy_heygen_partner",
    }
    VALID_QUALITY_PRESETS = {"draft", "balanced", "high"}
    VALID_APPROVAL_POLICIES = {"auto", "needs_review", "always"}
    MIN_DURATION = 4
    MAX_DURATION = 600
    MIN_COUNT = 1
    MAX_COUNT = 100

    def validate(self, req: CampaignRequest) -> IntakeReport:
        applied: List[str] = []
        warnings: List[str] = []

        if not req.topic.strip():
            raise ValueError("topic is required")
        if not req.audience.strip():
            raise ValueError("audience is required")
        if not req.goal.strip():
            raise ValueError("goal is required")
        if not _LANG_RE.match(req.language):
            raise ValueError(f"language must be a BCP-47 tag, got {req.language!r}")
        if req.render_profile not in self.VALID_RENDER_PROFILES:
            raise ValueError(f"render_profile {req.render_profile!r} invalid")
        if req.quality_preset not in self.VALID_QUALITY_PRESETS:
            raise ValueError(f"quality_preset {req.quality_preset!r} invalid")
        if req.approval_policy not in self.VALID_APPROVAL_POLICIES:
            raise ValueError(f"approval_policy {req.approval_policy!r} invalid")
        if not (self.MIN_DURATION <= req.target_duration_seconds <= self.MAX_DURATION):
            raise ValueError(
                f"target_duration_seconds must be in [{self.MIN_DURATION}, {self.MAX_DURATION}]"
            )
        if not (self.MIN_COUNT <= req.count <= self.MAX_COUNT):
            raise ValueError(f"count must be in [{self.MIN_COUNT}, {self.MAX_COUNT}]")
        if req.takes_per_scene < 1:
            raise ValueError("takes_per_scene must be >= 1")

        # Render profile + output kind default compatibility
        output_kind_for_profile = {
            "mock": "scene_clips",
            "heygen_direct": "whole_video",
            "comfy_cloud": "scene_clips",
            "comfy_gpu_worker": "scene_clips",
            "comfy_heygen_partner": "avatar_video",
        }
        if req.render_profile == "heygen_direct" and req.takes_per_scene > 1:
            warnings.append(
                "takes_per_scene > 1 with heygen_direct is unusual; takes are typically per-scene only."
            )

        # Resolve platforms
        try:
            resolved = [PlatformSpec.from_name(p) for p in req.platforms]
        except ValueError as e:
            raise ValueError(str(e)) from e
        if not resolved:
            raise ValueError("at least one platform is required")

        # Resolve aspect ratios: explicit > derived > default 9:16
        if req.aspect_ratios:
            for ar in req.aspect_ratios:
                if ar not in {"9:16", "16:9", "1:1", "4:5"}:
                    raise ValueError(f"aspect_ratio {ar!r} invalid")
            aspect_ratios = list(req.aspect_ratios)
        else:
            seen: set[str] = set()
            aspect_ratios = []
            for ps in resolved:
                for ar in ps.aspect_ratios:
                    if ar not in seen:
                        seen.add(ar)
                        aspect_ratios.append(ar)
            if not aspect_ratios:
                aspect_ratios = ["9:16"]
                applied.append("aspect_ratios defaulted to ['9:16']")

        # Defaults
        if not req.cta:
            applied.append("cta defaulted to None")

        if req.quality_preset == "high" and req.render_profile == "mock":
            warnings.append("quality_preset=high with render_profile=mock has no effect on output")

        return IntakeReport(
            request=req,
            resolved_platforms=resolved,
            resolved_aspect_ratios=aspect_ratios,
            applied_defaults=applied,
            warnings=warnings,
        )