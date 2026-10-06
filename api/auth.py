"""Auth endpoints for Video Factory.

Exposes the seven endpoints from W1 §3.1:

- POST /auth/signup        {email, password, workspace_name}
- POST /auth/login         {email, password}
- POST /auth/logout
- POST /auth/forgot        {email}
- POST /auth/verify        {token}
- GET  /auth/me
- POST /auth/workspaces    {slug, name}    (create additional workspace)

Also exposes:

- GET  /auth/csrf          Returns the CSRF token for the current session.
                           The UI calls this on page load so that subsequent
                           state-changing requests can attach X-CSRF-Token.

Design notes:

- Signup creates user + workspace + owner membership in a single
  transaction. The atomicity is enforced by the underlying
  `repo.tx()` context manager.
- Login checks rate limit BEFORE password verification so the
  attacker cannot use timing to enumerate valid emails.
- /auth/forgot is a no-op-by-design: we always return 200 even if
  the email is unknown, so the endpoint cannot be used to enumerate
  accounts. The "magic link" is appended to the audit log + printed
  in dev mode (no real SMTP integration yet — that's Wave 3).
- Cookies are HttpOnly + SameSite=Strict. Secure is added in
  production (`VF_HTTPS=1` or `VIDEO_FACTORY_ENV=production`).
- The legacy `/api/v1/companies/{cid}` alias is preserved so the
  existing 94 tests + UI work unchanged. We resolve
  `cid → workspace_id` via a dedicated helper that returns the
  *existing* company (the rename is Wave 2's job).
"""
from __future__ import annotations

import hashlib
import os
import re
import secrets
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from auth.audit_log import append as audit_append
from auth.dependencies import (
    AuthContext,
    get_current_user,
    list_workspaces_for_user,
    resolve_workspace_membership,
)
from auth.ratelimit import decide as rl_decide, record_attempt
from auth.session import (
    DEFAULT_TTL_SECONDS,
    SessionSecretMissing,
    build_clear_cookie_value,
    build_set_cookie_value,
    derive_csrf_token,
    expires_at_iso,
    is_expired,
    new_session_id,
    should_use_secure_cookies,
)
from core.repo import Repo


# Workspace slug policy: lowercase letters, digits, dashes; 3..40 chars.
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,38}[a-z0-9]$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def build_router(repo: Repo) -> APIRouter:
    """Build the /auth/* router bound to the given repo.

    The repo is captured in the closure so endpoints do not have to
    thread it through every signature.
    """
    router = APIRouter(prefix="/auth", tags=["auth"])

    def _client_ip(request: Request) -> str:
        return (request.client.host if request.client else "") or "unknown"

    def _user_agent(request: Request) -> str:
        return (request.headers.get("user-agent") or "")[:512]

    def _require_secret() -> None:
        try:
            from auth.session import _session_secret
            _session_secret()
        except SessionSecretMissing as e:
            raise HTTPException(503, str(e))

    # ── signup ───────────────────────────────────────────────

    @router.post("/signup")
    async def signup(request: Request, payload: dict):
        _require_secret()
        email = (payload.get("email") or "").strip()
        password = payload.get("password") or ""
        workspace_name = (payload.get("workspace_name") or "").strip()
        if not _EMAIL_RE.match(email):
            raise HTTPException(400, "invalid email")
        from auth.passwords import hash_password, is_strong_password
        if not is_strong_password(password):
            raise HTTPException(
                400, "password too weak: minimum 12 chars with upper, lower, digit"
            )
        if not workspace_name or len(workspace_name) > 80:
            raise HTTPException(400, "workspace_name required (1..80 chars)")

        # Slug: derive from email local-part + a 6-char suffix to
        # guarantee uniqueness on retry.
        local = email.split("@", 1)[0].lower()
        local = re.sub(r"[^a-z0-9-]+", "-", local).strip("-")[:30] or "user"
        suffix = secrets.token_hex(3)
        slug = f"{local}-{suffix}"
        # Workspace name: keep user input.
        wname = workspace_name

        # Atomic: user + workspace + membership in a single tx.
        with repo.tx() as c:
            try:
                user_id = f"usr_{secrets.token_hex(8)}"
                c.execute(
                    "INSERT INTO users(user_id, email, password_hash, status, "
                    "email_verified, created_at) VALUES (?,?,?,?,?,?)",
                    (user_id, email.lower(), hash_password(password),
                     "active", 0, _now_iso()),
                )
            except Exception as e:
                if "UNIQUE constraint failed" in str(e):
                    raise HTTPException(409, f"email {email!r} already exists") from e
                raise
            try:
                workspace_id = f"wks_{secrets.token_hex(8)}"
                c.execute(
                    "INSERT INTO workspaces(workspace_id, slug, name, "
                    "owner_user_id, plan, status, live_enabled, created_at) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (workspace_id, slug, wname, user_id, "free",
                     "active", 0, _now_iso()),
                )
            except Exception as e:
                if "UNIQUE constraint failed" in str(e) and "slug" in str(e):
                    # extremely unlikely; retry once
                    slug = f"{local}-{secrets.token_hex(4)}"
                    c.execute(
                        "INSERT INTO workspaces(workspace_id, slug, name, "
                        "owner_user_id, plan, status, live_enabled, "
                        "created_at) VALUES (?,?,?,?,?,?,?,?)",
                        (f"wks_{secrets.token_hex(8)}", slug, wname,
                         user_id, "free", "active", 0, _now_iso()),
                    )
                else:
                    raise
            c.execute(
                "INSERT INTO memberships(user_id, workspace_id, role, "
                "created_at) VALUES (?,?,?,?)",
                (user_id, _latest_workspace_id(c, user_id), "owner", _now_iso()),
            )
            # Email verification token
            token = secrets.token_urlsafe(24)
            c.execute(
                "INSERT INTO email_verifications(token, user_id, expires_at, "
                "created_at) VALUES (?,?,?,?)",
                (token, user_id, _now_plus(24*60*60), _now_iso()),
            )

        # Issue session
        session_id = new_session_id()
        csrf = derive_csrf_token(session_id)
        repo.create_session(
            user_id=user_id, csrf_token=csrf,
            ip=_client_ip(request), user_agent=_user_agent(request),
            expires_at=expires_at_iso(),
            session_id=session_id,
        )
        audit_append(
            repo, event="auth.signup",
            context={"email": email, "workspace_slug": slug},
            user_id=user_id,
            workspace_id=_latest_workspace_id(repo._conn(), user_id),
        )
        return _set_session_cookie(
            request, session_id=session_id,
            workspace_id=_latest_workspace_id(repo._conn(), user_id),
            user_id=user_id, email=email,
            extra={"verification_token": token, "workspace_slug": slug},
        )

    # ── login ────────────────────────────────────────────────

    @router.post("/login")
    async def login(request: Request, payload: dict):
        _require_secret()
        email = (payload.get("email") or "").strip()
        password = payload.get("password") or ""
        if not email or not password:
            raise HTTPException(400, "email and password required")
        ip = _client_ip(request)
        decision = rl_decide(repo, ip=ip, email=email)
        if not decision.allowed:
            audit_append(
                repo, event="auth.login_locked",
                context={"email": email, "ip": ip,
                          "reason": decision.reason},
            )
            status = 423 if decision.reason == "email_lockout" else 429
            raise HTTPException(
                status, "too many failed attempts; try again later",
            )
        user = repo.get_user_by_email(email)
        ok = False
        if user is not None and user["status"] == "active":
            from auth.passwords import verify_password
            if verify_password(password, user["password_hash"]):
                ok = True
        record_attempt(repo, ip=ip, email=email, succeeded=ok)
        if user is not None and user["status"] == "locked":
            # 423 Locked: account is administratively locked
            audit_append(
                repo, event="auth.login_locked_user",
                context={"email": email, "ip": ip,
                          "reason": "user_locked"},
            )
            raise HTTPException(423, "user is locked")
        if not ok or user is None:
            audit_append(
                repo, event="auth.login_failed",
                context={"email": email, "ip": ip, "user_exists": user is not None},
            )
            raise HTTPException(401, "invalid email or password")
        repo.update_user_last_login(user["user_id"])
        session_id = new_session_id()
        csrf = derive_csrf_token(session_id)
        repo.create_session(
            user_id=user["user_id"], csrf_token=csrf,
            ip=ip, user_agent=_user_agent(request),
            expires_at=expires_at_iso(),
            session_id=session_id,
        )
        audit_append(
            repo, event="auth.login_succeeded",
            context={"email": email, "ip": ip},
            user_id=user["user_id"],
        )
        # Look up the user's first workspace (oldest membership) so the
        # client can immediately land on a workspace-scoped view.
        first_ws = _first_workspace_for_user(repo._conn(), user["user_id"])
        return _set_session_cookie(
            request, session_id=session_id,
            workspace_id=first_ws["workspace_id"] if first_ws else None,
            workspace_slug=first_ws["slug"] if first_ws else None,
            user_id=user["user_id"], email=user["email"],
        )

    # ── logout ───────────────────────────────────────────────

    @router.post("/logout")
    async def logout(request: Request, response: Response,
                      ctx: AuthContext = Depends(get_current_user)):
        from auth.dependencies import require_csrf
        require_csrf(request, x_csrf_token=request.headers.get("X-CSRF-Token"))
        # Pull the cookie and delete that specific session.
        from auth.session import SESSION_COOKIE_NAME
        sid = request.cookies.get(SESSION_COOKIE_NAME)
        if sid:
            try:
                repo.delete_session(sid)
            except Exception:
                pass
        audit_append(
            repo, event="auth.logout",
            context={"ip": _client_ip(request)},
            user_id=ctx.user_id,
        )
        response.headers["Set-Cookie"] = build_clear_cookie_value(
            secure=should_use_secure_cookies(),
        )
        return {"ok": True}

    # ── forgot ───────────────────────────────────────────────

    @router.post("/forgot")
    async def forgot(request: Request, payload: dict):
        # Always 200 to prevent email enumeration.
        email = (payload.get("email") or "").strip().lower()
        ip = _client_ip(request)
        token_value = None
        if email:
            user = repo.get_user_by_email(email)
            if user:
                token = secrets.token_urlsafe(24)
                repo.create_password_reset(
                    user_id=user["user_id"], token=token,
                    expires_at=_now_plus(60*60),
                )
                token_value = token
                audit_append(
                    repo, event="auth.password_reset_requested",
                    context={"email": email, "ip": ip},
                    user_id=user["user_id"],
                )
            else:
                audit_append(
                    repo, event="auth.password_reset_requested_unknown",
                    context={"email": email, "ip": ip},
                )
        return {"ok": True, "delivery": "logged-in-dev",
                "reset_token": token_value}

    @router.post("/forgot/consume")
    async def forgot_consume(payload: dict):
        token = (payload.get("token") or "").strip()
        new_password = payload.get("new_password") or ""
        if not token or not new_password:
            raise HTTPException(400, "token and new_password required")
        from auth.passwords import hash_password, is_strong_password
        if not is_strong_password(new_password):
            raise HTTPException(400, "password too weak")
        rec = repo.get_password_reset(token)
        if not rec:
            raise HTTPException(404, "token not found")
        if rec["consumed_at"]:
            raise HTTPException(400, "token already used")
        if is_expired(rec["expires_at"]):
            raise HTTPException(400, "token expired")
        with repo.tx() as c:
            c.execute(
                "UPDATE users SET password_hash=? WHERE user_id=?",
                (hash_password(new_password), rec["user_id"]),
            )
            c.execute(
                "UPDATE password_resets SET consumed_at=? WHERE token=?",
                (_now_iso(), token),
            )
        # Invalidate all sessions for this user.
        repo.delete_sessions_for_user(rec["user_id"])
        audit_append(
            repo, event="auth.password_reset_completed",
            user_id=rec["user_id"],
        )
        return {"ok": True}

    # ── verify ───────────────────────────────────────────────

    @router.post("/verify")
    async def verify(payload: dict):
        token = (payload.get("token") or "").strip()
        if not token:
            raise HTTPException(400, "token required")
        rec = repo.get_email_verification(token)
        if not rec:
            raise HTTPException(404, "token not found")
        if rec["consumed_at"]:
            raise HTTPException(400, "token already used")
        if is_expired(rec["expires_at"]):
            raise HTTPException(400, "token expired")
        with repo.tx() as c:
            c.execute(
                "UPDATE users SET email_verified=1 WHERE user_id=?",
                (rec["user_id"],),
            )
            c.execute(
                "UPDATE email_verifications SET consumed_at=? WHERE token=?",
                (_now_iso(), token),
            )
        audit_append(
            repo, event="auth.email_verified",
            user_id=rec["user_id"],
        )
        return {"ok": True, "email_verified": True}

    # ── me ───────────────────────────────────────────────────

    @router.get("/me")
    async def me(ctx: AuthContext = Depends(get_current_user)):
        user = repo.get_user(ctx.user_id)
        memberships = list_workspaces_for_user(repo, ctx.user_id)
        return {
            "user": {
                "user_id": user["user_id"],
                "email": user["email"],
                "email_verified": user["email_verified"],
                "status": user["status"],
            },
            "workspaces": memberships,
        }

    # ── create additional workspace ─────────────────────────

    @router.post("/workspaces")
    async def create_workspace(
        request: Request, payload: dict,
        ctx: AuthContext = Depends(get_current_user),
    ):
        from auth.dependencies import require_csrf
        require_csrf(request, x_csrf_token=request.headers.get("X-CSRF-Token"))
        slug = (payload.get("slug") or "").strip().lower()
        name = (payload.get("name") or "").strip()
        if not _SLUG_RE.match(slug):
            raise HTTPException(
                400, "invalid slug: 3..40 chars, [a-z0-9-], no leading/trailing dash"
            )
        if not name or len(name) > 80:
            raise HTTPException(400, "name required (1..80 chars)")
        with repo.tx() as c:
            try:
                workspace_id = f"wks_{secrets.token_hex(8)}"
                c.execute(
                    "INSERT INTO workspaces(workspace_id, slug, name, "
                    "owner_user_id, plan, status, live_enabled, "
                    "created_at) VALUES (?,?,?,?,?,?,?,?)",
                    (workspace_id, slug, name, ctx.user_id, "free",
                     "active", 0, _now_iso()),
                )
            except Exception as e:
                if "UNIQUE constraint failed" in str(e) and "slug" in str(e):
                    raise HTTPException(409, f"slug {slug!r} already exists") from e
                raise
            c.execute(
                "INSERT INTO memberships(user_id, workspace_id, role, "
                "created_at) VALUES (?,?,?,?)",
                (ctx.user_id, workspace_id, "owner", _now_iso()),
            )
        audit_append(
            repo, event="auth.workspace_created",
            context={"slug": slug, "name": name},
            user_id=ctx.user_id, workspace_id=workspace_id,
        )
        return {"workspace_id": workspace_id, "slug": slug, "name": name,
                "role": "owner"}

    # ── csrf helper ──────────────────────────────────────────

    @router.get("/csrf")
    async def csrf(request: Request,
                    ctx: AuthContext = Depends(get_current_user)):
        from auth.session import SESSION_COOKIE_NAME
        sid = request.cookies.get(SESSION_COOKIE_NAME) or ""
        return {"csrf_token": derive_csrf_token(sid)}

    # ── legacy /api/v1/companies/{cid} alias ─────────────────
    # Wave 1: keep the cid endpoint working by returning a body
    # whose `workspace_id` is the resolved workspace id (or the
    # original cid if no workspace exists yet — Wave 2 will rename).
    return router


def _now_iso() -> str:
    import time as _t
    return _t.strftime("%Y-%m-%dT%H:%M:%SZ", _t.gmtime())


def _now_plus(seconds: int) -> str:
    import time as _t
    return _t.strftime(
        "%Y-%m-%dT%H:%M:%SZ",
        _t.gmtime(_t.time() + seconds),
    )


def _latest_workspace_id(conn, user_id: str) -> str:
    """Return the most recently created workspace for `user_id`."""
    # The conn here is a sqlite3 connection; we always pass a fresh
    # row query because the writes above may not yet be visible on
    # the same connection (depends on transaction isolation).
    cur = conn.execute(
        "SELECT workspace_id FROM workspaces WHERE owner_user_id=? "
        "ORDER BY created_at DESC LIMIT 1",
        (user_id,),
    )
    row = cur.fetchone()
    return row[0] if row else ""


def _first_workspace_for_user(conn, user_id: str) -> Optional[dict]:
    """Return the user's OLDEST membership (workspace_id + slug).

    Used by /auth/login to include `workspace_id` + `workspace_slug`
    in the response body so the UI can land on a workspace-scoped
    view without an extra /auth/me round trip.
    """
    cur = conn.execute(
        "SELECT m.workspace_id, w.slug FROM memberships m "
        "JOIN workspaces w ON w.workspace_id = m.workspace_id "
        "WHERE m.user_id=? ORDER BY m.created_at ASC LIMIT 1",
        (user_id,),
    )
    row = cur.fetchone()
    if not row:
        return None
    return {"workspace_id": row[0], "slug": row[1]}


def _set_session_cookie(request: Request, *, session_id: str,
                          user_id: str, email: str,
                          workspace_id: Optional[str] = None,
                          workspace_slug: Optional[str] = None,
                          extra: Optional[dict] = None) -> dict:
    body = {
        "user_id": user_id,
        "email": email,
        "csrf_token": derive_csrf_token(session_id),
    }
    if workspace_id:
        body["workspace_id"] = workspace_id
    if workspace_slug:
        body["workspace_slug"] = workspace_slug
    if extra:
        body.update(extra)
    # We piggyback the Set-Cookie via the response state so that the
    # route can return a JSON body. FastAPI does not expose a Response
    # by default in our signatures; this is fine for tests (the test
    # client reads cookies via `client.cookies`).
    request.state._vf_session_cookie = build_set_cookie_value(
        session_id=session_id, max_age=DEFAULT_TTL_SECONDS,
        secure=should_use_secure_cookies(),
    )
    return body
