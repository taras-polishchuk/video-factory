"""HyperFrames local renderer (HTML + GSAP -> MP4 via the pinned `hyperframes` CLI).

Free, offline-capable, deterministic motion-graphics renderer. No HeyGen API,
no cloud render, no secret. Runs the CLI as a subprocess on this host.

Pack contract (all optional):
    pack["hyperframes"] = {
        "project_dir": "<dir with index.html>",   # agent-authored composition;
                                                   # must sit under an allowed root
        "variables": {"title": "...", ...},       # merged over template defaults
        "quality": "draft" | "looks" | "delivery",
        "fps": 30,
    }
Without project_dir the built-in `promo-cards` template renders, its text taken
from `variables` or, failing that, from the pack's first scenes.

Allowed roots for project_dir: VIDEO_FACTORY_HYPERFRAMES_ROOTS (os.pathsep list),
default = the built-in templates dir + the storage root. Path escapes are refused.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from control_plane.errors import ProviderError, TeardownUnsupported
from control_plane.idempotency import IdempotencyStore, ProviderSubmitReceipt
from control_plane.storage import LocalDiskStorage, Storage
from render_plane.provider import (
    Capabilities,
    CostEstimate,
    JobState,
    OutputAsset,
    OutputKind,
    StatusResult,
    SubmitResult,
    TeardownResult,
)

TEMPLATES_DIR = Path(__file__).resolve().parent / "hyperframes_templates"
DEFAULT_TEMPLATE = "promo-cards"
DIMENSIONS = {"9:16": (1080, 1920), "16:9": (1920, 1080), "1:1": (1080, 1080)}
QUALITIES = {"draft", "looks", "delivery"}
RENDER_TIMEOUT_S = int(os.environ.get("VIDEO_FACTORY_HYPERFRAMES_TIMEOUT_S", "900"))


def hyperframes_binary() -> Optional[str]:
    return os.environ.get("VIDEO_FACTORY_HYPERFRAMES_BIN") or shutil.which("hyperframes")


def _aspect(pack: dict) -> str:
    for key in ("aspect_ratio", "aspect"):
        val = pack.get(key) or (pack.get("render_capabilities") or {}).get(key)
        if val in DIMENSIONS:
            return val
    return "9:16"


def _template_variables(pack: dict) -> Dict[str, str]:
    scenes = [s for s in pack.get("scenes", []) if isinstance(s, dict)]
    lines = [str(s.get(k)) for s in scenes for k in ("on_screen_text", "caption", "narration", "text")
             if s.get(k)]
    out: Dict[str, str] = {}
    if pack.get("title"):
        out["title"] = str(pack["title"])
    elif lines:
        out["title"] = lines[0]
    if len(lines) > 1:
        out["caption"] = lines[1]
    return out


@dataclass
class _Job:
    job_id: str
    state: JobState = JobState.PENDING
    progress: float = 0.0
    error: Optional[str] = None
    outputs: List[OutputAsset] = field(default_factory=list)
    thread: Optional[threading.Thread] = None


class HyperFramesLocalAdapter:
    profile = "hyperframes_local"

    def __init__(self, storage: Optional[Storage] = None) -> None:
        self.storage = storage or LocalDiskStorage()
        self._store = IdempotencyStore()
        self._jobs: Dict[str, _Job] = {}
        self._lock = threading.Lock()

    def set_idempotency_store(self, store: IdempotencyStore) -> None:
        self._store = store

    def capabilities(self) -> Capabilities:
        return Capabilities(
            profile=self.profile,
            output_kinds=[OutputKind.WHOLE_VIDEO],
            supports_cancel=False,
            supports_status_poll=True,
            supports_webhook=False,
            aspect_ratios=list(DIMENSIONS),
            provider_billing_owner="tenant",
            requires_secret=False,
            notes="Local HyperFrames CLI render (Chrome headless + FFmpeg). Free; no HeyGen API.",
        )

    def estimate(self, *, pack: dict) -> CostEstimate:
        return CostEstimate(amount_usd=0.0, range_low_usd=0.0, range_high_usd=0.0,
                            assumptions="local CPU render; no provider spend")

    # ── paths ──────────────────────────────────────────────────────────
    def _allowed_roots(self) -> List[Path]:
        raw = os.environ.get("VIDEO_FACTORY_HYPERFRAMES_ROOTS", "")
        roots = [Path(p).resolve() for p in raw.split(os.pathsep) if p.strip()]
        root = getattr(self.storage, "root", None)
        return roots + [TEMPLATES_DIR.resolve()] + ([Path(root).resolve()] if root else [])

    def _project_dir(self, spec: dict) -> Optional[Path]:
        raw = spec.get("project_dir")
        if not raw:
            return None
        path = Path(raw).resolve()
        if not any(path == r or r in path.parents for r in self._allowed_roots()):
            raise ProviderError(f"hyperframes project_dir outside allowed roots: {raw}")
        if not (path / "index.html").is_file():
            raise ProviderError(f"hyperframes project_dir has no index.html: {raw}")
        return path

    # ── protocol ───────────────────────────────────────────────────────
    def submit(self, *, pack: dict, idempotency_key: str) -> SubmitResult:
        existing = self._store.lookup(idempotency_key)
        if existing is not None:
            return SubmitResult(provider_job_id=existing.provider_job_id,
                                accepted_metadata={"replay": "true"})
        binary = hyperframes_binary()
        if not binary:
            raise ProviderError("hyperframes CLI not found (install the pinned wrapper)")
        spec = dict(pack.get("hyperframes") or {})
        quality = spec.get("quality", "draft")
        if quality not in QUALITIES:
            raise ProviderError(f"hyperframes quality {quality!r} invalid")
        project = self._project_dir(spec)  # validate before accepting
        job = _Job(job_id=f"hfjob_{uuid.uuid4().hex[:12]}")
        with self._lock:
            self._store.save(idempotency_key, ProviderSubmitReceipt(provider_job_id=job.job_id,
                                                                    accepted_metadata={}))
            self._jobs[job.job_id] = job
        job.thread = threading.Thread(target=self._run, args=(job, pack, spec, project, binary, quality),
                                      daemon=True)
        job.thread.start()
        return SubmitResult(provider_job_id=job.job_id, accepted_metadata={"engine": "hyperframes"})

    def _run(self, job: _Job, pack: dict, spec: dict, project: Optional[Path], binary: str,
             quality: str) -> None:
        job.state, job.progress = JobState.PROCESSING, 0.1
        work = Path(tempfile.mkdtemp(prefix="vf-hyperframes-"))
        try:
            variables = dict(spec.get("variables") or {})
            if project is None:
                w, h = DIMENSIONS[_aspect(pack)]
                project = work / DEFAULT_TEMPLATE
                shutil.copytree(TEMPLATES_DIR / DEFAULT_TEMPLATE, project)
                index = project / "index.html"
                index.write_text(index.read_text().replace("__W__", str(w)).replace("__H__", str(h)))
                variables = {**_template_variables(pack), **variables}
            out = work / "out.mp4"
            cmd = [binary, "render", str(project), "-o", str(out), "--quality", quality, "--quiet"]
            if spec.get("fps"):
                cmd += ["--fps", str(int(spec["fps"]))]
            if variables:
                cmd += ["--variables", json.dumps(variables)]
            env = {**os.environ, "HYPERFRAMES_NO_TELEMETRY": "1", "DO_NOT_TRACK": "1",
                   "HYPERFRAMES_SKIP_SKILLS": "1"}
            proc = subprocess.run(cmd, cwd=work, env=env, capture_output=True, text=True,
                                  timeout=RENDER_TIMEOUT_S)
            if proc.returncode != 0 or not out.is_file():
                raise ProviderError(f"hyperframes render failed rc={proc.returncode}: {proc.stderr[-800:]}")
            job.progress = 0.9
            data = out.read_bytes()
            key = f"hyperframes/{pack.get('pack_id', 'vp_unknown')}/{job.job_id}.mp4"
            ref = self.storage.put(key=key, data=data, mime_type="video/mp4")
            meta = _probe(out)
            job.outputs.append(OutputAsset(
                asset_kind="video", ref=ref.key, checksum=ref.checksum or hashlib.sha256(data).hexdigest(),
                size_bytes=ref.size_bytes, mime_type="video/mp4", duration_seconds=meta.get("duration"),
                width=meta.get("width"), height=meta.get("height"),
            ))
            job.state, job.progress = JobState.COMPLETED, 1.0
        except subprocess.TimeoutExpired:
            job.state, job.error = JobState.FAILED, f"hyperframes render timed out after {RENDER_TIMEOUT_S}s"
        except Exception as exc:  # surfaced via status(); never raised into the runner thread
            job.state, job.error = JobState.FAILED, str(exc)
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def wait(self, provider_job_id: str, timeout: Optional[float] = None) -> StatusResult:
        job = self._jobs.get(provider_job_id)
        if job and job.thread:
            job.thread.join(timeout)
        return self.status(provider_job_id=provider_job_id)

    def status(self, *, provider_job_id: str) -> StatusResult:
        job = self._jobs.get(provider_job_id)
        if job is None:
            return StatusResult(state=JobState.UNKNOWN, error="unknown provider_job_id")
        return StatusResult(state=job.state, progress=job.progress, error=job.error,
                            outputs=list(job.outputs), cost_actual_usd=0.0)

    def fetch_outputs(self, *, provider_job_id: str) -> List[OutputAsset]:
        job = self._jobs.get(provider_job_id)
        return list(job.outputs) if job else []

    def cancel(self, *, provider_job_id: str) -> TeardownResult:
        return TeardownResult(ok=False, detail="local hyperframes render is not cancellable mid-run")

    def teardown_gpu(self) -> bool:
        raise TeardownUnsupported("hyperframes_local runs on CPU; nothing to tear down")


def _probe(path: Path) -> dict:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return {}
    proc = subprocess.run([ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries",
                           "stream=width,height:format=duration", "-of", "json", str(path)],
                          capture_output=True, text=True, timeout=30)
    try:
        info = json.loads(proc.stdout)
        stream = (info.get("streams") or [{}])[0]
        return {"width": stream.get("width"), "height": stream.get("height"),
                "duration": float(info.get("format", {}).get("duration", 0)) or None}
    except (ValueError, TypeError):
        return {}
