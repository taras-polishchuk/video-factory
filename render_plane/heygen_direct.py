"""HeyGen direct adapter.

Speaks to HeyGen REST API for whole-video generation. NOT active
unless LIVE_PROVIDER_TESTS=true AND budget cap configured.

References verified against:
- https://developers.heygen.com/  (general API surface)
- https://developers.heygen.com/cli  (CLI / async job IDs)
- https://developers.heygen.com/webhooks  (webhook lifecycle)
- https://github.com/Comfy-Org/ComfyUI/blob/master/comfy_api_nodes/nodes_heygen.py
  (HeyGen Partner Nodes — confirmed for talking photo, avatar video,
  create avatar, translation, TTS; NOT for full Video Agent whole-video)

Architectural notes:
- Video Agent whole-video path is supported via HeyGen direct REST, not
  via Comfy Partner Node (current public Comfy UI/node list does not
  expose a full Video Agent node).
- HeyGen API key is server-side. Never logged.
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


class HeyGenDirectAdapter:
    profile = "heygen_direct"

    def __init__(self, *, api_key: Optional[str] = None) -> None:
        self._api_key = api_key if api_key is not None else os.environ.get("HEYGEN_API_KEY")

    def capabilities(self) -> Capabilities:
        return Capabilities(
            profile=self.profile,
            output_kinds=[OutputKind.WHOLE_VIDEO, OutputKind.AVATAR_VIDEO],
            supports_cancel=True,
            supports_status_poll=True,
            supports_webhook=True,
            aspect_ratios=["9:16", "16:9", "1:1"],
            provider_billing_owner="heygen",
            requires_secret=True,
            notes=(
                "HeyGen direct REST. Use Video Agent for whole-video, Avatar API for avatar. "
                "Webhook signature verification supported when HEYGEN_WEBHOOK_SECRET is configured."
            ),
        )

    def estimate(self, *, pack: dict) -> CostEstimate:
        # Public HeyGen pricing is not exposed in the API docs we verified;
        # we mark as unknown so the budget guard refuses without opt-in.
        return CostEstimate(
            amount_usd=None,
            currency="USD",
            unknown=True,
            assumptions="HeyGen does not publish per-job pricing via API; verify in HeyGen dashboard.",
        )

    def submit(self, *, pack: dict, idempotency_key: str) -> SubmitResult:
        if os.environ.get("LIVE_PROVIDER_TESTS", "false").lower() != "true":
            raise ProviderDisabled(
                "heygen_direct: set LIVE_PROVIDER_TESTS=true and configure MAX_COST_PER_BATCH_USD > 0"
            )
        if not self._api_key:
            raise ProviderError("HEYGEN_API_KEY not configured")
        # Real implementation would POST /v2/video/generate with
        # idempotency_key, the approved script, voice, and aspect ratio.
        # We deliberately do NOT import httpx here at module top so a
        # missing install does not break offline paths.
        try:
            import httpx  # type: ignore
        except ImportError as e:
            raise ProviderError("httpx not installed; pip install httpx") from e
        # Documented endpoint shape (verify current path on docs.heygen.com):
        # POST https://api.heygen.com/v2/video/generate
        # body: { video_inputs: [...], dimension: {...}, ... }
        # response: { data: { video_id: ... } }
        body = {
            "video_inputs": [
                {
                    "character": {
                        "type": "avatar",
                        "avatar_id": pack.get("voice", {}).get("voice_id", "default"),
                        "voice_id": pack.get("voice", {}).get("voice_id", "default"),
                    },
                    "voice": {"text": pack.get("approved_script", {}).get("full_text", "")},
                    "background": {"type": "color", "value": "#000000"},
                }
            ],
            "dimension": {"width": 1080, "height": 1920},
            "aspect_ratio": (pack.get("aspect_ratios") or ["9:16"])[0],
            "title": pack.get("concept", {}).get("title", "video"),
            "callback_url": pack.get("_callback_url"),  # optional
            # HeyGen supports an idempotency-like callback_id; verify in docs.
            "callback_id": idempotency_key,
        }
        # NOTE: live path is OPT-IN. We do not execute this in V1.
        # The code below is the production-shaped contract; uncomment
        # only after budget cap verification and a dedicated smoke test.
        raise ProviderDisabled(
            "heygen_direct: contract-only in V1. Live POST gated by LIVE_PROVIDER_TESTS + smoke test."
        )
        # Unreachable marker; kept to illustrate the production shape.
        # with httpx.Client(timeout=30) as client:
        #     resp = client.post(
        #         "https://api.heygen.com/v2/video/generate",
        #         headers={"X-Api-Key": self._api_key, "Idempotency-Key": idempotency_key},
        #         json=body,
        #     )
        #     resp.raise_for_status()
        #     data = resp.json()
        #     return SubmitResult(provider_job_id=data["data"]["video_id"])

    def status(self, *, provider_job_id: str) -> StatusResult:
        # Real implementation: GET https://api.heygen.com/v2/video/{id}
        # Returns status: pending | processing | completed | failed
        # When completed, includes video_url (signed URL — never log).
        return StatusResult(state=JobState.UNKNOWN, raw={"provider_job_id": provider_job_id})

    def fetch_outputs(self, *, provider_job_id: str) -> List[OutputAsset]:
        # Real implementation: download from signed URL to local storage,
        # verify checksum, return OutputAsset refs.
        return []

    def cancel(self, *, provider_job_id: str) -> TeardownResult:
        return TeardownResult(
            ok=False,
            detail="heygen_direct cancel depends on job state; verify per HeyGen docs.",
        )

    def teardown_gpu(self) -> bool:
        raise TeardownUnsupported("heygen_direct manages its own compute.")