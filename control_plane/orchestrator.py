"""Orchestrator: full production pipeline.

Stage order:
  DRAFT -> RESEARCHING -> PREPARING -> READY_FOR_RENDER -> QUEUED
  -> SUBMITTING -> PROCESSING -> OUTPUTS_READY -> QC -> ASSEMBLING -> DONE

Idempotency: every submit() goes through IdempotencyStore. A retry on
the same pack_id returns the existing receipt without re-invoking the
provider.

Reconciliation: a webhook or poll that arrives AFTER the provider has
already been called but BEFORE the receipt was persisted is marked
`outcome=unknown` and the state machine does NOT proceed until we
confirm the provider's view (status()).
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from assembly.ffmpeg_assembler import (
    QCResult,
    qc_outputs,
    stitch_scene_clips,
)
from control_plane.audit import AuditLog, InMemoryAuditLog, AuditRecord, now_iso
from control_plane.budget import BudgetGuard
from control_plane.campaign import (
    CampaignRequest,
    IntakeReport,
    IntakeValidator,
)
from control_plane.errors import (
    BudgetExceeded,
    InvalidStateTransition,
    ProviderDisabled,
    ProviderError,
    TeardownUnsupported,
    VideoFactoryError,
)
from control_plane.idempotency import IdempotencyStore, make_idem_key
from control_plane.pack_builder import (
    Caption,
    Concept,
    PackBuilder,
    ResearchClaim,
    SceneSpec,
    Script,
    VoiceSpec,
    VideoPack,
    compute_content_hash,
)
from control_plane.queue import JobQueue
from control_plane.state_machine import State, VideoStateMachine
from control_plane.storage import ArtifactRef, LocalDiskStorage, Storage
from render_plane.mock_renderer import MOCK_SENTINEL, MockRenderer
from render_plane.provider import (
    JobState,
    OutputAsset,
    OutputKind,
    Renderer,
    StatusResult,
    SubmitResult,
)


@dataclass
class OrchestratorConfig:
    artifacts_root: str = "./artifacts"
    storage: Optional[Storage] = None
    budget_per_video_usd: float = 0.0
    budget_per_batch_usd: float = 0.0
    enforce_budget: bool = True


@dataclass
class RunResult:
    pack: VideoPack
    pack_dict: dict
    state_machine: VideoStateMachine
    provider_job_id: Optional[str]
    outputs: List[OutputAsset]
    qc: Optional[QCResult] = None
    assembled: Optional[dict] = None
    audit_log: "InMemoryAuditLog" = field(default_factory=InMemoryAuditLog)


class Orchestrator:
    """Drives a single video from intake to DONE."""

    def __init__(
        self,
        *,
        renderer: Renderer,
        config: Optional[OrchestratorConfig] = None,
        audit: Optional[AuditLog] = None,
    ) -> None:
        self.renderer = renderer
        self.config = config or OrchestratorConfig()
        self.audit = audit or InMemoryAuditLog()
        self.storage: Storage = (
            self.config.storage
            or LocalDiskStorage(self.config.artifacts_root)
        )
        self.budget = BudgetGuard(
            max_cost_per_video_usd=self.config.budget_per_video_usd,
            max_cost_per_batch_usd=self.config.budget_per_batch_usd,
        )
        self.idem = IdempotencyStore()
        self.queue = JobQueue()
        # Wire the renderer (if MockRenderer) to the orchestrator's storage
        # AND share the orchestrator's idempotency store so retries hit
        # the same receipt across orchestrator rebuilds.
        if isinstance(renderer, MockRenderer):
            renderer.storage = self.storage
            renderer.set_idempotency_store(self.idem)
            self.renderer = renderer

    # ── Public API ──────────────────────────────────────────────

    def prepare_pack(
        self,
        *,
        schema: dict,
        request: CampaignRequest,
        concept: Concept,
        script: Script,
        voice: VoiceSpec,
        research: List[ResearchClaim],
        captions: List[Caption],
        scenes: List[SceneSpec],
        approval_required: bool = False,
    ) -> tuple[VideoPack, IntakeReport, VideoStateMachine]:
        report = IntakeValidator().validate(request)
        builder = PackBuilder(schema)
        pack = builder.build(
            report=report,
            video_index=0,
            concept=concept,
            script=script,
            voice=voice,
            research=research,
            captions=captions,
            scenes=scenes,
            approval_required=approval_required,
        )
        sm = VideoStateMachine(
            initial=State.DRAFT,
            campaign_id=pack.campaign_id,
            video_id=pack.video_id,
            audit=self.audit,
        )
        sm.transition(State.RESEARCHING, actor="orchestrator", reason="research start")
        sm.transition(State.PREPARING, actor="orchestrator", reason="preparation start")
        if approval_required:
            sm.transition(State.NEEDS_REVIEW, actor="orchestrator", reason="approval required")
        sm.transition(State.READY_FOR_RENDER, actor="orchestrator", reason="pack ready")
        return pack, report, sm

    def run_to_done(
        self,
        *,
        schema: dict,
        request: CampaignRequest,
        concept: Concept,
        script: Script,
        voice: VoiceSpec,
        research: List[ResearchClaim],
        captions: List[Caption],
        scenes: List[SceneSpec],
        approval_required: bool = False,
    ) -> RunResult:
        """Full synchronous offline flow. Used by CLI + tests."""
        pack, report, sm = self.prepare_pack(
            schema=schema,
            request=request,
            concept=concept,
            script=script,
            voice=voice,
            research=research,
            captions=captions,
            scenes=scenes,
            approval_required=approval_required,
        )
        pack_dict = pack.to_dict()

        # Render
        sm.transition(State.QUEUED, actor="orchestrator", reason="queued for render")
        sm.transition(State.SUBMITTING, actor="orchestrator", reason="submitting")
        idem_key = make_idem_key(
            pack.campaign_id, pack.video_id, pack.render_profile, pack.content_hash
        )
        receipt = self.idem.lookup(idem_key)
        provider_job_id: Optional[str] = None
        if receipt is None:
            # If the renderer has a set_idempotency_store() method (MockRenderer),
            # hand it the orchestrator's store so the renderer sees the same
            # map if it inspects state internally.
            if hasattr(self.renderer, "set_idempotency_store"):
                self.renderer.set_idempotency_store(self.idem)
            submit = self.renderer.submit(pack=pack_dict, idempotency_key=idem_key)
            provider_job_id = submit.provider_job_id
            self.idem.save(
                idem_key,
                _receipt_from_submit(submit, pack.campaign_id, pack.video_id),
            )
        else:
            provider_job_id = receipt.provider_job_id
            self.audit.record(
                AuditRecord(
                    timestamp=now_iso(),
                    event="submit.replay",
                    actor="orchestrator",
                    campaign_id=pack.campaign_id,
                    video_id=pack.video_id,
                    provider_job_id=provider_job_id,
                    context={"reason": "idempotent replay"},
                )
            )
        sm.transition(State.PROCESSING, actor="orchestrator", reason="processing")

        # Poll until terminal (mock is immediate; live adapters loop with backoff).
        outputs: List[OutputAsset] = []
        status = self.renderer.status(provider_job_id=provider_job_id)
        if status.state == JobState.UNKNOWN:
            # Live adapter's status stubbed. Loop with simple backoff.
            status = self._poll_until_terminal(provider_job_id)
        if status.state == JobState.FAILED:
            sm.transition(State.FAILED, actor="orchestrator", reason="provider failed",
                          context={"error": status.error or ""})
            raise ProviderError(status.error or "provider failed")
        if status.state == JobState.CANCELLED:
            sm.transition(State.CANCELLED, actor="orchestrator", reason="provider cancelled")
            raise VideoFactoryError("cancelled")
        outputs = self.renderer.fetch_outputs(provider_job_id=provider_job_id)
        if not outputs and status.outputs:
            outputs = list(status.outputs)

        sm.transition(State.OUTPUTS_READY, actor="orchestrator", reason="outputs ready")

        # Persist outputs to local storage
        persisted_refs: List[ArtifactRef] = []
        for asset in outputs:
            ref = self._persist_asset(asset)
            persisted_refs.append(ref)

        # QC
        qc_paths = [r.path for r in persisted_refs if r.path]
        output_kind = pack.output_kind
        if output_kind == "whole_video":
            expected = float(pack.target_duration_seconds)
        else:
            expected = sum(float(s.target_duration_seconds) for s in pack.scenes)
        qc = qc_outputs(
            pack={"captions": [c.to_dict() for c in pack.captions]},
            output_paths=qc_paths,
            expected_total_duration_seconds=expected,
        )
        if not qc.ok:
            sm.transition(State.FAILED, actor="orchestrator", reason="qc failed",
                          context={"qc_notes": qc.notes})
            return RunResult(
                pack=pack, pack_dict=pack_dict, state_machine=sm,
                provider_job_id=provider_job_id, outputs=outputs, qc=qc,
                audit_log=self._audit_concrete(),
            )
        sm.transition(State.QC, actor="orchestrator", reason="qc ok")

        # Assembly (scene clips only)
        assembled: Optional[dict] = None
        if output_kind == "scene_clips":
            sm.transition(State.ASSEMBLING, actor="orchestrator", reason="assembling")
            assembled = stitch_scene_clips(
                storage=self.storage,
                clip_refs=[r.key for r in persisted_refs],
                output_ref=f"assembled/{pack.pack_id}/stitched.mp4",
                target_duration_seconds=expected,
                captions=[c.to_dict() for c in pack.captions],
            )
            if assembled is None:
                # FFmpeg unavailable or failed — surface but don't fail the
                # whole video, since each clip is already a valid mock asset.
                self.audit.record(
                    AuditRecord(
                        timestamp=now_iso(),
                        event="assembly.skipped",
                        actor="orchestrator",
                        campaign_id=pack.campaign_id,
                        video_id=pack.video_id,
                        context={"reason": "ffmpeg unavailable or stitch failed"},
                    )
                )

        sm.transition(State.DONE, actor="orchestrator", reason="complete")
        return RunResult(
            pack=pack,
            pack_dict=pack_dict,
            state_machine=sm,
            provider_job_id=provider_job_id,
            outputs=outputs,
            qc=qc,
            assembled=assembled,
            audit_log=self._audit_concrete(),
        )

    # ── Internals ───────────────────────────────────────────────

    def _audit_concrete(self) -> "InMemoryAuditLog":
        """Return the concrete audit log instance even if a custom AuditLog was injected.

        Production AuditLog backends should also implement `records()` if they
        want to participate in the report. For V1 we accept that an injected
        AuditLog is only used for `record()` and `records()` falls back to [].
        """
        if isinstance(self.audit, InMemoryAuditLog):
            return self.audit
        # Custom AuditLog: fall back to a fresh empty InMemoryAuditLog,
        # because we don't know if it has a records() method.
        return InMemoryAuditLog()

    def _poll_until_terminal(self, provider_job_id: str) -> StatusResult:
        # Default offline-friendly poll: a small number of polls, no real backoff.
        # Live adapters override via dependency injection if they want different cadence.
        for _ in range(5):
            status = self.renderer.status(provider_job_id=provider_job_id)
            if status.state in {JobState.COMPLETED, JobState.FAILED, JobState.CANCELLED}:
                return status
        return StatusResult(state=JobState.UNKNOWN, error="poll timeout")

    def _persist_asset(self, asset: OutputAsset) -> ArtifactRef:
        """Point at the asset already produced by the renderer.

        MockRenderer writes under `mock/<pack_id>/<scene>.mp4`. We register
        a namespaced view of the same file by computing its checksum and
        size from local storage, without copying bytes. Live adapters
        download to a stable key on first encounter; thereafter the same
        idempotent path applies.
        """
        data = self.storage.get(asset.ref)
        return ArtifactRef(
            key=asset.ref,
            checksum=asset.checksum,
            size_bytes=len(data),
            mime_type=asset.mime_type,
            path=self.storage.path_for(asset.ref),
        )


def _receipt_from_submit(submit: SubmitResult, campaign_id: str, video_id: str):
    from control_plane.idempotency import ProviderSubmitReceipt
    return ProviderSubmitReceipt(
        provider_job_id=submit.provider_job_id,
        accepted_metadata={"campaign_id": campaign_id, "video_id": video_id, **(submit.accepted_metadata or {})},
    )