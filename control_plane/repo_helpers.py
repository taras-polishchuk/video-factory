"""Runner-side audit append helper.

Used by core/runners.py to surface idempotency-durable-mirror outcomes
without dragging in the full Orchestrator / AuditLog graph (F3 fix).

The runner's "job" is the natural anchor for audit appends: every
`_write_durable` call happens during a job run, so we route the audit
event through a new lightweight SQLite table `runner_audit` keyed by
job_id. This avoids polluting the canonical job_stages table with
non-stage rows.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Dict, Optional


# Schema for the runner_audit table. Owned by Repo because Repo owns the
# connection + schema bootstrap. Kept here so the column shape is
# self-documenting alongside the helper.
RUNNER_AUDIT_SCHEMA = """
CREATE TABLE IF NOT EXISTS runner_audit (
    audit_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    event TEXT NOT NULL,
    context_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_runner_audit_job ON runner_audit(job_id);
"""


def bootstrap_runner_audit(repo) -> None:
    """Idempotently create the runner_audit table on the repo's connection.

    Called from Repo.__init__ AFTER the main SCHEMA executes so the
    audit table does not get into a half-created state on crash.
    """
    conn = repo._conn()
    conn.executescript(RUNNER_AUDIT_SCHEMA)


def append_runner_audit(
    repo,
    *,
    job_id: str,
    event: str,
    context: Optional[Dict[str, Any]] = None,
    user_id: Optional[str] = None,
    workspace_id: Optional[str] = None,
) -> None:
    """Append one row to runner_audit. Best-effort; never raises.

    Wave 1: optional `user_id` + `workspace_id` are accepted and
    stored in `audit_log` (the cross-actor audit surface) so that
    the operator can grep for actions by actor. The runner_audit
    table itself stays job-scoped; we use audit_log for the actor
    attribution so that auth events (which are not job-scoped)
    can also be recorded.
    """
    import time as _t
    import uuid as _u
    try:
        audit_id = f"aud_{_u.uuid4().hex[:12]}"
        conn = repo._conn()
        conn.execute(
            """INSERT INTO runner_audit(audit_id, job_id, event, context_json, created_at)
               VALUES (?,?,?,?,?)""",
            (
                audit_id,
                job_id,
                event,
                json.dumps(context or {}),
                _t.strftime("%Y-%m-%dT%H:%M:%SZ", _t.gmtime()),
            ),
        )
        # Mirror to audit_log with actor attribution. This is the
        # cross-actor audit surface (W1 brief §3.1). MAJ-1: routed
        # through Repo.append_audit, the single audit_log writer
        # (best-effort, never raises).
        if user_id is not None or workspace_id is not None:
            append_audit = getattr(repo, "append_audit", None)
            if append_audit is not None:
                append_audit(
                    event=event,
                    context=context or {},
                    user_id=user_id,
                    workspace_id=workspace_id,
                )
    except Exception:
        pass