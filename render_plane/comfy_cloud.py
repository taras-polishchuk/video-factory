"""ComfyUI Cloud adapter.

Submits API-format workflow JSON to a Comfy Cloud tenant, polls for
completion, and downloads outputs. Auth and billing belong to the
Comfy Cloud account.

References verified against:
- https://docs.comfy.org/development/comfyui-server/api-examples
- https://docs.comfy.org/tutorials/partner-nodes/openai/dall-e-3
- https://support.comfy.org/articles/2703236295-comfy-api-deploy-your-comfyui-workflow-as-an-api
- https://comfy.org/pricing/

Architectural notes:
- We treat Comfy Cloud as a separate billing path from HeyGen.
- Workflow JSON is versioned and stored with the VideoPack.
- We do NOT assume a per-render credit price; budget guard treats cost
  as unknown unless operator verifies on the dashboard.
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


class ComfyCloudAdapter:
    profile = "comfy_cloud"

    def __init__(
        self,
        *,
        api_base: Optional[str] = None,
        api_key: Optional[str] = None,
    ) -> None:
        self._api_base = api_base or os.environ.get("COMFY_CLOUD_API_BASE")
        self._api_key = api_key or os.environ.get("COMFY_CLOUD_API_KEY")

    def capabilities(self) -> Capabilities:
        return Capabilities(
            profile=self.profile,
            output_kinds=[OutputKind.SCENE_CLIPS, OutputKind.TRANSLATION],
            supports_cancel=True,
            supports_status_poll=True,
            supports_webhook=False,
            aspect_ratios=["9:16", "16:9", "1:1", "4:5"],
            provider_billing_owner="comfy_cloud",
            requires_secret=True,
            notes=(
                "Comfy Cloud API-format workflow submit + poll. Per-render credit cost is "
                "tenant-specific; verify on Comfy dashboard before budget approval."
            ),
        )

    def estimate(self, *, pack: dict) -> CostEstimate:
        return CostEstimate(
            amount_usd=None,
            currency="USD",
            unknown=True,
            assumptions="Comfy Cloud pricing is per-credit; verify dashboard before live submit.",
        )

    def submit(self, *, pack: dict, idempotency_key: str) -> SubmitResult:
        if os.environ.get("LIVE_PROVIDER_TESTS", "false").lower() != "true":
            raise ProviderDisabled("comfy_cloud: set LIVE_PROVIDER_TESTS=true and budget cap > 0")
        if not self._api_key or not self._api_base:
            raise ProviderError("COMFY_CLOUD_API_BASE / COMFY_CLOUD_API_KEY not configured")
        # Real implementation would:
        # 1. Resolve the API-format workflow JSON (pack['render_capabilities']['workflow_ref'])
        # 2. Substitute scene-level parameters from pack['scenes']
        # 3. POST {api_base}/workflows with auth + idempotency_key
        # 4. Capture the returned prompt_id (provider_job_id)
        raise ProviderDisabled(
            "comfy_cloud: contract-only in V1. Live POST gated by LIVE_PROVIDER_TESTS + smoke test."
        )

    def status(self, *, provider_job_id: str) -> StatusResult:
        return StatusResult(state=JobState.UNKNOWN, raw={"provider_job_id": provider_job_id})

    def fetch_outputs(self, *, provider_job_id: str) -> List[OutputAsset]:
        return []

    def cancel(self, *, provider_job_id: str) -> TeardownResult:
        return TeardownResult(ok=False, detail="verify per Comfy Cloud API docs.")

    def teardown_gpu(self) -> bool:
        raise TeardownUnsupported("comfy_cloud manages its own compute.")