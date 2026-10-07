"""Workspace resolution: pick the active workspace for a request.

The X-Workspace-Id header convention:

- If the header is present AND the user is a member of that
  workspace -> use that workspace for the request.
- If the header is present AND the user is NOT a member -> 403.
- If the header is absent -> use the session's
  `active_workspace_id` (set on login or by a previous
  /auth/switch-workspace call).

The resolver is a FastAPI dependency that takes the auth context
+ the request and returns a workspace dict (or raises HTTP 401
if the user is unauthenticated).

The session's active_workspace_id is stored in the
`sessions.active_workspace_id` column.  Migration 006 adds the
column if it does not exist; older sessions are treated as
"first workspace" by `_pick_default_workspace()`.
"""
from __future__ import annotations

from typing import Optional

from fastapi import Header, HTTPException, Request


def resolve_active_workspace(
    request: Request,
    x_workspace_id: Optional[str] = Header(default=None,
                                            alias="X-Workspace-Id"),
) -> dict:
    """Resolve the active workspace for the current request.

    Returns the workspace dict.  Raises:
    - 401 if the user is not authenticated.
    - 403 if the user is not a member of the requested workspace.
    - 404 if the workspace does not exist.
    - 503 if the workspace is disabled.

    The function is written to be safely callable WITHOUT FastAPI
    dependency injection (i.e. when invoked directly as
    `resolve_active_workspace(request)`).  In that case the
    `x_workspace_id` arg is a FastAPI `Header` object, and we
    re-read the value from `request.headers` instead.
    """
    from auth.dependencies import get_current_user
    from core.repo import NotFound
    # Re-resolve the header from the request when the function
    # was called without FastAPI injection (so x_workspace_id is
    # a Header placeholder, not a string).
    if x_workspace_id is None or not isinstance(x_workspace_id, str):
        x_workspace_id = request.headers.get("X-Workspace-Id") or None
    # `get_current_user` reads the cookie and 401s if missing.
    try:
        ctx = get_current_user(request)
    except HTTPException as e:
        raise HTTPException(e.status_code,
                            "authentication required for workspace access")
    repo = request.app.state.repo
    # Pick the workspace id.
    if x_workspace_id:
        # Header wins, but only if the user is a member.
        try:
            ws = repo.get_workspace(x_workspace_id)
        except NotFound:
            raise HTTPException(404, f"workspace {x_workspace_id} not found")
        if ws.get("status") == "disabled":
            raise HTTPException(503, f"workspace is disabled")
        m = repo.get_membership(ctx.user_id, ws["workspace_id"])
        if not m:
            raise HTTPException(403, "not a member of this workspace")
        return ws
    # No header: use the session's active workspace, else the
    # first workspace the user is a member of.
    active = _session_active_workspace(repo, request, ctx.user_id)
    if active:
        return active
    ms = repo.list_workspaces_for_user(ctx.user_id)
    if not ms:
        raise HTTPException(404, "user has no workspace")
    return repo.get_workspace(ms[0]["workspace_id"])


def _session_active_workspace(repo, request: Request,
                                user_id: str) -> Optional[dict]:
    """Return the workspace the user's current session is bound to.

    Reads `sessions.active_workspace_id` (added in Wave 2).  If
    the column is missing or NULL, falls back to the user's
    oldest membership.

    Defense-in-depth (Wave 2 follow-up, t_d76b7c0c, MAJOR #4):
    after reading `sessions.active_workspace_id`, verify the
    user is actually a member of that workspace. If not, the
    session column was tampered with (or a stale binding
    outlived a removed membership) — return None so the
    resolver falls back to `list_workspaces_for_user`.
    """
    from auth.session import SESSION_COOKIE_NAME
    sid = request.cookies.get(SESSION_COOKIE_NAME)
    if not sid:
        return None
    # Read the active_workspace_id column if it exists.
    try:
        cols = {row[1] for row in repo._conn().execute(
            "PRAGMA table_info(sessions)"
        ).fetchall()}
    except Exception:
        return None
    if "active_workspace_id" not in cols:
        return None
    row = repo._conn().execute(
        "SELECT active_workspace_id FROM sessions WHERE session_id=?",
        (sid,),
    ).fetchone()
    if not row or not row[0]:
        return None
    wid = row[0]
    # Re-verify membership before trusting the session column.
    if not repo.get_membership(user_id, wid):
        return None
    try:
        return repo.get_workspace(wid)
    except Exception:
        return None


def set_session_active_workspace(repo, session_id: str,
                                    workspace_id: str) -> None:
    """Set the active_workspace_id on a session row.

    Adds the column if it does not exist (idempotent).
    """
    cols = {row[1] for row in repo._conn().execute(
        "PRAGMA table_info(sessions)"
    ).fetchall()}
    if "active_workspace_id" not in cols:
        repo._conn().execute(
            "ALTER TABLE sessions ADD COLUMN active_workspace_id TEXT"
        )
    repo._conn().execute(
        "UPDATE sessions SET active_workspace_id=? WHERE session_id=?",
        (workspace_id, session_id),
    )
    repo._conn().commit()
