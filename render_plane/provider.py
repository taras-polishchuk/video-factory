"""Renderer protocol. All adapters conform to this surface."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Protocol

from control_plane.errors import ProviderError, TeardownUnsupported


class OutputKind(str, Enum):
    WHOLE_VIDEO = "whole_video"
    AVATAR_VIDEO = "avatar_video"
    SCENE_CLIPS = "scene_clips"
    TRANSLATION = "translation"
    TTS = "tts"


class JobState(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Capabilities:
    """Static capability surface. Adapters return this once."""
    profile: str  # mock | heygen_direct | comfy_cloud | comfy_gpu_worker | comfy_heygen_partner
    output_kinds: List[OutputKind]
    supports_cancel: bool
    supports_status_poll: bool
    supports_webhook: bool
    aspect_ratios: List[str]
    provider_billing_owner: str  # "mock" | "heygen" | "comfy_cloud" | "tenant"
    requires_secret: bool
    notes: str = ""


@dataclass(frozen=True)
class CostEstimate:
    amount_usd: Optional[float]
    currency: str = "USD"
    range_low_usd: Optional[float] = None
    range_high_usd: Optional[float] = None
    assumptions: str = ""
    unknown: bool = False


@dataclass(frozen=True)
class SubmitResult:
    provider_job_id: str
    accepted_metadata: Dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class OutputAsset:
    asset_kind: str  # "video" | "audio" | "image" | "scene_clip"
    ref: str
    checksum: str
    size_bytes: int
    mime_type: str
    duration_seconds: Optional[float] = None
    width: Optional[int] = None
    height: Optional[int] = None
    scene_id: Optional[str] = None
    take_id: Optional[str] = None


@dataclass(frozen=True)
class StatusResult:
    state: JobState
    progress: float = 0.0
    error: Optional[str] = None
    outputs: List[OutputAsset] = field(default_factory=list)
    cost_actual_usd: Optional[float] = None
    raw: Dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class TeardownResult:
    ok: bool
    detail: str = ""


class Renderer(Protocol):
    """Common adapter surface. Every render profile implements this."""

    profile: str

    def capabilities(self) -> Capabilities: ...

    def estimate(self, *, pack: dict) -> CostEstimate: ...

    def submit(self, *, pack: dict, idempotency_key: str) -> SubmitResult: ...

    def status(self, *, provider_job_id: str) -> StatusResult: ...

    def fetch_outputs(self, *, provider_job_id: str) -> List[OutputAsset]: ...

    def cancel(self, *, provider_job_id: str) -> TeardownResult: ...