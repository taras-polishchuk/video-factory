"""Cross-actor audit log for auth + workspace events.

The `audit_log` table lives in core/migrations/003..004 and
carries optional `user_id` + `workspace_id` columns for actor
attribution. Writes are best-effort; we never let a failed audit
append break the calling flow (we want a missed audit row, not
a missed login).

We deliberately do NOT extend the runner_audit table with these
columns — that table is a per-job audit surface, and
authentication events are not job-scoped.
"""
from __future__ import annotations

import json
import logging
import sys
import time
import uuid
from typing import Any, Dict, Optional

_logger = logging.getLogger("auth.audit_log")
if not _logger.handlers:
    _h = logging.StreamHandler(sys.stderr)
    _h.setFormatter(logging.Formatter(
        "[%(asctime)s] [%(name)s] [%(levelname)s] %(message)s"
    ))
    _logger.addHandler(_h)
    _logger.setLevel(logging.WARNING)
    _logger.propagate = False


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def append(repo, *, event: str, context: Optional[Dict[str, Any]] = None,
            user_id: Optional[str] = None,
            workspace_id: Optional[str] = None,
            actor: Optional[str] = None) -> None:
    """Append a row to audit_log. Never raises.

    `actor` is the denormalized display name (email or
    'operator') that downstream consumers can read without
    joining the users table.
    """
    try:
        audit_id = f"audlog_{uuid.uuid4().hex[:12]}"
        repo._conn().execute(
            "INSERT INTO audit_log(audit_id, event, context_json, user_id, "
            "workspace_id, actor, created_at) VALUES (?,?,?,?,?,?,?)",
            (audit_id, event, json.dumps(context or {}), user_id,
             workspace_id, actor, _now()),
        )
        repo._conn().commit()
    except Exception as e:
        # Surface the failure to stderr instead of silently dropping
        # the row. We do NOT raise — a missed audit row is better
        # than a missed login — but the operator now sees the
        # regression (most commonly: migration 004 not applied yet).
        _logger.warning(
            "audit_append dropped row event=%s user=%s workspace=%s err=%s: %s",
            event, user_id, workspace_id, type(e).__name__, e,
        )


def list_for_workspace(repo, *, workspace_id: str, limit: int = 100) -> list[dict]:
    """Return recent audit rows for a workspace (newest first)."""
    rows = repo._conn().execute(
        "SELECT audit_id, event, context_json, user_id, workspace_id, "
        "actor, created_at FROM audit_log WHERE workspace_id=? "
        "ORDER BY created_at DESC LIMIT ?",
        (workspace_id, int(limit)),
    ).fetchall()
    return [
        {
            "audit_id": r[0],
            "event": r[1],
            "context": json.loads(r[2]) if r[2] else {},
            "user_id": r[3],
            "workspace_id": r[4],
            "actor": r[5],
            "created_at": r[6],
        }
        for r in rows
    ]
