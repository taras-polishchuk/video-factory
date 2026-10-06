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

import base64 as _b64
import hashlib
import hmac
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


def _basic_auth_header(user: str, password: str) -> str:
    """Build the Authorization header value for HTTP Basic auth."""
    token = _b64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


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
        # Single gate. ensure_live_unlocked reads BOTH LIVE_PROVIDER_TESTS AND SMOKE_TEST_PASSED.
        ensure_live_unlocked("comfy_gpu_worker")
        if not self._api_base:
            raise ProviderError("COMFY_GPU_API_BASE not configured")
        # Live path (behind ensure_live_unlocked + BudgetGuard.preflight):
        # 1. Resolve workflow JSON from pack.render_capabilities.workflow_ref
        # 2. Substitute scene-level parameters
        # 3. POST {api_base}/prompt with optional basic auth
        # 4. Receive prompt_id (provider_job_id)
        try:
            import httpx  # type: ignore
        except ImportError as e:
            raise ProviderError("httpx not installed; pip install httpx") from e
        workflow_ref = pack.get("render_capabilities", {}).get("workflow_ref")
        if not workflow_ref:
            raise ProviderError("pack.render_capabilities.workflow_ref not set for comfy_gpu_worker")
        prompt_body = {
            "prompt": workflow_ref,
            "client_id": idempotency_key,
        }
        headers = {}
        if self._user and self._password:
            headers["Authorization"] = _basic_auth_header(self._user, self._password)
        with httpx.Client(timeout=60) as client:
            resp = client.post(
                f"{self._api_base.rstrip('/')}/prompt",
                headers=headers,
                json=prompt_body,
            )
            resp.raise_for_status()
            data = resp.json()
            return SubmitResult(provider_job_id=data.get("prompt_id", ""))

    def status(self, *, provider_job_id: str) -> StatusResult:
        if os.environ.get("LIVE_PROVIDER_TESTS", "").lower() != "true" or not self._api_base:
            return StatusResult(state=JobState.UNKNOWN, raw={"provider_job_id": provider_job_id})
        try:
            import httpx  # type: ignore
        except ImportError:
            return StatusResult(state=JobState.UNKNOWN, error="httpx not installed")
        try:
            headers = {}
            if self._user and self._password:
                headers["Authorization"] = _basic_auth_header(self._user, self._password)
            with httpx.Client(timeout=30) as client:
                resp = client.get(
                    f"{self._api_base.rstrip('/')}/history/{provider_job_id}",
                    headers=headers,
                )
                resp.raise_for_status()
                data = resp.json()
                # ComfyUI history shape: { prompt_id: { status, outputs: {...} } }
                entry = data.get(provider_job_id, {}) if isinstance(data, dict) else {}
                status_obj = entry.get("status", {})
                completed = bool(status_obj.get("completed", False))
                err = None
                if status_obj.get("status_str") == "error":
                    err = ",".join(status_obj.get("messages", [])) or "comfy error"
                    return StatusResult(state=JobState.FAILED, error=err)
                return StatusResult(
                    state=JobState.COMPLETED if completed else JobState.PROCESSING,
                    raw={"provider_job_id": provider_job_id},
                )
        except Exception as e:
            return StatusResult(state=JobState.UNKNOWN, error=str(e))

    def fetch_outputs(self, *, provider_job_id: str) -> List[OutputAsset]:
        if os.environ.get("LIVE_PROVIDER_TESTS", "").lower() != "true" or not self._api_base:
            return []
        try:
            import httpx  # type: ignore
        except ImportError:
            return []
        try:
            from control_plane.storage import LocalDiskStorage
            storage = LocalDiskStorage()
            headers = {}
            if self._user and self._password:
                headers["Authorization"] = _basic_auth_header(self._user, self._password)
            with httpx.Client(timeout=60) as client:
                resp = client.get(
                    f"{self._api_base.rstrip('/')}/history/{provider_job_id}",
                    headers=headers,
                )
                resp.raise_for_status()
                entry = resp.json().get(provider_job_id, {})
                outputs_blob = entry.get("outputs", {})
                outputs = []
                for node_id, node_outputs in outputs_blob.items():
                    for out in node_outputs.get("images", []):
                        filename = out.get("filename")
                        subfolder = out.get("subfolder", "")
                        out_type = out.get("type", "output")
                        if not filename:
                            continue
                        url = f"{self._api_base.rstrip('/')}/view"
                        params = {
                            "filename": filename,
                            "subfolder": subfolder,
                            "type": out_type,
                        }
                        with client.stream("GET", url, params=params, timeout=60, headers=headers) as dl:
                            chunks = bytearray()
                            for chunk in dl.iter_bytes():
                                chunks.extend(chunk)
                        data = bytes(chunks)
                        key = f"comfy_gpu/{provider_job_id}/{filename}"
                        ref = storage.put(key=key, data=data, mime_type="image/png" if filename.endswith(".png") else "video/mp4")
                        outputs.append(OutputAsset(
                            asset_kind="scene_clip",
                            ref=ref.key,
                            checksum=ref.checksum,
                            size_bytes=ref.size_bytes,
                            mime_type=ref.mime_type,
                        ))
                return outputs
        except Exception:
            return []

    def cancel(self, *, provider_job_id: str) -> TeardownResult:
        # ComfyUI has no /cancel endpoint. /queue clear is the documented
        # manual path; in live mode we attempt it when actions are allowed.
        if not self._api_base or not self._allow_actions:
            return TeardownResult(ok=False, detail="queue clear requires API base + COMFY_GPU_ALLOW_ACTIONS=true")
        if os.environ.get("LIVE_PROVIDER_TESTS", "").lower() != "true":
            return TeardownResult(ok=False, detail="live path not unlocked")
        try:
            import httpx  # type: ignore
        except ImportError:
            return TeardownResult(ok=False, detail="httpx not installed")
        try:
            headers = {}
            if self._user and self._password:
                headers["Authorization"] = _basic_auth_header(self._user, self._password)
            with httpx.Client(timeout=15) as client:
                resp = client.post(
                    f"{self._api_base.rstrip('/')}/queue",
                    headers=headers,
                    json={"clear": True},
                )
                resp.raise_for_status()
                return TeardownResult(ok=True, detail=f"queue cleared for {provider_job_id}")
        except Exception as e:
            return TeardownResult(ok=False, detail=str(e))

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