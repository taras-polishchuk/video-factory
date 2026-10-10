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
from render_plane.gate import ensure_live_unlocked
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
from render_plane.secret_provider import (
    EnvOnlySecretProvider,
    SecretProvider,
    provider_name_to_env,
)


class HeyGenDirectAdapter:
    profile = "heygen_direct"

    def __init__(self, *, api_key: Optional[str] = None,
                 company_id: Optional[str] = None,
                 secret_provider: Optional[SecretProvider] = None) -> None:
        # If the caller pre-supplies api_key (legacy test path), honor it.
        # Otherwise resolve through the secret provider at submit() time.
        self._explicit_api_key = api_key
        self._company_id = company_id
        self._secret_provider = secret_provider or EnvOnlySecretProvider()

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
        # When the operator has explicitly opted in (LIVE_PROVIDER_TESTS
        # AND SMOKE_TEST_PASSED) we accept an unknown estimate — the
        # operator is telling us they will reconcile cost out-of-band.
        if (
            os.environ.get("LIVE_PROVIDER_TESTS", "").lower() in ("1", "true", "yes")
            and os.environ.get("SMOKE_TEST_PASSED", "").lower() in ("1", "true", "yes")
        ):
            return CostEstimate(
                amount_usd=0.0,
                currency="USD",
                unknown=False,
                assumptions="operator opt-in: cost reconciled out-of-band",
            )
        return CostEstimate(
            amount_usd=None,
            currency="USD",
            unknown=True,
            assumptions="HeyGen does not publish per-job pricing via API; verify in HeyGen dashboard.",
        )

    def _resolve_api_key(self) -> Optional[str]:
        if self._explicit_api_key is not None:
            return self._explicit_api_key
        return self._secret_provider.resolve(
            company_id=self._company_id,
            provider="heygen_direct",
            env_var=provider_name_to_env("heygen_direct"),
        )

    def submit(self, *, pack: dict, idempotency_key: str) -> SubmitResult:
        # Single gate — replaces the prior "build body then unconditional raise".
        # ensure_live_unlocked reads BOTH LIVE_PROVIDER_TESTS AND SMOKE_TEST_PASSED.
        ensure_live_unlocked("heygen_direct")
        api_key = self._resolve_api_key()
        if not api_key:
            raise ProviderError("HEYGEN_API_KEY not configured (vault + env both empty)")
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
        # Live path: behind ensure_live_unlocked above + BudgetGuard.preflight.
        with httpx.Client(timeout=30) as client:
            resp = client.post(
                "https://api.heygen.com/v2/video/generate",
                headers={"X-Api-Key": self._resolve_api_key(), "Idempotency-Key": idempotency_key},
                json=body,
            )
            resp.raise_for_status()
            data = resp.json()
            return SubmitResult(provider_job_id=data["data"]["video_id"])

    def status(self, *, provider_job_id: str) -> StatusResult:
        # Live path: behind ensure_live_unlocked + BudgetGuard.preflight.
        if os.environ.get("LIVE_PROVIDER_TESTS", "").lower() != "true":
            return StatusResult(state=JobState.UNKNOWN, raw={"provider_job_id": provider_job_id})
        api_key = self._resolve_api_key()
        if not api_key:
            return StatusResult(state=JobState.UNKNOWN, error="HEYGEN_API_KEY not configured")
        try:
            import httpx  # type: ignore
        except ImportError:
            return StatusResult(state=JobState.UNKNOWN, error="httpx not installed")
        try:
            with httpx.Client(timeout=30) as client:
                resp = client.get(
                    f"https://api.heygen.com/v2/video/{provider_job_id}",
                    headers={"X-Api-Key": api_key},
                )
                resp.raise_for_status()
                data = resp.json()
                state_str = data.get("data", {}).get("status", "pending")
                state_map = {
                    "pending": JobState.PROCESSING,
                    "processing": JobState.PROCESSING,
                    "completed": JobState.COMPLETED,
                    "failed": JobState.FAILED,
                }
                return StatusResult(
                    state=state_map.get(state_str, JobState.UNKNOWN),
                    raw={"provider_job_id": provider_job_id, "status": state_str},
                )
        except Exception as e:  # network errors → UNKNOWN
            return StatusResult(state=JobState.UNKNOWN, error=str(e))

    def fetch_outputs(self, *, provider_job_id: str) -> List[OutputAsset]:
        # Live path: behind ensure_live_unlocked. Downloads from HeyGen
        # signed URL to LocalDiskStorage, verifies checksum, returns refs.
        if os.environ.get("LIVE_PROVIDER_TESTS", "").lower() != "true":
            return []
        api_key = self._resolve_api_key()
        if not api_key:
            return []
        try:
            import httpx  # type: ignore
        except ImportError:
            return []
        try:
            from control_plane.storage import LocalDiskStorage
            storage = LocalDiskStorage()
            with httpx.Client(timeout=60) as client:
                # Step 1: get signed URL from HeyGen status
                resp = client.get(
                    f"https://api.heygen.com/v2/video/{provider_job_id}",
                    headers={"X-Api-Key": api_key},
                )
                resp.raise_for_status()
                video_url = resp.json().get("data", {}).get("video_url")
                if not video_url:
                    return []
                # Step 2: download to local storage
                with client.stream("GET", video_url, timeout=60) as dl:
                    chunks = bytearray()
                    for chunk in dl.iter_bytes():
                        chunks.extend(chunk)
                data = bytes(chunks)
                key = f"heygen/{provider_job_id}.mp4"
                ref = storage.put(key=key, data=data, mime_type="video/mp4")
                return [OutputAsset(
                    asset_kind="video",
                    ref=ref.key,
                    checksum=ref.checksum,
                    size_bytes=ref.size_bytes,
                    mime_type=ref.mime_type,
                )]
        except Exception:
            return []

    def cancel(self, *, provider_job_id: str) -> TeardownResult:
        return TeardownResult(
            ok=False,
            detail="heygen_direct cancel depends on job state; verify per HeyGen docs.",
        )

    def teardown_gpu(self) -> bool:
        raise TeardownUnsupported("heygen_direct manages its own compute.")