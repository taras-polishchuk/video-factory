"""HeyGen webhook handler + signature verification.

HeyGen docs (https://developers.heygen.com/webhooks) recommend HMAC-SHA256
signing with a shared secret. The signature is delivered in the
`X-Heygen-Signature` request header as `sha256=<hex>`.

This module:

  * Exposes a FastAPI router at /api/v1/webhooks/heygen.
  * Verifies the signature against `HEYGEN_WEBHOOK_SECRET`.
  * Rejects replays of an already-processed signed delivery
    (S1b / A1 F12, see below).
  * Translates the documented lifecycle events into state-machine
    transitions on the matching job.
  * Records an audit row per webhook receipt.

The handler refuses ALL payloads when `HEYGEN_WEBHOOK_SECRET` is
unset — that prevents accidental acceptance of unsigned webhooks from
the public internet.

What a HeyGen delivery carries (as modelled by this repo; read from
this module, render_plane/heygen_direct.py and the tests, no network
calls made for S1b):

  * body: JSON with ``event_type`` (``video.completed`` /
    ``video.failed``) and ``video_id`` at the top level or under
    ``data``; the submit path also sets a ``callback_id`` (our
    idempotency key) for HeyGen to echo back.
  * ``X-Heygen-Signature: sha256=<hex>`` = HMAC-SHA256(secret, raw body).
  * optional ``X-Heygen-Event`` header.
  * NO signed timestamp and NO delivery / event id.

Replay protection (F12): because nothing time-bound is signed, a
5-minute timestamp tolerance is impossible (an unsigned timestamp
header could simply be rewritten by the replayer). Instead every
accepted delivery is recorded in ``webhook_receipts`` keyed by
SHA-256 of the raw signed body. The signature covers the whole body,
so a replayer can only resend byte-identical bodies, and those hit
the dedup row: the handler answers ``200 {"duplicate": true}`` (so
HeyGen's own retry of a delivery we already processed stops) and
drives NO state change. Rows are kept ``HEYGEN_WEBHOOK_DEDUP_DAYS``
(default 30) days, far longer than any render job stays in
``processing``.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import time
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


def _dedup_days() -> int:
    raw = (os.environ.get("HEYGEN_WEBHOOK_DEDUP_DAYS") or "").strip()
    try:
        return max(1, int(raw)) if raw else 30
    except ValueError:
        return 30


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def claim_delivery(repo, *, body: bytes, event: str) -> bool:
    """Record a signed delivery; False if this exact body was seen.

    The table is created on demand (same pattern as ``oidc_pending``)
    so no migration number is consumed. ``INSERT OR IGNORE`` on the
    primary key makes the claim atomic across threads / workers that
    share the DB.
    """
    key = hashlib.sha256(body).hexdigest()
    now = time.time()
    with repo.tx() as c:
        c.execute(
            "CREATE TABLE IF NOT EXISTS webhook_receipts ("
            "  source TEXT NOT NULL,"
            "  dedup_key TEXT NOT NULL,"
            "  event TEXT,"
            "  received_at TEXT NOT NULL,"
            "  PRIMARY KEY (source, dedup_key)"
            ")"
        )
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_webhook_receipts_received "
            "ON webhook_receipts(received_at)"
        )
        c.execute(
            "DELETE FROM webhook_receipts WHERE received_at < ?",
            (_iso(now - _dedup_days() * 86400),),
        )
        cur = c.execute(
            "INSERT OR IGNORE INTO webhook_receipts"
            "(source, dedup_key, event, received_at) VALUES (?,?,?,?)",
            ("heygen", key, (event or "")[:64], _iso(now)),
        )
        return cur.rowcount == 1


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

        # S1b / A1 F12: a byte-identical signed body that was already
        # accepted is a replay (or HeyGen re-delivering something we
        # processed). Acknowledge it, but never drive state again.
        if not claim_delivery(repo, body=body, event=event):
            if audit is not None:
                from control_plane.audit import AuditRecord, now_iso
                audit.record(AuditRecord(
                    timestamp=now_iso(), event="webhook.heygen.replay_rejected",
                    actor="heygen_webhook",
                    provider_job_id=video_id,
                    context={"event": event},
                ))
            return {"received": True, "duplicate": True, "event": event,
                    "video_id": video_id}

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