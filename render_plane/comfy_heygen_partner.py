"""ComfyUI HeyGen Partner Node adapter.

Capability-gated. Only enabled when the running Comfy server's
`/object_info` exposes HeyGen node types. Uses Comfy proxy auth, NOT
the operator's HeyGen API key. Billing belongs to Comfy account per
verified docs.

References verified against:
- https://github.com/Comfy-Org/ComfyUI/blob/master/comfy_api_nodes/nodes_heygen.py
  (HeyGen Partner Nodes — talking photo, avatar video, create avatar,
   translation, TTS.)
- https://docs.comfy.org/tutorials/partner-nodes/openai/dall-e-3
  (Partner Node pattern.)

Architectural notes:
- Comfy partner nodes invoke HeyGen via Comfy proxy; passing the user's
  own HeyGen API key to them is NOT a documented BYOK path. We do not
  ship that configuration.
- Verified public BYOK docs list Runway and Gemini; for other providers
  the path is Enterprise contact. We treat HeyGen BYOK as unverified.
"""

from __future__ import annotations

import os
from typing import Dict, List, Optional

from control_plane.errors import ProviderDisabled, ProviderError, TeardownUnsupported
from render_plane.provider import (
    Capabilities,
    CostEstimate,
    JobState,
    OutputAsset,
    OutputKind,
    Renderer,
    StatusResult,
    SubmitResult,
    TeardownResult,
)


# The HeyGen Partner Nodes exposed by the verified comfy_api_nodes/nodes_heygen.py.
# These are the *types* the running server's /object_info can advertise.
HEYGEN_PARTNER_NODE_TYPES = {
    "HeyGenTalkingPhoto",
    "HeyGenAvatarVideo",
    "HeyGenCreateAvatar",
    "HeyGenTranslation",
    "HeyGenTTS",
}


class ComfyHeyGenPartnerAdapter:
    profile = "comfy_heygen_partner"

    def __init__(self, *, api_base: Optional[str] = None) -> None:
        self._api_base = api_base or os.environ.get("COMFY_GPU_API_BASE")
        # We do NOT read HEYGEN_API_KEY here. Auth belongs to Comfy.

    def capabilities(self) -> Capabilities:
        return Capabilities(
            profile=self.profile,
            output_kinds=[OutputKind.AVATAR_VIDEO, OutputKind.TRANSLATION, OutputKind.TTS],
            supports_cancel=False,
            supports_status_poll=True,
            supports_webhook=False,
            aspect_ratios=["9:16", "16:9", "1:1"],
            provider_billing_owner="comfy_cloud",
            requires_secret=False,  # Comfy owns the auth
            notes=(
                "Comfy HeyGen Partner Nodes (talking photo, avatar video, create avatar, "
                "translation, TTS). Auth + billing belongs to Comfy. HeyGen API key is NOT "
                "passed to this adapter. No verified BYOK path for HeyGen via this route."
            ),
        )

    def _object_info_heygen_nodes(self) -> List[str]:
        """Returns the HeyGen node types advertised by the live Comfy server.

        Live path only. Returns [] in offline mode.
        """
        if not self._api_base:
            return []
        if os.environ.get("LIVE_PROVIDER_TESTS", "false").lower() != "true":
            return []
        # Real implementation: GET {api_base}/object_info
        # response shape: { <node_type>: { ... } , ... }
        # Filter keys that intersect HEYGEN_PARTNER_NODE_TYPES.
        return []

    def is_capable(self) -> bool:
        """True only when the running server actually exposes a HeyGen partner node."""
        advertised = self._object_info_heygen_nodes()
        return bool(set(advertised) & HEYGEN_PARTNER_NODE_TYPES)

    def estimate(self, *, pack: dict) -> CostEstimate:
        return CostEstimate(
            amount_usd=None,
            currency="USD",
            unknown=True,
            assumptions="Comfy Partner Node billing goes through Comfy account; verify dashboard.",
        )

    def submit(self, *, pack: dict, idempotency_key: str) -> SubmitResult:
        if os.environ.get("LIVE_PROVIDER_TESTS", "false").lower() != "true":
            raise ProviderDisabled(
                "comfy_heygen_partner: set LIVE_PROVIDER_TESTS=true + verify server capability."
            )
        if not self._api_base:
            raise ProviderError("COMFY_GPU_API_BASE not configured")
        if not self.is_capable():
            raise ProviderDisabled(
                "comfy_heygen_partner: server object_info does not advertise HeyGen partner nodes."
            )
        raise ProviderDisabled(
            "comfy_heygen_partner: contract-only in V1. Live submit gated by capability check."
        )

    def status(self, *, provider_job_id: str) -> StatusResult:
        return StatusResult(state=JobState.UNKNOWN, raw={"provider_job_id": provider_job_id})

    def fetch_outputs(self, *, provider_job_id: str) -> List[OutputAsset]:
        return []

    def cancel(self, *, provider_job_id: str) -> TeardownResult:
        return TeardownResult(ok=False, detail="no verified cancel path for partner nodes.")

    def teardown_gpu(self) -> bool:
        raise TeardownUnsupported("partner node billing is per-job; teardown is per-job, not GPU-level.")