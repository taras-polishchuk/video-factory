"""FastAPI dependency-injection helpers for auth.

The endpoints import these directly:

    from auth.dependencies import (
        get_current_user, get_user_or_401, require_csrf,
        resolve_workspace_membership, AuthContext,
    )

Error semantics:

- Missing/invalid/expired cookie → 401
- Cookie valid, but the user has no membership in the requested
  workspace → 403
- Membership present but role < required → 403
- The user is locked (status='locked') → 423

The CSRF guard is a separate dependency (`require_csrf`) that
state-changing endpoints (POST/PUT/DELETE/PATCH) opt into. It
verifies the `X-CSRF-Token` header matches the session's CSRF
token. The header is mandatory even for the signup endpoint
because signup IS a state-changing request that establishes a
new account.

Why a separate header (and not the cookie) for CSRF:

The cookie is HttpOnly + SameSite=Strict, so the browser sends
it automatically. An attacker page can also cause the cookie to
be sent on a cross-site request, but cannot read it. The header
requires the JS app to explicitly echo a value bound to the
session, which the attacker page cannot do.
"""
from __future__ import annotations

import time as _time
from dataclasses import dataclass
from typing import Optional

from fastapi import Header, HTTPException, Request

from auth.roles import has_min_role
from auth.session import (
    SESSION_COOKIE_NAME,
    is_expired,
    verify_csrf_token,
)
from auth.session import expires_at_iso as _expires_at_iso


@dataclass
class AuthContext:
    user_id: str
    email: str
    workspace_id: Optional[str] = None
    role: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "user_id": self.user_id,
            "email": self.email,
            "workspace_id": self.workspace_id,
            "role": self.role,
        }


def _read_cookie(request: Request) -> Optional[str]:
    raw = request.cookies.get(SESSION_COOKIE_NAME)
    if not raw:
        return None
    return raw.strip() or None


def _load_session(repo, session_id: str) -> Optional[dict]:
    row = repo._conn().execute(
        "SELECT session_id, user_id, csrf_token, expires_at "
        "FROM sessions WHERE session_id=?",
        (session_id,),
    ).fetchone()
    if not row:
        return None
    return {
        "session_id": row[0],
        "user_id": row[1],
        "csrf_token": row[2],
        "expires_at": row[3],
    }


def _load_user(repo, user_id: str) -> Optional[dict]:
    row = repo._conn().execute(
        "SELECT user_id, email, status FROM users WHERE user_id=?",
        (user_id,),
    ).fetchone()
    if not row:
        return None
    return {"user_id": row[0], "email": row[1], "status": row[2]}


def get_current_user(request: Request) -> AuthContext:
    """Resolve the current authenticated user, or 401.

    The returned AuthContext has workspace_id=None. Endpoints that
    need a workspace-scoped view should call
    `resolve_workspace_membership(ctx, workspace_id, min_role)`.
    """
    repo = request.app.state.repo
    session_id = _read_cookie(request)
    if not session_id:
        raise HTTPException(401, "missing session cookie")
    sess = _load_session(repo, session_id)
    if not sess:
        raise HTTPException(401, "invalid session")
    if is_expired(sess["expires_at"]):
        try:
            repo._conn().execute(
                "DELETE FROM sessions WHERE session_id=?", (session_id,)
            )
            repo._conn().commit()
        except Exception:
            pass
        raise HTTPException(401, "session expired")
    user = _load_user(repo, sess["user_id"])
    if not user:
        raise HTTPException(401, "user not found")
    if user["status"] == "locked":
        raise HTTPException(423, "user is locked")
    # Update rolling expiry (best-effort, not on the critical path).
    try:
        repo._conn().execute(
            "UPDATE sessions SET last_seen_at=?, expires_at=? WHERE session_id=?",
            (_time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime()),
             _expires_at_iso(), session_id),
        )
        repo._conn().commit()
    except Exception:
        pass
    return AuthContext(user_id=user["user_id"], email=user["email"])


def list_workspaces_for_user(repo, user_id: str) -> list[dict]:
    """Return all workspaces the user is a member of, with their role."""
    rows = repo._conn().execute(
        "SELECT m.workspace_id, m.role, w.slug, w.name, w.plan "
        "FROM memberships m JOIN workspaces w ON w.workspace_id = m.workspace_id "
        "WHERE m.user_id=? ORDER BY w.created_at, w.rowid",
        (user_id,),
    ).fetchall()
    return [
        {
            "workspace_id": r[0],
            "role": r[1],
            "slug": r[2],
            "name": r[3],
            "plan": r[4],
        }
        for r in rows
    ]


def get_membership(repo, user_id: str, workspace_id: str) -> Optional[dict]:
    row = repo._conn().execute(
        "SELECT user_id, workspace_id, role FROM memberships "
        "WHERE user_id=? AND workspace_id=?",
        (user_id, workspace_id),
    ).fetchone()
    if not row:
        return None
    return {"user_id": row[0], "workspace_id": row[1], "role": row[2]}


def resolve_workspace_membership(repo, ctx: AuthContext, workspace_id: str,
                                  min_role: str = "viewer") -> AuthContext:
    """Verify the user has at least `min_role` in `workspace_id` or 403.

    Returns an AuthContext with `workspace_id` and `role` populated.
    Use this in any endpoint that touches workspace-scoped data.
    """
    m = get_membership(repo, ctx.user_id, workspace_id)
    if not m:
        raise HTTPException(403, "not a member of this workspace")
    if not has_min_role(m["role"], min_role):
        raise HTTPException(403, f"requires role >= {min_role}")
    ctx.workspace_id = workspace_id
    ctx.role = m["role"]
    return ctx


def require_csrf(request: Request,
                  x_csrf_token: Optional[str] = Header(default=None)) -> None:
    """Verify the X-CSRF-Token header for state-changing requests.

    Endpoints that need this should declare it as a dependency.

    S1b / A1 F9: the session behind the cookie must also still be
    LIVE. An expired row (not yet GC'd) is deleted and refused with
    401, exactly like ``get_current_user``; a revoked session (row
    deleted by logout / password reset / login rotation) has no row
    and is refused with 401; a locked or deleted user is refused too.
    """
    repo = request.app.state.repo
    session_id = _read_cookie(request)
    if not session_id:
        raise HTTPException(401, "session required for state-changing request")
    sess = _load_session(repo, session_id)
    if not sess:
        raise HTTPException(401, "invalid session")
    if is_expired(sess["expires_at"]):
        try:
            repo._conn().execute(
                "DELETE FROM sessions WHERE session_id=?", (session_id,)
            )
            repo._conn().commit()
        except Exception:
            pass
        raise HTTPException(401, "session expired")
    user = _load_user(repo, sess["user_id"])
    if not user:
        raise HTTPException(401, "user not found")
    if user["status"] == "locked":
        raise HTTPException(423, "user is locked")
    if not x_csrf_token or not verify_csrf_token(session_id, x_csrf_token):
        raise HTTPException(403, "invalid CSRF token")
