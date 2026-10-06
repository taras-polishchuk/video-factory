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


class ComfyCloudAdapter:
    profile = "comfy_cloud"

    def __init__(
        self,
        *,
        api_base: Optional[str] = None,
        api_key: Optional[str] = None,
        company_id: Optional[str] = None,
        secret_provider: Optional[SecretProvider] = None,
    ) -> None:
        # Backward compat: callers may pass api_base/api_key directly
        # (test path). Otherwise resolve at submit() time.
        self._explicit_api_base = api_base
        self._explicit_api_key = api_key
        self._company_id = company_id
        self._secret_provider = secret_provider or EnvOnlySecretProvider()

    def _resolve_api_base(self) -> Optional[str]:
        if self._explicit_api_base is not None:
            return self._explicit_api_base
        return self._secret_provider.resolve(
            company_id=self._company_id, provider="comfy_cloud",
            env_var="COMFY_CLOUD_API_BASE",
        )

    def _resolve_api_key(self) -> Optional[str]:
        if self._explicit_api_key is not None:
            return self._explicit_api_key
        return self._secret_provider.resolve(
            company_id=self._company_id, provider="comfy_cloud",
            env_var=provider_name_to_env("comfy_cloud"),
        )

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
        # Single gate. ensure_live_unlocked reads BOTH LIVE_PROVIDER_TESTS AND SMOKE_TEST_PASSED.
        ensure_live_unlocked("comfy_cloud")
        api_key = self._resolve_api_key()
        api_base = self._resolve_api_base()
        if not api_key or not api_base:
            raise ProviderError("COMFY_CLOUD_API_BASE / COMFY_CLOUD_API_KEY not configured (vault + env both empty)")
        # Live path (behind ensure_live_unlocked + BudgetGuard.preflight):
        # 1. Resolve the API-format workflow JSON (pack['render_capabilities']['workflow_ref'])
        # 2. Substitute scene-level parameters from pack['scenes']
        # 3. POST {api_base}/workflows with auth + idempotency_key
        # 4. Capture the returned prompt_id (provider_job_id)
        try:
            import httpx  # type: ignore
        except ImportError as e:
            raise ProviderError("httpx not installed; pip install httpx") from e
        workflow_ref = pack.get("render_capabilities", {}).get("workflow_ref")
        if not workflow_ref:
            raise ProviderError("pack.render_capabilities.workflow_ref not set for comfy_cloud")
        body = {
            "workflow": workflow_ref,
            "idempotency_key": idempotency_key,
            "scenes": pack.get("scenes", []),
            "aspect_ratio": (pack.get("aspect_ratios") or ["9:16"])[0],
        }
        with httpx.Client(timeout=30) as client:
            resp = client.post(
                f"{api_base.rstrip('/')}/workflows",
                headers={"Authorization": f"Bearer {api_key}"},
                json=body,
            )
            resp.raise_for_status()
            data = resp.json()
            return SubmitResult(provider_job_id=data.get("prompt_id") or data.get("id") or "")

    def status(self, *, provider_job_id: str) -> StatusResult:
        if os.environ.get("LIVE_PROVIDER_TESTS", "").lower() != "true":
            return StatusResult(state=JobState.UNKNOWN, raw={"provider_job_id": provider_job_id})
        api_key = self._resolve_api_key()
        api_base = self._resolve_api_base()
        if not api_key or not api_base:
            return StatusResult(state=JobState.UNKNOWN, error="COMFY_CLOUD_API_BASE / COMFY_CLOUD_API_KEY not configured")
        try:
            import httpx  # type: ignore
        except ImportError:
            return StatusResult(state=JobState.UNKNOWN, error="httpx not installed")
        try:
            with httpx.Client(timeout=30) as client:
                resp = client.get(
                    f"{api_base.rstrip('/')}/workflows/{provider_job_id}",
                    headers={"Authorization": f"Bearer {api_key}"},
                )
                resp.raise_for_status()
                data = resp.json()
                state_str = data.get("status", "pending")
                state_map = {
                    "queued": JobState.PROCESSING,
                    "running": JobState.PROCESSING,
                    "completed": JobState.COMPLETED,
                    "failed": JobState.FAILED,
                }
                return StatusResult(
                    state=state_map.get(state_str, JobState.UNKNOWN),
                    raw={"provider_job_id": provider_job_id, "status": state_str},
                )
        except Exception as e:
            return StatusResult(state=JobState.UNKNOWN, error=str(e))

    def fetch_outputs(self, *, provider_job_id: str) -> List[OutputAsset]:
        if os.environ.get("LIVE_PROVIDER_TESTS", "").lower() != "true":
            return []
        api_key = self._resolve_api_key()
        api_base = self._resolve_api_base()
        if not api_key or not api_base:
            return []
        try:
            import httpx  # type: ignore
        except ImportError:
            return []
        try:
            from control_plane.storage import LocalDiskStorage
            storage = LocalDiskStorage()
            with httpx.Client(timeout=60) as client:
                resp = client.get(
                    f"{api_base.rstrip('/')}/workflows/{provider_job_id}/outputs",
                    headers={"Authorization": f"Bearer {api_key}"},
                )
                resp.raise_for_status()
                items = resp.json().get("outputs", [])
                outputs = []
                for item in items:
                    url = item.get("url")
                    if not url:
                        continue
                    with client.stream("GET", url, timeout=60) as dl:
                        chunks = bytearray()
                        for chunk in dl.iter_bytes():
                            chunks.extend(chunk)
                    data = bytes(chunks)
                    key = f"comfy_cloud/{provider_job_id}/{item.get('name', 'clip.mp4')}"
                    ref = storage.put(key=key, data=data, mime_type=item.get("mime_type", "video/mp4"))
                    outputs.append(OutputAsset(
                        asset_kind="scene_clip",
                        ref=ref.key,
                        checksum=ref.checksum,
                        size_bytes=ref.size_bytes,
                        mime_type=ref.mime_type,
                        scene_id=item.get("scene_id"),
                    ))
                return outputs
        except Exception:
            return []

    def cancel(self, *, provider_job_id: str) -> TeardownResult:
        if os.environ.get("LIVE_PROVIDER_TESTS", "").lower() != "true":
            return TeardownResult(ok=False, detail="live path not unlocked")
        api_key = self._resolve_api_key()
        api_base = self._resolve_api_base()
        if not api_key or not api_base:
            return TeardownResult(ok=False, detail="credentials not configured")
        try:
            import httpx  # type: ignore
        except ImportError:
            return TeardownResult(ok=False, detail="httpx not installed")
        try:
            with httpx.Client(timeout=15) as client:
                resp = client.delete(
                    f"{api_base.rstrip('/')}/workflows/{provider_job_id}",
                    headers={"Authorization": f"Bearer {api_key}"},
                )
                resp.raise_for_status()
                return TeardownResult(ok=True, detail=f"deleted workflow {provider_job_id}")
        except Exception as e:
            return TeardownResult(ok=False, detail=str(e))

    def teardown_gpu(self) -> bool:
        raise TeardownUnsupported("comfy_cloud manages its own compute.")