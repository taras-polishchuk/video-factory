"""Background job runner.

Drives an Orchestrator through stages off the request thread. Each
job gets a sequence of stages recorded in the repo. Idempotency is
durable (job_id + idempotency key). Restart-safe within the SQLite
session (jobs remain in their last state on restart; no in-memory
state required to resume the *runner*).
"""
from __future__ import annotations

import collections
import json
import logging
import os
import shutil
import threading
import time
from pathlib import Path
from typing import Optional

from control_plane.idempotency import IdempotencyStore, ProviderSubmitReceipt
from control_plane.idempotency import DurableIdempotencyStore
from control_plane.storage import LocalDiskStorage
from control_plane.orchestrator import Orchestrator, OrchestratorConfig
from control_plane.budget import BudgetGuard
from control_plane.pack_builder import (
    Caption, Concept, PackBuilder, ResearchClaim, SceneSpec, Script, VoiceSpec,
)
from render_plane.mock_renderer import MockRenderer

from core.edit_plan import build_plan, render_plan_to_storage
from core.repo import NotFound, Repo

_log = logging.getLogger("video_factory.runner")

# Concurrency cap (JOBS S3 item 2). Each running job may spawn an
# ffmpeg process (see core/edit_plan.py for its memory bound), so the
# cap is what keeps a 4 GB host out of OOM. Excess jobs stay `queued`
# and start FIFO when a slot frees up.
MAX_CONCURRENT_JOBS_ENV = "VIDEO_FACTORY_MAX_CONCURRENT_JOBS"
DEFAULT_MAX_CONCURRENT_JOBS = 2

# Restart recovery (F-30). A job still `running` whose row was not
# touched for this long, with no live pipeline thread, is re-queued
# once and failed the second time.
STALE_RUNNING_MINUTES_ENV = "VIDEO_FACTORY_STALE_RUNNING_MINUTES"
DEFAULT_STALE_RUNNING_MINUTES = 30.0
MAX_RECOVERY_REQUEUES = 1


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return max(minimum, int(raw))
    except ValueError:
        _log.warning("ignoring invalid %s=%r; using %s", name, raw, default)
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return max(0.0, float(raw))
    except ValueError:
        _log.warning("ignoring invalid %s=%r; using %s", name, raw, default)
        return default


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
    """Runs jobs on background threads, at most ``max_concurrent_jobs``
    at a time. State persists to SQLite.

    Scheduling (JOBS S3): ``submit`` puts a job in a FIFO queue and
    starts a worker thread only while fewer than the cap are running.
    A worker that finishes a job immediately takes the next queued job
    on the same thread (renamed ``job-<id>``), so the number of live
    pipeline threads never exceeds the cap and joining a worker waits
    for everything it will pick up.
    """

    def __init__(self, *, repo: Repo, storage_root: str, schema: dict,
                 budget_per_video_usd: float = 0.0,
                 budget_per_batch_usd: float = 0.0,
                 max_concurrent_jobs: Optional[int] = None) -> None:
        self.repo = repo
        self.storage_root = Path(storage_root).resolve()
        self.storage_root.mkdir(parents=True, exist_ok=True)
        self.schema = schema
        self.budget_per_video_usd = budget_per_video_usd
        self.budget_per_batch_usd = budget_per_batch_usd
        if max_concurrent_jobs is None:
            max_concurrent_jobs = _env_int(
                MAX_CONCURRENT_JOBS_ENV, DEFAULT_MAX_CONCURRENT_JOBS,
            )
        self.max_concurrent_jobs = max(1, int(max_concurrent_jobs))
        self._lock = threading.Lock()
        # job_id -> worker thread currently executing it.
        self._active_jobs: dict[str, threading.Thread] = {}
        # FIFO of job_ids accepted but not started (cap reached).
        self._queue: collections.deque[str] = collections.deque()
        # job_id -> Event set when the job leaves the runner (done,
        # failed, or crashed). Present while queued or running.
        self._done: dict[str, threading.Event] = {}
        # job_id -> budget reserved at admission (BLK-1); consumed by
        # _run so the admitted job is not charged twice.
        self._reserved: dict[str, float] = {}
        # F8: expose an audit surface so the webhook handler can record
        # receipts. We use the JobRunner's own in-memory audit log;
        # backend persistence lives in Repo (job_stages + runner_audit).
        from control_plane.audit import InMemoryAuditLog
        self.audit = InMemoryAuditLog()
        # Wave 1: actor attribution for runner_audit rows.
        self._actor_user_id: Optional[str] = None
        self._actor_workspace_id: Optional[str] = None
        # Durable idempotency: SQLite-backed so receipts survive restart.
        idem_db = self.storage_root / "idem.db"
        self._idem_store = DurableIdempotencyStore(str(idem_db))
        # F-10 / MAJ-5 / MAJ-6: ONE in-memory store per runner, built
        # and seeded eagerly here (deterministic order) instead of
        # lazily under an id(self)-keyed dict on the first job thread.
        self._idem_inmem = self._seed_idempotency_store()
        self._budget = BudgetGuard(
            max_cost_per_video_usd=budget_per_video_usd,
            max_cost_per_batch_usd=budget_per_batch_usd,
        )
        self._registry = None

    # ── Queue introspection (Prometheus gauges) ───────────────

    def queue_depth(self) -> int:
        with self._lock:
            return len(self._queue)

    def active_count(self) -> int:
        with self._lock:
            return len(self._active_jobs)

    def concurrency_limit(self) -> int:
        return self.max_concurrent_jobs

    # ── Atomic admission (BLK-1) ──────────────────────────────

    def estimate_cost(self, render_profile: Optional[str]):
        """Cost estimate for a job with ``render_profile``, computed in
        the request thread (no provider call: every adapter's
        ``estimate`` is static). Unknown profiles are treated as an
        unknown cost, which the budget guard refuses."""
        from render_plane.provider import CostEstimate
        profile = (render_profile or "mock").strip() or "mock"
        try:
            if self._registry is None:
                from render_plane.registry import default_registry
                self._registry = default_registry()
            return self._registry.resolve(profile).estimate(pack={})
        except Exception:  # UnknownProfile or adapter error
            return CostEstimate(amount_usd=None, unknown=True,
                                assumptions=f"unknown render profile {profile!r}")

    def create_job_admitted(self, *, workspace: dict, endpoint: str,
                              company_id: Optional[str] = None,
                              user_id: Optional[str] = None,
                              actor: Optional[str] = None,
                              now=None, **job_fields) -> dict:
        """Create a job only if quota AND budget admit it, atomically.

        One ``BEGIN IMMEDIATE`` transaction (``Repo.create_job`` with
        an ``admission`` callback) covers: count this workspace's jobs
        this month -> FREE quota check -> budget reserve -> INSERT.
        Concurrent submits are serialised by SQLite's write lock, so
        they cannot exceed the free quota or the budget ceiling.
        Raises ``core.admission.JobAdmissionRefused`` (no row written)
        when refused. Does NOT start the job: call ``submit``.
        """
        from datetime import datetime, timezone
        from core.admission import budget_refusal, quota_refusal
        from core.quotas import check_job_quota

        wid = workspace["workspace_id"]
        plan = workspace.get("plan", "free")
        profile = job_fields.get("render_profile", "mock") or "mock"
        estimate = self.estimate_cost(profile)
        when = now or datetime.now(timezone.utc)
        reserved: dict = {}

        def _admit(conn, job_id: str) -> None:
            quota = check_job_quota(
                workspace_id=wid, plan=plan, now=when,
                job_count_for_month=lambda w, s: self.repo.count_jobs_since(
                    workspace_id=w, since_iso=s, conn=conn,
                ),
            )
            if not quota.allowed:
                raise quota_refusal(quota, workspace_id=wid, endpoint=endpoint,
                                    user_id=user_id, actor=actor)
            decision = self._budget.reserve(
                estimated_cost_usd=None if estimate.unknown else estimate.amount_usd,
            )
            if not decision.allowed:
                raise budget_refusal(
                    decision, workspace_id=wid, endpoint=endpoint,
                    render_profile=profile, unknown=bool(estimate.unknown),
                    user_id=user_id, actor=actor,
                )
            reserved[job_id] = float(estimate.amount_usd or 0.0)

        if company_id is not None:
            job_fields["company_id"] = company_id
        else:
            job_fields["workspace_id"] = wid
        try:
            job = self.repo.create_job(admission=_admit, **job_fields)
        except BaseException:
            # The INSERT (or anything after the reserve) failed: the
            # transaction rolled back, so give the reservation back.
            for amount in reserved.values():
                self._budget.release(amount)
            raise
        with self._lock:
            self._reserved.update(reserved)
        return job

    # ── Restart recovery (F-30, F-10) ─────────────────────────

    def recover_stale_jobs(self, *, older_than_seconds: Optional[float] = None
                             ) -> dict:
        """Requeue-once / fail jobs stuck in ``running`` (see
        ``Repo.recover_stale_running_jobs``), skipping jobs this runner
        is executing right now, and record each decision in
        runner_audit."""
        if older_than_seconds is None:
            older_than_seconds = 60.0 * _env_float(
                STALE_RUNNING_MINUTES_ENV, DEFAULT_STALE_RUNNING_MINUTES,
            )
        with self._lock:
            live = set(self._active_jobs) | set(self._queue)
        result = self.repo.recover_stale_running_jobs(
            older_than_seconds=older_than_seconds,
            max_requeues=MAX_RECOVERY_REQUEUES,
            exclude_job_ids=live,
        )
        from control_plane.repo_helpers import append_runner_audit
        for jid in result["requeued"]:
            append_runner_audit(self.repo, job_id=jid, event="job.recovered_requeued",
                                context={"older_than_seconds": older_than_seconds})
        for jid in result["failed"]:
            append_runner_audit(self.repo, job_id=jid, event="job.recovered_failed",
                                context={"older_than_seconds": older_than_seconds,
                                         "max_requeues": MAX_RECOVERY_REQUEUES})
        if result["requeued"] or result["failed"]:
            _log.warning("restart recovery: requeued=%s failed=%s",
                         result["requeued"], result["failed"])
        return result

    def recover_on_startup(self, *, older_than_seconds: Optional[float] = None
                             ) -> dict:
        """Process start-up: recover stale ``running`` jobs, then hand
        every ``queued`` job (including the ones just requeued and the
        ones that were waiting in the previous process's in-memory
        queue) back to the scheduler, oldest first (FIFO)."""
        result = self.recover_stale_jobs(older_than_seconds=older_than_seconds)
        queued = self.repo.list_queued_job_ids()
        for jid in queued:
            self.submit(jid)
        result["resubmitted"] = queued
        return result

    def set_actor(self, *, user_id: Optional[str] = None,
                   workspace_id: Optional[str] = None) -> None:
        """Stamp the runner with the current actor (per-request).

        Authenticated endpoints call this before submit() so every
        audit row written by the runner carries the actor. Pass
        None to clear.
        """
        self._actor_user_id = user_id
        self._actor_workspace_id = workspace_id

    def audit_runner_event(self, *, event: str,
                            context: Optional[dict] = None) -> None:
        """Append a runner_audit row attributed to the current actor."""
        from control_plane.repo_helpers import append_runner_audit
        append_runner_audit(
            self.repo,
            job_id=context.get("job_id", "_system") if context else "_system",
            event=event,
            context=context,
            user_id=self._actor_user_id,
            workspace_id=self._actor_workspace_id,
        )

    def _seed_idempotency_store(self) -> IdempotencyStore:
        """Build the in-memory store the MockRenderer expects, seeded
        from the durable store (oldest receipt first, then key).

        Runs once, in ``__init__``, before any job thread exists, so
        every thread of this runner shares the same fully-seeded store
        (MAJ-5). A seeding failure is not swallowed silently: it is
        logged and recorded in runner_audit, and the runner continues
        with whatever was loaded (the durable store stays the source
        of truth for ``_write_durable``).
        """
        inmem = IdempotencyStore()
        try:
            for key, receipt in self._idem_store.all_receipts():
                inmem.save(key, receipt)
        except Exception as e:  # noqa: BLE001
            _log.warning("idempotency seed from durable store failed: %s: %s",
                         type(e).__name__, e)
            from control_plane.repo_helpers import append_runner_audit
            append_runner_audit(
                self.repo, job_id="_system",
                event="idempotency.seed_failed",
                context={"error": f"{type(e).__name__}: {e}"},
            )
        return inmem

    def _idem_for(self, job_id: str) -> IdempotencyStore:
        """Return the in-memory store the MockRenderer expects.

        One store per runner, seeded from the durable store in
        ``__init__`` (see ``_seed_idempotency_store``). New submits
        write to it; ``_write_durable`` mirrors each receipt into the
        durable store, the source of truth across restarts.
        """
        return self._idem_inmem

    def _write_durable(self, idem_key: str, receipt, *, job_id: str = "") -> None:
        from control_plane.repo_helpers import append_runner_audit
        try:
            self._idem_store.save(idem_key, receipt)
            # Surface a successful mirror to the runner_audit log so
            # operators can confirm the durable receipt exists after a
            # restart. Closes the silent-except class bug (audit §2.8).
            append_runner_audit(
                self.repo,
                job_id=job_id or "_system",
                event="idempotency.durable_mirror",
                context={"idem_key": idem_key, "ok": True},
            )
        except Exception as e:
            # Surface durable-write failure to the runner_audit log so
            # the operator sees it in the reprocess path. The in-memory
            # store still has the receipt for this process; cross-restart
            # safety is the only thing compromised.
            append_runner_audit(
                self.repo,
                job_id=job_id or "_system",
                event="idempotency.durable_mirror_failed",
                context={
                    "idem_key": idem_key,
                    "error": f"{type(e).__name__}: {e}",
                },
            )
            raise  # re-raise so caller knows the durability path failed

    def submit(self, job_id: str) -> None:
        """Schedule a job. Returns immediately; runs in background.

        At most ``max_concurrent_jobs`` jobs run at once. A submit
        beyond the cap is not rejected: the job stays ``queued`` (DB
        state) and waits in a FIFO queue until a running job finishes.
        Re-submitting a job that is already queued or running is a
        no-op (double-clicked retry). Everything is decided under
        ``self._lock``.
        """
        with self._lock:
            if job_id in self._done:
                return  # already queued or running
            self._done[job_id] = threading.Event()
            if len(self._active_jobs) < self.max_concurrent_jobs:
                self._start_worker_locked(job_id)
            else:
                self._queue.append(job_id)

    def _start_worker_locked(self, job_id: str) -> None:
        t = threading.Thread(target=self._worker, args=(job_id,),
                             daemon=True, name=f"job-{job_id}")
        self._active_jobs[job_id] = t
        t.start()

    def _worker(self, job_id: str) -> None:
        """Run ``job_id``, then keep draining the FIFO queue on this
        same thread until it is empty."""
        me = threading.current_thread()
        current: Optional[str] = job_id
        while current is not None:
            nxt: Optional[str] = None
            try:
                self._run(current)
            except BaseException as e:  # noqa: BLE001
                # _run handles its own failures; this only fires for a
                # bug outside its try (e.g. repo unavailable). Never let
                # it leak the slot.
                _log.exception("job %s crashed outside _run: %s", current, e)
            finally:
                # F-32: the slot is ALWAYS released, even if _run
                # raised, so the job can be resubmitted later and the
                # next queued job can start.
                with self._lock:
                    self._active_jobs.pop(current, None)
                    unspent = self._reserved.pop(current, None)
                    ev = self._done.pop(current, None)
                    nxt = self._queue.popleft() if self._queue else None
                    if nxt is not None:
                        self._active_jobs[nxt] = me
                        try:
                            me.name = f"job-{nxt}"
                        except Exception:  # noqa: BLE001
                            pass
                if unspent:
                    # Admitted but never reached the provider submit
                    # (e.g. job row vanished): give the budget back.
                    self._budget.release(unspent)
                if ev is not None:
                    ev.set()
            current = nxt

    def wait(self, job_id: str, timeout: Optional[float] = None) -> bool:
        """Block until ``job_id`` has left the runner (finished,
        failed, or crashed), including any time spent queued.

        Returns True when the job is no longer queued or running in
        this runner (or was never submitted to it) and False if
        ``timeout`` elapsed first. Callers that need a completion
        signal (graceful shutdown, CLI, tests) use this instead of
        polling the job row against a wall-clock deadline.
        """
        with self._lock:
            ev = self._done.get(job_id)
        if ev is None:
            return True
        return ev.wait(timeout)

    def wait_all(self, timeout: Optional[float] = None) -> bool:
        """Block until every job submitted to this runner (running or
        queued) has finished. ``timeout`` is a total budget in seconds.
        Returns True when all are done."""
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            with self._lock:
                pending = list(self._done.values())
            if not pending:
                return True
            for ev in pending:
                remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
                if not ev.wait(remaining):
                    return False

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

            # Budget (BLK-1). A job admitted through
            # create_job_admitted already reserved its estimate on
            # self._budget before the API answered; consume that
            # reservation instead of charging twice. Jobs that reach
            # the runner any other way (retry, restart recovery,
            # direct submit) get an atomic reserve here, before the
            # provider submit. With the mock renderer the estimate is
            # 0.0 and always fits; an unknown estimate is refused.
            cost_estimate = renderer.estimate(pack=pack_dict)
            with self._lock:
                reserved = self._reserved.pop(job_id, None)
            if reserved is None:
                decision = self._budget.reserve(
                    estimated_cost_usd=cost_estimate.amount_usd,
                )
                if not decision.allowed:
                    self._stage_done(job_id, "generate_assets", ordinal, start,
                                     error=f"budget preflight failed: {decision.reason}")
                    self.repo.update_job(job_id, state="failed",
                                          status_message=f"budget preflight: {decision.reason}")
                    return
                reserved = float(cost_estimate.amount_usd or 0.0)

            try:
                submit = renderer.submit(pack=pack_dict, idempotency_key=pack_id)
            except BaseException:
                # Provider never accepted the job: nothing was spent.
                self._budget.release(reserved)
                raise
            provider_job_id = submit.provider_job_id
            # Mirror the receipt into the durable store so it survives
            # a process restart and the next run can replay.
            self._write_durable(
                idem_key,
                ProviderSubmitReceipt(
                    provider_job_id=submit.provider_job_id,
                    accepted_metadata=dict(submit.accepted_metadata or {}),
                ),
                job_id=job_id,
            )
            # The reservation above IS the recorded spend (mock = 0).
            status = renderer.status(provider_job_id=provider_job_id)
            outputs = renderer.fetch_outputs(provider_job_id=provider_job_id) or status.outputs
            # Materialize mock assets to durable storage under job_id
            output_records = []
            # Wave 5: bind each output to the job's workspace via the
            # per-blob HMAC sidecar.  Reading the job is cheap (one
            # SELECT) and the workspace_id is already known by the
            # repo layer.
            _job = self.repo.get_job(job_id)
            _output_wid = _job.get("workspace_id") if _job else None
            for o in outputs:
                data = render_storage.get(o.ref)
                out_key = f"jobs/{job_id}/assets/{Path(o.ref).name}"
                ref = global_storage.put(key=out_key, data=data,
                                          mime_type=o.mime_type,
                                          workspace_id=_output_wid)
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
                # S1a: bind the final MP4 to the job's workspace with
                # the Wave 5 HMAC sidecar.  render_plan_to_storage
                # writes the blob without one, and the download route
                # now fails closed when the sidecar is missing.
                if job.get("workspace_id"):
                    from control_plane import artifact_hmac
                    artifact_hmac.write_sidecar(
                        storage_root=str(self.storage_root),
                        storage_key=ref["key"],
                        workspace_id=job["workspace_id"],
                    )
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
                # F5: assembly stage is no longer a hard dependency for
                # the offline demo. Record the skip with a clear reason;
                # the run continues to export_publish so operators see a
                # done job with a documented stage.skipped.
                self._stage_done(
                    job_id, "assembly", ordinal, start,
                    summary={"note": "ffmpeg not available; assembly skipped",
                             "is_mock": True},
                    error=None,
                )

            # Stage 10: qc
            ordinal += 1
            start, _ = self._stage_started(job_id, "qc", ordinal)
            qc_ok = True
            qc_notes = []
            final_outputs = [o for o in self.repo.list_outputs(job_id) if o["kind"] == "final_video"]
            if not final_outputs:
                # F5: absence of a final_video output is not automatically
                # a QC failure — only an indication that the assembly stage
                # was skipped (no ffmpeg). Log the reason so operators
                # understand the empty outputs list. The job still reaches
                # 'done' so the rest of the pipeline can be exercised.
                qc_notes.append(
                    "no final_video output: assembly was skipped (likely ffmpeg unavailable)"
                )
            else:
                if not shutil.which("ffprobe"):
                    qc_notes.append(
                        "ffprobe not available; durations not probed but outputs exist"
                    )
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