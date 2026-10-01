"""FastAPI app factory for Video Factory Control Room API.

Wires Repo + JobRunner + Storage. Single entrypoint used by
uvicorn api.main:app.
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

from core.repo import Forbidden, NotFound, Repo
from core.runners import JobRunner, Singleton


def create_app(*, repo: Optional[Repo] = None,
               runner: Optional[JobRunner] = None,
               storage_root: Optional[str] = None,
               schema: Optional[dict] = None,
               cors_origins: Optional[list[str]] = None,
               public_base_url: Optional[str] = None) -> FastAPI:
    if storage_root is None:
        storage_root = os.environ.get(
            "VIDEO_FACTORY_STORAGE",
            str(Path(__file__).resolve().parent.parent / "artifacts" / "storage"),
        )
    if schema is None:
        schema_path = Path(__file__).resolve().parent.parent / "schemas" / "video-pack.schema.json"
        schema = json.loads(schema_path.read_text())
    if repo is None:
        db_path = os.environ.get(
            "VIDEO_FACTORY_DB",
            str(Path(__file__).resolve().parent.parent / "artifacts" / "video_factory.db"),
        )
        repo = Repo(db_path=db_path)
    if runner is None:
        runner = JobRunner(
            repo=repo,
            storage_root=str(storage_root),
            schema=schema or {},
            budget_per_video_usd=float(os.environ.get("MAX_COST_PER_VIDEO_USD", "0") or 0),
            budget_per_batch_usd=float(os.environ.get("MAX_COST_PER_BATCH_USD", "0") or 0),
        )
    Singleton.set(runner)

    app = FastAPI(
        title="Video Factory Control Room API",
        version="0.2.0",
        description=(
            "Multi-company video production control plane. "
            "Mock-only by default; live providers disabled."
        ),
    )

    if cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cors_origins,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    @app.exception_handler(NotFound)
    async def _nf(request: Request, exc: NotFound):
        return JSONResponse({"error": str(exc)}, status_code=404)

    @app.exception_handler(Forbidden)
    async def _fb(request: Request, exc: Forbidden):
        return JSONResponse({"error": str(exc)}, status_code=403)

    @app.exception_handler(ValueError)
    async def _ve(request: Request, exc: ValueError):
        return JSONResponse({"error": str(exc)}, status_code=400)

    # ── health + openapi ───────────────────────────────────────

    @app.get("/health")
    async def health():
        return {
            "ok": True,
            "version": app.version,
            "ffmpeg": bool(shutil.which("ffmpeg")),
            "mock_only": True,
        }

    @app.get("/api/v1/spec")
    async def spec():
        return {"openapi_url": "/openapi.json", "docs_url": "/docs"}

    # ── companies ──────────────────────────────────────────────

    @app.post("/api/v1/companies")
    async def create_company(payload: dict):
        slug = (payload.get("slug") or "").strip()
        name = (payload.get("name") or "").strip()
        if not slug or not name:
            raise HTTPException(400, "slug and name required")
        return repo.create_company(slug=slug, name=name,
                                    description=payload.get("description", ""))

    @app.get("/api/v1/companies")
    async def list_companies():
        return repo.list_companies()

    @app.get("/api/v1/companies/{company_id}")
    async def get_company(company_id: str):
        return repo.get_company(company_id)

    @app.patch("/api/v1/companies/{company_id}")
    async def update_company(company_id: str, payload: dict):
        return repo.update_company(
            company_id,
            name=payload.get("name"),
            description=payload.get("description"),
        )

    # ── bible ──────────────────────────────────────────────────

    @app.post("/api/v1/companies/{company_id}/bibles")
    async def create_bible(company_id: str, payload: dict):
        return repo.create_bible(company_id, payload=payload.get("payload", {}),
                                  notes=payload.get("notes", ""))

    @app.get("/api/v1/companies/{company_id}/bibles")
    async def list_bibles(company_id: str):
        return repo.list_bibles(company_id)

    @app.get("/api/v1/bibles/{bible_id}")
    async def get_bible(bible_id: str):
        return repo.get_bible(bible_id)

    @app.post("/api/v1/bibles/{bible_id}/approve")
    async def approve_bible(bible_id: str):
        return repo.approve_bible(bible_id)

    # ── assets ─────────────────────────────────────────────────

    @app.post("/api/v1/companies/{company_id}/assets")
    async def upload_asset(company_id: str, kind: str = "misc",
                            file: UploadFile = File(...)):
        # Safe filename. We check the raw upload filename (not Path().name) so
        # that uploads named like "../escape.txt" are rejected even though
        # Path normalization would have masked them downstream.
        from core.repo import new_id, now_iso
        import hashlib
        raw_filename = file.filename or "upload"
        safe_name = Path(raw_filename).name
        if ".." in raw_filename.replace("\\", "/").split("/"):
            raise HTTPException(400, "invalid filename: path traversal")
        if not safe_name or safe_name in (".", ".."):
            raise HTTPException(400, "invalid filename")
        company = repo.get_company(company_id)
        asset_dir = Path(storage_root) / "companies" / company["slug"] / "assets"
        asset_dir.mkdir(parents=True, exist_ok=True)
        asset_id = new_id("ast")
        storage_key = f"companies/{company['slug']}/assets/{asset_id}_{safe_name}"
        data = await file.read()
        if len(data) > 50 * 1024 * 1024:
            raise HTTPException(413, "asset exceeds 50MB cap (mock mode)")
        target = Path(storage_root) / storage_key
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        checksum = hashlib.sha256(data).hexdigest()
        asset = repo.create_asset(
            company_id,
            kind=kind,
            filename=safe_name,
            storage_key=storage_key,
            mime_type=file.content_type or "application/octet-stream",
            size_bytes=len(data),
            checksum=checksum,
        )
        return asset

    @app.get("/api/v1/companies/{company_id}/assets")
    async def list_assets(company_id: str):
        return repo.list_assets(company_id)

    @app.get("/api/v1/assets/{asset_id}/download")
    async def download_asset(asset_id: str):
        asset = repo.get_asset(asset_id)
        path = Path(storage_root) / asset["storage_key"]
        if not path.exists():
            raise HTTPException(404, "asset bytes missing")
        return FileResponse(str(path), media_type=asset["mime_type"],
                            filename=asset["filename"])

    # ── jobs ───────────────────────────────────────────────────

    @app.post("/api/v1/jobs")
    async def create_job(payload: dict):
        company_id = payload.get("company_id")
        if not company_id:
            raise HTTPException(400, "company_id required")
        required = ("topic", "language", "duration_seconds", "platforms")
        for f in required:
            if f not in payload:
                raise HTTPException(400, f"missing {f}")
        job = repo.create_job(
            company_id,
            topic=payload["topic"],
            language=payload["language"],
            duration_seconds=int(payload["duration_seconds"]),
            platforms=list(payload["platforms"]),
            aspect_ratio=payload.get("aspect_ratio", "9:16"),
            count=int(payload.get("count", 1)),
            render_profile=payload.get("render_profile", "mock"),
            bible_id=payload.get("bible_id"),
            approval_required=bool(payload.get("approval_required", False)),
        )
        runner.submit(job["job_id"])
        return job

    @app.get("/api/v1/jobs")
    async def list_jobs(company_id: Optional[str] = None, state: Optional[str] = None):
        return repo.list_jobs(company_id=company_id, state=state)

    @app.get("/api/v1/jobs/{job_id}")
    async def get_job(job_id: str):
        return repo.get_job(job_id)

    @app.get("/api/v1/jobs/{job_id}/stages")
    async def list_stages(job_id: str):
        return repo.list_stages(job_id)

    @app.get("/api/v1/jobs/{job_id}/outputs")
    async def list_outputs(job_id: str):
        return repo.list_outputs(job_id)

    @app.post("/api/v1/jobs/{job_id}/retry")
    async def retry_job(job_id: str):
        job = repo.get_job(job_id)
        if job["state"] == "done":
            raise HTTPException(400, "job already done")
        repo.update_job(job_id, state="queued", status_message="retry requested")
        runner.submit(job_id)
        return repo.get_job(job_id)

    @app.get("/api/v1/jobs/{job_id}/outputs/{output_id}/download")
    async def download_output(job_id: str, output_id: str):
        output = repo.get_output(output_id)
        if output["job_id"] != job_id:
            raise HTTPException(404, "output does not belong to job")
        path = Path(storage_root) / output["storage_key"]
        if not path.exists():
            raise HTTPException(404, "output bytes missing")
        return FileResponse(str(path), media_type=output["mime_type"],
                            filename=output["filename"])

    # ── publishing (mock) ──────────────────────────────────────

    @app.post("/api/v1/jobs/{job_id}/publish")
    async def create_publish_intent(job_id: str, payload: dict):
        job = repo.get_job(job_id)
        if job["state"] != "done":
            raise HTTPException(400, "job must be in done state")
        action = payload.get("action")
        if action not in ("draft", "schedule", "publish"):
            raise HTTPException(400, "action must be draft/schedule/publish")
        platforms = payload.get("platforms") or []
        if not platforms:
            raise HTTPException(400, "platforms required")
        scheduled_for = payload.get("scheduled_for")
        if action == "schedule" and not scheduled_for:
            raise HTTPException(400, "scheduled_for required for schedule action")
        intent = repo.create_publish_intent(
            job_id=job_id,
            company_id=job["company_id"],
            action=action,
            platforms=platforms,
            caption=payload.get("caption", ""),
            scheduled_for=scheduled_for,
        )
        # Mock execution: mark as done immediately with a placeholder detail.
        # Live Publer integration is a separate, gated mission.
        detail = (
            f"mock {action} accepted for platforms={platforms}"
            + (f" at {scheduled_for}" if scheduled_for else "")
            + "; live Publer integration pending"
        )
        repo.update_publish_intent(intent["intent_id"], status="done", detail=detail)
        return repo.get_publish_intent(intent["intent_id"])

    @app.get("/api/v1/jobs/{job_id}/publish")
    async def list_publish_intents(job_id: str):
        return repo.list_publish_intents(job_id)

    # ── integrations ───────────────────────────────────────────

    @app.get("/api/v1/integrations")
    async def list_integrations():
        return repo.list_integrations()

    # ── schemas (Bible JSON Schema) ────────────────────────────

    @app.get("/api/v1/schemas/bible")
    async def bible_schema():
        from pathlib import Path as _P
        p = _P(__file__).resolve().parent.parent / "schemas" / "company-video-bible.schema.json"
        if not p.exists():
            raise HTTPException(404, "bible schema not found")
        return json.loads(p.read_text())

    return app


# Allow `uvicorn api.main:app` without explicit factory call.
app = create_app()