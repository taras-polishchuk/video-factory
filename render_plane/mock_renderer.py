"""Mock renderer.

Works offline. Walks the full production state machine. Generates
small fixture assets so downstream assembly + QC can be exercised.

NEVER masquerades as a real provider. Output files carry a sentinel
header so consumers and tests can distinguish them.
"""

from __future__ import annotations

import io
import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from control_plane.errors import ProviderError, TeardownUnsupported
from control_plane.idempotency import (
    IdempotencyStore,
    ProviderSubmitReceipt,
    make_idem_key,
)
from control_plane.storage import ArtifactRef, LocalDiskStorage, Storage
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


MOCK_SENTINEL = b"MOCKRENDERv1\x00\x00"


@dataclass
class MockRendererConfig:
    """Tunable knobs for tests + offline demo."""
    simulate_processing_seconds: float = 0.0
    fail_after_submit: bool = False
    output_duration_seconds: float = 0.5
    output_size_bytes: int = 4096
    asset_kind: str = "video"
    mime_type: str = "video/mp4"


class MockRenderer:
    """In-memory + on-disk mock. Idempotency-safe."""

    profile = "mock"

    def __init__(self, storage: Optional[Storage] = None) -> None:
        self._store = IdempotencyStore()
        self._jobs: Dict[str, "MockJob"] = {}
        self._lock = threading.Lock()
        self.storage = storage or LocalDiskStorage()
        self.config = MockRendererConfig()

    def set_idempotency_store(self, store: "IdempotencyStore") -> None:
        """Replace the internal store with one owned by the orchestrator.

        Required for end-to-end idempotency: the orchestrator is the
        canonical owner of the idempotency map so a retry sees the same
        receipt even if the renderer instance is rebuilt.
        """
        self._store = store

    def capabilities(self) -> Capabilities:
        return Capabilities(
            profile=self.profile,
            output_kinds=[OutputKind.SCENE_CLIPS, OutputKind.WHOLE_VIDEO, OutputKind.TTS],
            supports_cancel=True,
            supports_status_poll=True,
            supports_webhook=False,
            aspect_ratios=["9:16", "16:9", "1:1", "4:5"],
            provider_billing_owner="mock",
            requires_secret=False,
            notes="Offline renderer. Outputs are deterministic fixtures, not real media.",
        )

    def estimate(self, *, pack: dict) -> CostEstimate:
        return CostEstimate(
            amount_usd=0.0,
            currency="USD",
            range_low_usd=0.0,
            range_high_usd=0.0,
            assumptions="mock path is free; offline only",
            unknown=False,
        )

    def submit(self, *, pack: dict, idempotency_key: str) -> SubmitResult:
        # Pre-existing key → replay.
        existing = self._store.lookup(idempotency_key)
        if existing is not None:
            return SubmitResult(
                provider_job_id=existing.provider_job_id,
                accepted_metadata={"replay": "true", **(existing.accepted_metadata or {})},
            )
        provider_job_id = f"mockjob_{uuid.uuid4().hex[:12]}"
        receipt = ProviderSubmitReceipt(
            provider_job_id=provider_job_id,
            accepted_metadata={"submitted_at": str(time.time())},
        )
        # atomic-ish: only the first caller stores the receipt
        with self._lock:
            if idempotency_key in self._store._by_key:  # noqa: SLF001 (test-time access)
                existing = self._store.lookup(idempotency_key)
                return SubmitResult(
                    provider_job_id=existing.provider_job_id if existing else provider_job_id,
                    accepted_metadata={"replay": "true"},
                )
            self._store.save(idempotency_key, receipt)
            self._jobs[provider_job_id] = MockJob(
                provider_job_id=provider_job_id,
                pack=pack,
                idem_key=idempotency_key,
                config=self.config,
                storage=self.storage,
            )
        if self.config.fail_after_submit:
            with self._lock:
                self._jobs[provider_job_id].state = JobState.FAILED
            raise ProviderError("mock configured to fail after submit")
        return SubmitResult(provider_job_id=provider_job_id, accepted_metadata=receipt.accepted_metadata)

    def status(self, *, provider_job_id: str) -> StatusResult:
        job = self._jobs.get(provider_job_id)
        if job is None:
            return StatusResult(state=JobState.UNKNOWN, error="unknown provider_job_id")
        if job.state == JobState.COMPLETED:
            return StatusResult(
                state=JobState.COMPLETED,
                progress=1.0,
                outputs=job.outputs,
                cost_actual_usd=0.0,
            )
        # If simulated processing time configured, advance
        if self.config.simulate_processing_seconds > 0:
            time.sleep(self.config.simulate_processing_seconds)
        job.run()
        return StatusResult(
            state=job.state,
            progress=job.progress,
            outputs=job.outputs,
            cost_actual_usd=0.0,
        )

    def fetch_outputs(self, *, provider_job_id: str) -> List[OutputAsset]:
        job = self._jobs.get(provider_job_id)
        if job is None:
            return []
        return list(job.outputs)

    def cancel(self, *, provider_job_id: str) -> TeardownResult:
        job = self._jobs.get(provider_job_id)
        if job is None:
            return TeardownResult(ok=False, detail="unknown provider_job_id")
        job.state = JobState.CANCELLED
        return TeardownResult(ok=True, detail="mock supports cancel")

    def teardown_gpu(self) -> bool:
        raise TeardownUnsupported(
            "mock renderer has no GPU; teardown is a no-op. Real adapters must verify upload + checksum first."
        )


@dataclass
class MockJob:
    provider_job_id: str
    pack: dict
    idem_key: str
    config: MockRendererConfig
    storage: Storage
    state: JobState = JobState.PENDING
    progress: float = 0.0
    outputs: List[OutputAsset] = field(default_factory=list)

    def run(self) -> None:
        if self.state in {JobState.COMPLETED, JobState.FAILED, JobState.CANCELLED}:
            return
        self.state = JobState.PROCESSING
        self.progress = 0.5
        # For scene-clips output we mint one OutputAsset per scene.
        # For whole-video we mint a single combined asset.
        output_kind = self.pack.get("render_capabilities", {}).get("output_kind", "scene_clips")
        storage = self.storage
        if output_kind == "whole_video":
            asset = self._mint(storage)
            self.outputs.append(asset)
        else:
            for scene in self.pack.get("scenes", []):
                scene_id = scene.get("scene_id", "sc_unknown")
                asset = self._mint(storage, scene_id=scene_id)
                self.outputs.append(asset)
        self.progress = 1.0
        self.state = JobState.COMPLETED

    def _mint(self, storage: Storage, scene_id: Optional[str] = None) -> OutputAsset:
        # Construct a deterministic mock asset. The sentinel header
        # bytes make it impossible to mistake for a real video.
        suffix = scene_id or "whole"
        key = f"mock/{self.pack.get('pack_id', 'vp_unknown')}/{suffix}.mp4"
        # Build a payload of sentinel + json metadata + filler.
        meta = json.dumps({"scene_id": scene_id, "mock": True, "id": self.provider_job_id})
        payload = MOCK_SENTINEL + meta.encode("utf-8") + b"\x00" * self.config.output_size_bytes
        ref = storage.put(key=key, data=payload, mime_type=self.config.mime_type)
        return OutputAsset(
            asset_kind=self.config.asset_kind,
            ref=ref.key,
            checksum=ref.checksum,
            size_bytes=ref.size_bytes,
            mime_type=ref.mime_type,
            duration_seconds=self.config.output_duration_seconds,
            scene_id=scene_id,
        )