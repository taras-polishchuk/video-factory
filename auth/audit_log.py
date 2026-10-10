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

import logging
import sys
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


def append(repo, *, event: str, context: Optional[Dict[str, Any]] = None,
            user_id: Optional[str] = None,
            workspace_id: Optional[str] = None,
            actor: Optional[str] = None) -> None:
    """Append a row to audit_log. Never raises.

    `actor` is the denormalized display name (email or
    'operator') that downstream consumers can read without
    joining the users table.

    MAJ-1: delegates to ``Repo.append_audit``, the single writer of
    the ``audit_log`` table (which logs a warning on a dropped row).
    """
    try:
        repo.append_audit(
            event=event, context=context or {}, user_id=user_id,
            workspace_id=workspace_id, actor=actor,
        )
    except Exception as e:
        # Repo.append_audit never raises; this only fires for a
        # repo object without that method.
        _logger.warning(
            "audit_append dropped row event=%s user=%s workspace=%s err=%s: %s",
            event, user_id, workspace_id, type(e).__name__, e,
        )


def list_for_workspace(repo, *, workspace_id: str, limit: int = 100) -> list[dict]:
    """Return recent audit rows for a workspace (newest first)."""
    return repo.list_audit(workspace_id=workspace_id, limit=limit)
