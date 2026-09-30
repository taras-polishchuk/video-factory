"""Render Plane: swappable provider adapters behind a Renderer protocol."""

from render_plane.provider import (
    Capabilities,
    CostEstimate,
    OutputAsset,
    Renderer,
    SubmitResult,
    StatusResult,
    TeardownResult,
)
from render_plane.registry import RendererRegistry, default_registry
from render_plane.mock_renderer import MockRenderer, MockRendererConfig
from render_plane.heygen_direct import HeyGenDirectAdapter
from render_plane.comfy_cloud import ComfyCloudAdapter
from render_plane.comfy_gpu_worker import ComfyGPUWorkerAdapter
from render_plane.comfy_heygen_partner import ComfyHeyGenPartnerAdapter

__all__ = [
    "Capabilities",
    "ComfyCloudAdapter",
    "ComfyGPUWorkerAdapter",
    "ComfyHeyGenPartnerAdapter",
    "CostEstimate",
    "HeyGenDirectAdapter",
    "MockRenderer",
    "MockRendererConfig",
    "OutputAsset",
    "Renderer",
    "RendererRegistry",
    "StatusResult",
    "SubmitResult",
    "TeardownResult",
    "default_registry",
]