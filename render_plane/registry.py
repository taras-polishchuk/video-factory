"""Renderer registry. Resolves render_profile → adapter instance."""

from __future__ import annotations

from typing import Dict, Optional

from render_plane.provider import Renderer


class UnknownProfile(Exception):
    pass


class RendererRegistry:
    def __init__(self) -> None:
        self._by_profile: Dict[str, Renderer] = {}

    def register(self, renderer: Renderer) -> None:
        self._by_profile[renderer.profile] = renderer

    def resolve(self, profile: str) -> Renderer:
        r = self._by_profile.get(profile)
        if r is None:
            raise UnknownProfile(profile)
        return r

    def known_profiles(self) -> list[str]:
        return sorted(self._by_profile.keys())


def default_registry() -> RendererRegistry:
    """Registry pre-populated with mock + capability-gated live adapters.

    Live adapters are constructed with safe defaults; they raise
    ProviderDisabled unless the operator flips LIVE_PROVIDER_TESTS=true
    and configures budget caps. This keeps imports cheap and side-effect
    free.
    """
    from render_plane.mock_renderer import MockRenderer
    from render_plane.heygen_direct import HeyGenDirectAdapter
    from render_plane.comfy_cloud import ComfyCloudAdapter
    from render_plane.comfy_gpu_worker import ComfyGPUWorkerAdapter
    from render_plane.comfy_heygen_partner import ComfyHeyGenPartnerAdapter

    reg = RendererRegistry()
    reg.register(MockRenderer())
    reg.register(HeyGenDirectAdapter())
    reg.register(ComfyCloudAdapter())
    reg.register(ComfyGPUWorkerAdapter())
    reg.register(ComfyHeyGenPartnerAdapter())
    return reg