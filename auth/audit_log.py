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
import time
import uuid
from typing import Any, Dict, Optional


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def append(repo, *, event: str, context: Optional[Dict[str, Any]] = None,
            user_id: Optional[str] = None,
            workspace_id: Optional[str] = None) -> None:
    """Append a row to audit_log. Never raises."""
    try:
        audit_id = f"audlog_{uuid.uuid4().hex[:12]}"
        repo._conn().execute(
            "INSERT INTO audit_log(audit_id, event, context_json, user_id, "
            "workspace_id, created_at) VALUES (?,?,?,?,?,?)",
            (audit_id, event, json.dumps(context or {}), user_id,
             workspace_id, _now()),
        )
        repo._conn().commit()
    except Exception:
        # Best effort. If the columns are not yet present we silently
        # lose the row; the migration 004 ensures they exist in
        # production.
        pass


def list_for_workspace(repo, *, workspace_id: str, limit: int = 100) -> list[dict]:
    """Return recent audit rows for a workspace (newest first)."""
    rows = repo._conn().execute(
        "SELECT audit_id, event, context_json, user_id, workspace_id, created_at "
        "FROM audit_log WHERE workspace_id=? ORDER BY created_at DESC LIMIT ?",
        (workspace_id, int(limit)),
    ).fetchall()
    return [
        {
            "audit_id": r[0],
            "event": r[1],
            "context": json.loads(r[2]) if r[2] else {},
            "user_id": r[3],
            "workspace_id": r[4],
            "created_at": r[5],
        }
        for r in rows
    ]
