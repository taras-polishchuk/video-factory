"""ComfyUI GPU worker adapter.

Talks to a running ComfyUI server (rented VPS or self-hosted) via its
native /prompt API. Workflow JSON is versioned and submitted per scene.

References verified against:
- https://docs.comfy.org/development/comfyui-server/api-examples
  (POST /prompt with workflow JSON; GET /history/{id} for outputs;
   WebSocket streaming available.)

Lifecycle (per docs/operations-gpu.md):
- Bring up VPS (manual gate if no provider API).
- Health check /system_stats.
- Confirm required models are loaded.
- Smoke test: submit one scene, verify output.
- Run batch.
- Verify all outputs uploaded + checksums.
- Only then: teardown.

This adapter refuses `teardown_gpu()` if there are unverified outputs.
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


class ComfyGPUWorkerAdapter:
    profile = "comfy_gpu_worker"

    def __init__(
        self,
        *,
        api_base: Optional[str] = None,
        auth_user: Optional[str] = None,
        auth_password: Optional[str] = None,
    ) -> None:
        self._api_base = api_base or os.environ.get("COMFY_GPU_API_BASE")
        self._user = auth_user or os.environ.get("COMFY_GPU_USER")
        self._password = auth_password or os.environ.get("COMFY_GPU_PASSWORD")
        self._allow_actions = os.environ.get("COMFY_GPU_ALLOW_ACTIONS", "true").lower() == "true"
        # State owned by the adapter — not committed to git.
        self._verified_outputs: Dict[str, bool] = {}

    def capabilities(self) -> Capabilities:
        return Capabilities(
            profile=self.profile,
            output_kinds=[OutputKind.SCENE_CLIPS],
            supports_cancel=False,  # Comfy doesn't expose /cancel; queue clear is the documented path.
            supports_status_poll=True,
            supports_webhook=False,
            aspect_ratios=["9:16", "16:9", "1:1", "4:5"],
            provider_billing_owner="tenant",
            requires_secret=True,
            notes=(
                "Native ComfyUI /prompt API. Workflow JSON versioned per scene. "
                "Teardown blocked until all outputs verified uploaded + checksum matches."
            ),
        )

    def estimate(self, *, pack: dict) -> CostEstimate:
        return CostEstimate(
            amount_usd=None,
            currency="USD",
            unknown=True,
            assumptions="GPU VPS pricing is per-hour; estimate from provider dashboard.",
        )

    def submit(self, *, pack: dict, idempotency_key: str) -> SubmitResult:
        if os.environ.get("LIVE_PROVIDER_TESTS", "false").lower() != "true":
            raise ProviderDisabled("comfy_gpu_worker: set LIVE_PROVIDER_TESTS=true + budget cap")
        if not self._api_base:
            raise ProviderError("COMFY_GPU_API_BASE not configured")
        # Real implementation:
        # 1. Resolve workflow JSON
        # 2. Substitute scene-level parameters
        # 3. POST {api_base}/prompt with auth
        # 4. Receive prompt_id (provider_job_id)
        raise ProviderDisabled(
            "comfy_gpu_worker: contract-only in V1. Live submit gated by LIVE_PROVIDER_TESTS + smoke."
        )

    def status(self, *, provider_job_id: str) -> StatusResult:
        # Real implementation: GET {api_base}/history/{prompt_id}
        return StatusResult(state=JobState.UNKNOWN, raw={"provider_job_id": provider_job_id})

    def fetch_outputs(self, *, provider_job_id: str) -> List[OutputAsset]:
        # Real implementation: parse /history/{id} response, download to storage, verify checksum.
        # Return OutputAsset refs only after verification.
        return []

    def cancel(self, *, provider_job_id: str) -> TeardownResult:
        return TeardownResult(ok=False, detail="Comfy does not expose /cancel. Use /queue clear (manual).")

    def teardown_gpu(self, *, provider_job_id_to_output_refs: Dict[str, List[str]]) -> bool:
        """Refuse teardown until every output is verified uploaded + checksum OK.

        Args:
            provider_job_id_to_output_refs: mapping of provider_job_id -> list of
                output_ref strings. Each ref must already be checked into the
                IdempotencyStore via a `verify_upload(provider_job_id, ref)` call.
        """
        if not self._allow_actions:
            raise TeardownUnsupported("COMFY_GPU_ALLOW_ACTIONS=false; manual teardown only.")
        for pid, refs in provider_job_id_to_output_refs.items():
            if not refs:
                raise TeardownUnsupported(f"no outputs registered for {pid}; refusing teardown.")
            for ref in refs:
                if not self._verified_outputs.get(f"{pid}:{ref}"):
                    raise TeardownUnsupported(
                        f"output {pid}:{ref} not verified uploaded; refusing teardown."
                    )
        # Real implementation: POST {api_base}/free + /queue/clear, then destroy VPS.
        return True

    def verify_upload(self, provider_job_id: str, output_ref: str) -> None:
        """Operator (or storage layer) calls this after upload + checksum verification."""
        self._verified_outputs[f"{provider_job_id}:{output_ref}"] = True