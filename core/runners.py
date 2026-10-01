"""Background job runner.

Drives an Orchestrator through stages off the request thread. Each
job gets a sequence of stages recorded in the repo. Idempotency is
durable (job_id + idempotency key). Restart-safe within the SQLite
session (jobs remain in their last state on restart; no in-memory
state required to resume the *runner*).
"""
from __future__ import annotations

import json
import shutil
import threading
import time
from pathlib import Path
from typing import Optional

from control_plane.idempotency import IdempotencyStore, ProviderSubmitReceipt
from control_plane.idempotency import DurableIdempotencyStore
from control_plane.storage import LocalDiskStorage
from control_plane.orchestrator import Orchestrator, OrchestratorConfig
from core.budget_enforcer import BudgetEnforcer, preflight_or_block
from control_plane.pack_builder import (
    Caption, Concept, PackBuilder, ResearchClaim, SceneSpec, Script, VoiceSpec,
)
from render_plane.mock_renderer import MockRenderer

from core.edit_plan import build_plan, render_plan_to_storage
from core.repo import NotFound, Repo


# Ordered stage sequence. The orchestrator covers the latter half.
STAGE_SEQUENCE = [
    "brief",
    "research",
    "creative_direction",
    "script_captions",
    "storyboard",
    "generate_assets",
    "voice",
    "edit_plan",
    "assembly",
    "qc",
    "human_review",
    "export_publish",
]


def _duration_ms(start: float) -> int:
    return int((time.monotonic() - start) * 1000)


def _build_request_for(job: dict):
    from control_plane.campaign import CampaignRequest
    return CampaignRequest(
        topic=job["topic"],
        audience=job["topic"],
        goal="demonstrate the pipeline",
        target_duration_seconds=job["duration_seconds"],
        language=job["language"],
        platforms=list(job["platforms"]),
        count=job["count"],
        render_profile=job["render_profile"],
        approval_policy="needs_review" if job["approval_required"] else "auto",
    )


def _build_pack_for_job(job: dict, *, schema: dict, bible: dict) -> dict:
    """Translate job + bible into a VideoPack dict."""
    payload = bible.get("payload", {}) if bible else {}
    topic = payload.get("positioning") or job["topic"]
    audience = payload.get("audience") or job["topic"]
    voice_tone = payload.get("tone") or "neutral"
    approved_script = (
        payload.get("approved_script")
        or f"A short {job['duration_seconds']}s video about {job['topic']}. "
           f"Generated in mock mode."
    )
    concept = Concept(
        title=job["topic"],
        logline=f"Mock {job['aspect_ratio']} explainer for {job['topic']}.",
        tone=voice_tone,
        audience=audience,
        cta=(payload.get("cta") or "Try it. Link in bio."),
        negative_rules=list(payload.get("prohibited") or ["Do not make false claims."]),
    )
    research = [ResearchClaim(
        text=f"Mock research claim for {job['topic']}.",
        source_title="Mock source",
        source_url="https://example.com/mock",
        confidence="unresolved",
    )]
    script = Script(full_text=approved_script, approved_by="mock-runner")
    voice = VoiceSpec(provider="mock", voice_id="mock_en_neutral", ssml_or_text=approved_script)
    captions = []
    body = approved_script.split(".")
    seg = max(2.0, job["duration_seconds"] / max(1, len(body)))
    t = 0.0
    for line in body:
        line = line.strip()
        if not line:
            continue
        captions.append(Caption(t, t + seg, line + "."))
        t += seg
    n_scenes = max(2, min(4, job["count"] + 2))
    per_scene = job["duration_seconds"] / n_scenes
    scenes = [
        SceneSpec(
            scene_id=f"sc_{i+1}",
            order=i + 1,
            target_duration_seconds=per_scene,
            visual_intent=f"Mock scene {i+1} illustrating {job['topic']}.",
            motion_prompt="mock motion, smooth, calm pacing",
        )
        for i in range(n_scenes)
    ]
    from control_plane.campaign import IntakeValidator
    report = IntakeValidator().validate(_build_request_for(job))
    pack = PackBuilder(schema).build(
        report=report,
        video_index=0,
        concept=concept,
        script=script,
        voice=voice,
        research=research,
        captions=captions,
        scenes=scenes,
        approval_required=job["approval_required"],
    )
    return pack.to_dict()


class JobRunner:
    """Runs jobs in a background thread pool. State persists to SQLite."""

    def __init__(self, *, repo: Repo, storage_root: str, schema: dict,
                 budget_per_video_usd: float = 0.0,
                 budget_per_batch_usd: float = 0.0) -> None:
        self.repo = repo
        self.storage_root = Path(storage_root).resolve()
        self.storage_root.mkdir(parents=True, exist_ok=True)
        self.schema = schema
        self.budget_per_video_usd = budget_per_video_usd
        self.budget_per_batch_usd = budget_per_batch_usd
        self._lock = threading.Lock()
        self._active_jobs: dict[str, threading.Thread] = {}
        self._idem: dict[str, IdempotencyStore] = {}
        # Durable idempotency: SQLite-backed so receipts survive restart.
        idem_db = self.storage_root / "idem.db"
        self._idem_store = DurableIdempotencyStore(str(idem_db))
        self._budget = BudgetEnforcer(
            max_cost_per_video_usd=budget_per_video_usd,
            max_cost_per_batch_usd=budget_per_batch_usd,
        )

    def _idem_for(self, job_id: str) -> IdempotencyStore:
        """Return the in-memory store the MockRenderer expects.

        We seed it from the durable store at construction time so that
        on retry/restart the renderer sees prior receipts. New submits
        write to the in-memory store; we mirror the receipt into the
        durable store as the source of truth.
        """
        with self._lock:
            key = f"_idem_inmem_{id(self)}"
            if key not in self._idem:
                inmem = IdempotencyStore()
                # Seed from durable
                try:
                    rows = self._idem_store._conn.execute(
                        "SELECT idem_key, provider_job_id, metadata_json FROM idem_receipts"
                    ).fetchall()
                    for k, pid, meta in rows:
                        inmem.save(k, ProviderSubmitReceipt(
                            provider_job_id=pid,
                            accepted_metadata=json.loads(meta) if meta else {},
                        ))
                except Exception:
                    pass
                self._idem[key] = inmem
                self._idem_durable_mirror = self._idem_store  # for _write_durable
            return self._idem[key]

    def _write_durable(self, idem_key: str, receipt) -> None:
        try:
            self._idem_store.save(idem_key, receipt)
        except Exception:
            pass

    def submit(self, job_id: str) -> None:
        """Schedule a job. Returns immediately; runs in background."""
        if job_id in self._active_jobs and self._active_jobs[job_id].is_alive():
            return
        t = threading.Thread(target=self._run, args=(job_id,), daemon=True, name=f"job-{job_id}")
        self._active_jobs[job_id] = t
        t.start()

    def _stage_started(self, job_id: str, key: str, ordinal: int) -> tuple[float, str]:
        from core.repo import now_iso
        stage = self.repo.upsert_stage(
            job_id=job_id, stage_key=key, ordinal=ordinal, status="running",
            started_at=now_iso(),
        )
        self.repo.update_job(job_id, current_stage=key, state="running",
                              status_message=f"running {key}")
        return time.monotonic(), stage["stage_id"]

    def _stage_done(self, job_id: str, key: str, ordinal: int,
                    start: float, summary: Optional[dict] = None,
                    error: Optional[str] = None) -> None:
        from core.repo import now_iso
        self.repo.upsert_stage(
            job_id=job_id, stage_key=key, ordinal=ordinal,
            status="failed" if error else "done",
            finished_at=now_iso(),
            duration_ms=_duration_ms(start),
            summary=summary or {},
            error=error,
        )

    def _run(self, job_id: str) -> None:
        from core.repo import now_iso
        try:
            job = self.repo.get_job(job_id)
            bible = self.repo.get_bible(job["bible_id"]) if job.get("bible_id") else {"payload": {}}
        except NotFound:
            return

        ordinal = 0
        # One global storage handle for the entire run. Avoids
        # per-stage reimports that would shadow the module-level import.
        global_storage = LocalDiskStorage(root=str(self.storage_root))
        try:
            # Stages 1-5: synthetic mock planning phases
            for stage_key in STAGE_SEQUENCE[:5]:
                ordinal += 1
                start, _ = self._stage_started(job_id, stage_key, ordinal)
                time.sleep(0.05)
                self._stage_done(job_id, stage_key, ordinal, start,
                                 summary={"note": f"mock {stage_key}"})

            # Stage 6: generate_assets via mock renderer
            ordinal += 1
            start, _ = self._stage_started(job_id, "generate_assets", ordinal)
            pack_dict = _build_pack_for_job(job, schema=self.schema, bible=bible)
            render_storage = LocalDiskStorage(root=str(self.storage_root))
            renderer = MockRenderer()
            renderer.storage = render_storage
            idem = self._idem_for(job_id)
            renderer.set_idempotency_store(idem)
            pack_id = pack_dict["pack_id"]
            idem_key = pack_id  # content-derived key for the receipt

            # Real BudgetGuard preflight before submit. With mock renderer
            # (amount_usd=0.0, unknown=False) this always passes when a
            # ceiling > 0 is configured. With live adapters (unknown=True)
            # it would refuse. Documented in core/budget_enforcer.py.
            cost_estimate = renderer.estimate(pack=pack_dict)
            try:
                preflight_or_block(self._budget, estimated_cost_usd=cost_estimate.amount_usd)
            except Exception as e:
                self._stage_done(job_id, "generate_assets", ordinal, start,
                                 error=f"budget preflight failed: {e}")
                self.repo.update_job(job_id, state="failed",
                                      status_message=f"budget preflight: {e}")
                return

            submit = renderer.submit(pack=pack_dict, idempotency_key=pack_id)
            provider_job_id = submit.provider_job_id
            # Mirror the receipt into the durable store so it survives
            # a process restart and the next run can replay.
            self._write_durable(idem_key, ProviderSubmitReceipt(
                provider_job_id=submit.provider_job_id,
                accepted_metadata=dict(submit.accepted_metadata or {}),
            ))
            # Record the actual spend (mock = 0).
            self._budget.record_spend(cost_estimate.amount_usd or 0.0)
            status = renderer.status(provider_job_id=provider_job_id)
            outputs = renderer.fetch_outputs(provider_job_id=provider_job_id) or status.outputs
            # Materialize mock assets to durable storage under job_id
            output_records = []
            for o in outputs:
                data = render_storage.get(o.ref)
                out_key = f"jobs/{job_id}/assets/{Path(o.ref).name}"
                ref = global_storage.put(key=out_key, data=data, mime_type=o.mime_type)
                output_records.append({
                    "storage_key": out_key,
                    "filename": Path(o.ref).name,
                    "mime_type": o.mime_type,
                    "size_bytes": ref.size_bytes,
                    "checksum": ref.checksum,
                    "is_mock": True,
                    "kind": "scene_clip",
                })
            for rec in output_records:
                self.repo.create_output(job_id, **rec)
            self._stage_done(job_id, "generate_assets", ordinal, start,
                             summary={"provider_job_id": provider_job_id,
                                      "outputs": len(output_records)})

            # Stage 7: voice (synthetic)
            ordinal += 1
            start, _ = self._stage_started(job_id, "voice", ordinal)
            time.sleep(0.05)
            self._stage_done(job_id, "voice", ordinal, start,
                             summary={"voice_id": "mock_en_neutral"})

            # Stage 8: edit_plan
            ordinal += 1
            start, _ = self._stage_started(job_id, "edit_plan", ordinal)
            n_clips = max(2, len(output_records) or 3)
            plan = build_plan(
                duration_seconds=job["duration_seconds"],
                n_clips=n_clips,
                aspect_ratio=job["aspect_ratio"],
                topic=job["topic"],
            )
            self._stage_done(job_id, "edit_plan", ordinal, start,
                             summary=plan.to_dict())

            # Stage 9: assembly (real FFmpeg playable MP4)
            ordinal += 1
            start, _ = self._stage_started(job_id, "assembly", ordinal)
            if shutil.which("ffmpeg"):
                out_key = f"jobs/{job_id}/final/{plan.plan_id}.mp4"
                result = render_plan_to_storage(plan=plan, storage=global_storage,
                                                  output_key=out_key)
                if result is None:
                    self._stage_done(job_id, "assembly", ordinal, start,
                                     error="ffmpeg assembly failed")
                    self.repo.update_job(job_id, state="failed",
                                          status_message="assembly failed")
                    return
                ref = result["output_ref"]
                self.repo.create_output(
                    job_id,
                    storage_key=ref["key"],
                    filename=Path(ref["key"]).name,
                    mime_type=ref["mime_type"],
                    size_bytes=ref["size_bytes"],
                    checksum=ref["checksum"],
                    is_mock=True,
                    kind="final_video",
                )
                self._stage_done(job_id, "assembly", ordinal, start, summary=result)
            else:
                self._stage_done(job_id, "assembly", ordinal, start,
                                 summary={"note": "ffmpeg not available; assembly skipped"},
                                 error="ffmpeg not installed")

            # Stage 10: qc
            ordinal += 1
            start, _ = self._stage_started(job_id, "qc", ordinal)
            qc_ok = True
            qc_notes = []
            final_outputs = [o for o in self.repo.list_outputs(job_id) if o["kind"] == "final_video"]
            if not final_outputs:
                qc_ok = False
                qc_notes.append("no final_video output produced")
            else:
                from assembly.ffmpeg_assembler import probe_duration_seconds
                for o in final_outputs:
                    p = global_storage.path_for(o["storage_key"]) or ""
                    d = probe_duration_seconds(p)
                    if d is None:
                        qc_ok = False
                        qc_notes.append(f"{o['filename']}: ffprobe could not decode")
            self._stage_done(job_id, "qc", ordinal, start,
                             summary={"ok": qc_ok, "notes": qc_notes})

            # Stage 11: human_review
            ordinal += 1
            start, _ = self._stage_started(job_id, "human_review", ordinal)
            time.sleep(0.05)
            self._stage_done(job_id, "human_review", ordinal, start,
                             summary={"required": job["approval_required"],
                                      "auto_proceeded": not job["approval_required"]})

            # Stage 12: export_publish (mock)
            ordinal += 1
            start, _ = self._stage_started(job_id, "export_publish", ordinal)
            time.sleep(0.05)
            self._stage_done(job_id, "export_publish", ordinal, start,
                             summary={"note": "mock export only; use Review & Publish UI"})

            self.repo.update_job(job_id, state="done", current_stage="export_publish",
                                  status_message="done")
        except Exception as e:
            self.repo.update_job(job_id, state="failed",
                                  status_message=f"error: {e}")
            if ordinal > 0:
                try:
                    self._stage_done(job_id, STAGE_SEQUENCE[min(ordinal, len(STAGE_SEQUENCE) - 1)],
                                     ordinal, time.monotonic(), error=str(e))
                except Exception:
                    pass

    def _storage_for(self, job: dict):
        return LocalDiskStorage(root=str(self.storage_root))

    def _job_storage(self, job: dict):
        return LocalDiskStorage(root=str(self.storage_root / "jobs" / job["job_id"]))


class Singleton:
    """Process-wide singleton runner."""

    _instance: Optional["JobRunner"] = None

    @classmethod
    def get(cls) -> JobRunner:
        if cls._instance is None:
            raise RuntimeError("JobRunner not initialized")
        return cls._instance

    @classmethod
    def set(cls, runner: JobRunner) -> None:
        cls._instance = runner