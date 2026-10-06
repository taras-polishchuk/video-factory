"""HeyGen webhook handler + signature verification.

HeyGen docs (https://developers.heygen.com/webhooks) recommend HMAC-SHA256
signing with a shared secret. The signature is delivered in the
`X-Heygen-Signature` request header as `sha256=<hex>`.

This module:

  * Exposes a FastAPI router at /api/v1/webhooks/heygen.
  * Verifies the signature against `HEYGEN_WEBHOOK_SECRET`.
  * Translates the documented lifecycle events into state-machine
    transitions on the matching job.
  * Records an audit row per webhook receipt.

The handler refuses ALL payloads when `HEYGEN_WEBHOOK_SECRET` is
unset — that prevents accidental acceptance of unsigned webhooks from
the public internet.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from typing import Any, Dict, Optional

from fastapi import APIRouter, Header, HTTPException, Request


# Documented HeyGen event types we care about. Others are accepted but
# not acted on (logged).
HEYGEN_EVENT_COMPLETED = "video.completed"
HEYGEN_EVENT_FAILED = "video.failed"


def _secret() -> Optional[str]:
    return os.environ.get("HEYGEN_WEBHOOK_SECRET")


def verify_heygen_signature(*, body: bytes, signature_header: Optional[str]) -> bool:
    """Return True iff signature_header is a valid HMAC of body using HEYGEN_WEBHOOK_SECRET.

    Signature format: `sha256=<hex>`. Per HeyGen docs.
    """
    secret = _secret()
    if not secret:
        # No secret configured → refuse. Defense in depth.
        return False
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected_hex = signature_header.split("=", 1)[1]
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected_hex.lower(), expected.lower())


def build_router(repo, *, runner=None, audit=None) -> APIRouter:
    """Build the HeyGen webhook router bound to a repo + optional runner.

    The runner parameter lets us drive status transitions back into the
    state machine when a webhook arrives. If absent (test mode), the
    handler returns 202 and logs the event without driving state.
    """
    router = APIRouter()

    @router.post("/api/v1/webhooks/heygen")
    async def heygen_webhook(
        request: Request,
        x_heygen_signature: Optional[str] = Header(default=None),
        x_heygen_event: Optional[str] = Header(default=None),
    ):
        body = await request.body()
        if not verify_heygen_signature(body=body, signature_header=x_heygen_signature):
            # Log the rejection for forensics, but never echo the body.
            if audit is not None:
                from control_plane.audit import AuditRecord, now_iso
                audit.record(AuditRecord(
                    timestamp=now_iso(), event="webhook.rejected",
                    actor="heygen_webhook",
                    context={"reason": "signature_invalid_or_missing"},
                ))
            raise HTTPException(401, "invalid or missing signature")

        # Signature OK. Parse body and translate.
        try:
            payload: Dict[str, Any] = await request.json()
        except Exception:
            raise HTTPException(400, "invalid JSON body")
        event = (x_heygen_event or payload.get("event_type") or "").strip().lower()
        video_id = payload.get("video_id") or payload.get("data", {}).get("video_id")
        if audit is not None:
            from control_plane.audit import AuditRecord, now_iso
            audit.record(AuditRecord(
                timestamp=now_iso(), event="webhook.heygen",
                actor="heygen_webhook",
                provider_job_id=video_id,
                context={"event": event, "has_runner": runner is not None},
            ))

        # We do not drive state from a webhook unless we can find a job
        # whose provider_job_id matches the HeyGen video_id. That mapping
        # lives in the in-memory idempotency store of the running process
        # OR in the durable idem.db on disk. The runner owns that store.
        if runner is not None and video_id:
            try:
                # Look up by job_id in repo (the JobRunner has its own
                # _idem_store; cross-process lookup goes via repo).
                jobs = repo.list_jobs(state="processing")
                target_job = next(
                    (j for j in jobs if j.get("provider_job_id") == video_id),
                    None,
                )
                if target_job:
                    if event == HEYGEN_EVENT_COMPLETED:
                        repo.update_job(target_job["job_id"], status_message="heygen:video.completed")
                    elif event == HEYGEN_EVENT_FAILED:
                        repo.update_job(
                            target_job["job_id"],
                            state="failed",
                            status_message="heygen:video.failed",
                        )
            except Exception:
                # Webhook handler never raises on driver failures.
                pass

        return {"received": True, "event": event, "video_id": video_id}

    return router