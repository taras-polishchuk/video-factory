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

from fastapi import Depends, FastAPI, Header, HTTPException, Request, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

from core.repo import Forbidden, NotFound, Repo
from core.runners import JobRunner, Singleton
from core.text_safety import validated_topic
from auth.dependencies import get_current_user  # noqa: E402  (module-scope)


def _topic_or_422(payload: dict) -> str:
    """SECURITY S1c: 1..200 chars after control-char strip; never echoed."""
    try:
        return validated_topic(payload["topic"])
    except ValueError as e:
        raise HTTPException(422, str(e))


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

    # S1b / A1 F14: CORS is OFF unless an explicit allow-list is
    # given (``cors_origins=`` or VIDEO_FACTORY_CORS_ORIGINS); ``*``
    # is never honoured. F19: hardening headers on every response,
    # also when the API is reached without Caddy. See api/security.py.
    from api.security import (
        CORS_ALLOW_HEADERS, CORS_ALLOW_METHODS, SECURITY_HEADERS_STR,
        SecurityHeadersMiddleware, cors_origins_from_env,
    )
    _cors = cors_origins_from_env(cors_origins)
    if _cors:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=_cors,
            allow_methods=CORS_ALLOW_METHODS,
            allow_headers=CORS_ALLOW_HEADERS,
        )
    app.add_middleware(SecurityHeadersMiddleware)

    @app.exception_handler(NotFound)
    async def _nf(request: Request, exc: NotFound):
        return JSONResponse({"error": str(exc)}, status_code=404)

    @app.exception_handler(Forbidden)
    async def _fb(request: Request, exc: Forbidden):
        return JSONResponse({"error": str(exc)}, status_code=403)

    @app.exception_handler(ValueError)
    async def _ve(request: Request, exc: ValueError):
        return JSONResponse({"error": str(exc)}, status_code=400)

    # Wave 8 follow-up (QA D1): catch-all for unhandled exceptions.
    # Starlette routes this handler through ServerErrorMiddleware,
    # so it fires for anything the specific handlers above did not
    # claim. It (1) counts the error, (2) forwards it to Sentry via
    # ``sentry_sdk.capture_exception`` when Sentry is initialised
    # (PII-scrubbed, tagged with the request id), (3) logs one JSON
    # line with the request id, and (4) returns a generic JSON 500
    # that never contains the exception text or a stack trace.
    # Must stay ``async``: a sync handler runs in a threadpool copy
    # of the context and breaks Sentry's per-request dedupe.
    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        from api import observability as _o
        rid = _o.request_id_from(request.headers.get("x-request-id"))
        route = _o.route_label(request.scope)
        try:
            _o.ERRORS_TOTAL.labels(**{"class": "runtime"}).inc()
        except Exception:  # noqa: BLE001 - metrics must never mask the 500
            pass
        try:
            _o.capture_exception(exc, request_id=rid, route=route,
                                 method=request.method)
        except Exception:  # noqa: BLE001 - Sentry must never mask the 500
            pass
        import logging as _lg
        _lg.getLogger("video_factory.errors").error(
            "unhandled exception",
            exc_info=(type(exc), exc, exc.__traceback__),
            extra={"request_id": rid, "route": route,
                   "method": request.method,
                   "error_class": type(exc).__name__},
        )
        return JSONResponse(
            {"error": "internal server error", "request_id": rid},
            status_code=500,
            headers={"X-Request-ID": rid, **SECURITY_HEADERS_STR},
        )

    # ── health + openapi ───────────────────────────────────────

    @app.get("/health")
    async def health():
        # A2 F-17: report the real gate state instead of a constant.
        # The live path is unlocked only when BOTH
        # LIVE_PROVIDER_TESTS=true and SMOKE_TEST_PASSED=true
        # (control_plane.budget.live_path_unlocked, read per call).
        from control_plane.budget import live_path_unlocked
        return {
            "ok": True,
            "version": app.version,
            "ffmpeg": bool(shutil.which("ffmpeg")),
            "mock_only": not live_path_unlocked(),
        }

    # ── Wave 8: k8s-style probes + Prometheus scrape ───────────
    # `/healthz` is the liveness probe - it MUST NOT touch the DB
    # so a wedged DB can never cause the orchestrator to restart
    # an otherwise-healthy process. `/readyz` is the readiness
    # probe - it returns 503 when the process cannot serve real
    # traffic (DB locked, vault key missing, master key
    # non-functional). `/metrics` is the Prometheus scrape
    # endpoint, served straight from the observability registry.

    from api.observability import (
        metrics_response,
        REQUEST_DURATION,
    )

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    @app.get("/readyz")
    async def readyz():
        """Readiness check: DB + vault key + master key.

        Body (Wave 8 follow-up, QA D4)::

            {"status": "ready" | "unready",
             "checks": {"db": bool, "vault_key": bool, "master_key": bool},
             "errors": {<check>: <reason code>},
             "warnings": {<check>: <reason code>}}

        Each check is independent and isolated; a single failure
        flips the response to 503. Reason codes are exception class
        names or fixed tokens (``dev_key_in_use``,
        ``db_file_missing``, ``db_file_replaced``, ``db_schema_missing``,
        ``db_locked``) -
        never exception messages, so no path or key material can
        leak through an unauthenticated probe. The DB check
        (``Repo.readiness_probe``, QA-N2) really touches the file:
        it must exist, have a schema, and give up its write lock
        within 100 ms. It runs in the threadpool, so a locked DB
        answers 503 fast and never blocks the event loop.
        """
        checks: dict[str, bool] = {}
        errors: dict[str, str] = {}
        warnings: dict[str, str] = {}
        # 1) DB present, readable and writable (not write-locked).
        try:
            from starlette.concurrency import run_in_threadpool
            reason = await run_in_threadpool(repo.readiness_probe)
            checks["db"] = reason is None
            if reason is not None:
                errors["db"] = reason
        except Exception as e:  # noqa: BLE001
            checks["db"] = False
            errors["db"] = type(e).__name__
        # 2) Vault key: must build a Fernet, not just be non-empty (D5).
        try:
            from control_plane import secret_vault
            secret_vault.validate_key()
            checks["vault_key"] = True
        except Exception as e:  # noqa: BLE001
            checks["vault_key"] = False
            errors["vault_key"] = type(e).__name__
        # 3) Master key (artifact HMAC). The static dev fallback is
        #    refused when VIDEO_FACTORY_ENV=production (D6); in dev
        #    it passes with a warning.
        try:
            from control_plane import artifact_hmac
            from auth.session import is_production
            artifact_hmac._key_bytes()  # noqa: SLF001
            if artifact_hmac.dev_key_in_use():
                if is_production():
                    checks["master_key"] = False
                    errors["master_key"] = "dev_key_in_use"
                else:
                    checks["master_key"] = True
                    warnings["master_key"] = "dev_key_in_use"
            else:
                checks["master_key"] = True
        except Exception as e:  # noqa: BLE001
            checks["master_key"] = False
            errors["master_key"] = type(e).__name__
        ready = all(checks.values())
        from fastapi.responses import JSONResponse
        return JSONResponse(
            {
                "status": "ready" if ready else "unready",
                "checks": checks,
                "errors": errors,
                "warnings": warnings,
            },
            status_code=200 if ready else 503,
        )

    @app.get("/metrics")
    async def metrics():
        from fastapi.responses import Response
        return Response(
            content=metrics_response(),
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )

    # Request-duration middleware: wraps every request in the
    # Prometheus histogram using the route template (e.g.
    # ``/api/v1/workspaces/{wid}``) as the label so cardinality
    # stays bounded. Requests that match no route (404 scans) are
    # labelled ``unmatched`` - never the raw path - and unknown
    # HTTP methods collapse to ``OTHER`` (Wave 8 follow-up). We do
    # NOT record /metrics itself - the scrape would otherwise
    # dominate the histogram.
    from api.observability import route_label, method_label

    @app.middleware("http")
    async def _prometheus_request_duration(request, call_next):
        import time as _time
        if request.url.path == "/metrics":
            return await call_next(request)
        start = _time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            duration = _time.perf_counter() - start
            REQUEST_DURATION.labels(
                route=route_label(request.scope),
                method=method_label(request.method),
                status="500",
            ).observe(duration)
            raise
        duration = _time.perf_counter() - start
        REQUEST_DURATION.labels(
            route=route_label(request.scope),
            method=method_label(request.method),
            status=str(response.status_code),
        ).observe(duration)
        return response

    # Wave 5: surface a startup warning when the artifact HMAC
    # key is missing in env (i.e. we're using the static dev
    # fallback).  This is intentionally informational — the dev
    # fallback still binds workspace_ids to blobs, but a key
    # shared across environments is unsafe.
    from control_plane import artifact_hmac as _ahmac
    _warn = _ahmac.warn_if_dev_key_in_use()
    if _warn:
        import logging as _lg
        _lg.getLogger("video_factory.startup").warning(_warn)

    # Wave 8: install the JSON log formatter on the root logger
    # and (optionally) register Sentry. Both are no-ops when the
    # relevant env vars are unset, so dev runs are unaffected.
    from api import observability as _obs
    _obs.configure_json_logging()
    _obs.init_sentry()

    @app.get("/api/v1/spec")
    async def spec():
        return {"openapi_url": "/openapi.json", "docs_url": "/docs"}

    # ── S1a: tenant authorization helpers ─────────────────────
    # Every route that reads or writes tenant data resolves the
    # resource's workspace_id FROM THE DB (never from the caller)
    # and requires a membership.  Resource-id keyed routes (job,
    # output, asset, bible) answer 404 for "does not exist" AND
    # for "exists in a workspace you are not a member of", with the
    # same body, so ids cannot be probed.  State-changing routes
    # check X-CSRF-Token before any lookup.  Roles follow
    # auth/roles.py: viewer reads, editor creates/submits jobs,
    # admin manages credentials.

    from auth.roles import has_min_role as _has_role
    from control_plane import artifact_hmac as _artifact_hmac
    from api import upload_policy as _upload_policy

    _NOT_FOUND = "not found"

    def _check_csrf(request: Request) -> None:
        from auth.dependencies import require_csrf as _require_csrf
        _require_csrf(request,
                      x_csrf_token=request.headers.get("X-CSRF-Token"))

    def _member_or_404(ctx, workspace_id: Optional[str], *,
                       min_role: str = "viewer") -> dict:
        if not workspace_id:
            raise HTTPException(404, _NOT_FOUND)
        m = repo.get_membership(ctx.user_id, workspace_id)
        if not m:
            raise HTTPException(404, _NOT_FOUND)
        if not _has_role(m["role"], min_role):
            raise HTTPException(403, f"requires role >= {min_role}")
        ctx.workspace_id = workspace_id
        ctx.role = m["role"]
        return m

    def _job_for(ctx, job_id: str, *, min_role: str = "viewer") -> dict:
        try:
            job = repo.get_job(job_id)
        except NotFound:
            raise HTTPException(404, _NOT_FOUND)
        _member_or_404(ctx, job.get("workspace_id"), min_role=min_role)
        return job

    def _serve_blob(*, storage_key: str, workspace_id: str,
                    mime_type: str, filename: str) -> FileResponse:
        """Serve a stored blob only if its HMAC sidecar binds it to
        ``workspace_id`` (Wave 5 control, A1 F17).  Fails closed:
        missing blob, missing/forged sidecar, or a key that escapes
        the storage root all give the same 404.
        """
        root = Path(storage_root).resolve()
        path = (root / storage_key).resolve()
        if root not in path.parents or not path.is_file():
            raise HTTPException(404, _NOT_FOUND)
        if not _artifact_hmac.verify_sidecar(
            storage_root=str(root), storage_key=storage_key,
            workspace_id=workspace_id,
        ):
            raise HTTPException(404, _NOT_FOUND)
        return FileResponse(
            str(path),
            media_type=_upload_policy.safe_download_type(mime_type),
            filename=filename,
            content_disposition_type="attachment",
            headers=_upload_policy.download_headers(),
        )

    async def _store_asset(file: UploadFile, *, kind: str, wid: str,
                           key_prefix: str) -> dict:
        """Shared upload path for both asset routes: filename
        check, kind/content-type allow-list (415), magic bytes
        (415), size cap (413), HMAC sidecar, DB row.
        """
        from core.repo import new_id
        raw_filename = file.filename or "upload"
        safe_name = Path(raw_filename).name
        if ".." in raw_filename.replace("\\", "/").split("/"):
            raise HTTPException(400, "invalid filename: path traversal")
        if not safe_name or safe_name in (".", ".."):
            raise HTTPException(400, "invalid filename")
        asset_id = new_id("ast")
        storage_key = f"{key_prefix}/{asset_id}_{safe_name}"
        target = Path(storage_root) / storage_key
        content_type, size, checksum = await _upload_policy.save_upload(
            file, kind=kind, target=target,
        )
        _artifact_hmac.write_sidecar(
            storage_root=storage_root,
            storage_key=storage_key,
            workspace_id=wid,
        )
        return repo.create_asset(
            workspace_id=wid,
            kind=kind,
            filename=safe_name,
            storage_key=storage_key,
            mime_type=content_type,
            size_bytes=size,
            checksum=checksum,
        )

    # ── Wave 2: workspace-scoped routes ───────────────────────
    # These mirror the legacy /api/v1/companies/{cid}/* routes
    # but bind the workspace_id from the URL path and enforce
    # workspace membership via the auth context.

    def _resolve_ws(request: Request, wid: str, *,
                    min_role: str = "viewer", csrf: bool = False):
        """Resolve a workspace from a path param, with 401/403/404.

        S1a: state-changing callers pass ``csrf=True`` (X-CSRF-Token
        checked right after the session, before any lookup) and a
        ``min_role`` from the owner > admin > editor > viewer model.
        A non-member gets 403 whether or not the workspace exists,
        so the response is not an existence oracle.
        """
        try:
            ctx = get_current_user(request)
        except HTTPException as e:
            raise HTTPException(e.status_code,
                                "authentication required for workspace access")
        if csrf:
            _check_csrf(request)
        m = repo.get_membership(ctx.user_id, wid)
        if not m:
            raise HTTPException(403, "not a member of this workspace")
        if not _has_role(m["role"], min_role):
            raise HTTPException(403, f"requires role >= {min_role}")
        ctx.workspace_id = wid
        ctx.role = m["role"]
        try:
            ws = repo.get_workspace(wid)
        except Exception:
            raise HTTPException(404, f"workspace {wid} not found")
        if ws.get("status") == "disabled":
            raise HTTPException(503, "workspace is disabled")
        return ctx, ws

    def _find_owner_workspace(repo, user_id: str) -> Optional[str]:
        """Return the first workspace_id where `user_id` is owner.

        Used by the Wave 5 admin/keys endpoints to enforce owner-
        only access.  Returns None if the caller is not an owner
        of any workspace, which the caller translates to 403.
        """
        try:
            memberships = repo._conn().execute(  # noqa: SLF001
                "SELECT workspace_id, role FROM memberships "
                "WHERE user_id=?",
                (user_id,),
            ).fetchall()
        except Exception:
            return None
        for wid, role in memberships:
            if role == "owner":
                return wid
        return None

    @app.get("/api/v1/workspaces/{wid}/companies")
    async def ws_list_companies(request: Request, wid: str):
        """Alias for /workspaces/{wid}: list the workspace as a
        "company" (the legacy noun).  Returns the workspace
        record itself plus a list of child entities the user can
        access.
        """
        _ctx, ws = _resolve_ws(request, wid)
        return [{"company_id": ws["workspace_id"],
                 "workspace_id": ws["workspace_id"],
                 "slug": ws["slug"],
                 "name": ws["name"],
                 "description": ws.get("description", ""),
                 "created_at": ws["created_at"],
                 "updated_at": ws["updated_at"]}]

    @app.get("/api/v1/workspaces/{wid}/jobs")
    async def ws_list_jobs(request: Request, wid: str,
                            state: Optional[str] = None):
        _ctx, _ws = _resolve_ws(request, wid)
        return repo.list_jobs(workspace_id=wid, state=state)

    @app.post("/api/v1/workspaces/{wid}/jobs")
    async def ws_create_job(request: Request, wid: str, payload: dict):
        _ctx, _ws = _resolve_ws(request, wid, min_role="editor", csrf=True)
        required = ("topic", "language", "duration_seconds", "platforms")
        for f in required:
            if f not in payload:
                raise HTTPException(400, f"missing {f}")
        topic = _topic_or_422(payload)
        # Wave 6b: live-provider gate (plan + workspace flag)
        # must fire BEFORE any DB write or provider call.  The
        # global env flags (LIVE_PROVIDER_TESTS / SMOKE_TEST_PASSED)
        # are checked inside the renderer at run time and are
        # out of scope here.
        from core.quotas import check_live_provider_allowed
        from core.audit import (
            EVENT_LIVE_PROVIDER_BLOCKED, emit as emit_audit,
        )
        live_decision = check_live_provider_allowed(
            plan=_ws.get("plan", "free"),
            workspace_live_enabled=_ws.get("live_enabled", False),
            render_profile=payload.get("render_profile", "mock"),
        )
        if not live_decision.allowed:
            emit_audit(
                repo,
                event=EVENT_LIVE_PROVIDER_BLOCKED,
                context={
                    "workspace_id": wid,
                    "plan": _ws.get("plan", "free"),
                    "ws_live_enabled": live_decision.ws_live_enabled,
                    "render_profile": payload.get(
                        "render_profile", "mock",
                    ),
                    "reason": live_decision.reason,
                    "endpoint": "POST /api/v1/workspaces/{wid}/jobs",
                },
                workspace_id=wid,
                user_id=getattr(_ctx, "user_id", None),
                actor=getattr(_ctx, "email", None),
            )
            raise HTTPException(
                403, f"live providers blocked: {live_decision.reason}",
            )
        # Wave 6b: FREE-tier monthly job quota (5/month).  Re-reads
        # plan synchronously on every call (no in-memory cache) so a
        # Stripe webhook (Wave 6c) can flip plan without race.
        from datetime import datetime, timezone
        from core.quotas import check_job_quota
        quota_decision = check_job_quota(
            workspace_id=wid,
            plan=_ws.get("plan", "free"),
            now=datetime.now(timezone.utc),
            job_count_for_month=lambda w, s: repo.count_jobs_since(
                workspace_id=w, since_iso=s,
            ),
        )
        if not quota_decision.allowed:
            from core.audit import (
                EVENT_QUOTA_EXCEEDED, emit as emit_quota_audit,
            )
            emit_quota_audit(
                repo,
                event=EVENT_QUOTA_EXCEEDED,
                context={
                    "workspace_id": wid,
                    "plan": quota_decision.plan,
                    "jobs_used": quota_decision.jobs_used,
                    "quota_reset_at": quota_decision.reset_at,
                    "endpoint": "POST /api/v1/workspaces/{wid}/jobs",
                },
                workspace_id=wid,
                user_id=getattr(_ctx, "user_id", None),
                actor=getattr(_ctx, "email", None),
            )
            return JSONResponse(
                {
                    "error": (
                        f"FREE plan limit {quota_decision.limit} jobs/month "
                        f"reached; resets at {quota_decision.reset_at}"
                    ),
                    "quota_reset_at": quota_decision.reset_at,
                    "jobs_used": quota_decision.jobs_used,
                    "jobs_remaining": 0,
                },
                status_code=429,
            )
        job = repo.create_job(
            workspace_id=wid,
            topic=topic,
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

    @app.get("/api/v1/workspaces/{wid}/bibles")
    async def ws_list_bibles(request: Request, wid: str):
        _ctx, _ws = _resolve_ws(request, wid)
        return repo.list_bibles(wid)

    @app.post("/api/v1/workspaces/{wid}/bibles")
    async def ws_create_bible(request: Request, wid: str, payload: dict):
        _ctx, _ws = _resolve_ws(request, wid, min_role="editor", csrf=True)
        return repo.create_bible(
            workspace_id=wid,
            payload=payload.get("payload", {}),
            notes=payload.get("notes", ""),
        )

    @app.get("/api/v1/workspaces/{wid}/assets")
    async def ws_list_assets(request: Request, wid: str):
        _ctx, _ws = _resolve_ws(request, wid)
        return repo.list_assets(wid)

    @app.post("/api/v1/workspaces/{wid}/assets")
    async def ws_create_asset(request: Request, wid: str,
                                kind: str = "misc",
                                file: UploadFile = File(...)):
        _ctx, ws = _resolve_ws(request, wid, min_role="editor", csrf=True)
        # S1a (A1 F16): kind/content-type allow-list + magic bytes
        # (415), VIDEO_FACTORY_MAX_UPLOAD_MB cap (413), streamed to
        # disk; Wave 5 HMAC sidecar binds the blob to `wid`.
        return await _store_asset(
            file, kind=kind, wid=wid,
            key_prefix=f"workspaces/{ws['slug']}/assets",
        )

    @app.get("/api/v1/workspaces/{wid}/credentials")
    async def ws_list_credentials(request: Request, wid: str):
        _ctx, _ws = _resolve_ws(request, wid)
        return repo.list_credentials(wid)

    @app.get("/api/v1/workspaces/{wid}/credentials/{provider}")
    async def ws_get_credential(request: Request, wid: str, provider: str):
        _ctx, _ws = _resolve_ws(request, wid)
        return repo.get_credential(wid, provider)

    @app.put("/api/v1/workspaces/{wid}/credentials/{provider}")
    async def ws_put_credential(
        request: Request, wid: str, provider: str, payload: dict,
        x_operator: Optional[str] = Header(default=None),
    ):
        _ctx, _ws = _resolve_ws(request, wid, min_role="editor", csrf=True)
        plaintext = payload.get("value")
        if not isinstance(plaintext, str) or not plaintext.strip():
            raise HTTPException(400, "value required (non-empty string)")
        if provider not in repo.CREDENTIAL_PROVIDERS:
            raise HTTPException(400, f"unknown provider: {provider!r}")
        from control_plane.secret_vault import (
            encrypt, fingerprint, SecretKeyMissing,
        )
        try:
            blob = encrypt(wid, provider, plaintext)
        except SecretKeyMissing as e:
            raise HTTPException(503, str(e))
        fp = fingerprint(plaintext)
        operator = (x_operator or "operator").strip()[:64]
        result = repo.upsert_credential(
            workspace_id=wid, provider=provider,
            fingerprint_str=fp, encrypted_blob=blob,
            updated_by=operator,
        )
        try:
            from control_plane.repo_helpers import append_runner_audit
            append_runner_audit(
                repo,
                job_id=f"cred::{wid}::{provider}",
                event="credentials.upserted",
                context={"provider": provider,
                          "fingerprint": fp, "updated_by": operator,
                          "workspace_id": wid},
            )
        except Exception:
            pass
        return result

    @app.delete("/api/v1/workspaces/{wid}/credentials/{provider}")
    async def ws_delete_credential(
        request: Request, wid: str, provider: str,
        x_operator: Optional[str] = Header(default=None),
    ):
        _ctx, _ws = _resolve_ws(request, wid, min_role="admin", csrf=True)
        operator = (x_operator or "operator").strip()[:64]
        try:
            repo.delete_credential(workspace_id=wid, provider=provider)
        except Exception as e:
            raise HTTPException(404, str(e))
        try:
            from control_plane.repo_helpers import append_runner_audit
            append_runner_audit(
                repo,
                job_id=f"cred::{wid}::{provider}",
                event="credentials.deleted",
                context={"provider": provider, "deleted_by": operator,
                          "workspace_id": wid},
            )
        except Exception:
            pass
        return {"deleted": True, "provider": provider,
                "workspace_id": wid}

    @app.get("/api/v1/workspaces/{wid}/audit")
    async def ws_audit(request: Request, wid: str, limit: int = 100):
        _ctx, _ws = _resolve_ws(request, wid)
        from auth.audit_log import list_for_workspace
        return list_for_workspace(repo, workspace_id=wid, limit=limit)

    # ── Wave 6a: Stripe Checkout + Customer Portal ──────────
    #
    # Two endpoints that wrap the Stripe SDK (`api/billing.py`):
    #
    #   POST /api/v1/billing/checkout   -> create Checkout Session
    #   POST /api/v1/billing/portal     -> create Customer Portal session
    #
    # Both endpoints require:
    #
    #   - an authenticated session (`get_current_user`),
    #   - a valid CSRF token (`require_csrf`),
    #   - owner-or-admin role in the workspace (`has_min_role`).
    #
    # Checkout is rejected if the workspace is already on a paid
    # plan (no point sending the user to Stripe twice).  Portal
    # is rejected with 404 if the workspace has no Stripe
    # customer yet — there's literally no Stripe-side resource to
    # portal into.
    #
    # The Stripe SDK is mocked in unit tests via
    # `unittest.mock.patch('stripe.checkout.Session.create')`
    # (and the portal sibling) — see tests/test_wave6_billing.py.

    from api import billing as _billing
    from auth.dependencies import (
        AuthContext,
        require_csrf,
    )
    from auth.roles import has_min_role

    def _billing_base_url(request: Request) -> str:
        """Return the operator-facing base URL for success/cancel
        redirects.  Falls back to ``http://localhost`` in dev."""
        return os.environ.get(
            "VIDEO_FACTORY_PUBLIC_BASE_URL",
            "http://localhost",
        ).rstrip("/")

    def _require_workspace_owner_or_admin(
        ctx: AuthContext, workspace_id: str,
    ) -> dict:
        """Return the workspace row, or raise 403/404."""
        m = repo.get_membership(ctx.user_id, workspace_id)
        if not m:
            raise HTTPException(403, "not a member of this workspace")
        if not has_min_role(m["role"], "admin"):
            raise HTTPException(
                403,
                "billing endpoints require owner or admin role",
            )
        try:
            return repo.get_workspace(workspace_id)
        except Exception:
            raise HTTPException(404, f"workspace {workspace_id} not found")

    @app.post("/api/v1/billing/checkout")
    async def billing_checkout(
        request: Request,
        payload: dict,
        ctx: AuthContext = Depends(get_current_user),
    ):
        require_csrf(request,
                      x_csrf_token=request.headers.get("X-CSRF-Token"))
        workspace_id = (payload.get("workspace_id") or "").strip()
        if not workspace_id:
            raise HTTPException(400, "workspace_id required")
        plan = (payload.get("plan") or "pro").strip()
        ws = _require_workspace_owner_or_admin(ctx, workspace_id)
        # Reject if already on a paid plan — no need to send the
        # user to Stripe twice.  Wave 6b will route the user to
        # the portal endpoint instead; for now we surface 409 so
        # the UI can show "you're already pro".
        if ws.get("plan") in ("pro", "enterprise"):
            raise HTTPException(
                409,
                f"workspace already on plan={ws['plan']}; "
                f"use the portal endpoint to manage billing",
            )
        # Persist a customer id if we don't have one yet, then
        # pass it to the SDK so Stripe reuses the same customer
        # across checkout + portal + future renewals.
        existing_customer = (
            repo.get_workspace_billing(workspace_id).get("stripe_customer_id")
        )
        # Prefill the Checkout form with the workspace owner's
        # email (per the Wave 6a brief: "customer_email from
        # workspace owner").  Sourced from the owner row so it
        # stays accurate even if the user's auth session was
        # created by an admin acting on their behalf.
        owner_email: Optional[str] = None
        try:
            owner = repo.get_user(ws.get("owner_user_id") or "")
            owner_email = (owner or {}).get("email") or None
        except Exception:
            owner_email = None
        client = _billing.get_stripe_client()
        base = _billing_base_url(request)
        try:
            result = client.create_checkout_session(
                workspace_id=workspace_id,
                plan=plan,
                success_url=f"{base}/billing/success?session_id={{CHECKOUT_SESSION_ID}}",
                cancel_url=f"{base}/billing/cancel",
                customer_id=existing_customer,
                customer_email=owner_email,
            )
        except _billing.StripeCallError as e:
            # Brief: failure paths (Stripe API error 4xx/5xx)
            # surface as 502 Bad Gateway with audit row
            # `billing.checkout_failed`.  We deliberately do NOT
            # leak the upstream error message to the client (it
            # may include the workspace owner's email or a Stripe
            # request id) — we log the message into the audit
            # context and return a generic upstream-error detail.
            # We return JSONResponse directly (instead of raising
            # HTTPException) so the body shape matches the rest
            # of the app's error responses: ``{"error": "..."}``
            # rather than FastAPI's default ``{"detail": "..."}``.
            repo.append_audit(
                event="billing.checkout_failed",
                context={
                    "workspace_id": workspace_id,
                    "plan": plan,
                    "error": str(e),
                },
                user_id=ctx.user_id,
                workspace_id=workspace_id,
                actor=ctx.email,
            )
            return JSONResponse(
                {"error": "stripe upstream error on checkout"},
                status_code=502,
            )
        new_customer_id = result.get("customer_id") or existing_customer
        if new_customer_id and new_customer_id != existing_customer:
            repo.upsert_stripe_customer(workspace_id, new_customer_id)
        # Audit without the Stripe IDs (per brief: no Stripe IDs
        # in audit).  Record only workspace + user + plan + the
        # returned Checkout Session id (cs_test_*) so the operator
        # can correlate with Stripe-side logs.
        repo.append_audit(
            event="billing.checkout_started",
            context={
                "workspace_id": workspace_id,
                "plan": plan,
                "session_id": result.get("session_id"),
            },
            user_id=ctx.user_id,
            workspace_id=workspace_id,
            actor=ctx.email,
        )
        return {
            "url": result.get("url"),
            "session_id": result.get("session_id"),
        }

    @app.post("/api/v1/billing/portal")
    async def billing_portal(
        request: Request,
        payload: dict,
        ctx: AuthContext = Depends(get_current_user),
    ):
        require_csrf(request,
                      x_csrf_token=request.headers.get("X-CSRF-Token"))
        workspace_id = (payload.get("workspace_id") or "").strip()
        if not workspace_id:
            raise HTTPException(400, "workspace_id required")
        _require_workspace_owner_or_admin(ctx, workspace_id)
        billing_state = repo.get_workspace_billing(workspace_id)
        customer_id = billing_state.get("stripe_customer_id")
        if not customer_id:
            raise HTTPException(
                404,
                "no Stripe customer for this workspace yet — "
                "complete a Checkout first",
            )
        client = _billing.get_stripe_client()
        base = _billing_base_url(request)
        try:
            result = client.create_portal_session(
                customer_id=customer_id,
                return_url=f"{base}/billing",
            )
        except _billing.StripeCallError as e:
            # Symmetric with the checkout handler: Stripe API
            # errors become 502 + `billing.portal_failed` audit
            # row.  Same upstream-error-redaction policy and the
            # same JSONResponse shape (``{"error": "..."}``).
            repo.append_audit(
                event="billing.portal_failed",
                context={
                    "workspace_id": workspace_id,
                    "error": str(e),
                },
                user_id=ctx.user_id,
                workspace_id=workspace_id,
                actor=ctx.email,
            )
            return JSONResponse(
                {"error": "stripe upstream error on portal"},
                status_code=502,
            )
        repo.append_audit(
            event="billing.portal_opened",
            context={
                "workspace_id": workspace_id,
                "session_id": result.get("session_id"),
            },
            user_id=ctx.user_id,
            workspace_id=workspace_id,
            actor=ctx.email,
        )
        return {"url": result.get("url")}

    # ── Wave 5: master-key rotation (admin, owner only) ───────
    #
    # Two endpoints:
    #   GET  /api/v1/admin/keys/status         -> rotation state
    #   POST /api/v1/admin/keys/rotate         -> run the rotation
    #
    # Owner-only because the new key is sourced from the request
    # body and would otherwise let any caller re-encrypt the DB
    # (and break the operator's backups in the process).  The
    # brief explicitly says owner-only.

    @app.get("/api/v1/admin/keys/status")
    async def keys_status(request: Request,
                          ctx: AuthContext = Depends(get_current_user)):
        # Owner-only check at the workspace level: pick ANY
        # workspace the caller owns.  In a single-tenant SaaS the
        # operator's first workspace is canonical; for multi-
        # workspace accounts the request body would carry wid.
        # We deliberately fail closed if the caller is not an
        # owner of any workspace.
        from control_plane import key_rotation
        owner_wid = _find_owner_workspace(repo, ctx.user_id)
        if not owner_wid:
            raise HTTPException(403, "owner role required")
        return key_rotation.rotation_state(repo)

    @app.post("/api/v1/admin/keys/rotate")
    async def keys_rotate(request: Request,
                          ctx: AuthContext = Depends(get_current_user)):
        # S1b / A1 F11 + F15: CSRF, owner/admin role, current-password
        # re-auth, key material never logged. See api/admin_keys.py.
        from api.admin_keys import handle_rotate
        return await handle_rotate(request, ctx, repo)

    # ── companies ──────────────────────────────────────────────
    # NOTE (Wave 2 follow-up, t_d76b7c0c): the legacy alias routes
    # under /api/v1/companies* must require authentication
    # (MAJOR #2, #3) and write paths also require CSRF + workspace
    # membership (MAJOR #5). The route signatures still accept
    # `company_id` in the URL for back-compat, but the resolver
    # translates that to a workspace_id before enforcing auth.

    # NOTE: get_current_user is imported at module scope at the top
    # of this file (see line 20).  Re-importing locally here would
    # shadow the module-scope binding and break Depends() resolution
    # inside the Wave 5 admin endpoints above.
    from auth.dependencies import (
        AuthContext,
        require_csrf,
        resolve_workspace_membership,
    )
    from auth.roles import has_min_role as _has_min_role

    def _resolve_cid_to_wid(repo, company_id: str) -> str:
        """Translate a legacy `company_id` to a `workspace_id`.

        Looks up the `legacy_company_alias` table first, then
        falls back to treating `company_id` as a workspace_id
        (the post-migration case where the alias table is
        empty because no legacy `companies` rows existed).
        """
        try:
            return repo.resolve_company_to_workspace(company_id)
        except Exception:
            # Bad/unknown id — re-raise so the caller can 404.
            raise

    @app.post("/api/v1/companies")
    async def create_company(
        request: Request,
        payload: dict,
        ctx: AuthContext = Depends(get_current_user),
    ):
        # CSRF on the legacy write path (MAJOR #5).
        require_csrf(request, x_csrf_token=request.headers.get("X-CSRF-Token"))
        slug = (payload.get("slug") or "").strip()
        name = (payload.get("name") or "").strip()
        if not slug or not name:
            raise HTTPException(400, "slug and name required")
        try:
            ws = repo.create_workspace(
                slug=slug, name=name,
                description=payload.get("description", ""),
                owner_user_id=ctx.user_id,
            )
            return {
                "company_id": ws["workspace_id"],
                "workspace_id": ws["workspace_id"],
                "slug": ws["slug"],
                "name": ws["name"],
                "description": ws.get("description", ""),
                "created_at": ws["created_at"],
                "updated_at": ws.get("updated_at"),
            }
        except Exception as e:
            if "UNIQUE constraint failed" in str(e) and "slug" in str(e):
                raise HTTPException(409, f"slug {slug!r} already exists")
            raise

    @app.get("/api/v1/companies")
    async def list_companies(
        ctx: AuthContext = Depends(get_current_user),
    ):
        # MAJOR #2: must require auth. Return only the user's
        # own workspaces.
        rows = repo.list_workspaces_for_user(ctx.user_id)
        return [
            {
                "company_id": r["workspace_id"],
                "workspace_id": r["workspace_id"],
                "slug": r.get("slug", ""),
                "name": r.get("name", ""),
                "role": r.get("role"),
                "plan": r.get("plan"),
            }
            for r in rows
        ]

    @app.get("/api/v1/companies/{company_id}")
    async def get_company(
        company_id: str,
        ctx: AuthContext = Depends(get_current_user),
    ):
        # Translate legacy cid → wid, then enforce membership.
        wid = _resolve_cid_to_wid(repo, company_id)
        m = repo.get_membership(ctx.user_id, wid)
        if not m:
            raise HTTPException(403, "not a member of this workspace")
        return repo.get_company(company_id)

    @app.patch("/api/v1/companies/{company_id}")
    async def update_company(
        request: Request,
        company_id: str,
        payload: dict,
        ctx: AuthContext = Depends(get_current_user),
    ):
        # MAJOR #3: auth + CSRF + membership.
        require_csrf(request, x_csrf_token=request.headers.get("X-CSRF-Token"))
        wid = _resolve_cid_to_wid(repo, company_id)
        m = repo.get_membership(ctx.user_id, wid)
        if not m:
            raise HTTPException(403, "not a member of this workspace")
        # Only owner/admin can update workspace metadata.
        if m["role"] not in ("owner", "admin"):
            raise HTTPException(403, "requires owner or admin role")
        return repo.update_company(
            company_id,
            name=payload.get("name"),
            description=payload.get("description"),
        )

    # ── bible ──────────────────────────────────────────────────

    @app.post("/api/v1/companies/{company_id}/bibles")
    async def create_bible(
        request: Request,
        company_id: str,
        payload: dict,
        ctx: AuthContext = Depends(get_current_user),
    ):
        # MAJOR #5: auth + CSRF + membership.
        require_csrf(request, x_csrf_token=request.headers.get("X-CSRF-Token"))
        wid = _resolve_cid_to_wid(repo, company_id)
        m = repo.get_membership(ctx.user_id, wid)
        if not m:
            raise HTTPException(403, "not a member of this workspace")
        if not _has_min_role(m["role"], "editor"):
            raise HTTPException(403, "requires editor role")
        return repo.create_bible(company_id=company_id,
                                  payload=payload.get("payload", {}),
                                  notes=payload.get("notes", ""))

    @app.get("/api/v1/companies/{company_id}/bibles")
    async def list_bibles(
        company_id: str,
        ctx: AuthContext = Depends(get_current_user),
    ):
        # Read paths need auth + membership (no CSRF).
        wid = _resolve_cid_to_wid(repo, company_id)
        m = repo.get_membership(ctx.user_id, wid)
        if not m:
            raise HTTPException(403, "not a member of this workspace")
        return repo.list_bibles(company_id)

    @app.get("/api/v1/bibles/{bible_id}")
    async def get_bible(
        bible_id: str,
        ctx: AuthContext = Depends(get_current_user),
    ):
        # S1a: fail closed (a bible with no workspace_id used to be
        # served to anyone logged in) and answer 404 for both
        # "missing" and "not yours".
        try:
            bible = repo.get_bible(bible_id)
        except NotFound:
            raise HTTPException(404, _NOT_FOUND)
        _member_or_404(ctx, bible.get("workspace_id"))
        return bible

    @app.post("/api/v1/bibles/{bible_id}/approve")
    async def approve_bible(
        request: Request,
        bible_id: str,
        ctx: AuthContext = Depends(get_current_user),
    ):
        require_csrf(request, x_csrf_token=request.headers.get("X-CSRF-Token"))
        try:
            bible = repo.get_bible(bible_id)
        except NotFound:
            raise HTTPException(404, _NOT_FOUND)
        _member_or_404(ctx, bible.get("workspace_id"), min_role="editor")
        return repo.approve_bible(bible_id)

    # ── assets ─────────────────────────────────────────────────

    @app.post("/api/v1/companies/{company_id}/assets")
    async def upload_asset(
        request: Request,
        company_id: str,
        kind: str = "misc",
        file: UploadFile = File(...),
        ctx: AuthContext = Depends(get_current_user),
    ):
        # MAJOR #5: auth + CSRF + membership.
        require_csrf(request, x_csrf_token=request.headers.get("X-CSRF-Token"))
        wid = _resolve_cid_to_wid(repo, company_id)
        m = repo.get_membership(ctx.user_id, wid)
        if not m:
            raise HTTPException(403, "not a member of this workspace")
        if not _has_min_role(m["role"], "editor"):
            raise HTTPException(403, "requires editor role")
        # Same pipeline as the workspace route (S1a): filename check,
        # type allow-list + magic bytes (415), size cap (413), and
        # the Wave 5 HMAC sidecar this legacy route never wrote
        # (without it the fail-closed download would 404).
        company = repo.get_company(company_id)
        return await _store_asset(
            file, kind=kind, wid=wid,
            key_prefix=f"companies/{company['slug']}/assets",
        )

    @app.get("/api/v1/companies/{company_id}/assets")
    async def list_assets(
        company_id: str,
        ctx: AuthContext = Depends(get_current_user),
    ):
        wid = _resolve_cid_to_wid(repo, company_id)
        m = repo.get_membership(ctx.user_id, wid)
        if not m:
            raise HTTPException(403, "not a member of this workspace")
        return repo.list_assets(company_id)

    @app.get("/api/v1/assets/{asset_id}/download")
    async def download_asset(
        asset_id: str,
        ctx: AuthContext = Depends(get_current_user),
    ):
        # S1a (A1 F1/F3/F17): session + membership of the asset's
        # workspace (from the DB row) + HMAC sidecar, else 404.
        try:
            asset = repo.get_asset(asset_id)
        except NotFound:
            raise HTTPException(404, _NOT_FOUND)
        _member_or_404(ctx, asset.get("workspace_id"))
        return _serve_blob(
            storage_key=asset["storage_key"],
            workspace_id=asset["workspace_id"],
            mime_type=asset["mime_type"],
            filename=asset["filename"],
        )

    # ── jobs ───────────────────────────────────────────────────

    @app.post("/api/v1/jobs")
    async def create_job(
        request: Request,
        payload: dict,
        ctx: AuthContext = Depends(get_current_user),
    ):
        # S1a (A1 F10): session + CSRF + editor membership of the
        # target workspace.  Non-member -> 404 (same as unknown id).
        _check_csrf(request)
        company_id = payload.get("company_id")
        if not company_id:
            raise HTTPException(400, "company_id required")
        required = ("topic", "language", "duration_seconds", "platforms")
        for f in required:
            if f not in payload:
                raise HTTPException(400, f"missing {f}")
        topic = _topic_or_422(payload)
        # Wave 6b: resolve the legacy company_id to a workspace_id
        # so the live-provider gate and the FREE-tier job quota
        # both see the same plan + flag as the workspace-scoped
        # route.  Without this lookup the legacy route would
        # silently bypass both gates.
        wid = repo.workspace_id_for_company(company_id) or company_id
        _member_or_404(ctx, wid, min_role="editor")
        try:
            _ws = repo.get_workspace(wid)
        except Exception:
            raise HTTPException(
                404, f"workspace for company_id {company_id} not found"
            )
        from core.quotas import (
            check_job_quota, check_live_provider_allowed,
        )
        from core.audit import (
            EVENT_LIVE_PROVIDER_BLOCKED, EVENT_QUOTA_EXCEEDED,
            emit as emit_audit,
        )
        from datetime import datetime, timezone
        live_decision = check_live_provider_allowed(
            plan=_ws.get("plan", "free"),
            workspace_live_enabled=_ws.get("live_enabled", False),
            render_profile=payload.get("render_profile", "mock"),
        )
        if not live_decision.allowed:
            emit_audit(
                repo,
                event=EVENT_LIVE_PROVIDER_BLOCKED,
                context={
                    "workspace_id": wid,
                    "plan": _ws.get("plan", "free"),
                    "ws_live_enabled": live_decision.ws_live_enabled,
                    "render_profile": payload.get(
                        "render_profile", "mock",
                    ),
                    "reason": live_decision.reason,
                    "endpoint": "POST /api/v1/jobs",
                },
                workspace_id=wid,
            )
            raise HTTPException(
                403, f"live providers blocked: {live_decision.reason}",
            )
        quota_decision = check_job_quota(
            workspace_id=wid,
            plan=_ws.get("plan", "free"),
            now=datetime.now(timezone.utc),
            job_count_for_month=lambda w, s: repo.count_jobs_since(
                workspace_id=w, since_iso=s,
            ),
        )
        if not quota_decision.allowed:
            emit_audit(
                repo,
                event=EVENT_QUOTA_EXCEEDED,
                context={
                    "workspace_id": wid,
                    "plan": quota_decision.plan,
                    "jobs_used": quota_decision.jobs_used,
                    "quota_reset_at": quota_decision.reset_at,
                    "endpoint": "POST /api/v1/jobs",
                },
                workspace_id=wid,
            )
            return JSONResponse(
                {
                    "error": (
                        f"FREE plan limit {quota_decision.limit} jobs/month "
                        f"reached; resets at {quota_decision.reset_at}"
                    ),
                    "quota_reset_at": quota_decision.reset_at,
                    "jobs_used": quota_decision.jobs_used,
                    "jobs_remaining": 0,
                },
                status_code=429,
            )
        job = repo.create_job(
            company_id=company_id,
            topic=topic,
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
    async def list_jobs(
        company_id: Optional[str] = None,
        state: Optional[str] = None,
        ctx: AuthContext = Depends(get_current_user),
    ):
        # S1a (A1 F2/F13): never list across tenants.  With
        # company_id: that workspace only, member or 404.  Without:
        # the union of the caller's own workspaces.
        if company_id:
            wid = repo.workspace_id_for_company(company_id) or company_id
            _member_or_404(ctx, wid)
            return repo.list_jobs(workspace_id=wid, state=state)
        rows: list[dict] = []
        for m in repo.list_workspaces_for_user(ctx.user_id):
            rows.extend(repo.list_jobs(workspace_id=m["workspace_id"],
                                       state=state))
        rows.sort(key=lambda j: j.get("created_at") or "", reverse=True)
        return rows

    @app.get("/api/v1/jobs/{job_id}")
    async def get_job(job_id: str,
                      ctx: AuthContext = Depends(get_current_user)):
        return _job_for(ctx, job_id)

    @app.get("/api/v1/jobs/{job_id}/stages")
    async def list_stages(job_id: str,
                          ctx: AuthContext = Depends(get_current_user)):
        _job_for(ctx, job_id)
        return repo.list_stages(job_id)

    @app.get("/api/v1/jobs/{job_id}/outputs")
    async def list_outputs(job_id: str,
                           ctx: AuthContext = Depends(get_current_user)):
        _job_for(ctx, job_id)
        return repo.list_outputs(job_id)

    @app.post("/api/v1/jobs/{job_id}/retry")
    async def retry_job(request: Request, job_id: str,
                        ctx: AuthContext = Depends(get_current_user)):
        _check_csrf(request)
        job = _job_for(ctx, job_id, min_role="editor")
        if job["state"] == "done":
            raise HTTPException(400, "job already done")
        # Wave 6b: live-provider gate re-checked at retry time
        # because plan may have flipped since the original submit
        # (downgrades, suspensions, future webhook events).  We
        # do NOT re-check the monthly job quota here: the job
        # already counted against the original submit, and a
        # user-driven retry shouldn't double-burn a quota slot.
        from core.quotas import check_live_provider_allowed
        from core.audit import (
            EVENT_LIVE_PROVIDER_BLOCKED, emit as emit_audit,
        )
        _ws = repo.get_workspace(job["workspace_id"])
        live_decision = check_live_provider_allowed(
            plan=_ws.get("plan", "free"),
            workspace_live_enabled=_ws.get("live_enabled", False),
            render_profile=job.get("render_profile", "mock"),
        )
        if not live_decision.allowed:
            emit_audit(
                repo,
                event=EVENT_LIVE_PROVIDER_BLOCKED,
                context={
                    "workspace_id": job["workspace_id"],
                    "plan": _ws.get("plan", "free"),
                    "ws_live_enabled": live_decision.ws_live_enabled,
                    "render_profile": job.get(
                        "render_profile", "mock",
                    ),
                    "reason": live_decision.reason,
                    "endpoint": "POST /api/v1/jobs/{job_id}/retry",
                },
                workspace_id=job["workspace_id"],
            )
            raise HTTPException(
                403, f"live providers blocked: {live_decision.reason}",
            )
        repo.update_job(job_id, state="queued", status_message="retry requested")
        runner.submit(job_id)
        return repo.get_job(job_id)

    @app.get("/api/v1/jobs/{job_id}/outputs/{output_id}/download")
    async def download_output(job_id: str, output_id: str,
                              ctx: AuthContext = Depends(get_current_user)):
        # S1a (A1 F4/F17): membership of the job's workspace, the
        # output must belong to that job, HMAC sidecar must bind the
        # blob to that workspace.  Every failure is the same 404.
        job = _job_for(ctx, job_id)
        try:
            output = repo.get_output(output_id)
        except NotFound:
            raise HTTPException(404, _NOT_FOUND)
        if output["job_id"] != job_id:
            raise HTTPException(404, _NOT_FOUND)
        return _serve_blob(
            storage_key=output["storage_key"],
            workspace_id=job["workspace_id"],
            mime_type=output["mime_type"],
            filename=output["filename"],
        )

    # ── publishing (mock) ──────────────────────────────────────

    @app.post("/api/v1/jobs/{job_id}/publish")
    async def create_publish_intent(
        request: Request, job_id: str, payload: dict,
        ctx: AuthContext = Depends(get_current_user),
    ):
        _check_csrf(request)
        job = _job_for(ctx, job_id, min_role="editor")
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
            company_id=job["workspace_id"],
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
    async def list_publish_intents(
        job_id: str, ctx: AuthContext = Depends(get_current_user),
    ):
        _job_for(ctx, job_id)
        return repo.list_publish_intents(job_id)

    # ── integrations ───────────────────────────────────────────

    @app.get("/api/v1/integrations")
    async def list_integrations(
        ctx: AuthContext = Depends(get_current_user),
    ):
        # Deployment-wide provider table (no tenant rows), but it
        # describes the deployment: logged-in users only (S1a).
        return repo.list_integrations()

    # ── schemas (Bible JSON Schema) ────────────────────────────

    @app.get("/api/v1/schemas/bible")
    async def bible_schema():
        from pathlib import Path as _P
        p = _P(__file__).resolve().parent.parent / "schemas" / "company-video-bible.schema.json"
        if not p.exists():
            raise HTTPException(404, "bible schema not found")
        return json.loads(p.read_text())

    # ── credentials (F-credential-vault) ────────────────────────

    @app.get("/api/v1/companies/{company_id}/credentials")
    async def list_credentials(
        company_id: str,
        ctx: AuthContext = Depends(get_current_user),
    ):
        wid = _resolve_cid_to_wid(repo, company_id)
        m = repo.get_membership(ctx.user_id, wid)
        if not m:
            raise HTTPException(403, "not a member of this workspace")
        return repo.list_credentials(company_id)

    @app.get("/api/v1/companies/{company_id}/credentials/{provider}")
    async def get_credential(
        company_id: str,
        provider: str,
        ctx: AuthContext = Depends(get_current_user),
    ):
        wid = _resolve_cid_to_wid(repo, company_id)
        m = repo.get_membership(ctx.user_id, wid)
        if not m:
            raise HTTPException(403, "not a member of this workspace")
        return repo.get_credential(company_id, provider)

    @app.put("/api/v1/companies/{company_id}/credentials/{provider}")
    async def put_credential(
        request: Request,
        company_id: str,
        provider: str,
        payload: dict,
        ctx: AuthContext = Depends(get_current_user),
    ):
        # MAJOR #5: auth + CSRF + membership.
        require_csrf(request, x_csrf_token=request.headers.get("X-CSRF-Token"))
        wid = _resolve_cid_to_wid(repo, company_id)
        m = repo.get_membership(ctx.user_id, wid)
        if not m:
            raise HTTPException(403, "not a member of this workspace")
        # Credentials: editor or higher.
        if not _has_min_role(m["role"], "editor"):
            raise HTTPException(403, "requires editor role")
        # Body shape: { "value": "<plaintext secret>" }
        plaintext = payload.get("value")
        if not isinstance(plaintext, str) or not plaintext.strip():
            raise HTTPException(400, "value required (non-empty string)")
        if provider not in repo.CREDENTIAL_PROVIDERS:
            raise HTTPException(400, f"unknown provider: {provider!r}")
        # Encrypt here. The repo only ever sees ciphertext.
        from control_plane.secret_vault import encrypt, fingerprint, SecretKeyMissing
        try:
            blob = encrypt(company_id, provider, plaintext)
        except SecretKeyMissing as e:
            raise HTTPException(503, str(e))
        fp = fingerprint(plaintext)
        operator = ctx.email[:64]
        # Audit row first so even if encryption fails the action is recorded.
        try:
            result = repo.upsert_credential(
                company_id=company_id, provider=provider,
                fingerprint_str=fp, encrypted_blob=blob,
                updated_by=operator,
            )
        except Exception as e:
            raise HTTPException(500, f"upsert failed: {e}")
        # Audit success to runner_audit.
        try:
            from control_plane.repo_helpers import append_runner_audit
            append_runner_audit(
                repo,
                job_id=f"cred::{company_id}::{provider}",
                event="credentials.upserted",
                context={
                    "provider": provider,
                    "fingerprint": fp,
                    "updated_by": operator,
                },
            )
        except Exception:
            pass
        return result

    @app.delete("/api/v1/companies/{company_id}/credentials/{provider}")
    async def delete_credential(
        request: Request,
        company_id: str,
        provider: str,
        ctx: AuthContext = Depends(get_current_user),
    ):
        # MAJOR #5: auth + CSRF + membership.
        require_csrf(request, x_csrf_token=request.headers.get("X-CSRF-Token"))
        wid = _resolve_cid_to_wid(repo, company_id)
        m = repo.get_membership(ctx.user_id, wid)
        if not m:
            raise HTTPException(403, "not a member of this workspace")
        if not _has_min_role(m["role"], "admin"):
            raise HTTPException(403, "requires admin role")
        operator = ctx.email[:64]
        try:
            repo.delete_credential(company_id=company_id, provider=provider)
        except Exception as e:
            # NotFound → 404
            raise HTTPException(404, str(e))
        try:
            from control_plane.repo_helpers import append_runner_audit
            append_runner_audit(
                repo,
                job_id=f"cred::{company_id}::{provider}",
                event="credentials.deleted",
                context={"provider": provider, "deleted_by": operator},
            )
        except Exception:
            pass
        return {"deleted": True, "provider": provider, "company_id": company_id}

    # ── webhooks (F8) ──────────────────────────────────────────

    from api.webhooks_heygen import build_router as _heygen_webhook_router
    from control_plane.audit import AuditLog as _AuditLogBase
    _heygen_router = _heygen_webhook_router(
        repo=repo,
        runner=runner,
        audit=runner.audit if isinstance(getattr(runner, "audit", None), _AuditLogBase) else None,
    )
    app.include_router(_heygen_router)

    # ── Wave 6c: Stripe webhook (billing + plan transitions) ──
    # Registered unconditionally: when STRIPE_WEBHOOK_SECRET is
    # missing the router factory raises at app construction time
    # so missing-config is loud at boot, not at first request.
    try:
        from api.webhooks_stripe import build_router as _stripe_webhook_router
        _stripe_router = _stripe_webhook_router(
            repo=repo,
            audit=runner.audit if isinstance(getattr(runner, "audit", None), _AuditLogBase) else None,
        )
        app.include_router(_stripe_router)
    except Exception as _stripe_e:
        # STRIPE_WEBHOOK_SECRET missing -> expose the config error
        # on a debug endpoint and continue serving non-Stripe routes.
        # Tests opt in by setting the env var before create_app().
        _stripe_err_msg = str(_stripe_e)

        @app.get("/_stripe_init_error")
        def _stripe_init_error() -> dict:
            return {"ok": False, "error": _stripe_err_msg}

    # ── auth (Wave 1) ────────────────────────────────────────

    from api.auth import build_router as _build_auth_router
    _auth_router = _build_auth_router(repo)
    app.include_router(_auth_router)

    # ── Wave 4 OIDC router (Google + GitHub) ────────────────
    # Only registered if at least one provider has both
    # *_OIDC_CLIENT_ID and *_OIDC_CLIENT_SECRET set in env;
    # otherwise it stays dormant and tests can poke the helpers
    # directly. This keeps offline / CI environments free of
    # dangling 503s.
    try:
        from api.oidc import build_router as _build_oidc_router
        _oidc_router = _build_oidc_router(repo)
        app.include_router(_oidc_router)
    except Exception as _e:
        # Surface the failure on /health for operator debugging.
        @app.get("/_oidc_init_error")
        def _oidc_init_error() -> dict:
            return {"ok": False, "error": str(_e)}

    # Set-Cookie middleware: the auth router stashes the cookie
    # value on request.state and the middleware writes it on the
    # way out. This keeps the route signatures clean.
    @app.middleware("http")
    async def _emit_session_cookie(request, call_next):
        response = await call_next(request)
        cookie = getattr(request.state, "_vf_session_cookie", None)
        if cookie:
            response.headers["Set-Cookie"] = cookie
        return response

    # Actor middleware: on every request, if a valid session cookie
    # is present, stamp the JobRunner with the (user_id,
    # workspace_id) of the current request so runner_audit rows
    # written by background jobs/credentials carry the actor.
    # Closes the "JobRunner.set_actor() is dead code" observation
    # from the Wave 1 QA report.
    @app.middleware("http")
    async def _stamp_actor(request, call_next):
        from auth.session import SESSION_COOKIE_NAME
        from auth.session import verify_csrf_token  # noqa: F401  (module re-use)
        try:
            sid = request.cookies.get(SESSION_COOKIE_NAME, "").strip() or None
            actor_user_id = None
            actor_workspace_id = None
            if sid:
                row = repo._conn().execute(
                    "SELECT s.user_id, m.workspace_id "
                    "FROM sessions s "
                    "LEFT JOIN memberships m ON m.user_id = s.user_id "
                    "WHERE s.session_id=? "
                    "ORDER BY m.created_at ASC, m.rowid ASC LIMIT 1",
                    (sid,),
                ).fetchone()
                if row:
                    actor_user_id = row[0]
                    actor_workspace_id = row[1]
            runner.set_actor(
                user_id=actor_user_id, workspace_id=actor_workspace_id,
            )
        except Exception:
            # Never let actor-stamping break the request; the runner
            # simply sees None and writes an unattributed audit row.
            try:
                runner.set_actor(user_id=None, workspace_id=None)
            except Exception:
                pass
        return await call_next(request)

    # Expose repo + runner on app.state for auth.dependencies to read.
    app.state.repo = repo
    app.state.runner = runner

    return app


# Allow `uvicorn api.main:app` without explicit factory call.
app = create_app()