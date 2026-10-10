"""POST /api/v1/admin/keys/rotate request handling (S1b / A1 F11, F15).

The endpoint re-keys the whole SQLCipher DB, so it gets every guard:

1. CSRF (``X-CSRF-Token`` bound to a LIVE session, see
   ``auth.dependencies.require_csrf``).
2. Role: owner or admin of at least one workspace.
3. Re-authentication: ``current_password`` in the body must match the
   caller's password (OIDC-only accounts cannot rotate). Attempts are
   budgeted per user (``keys_reauth`` bucket, auth/ratelimit.py) so a
   stolen session cannot be used to brute-force the password here.
4. Key hygiene: the body is parsed by hand so a malformed body is
   never echoed back (FastAPI's 422 echoes ``input``); ``old_key`` /
   ``new_key`` are only ever logged, audited or returned as SHA-256
   fingerprints; every error string is passed through
   ``key_rotation.redact_keys`` first. Sentry never sees request bodies
   (``max_request_body_size="never"`` + ``scrub_event``).
"""
from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Optional

from fastapi import HTTPException, Request

if TYPE_CHECKING:
    from auth.dependencies import AuthContext

_log = logging.getLogger("video_factory.security")

_ADMIN_ROLES = ("owner", "admin")
_PLATFORM_ADMINS_ENV = "VIDEO_FACTORY_PLATFORM_ADMINS"


def _has_admin_role(repo, user_id: str) -> bool:
    rows = repo._conn().execute(  # noqa: SLF001
        "SELECT role FROM memberships WHERE user_id=?", (user_id,),
    ).fetchall()
    return any(r[0] in _ADMIN_ROLES for r in rows)


def platform_admins() -> set[str]:
    """Return the allow-list of platform-admin user ids.

    Reads ``VIDEO_FACTORY_PLATFORM_ADMINS`` (comma-separated). Unset
    or whitespace-only entries mean the allow-list is empty —
    everyone is denied (fail-closed).
    """
    raw = os.environ.get(_PLATFORM_ADMINS_ENV, "")
    return {tok.strip() for tok in raw.split(",") if tok.strip()}


def is_platform_admin(user_id: Optional[str]) -> bool:
    """True iff ``user_id`` is in the platform-admin allow-list."""
    if not user_id:
        return False
    return user_id in platform_admins()


def require_platform_admin(request: Request) -> "AuthContext":  # noqa: F821
    """Depends() that gates a request on the platform-admin allow-list.

    Behaviour:
    - No cookie / invalid session / expired / missing user → 403
      ("platform admin required"). Authenticated non-admins also 403.
    - User present but not in ``VIDEO_FACTORY_PLATFORM_ADMINS`` → 403.
    - User in the allow-list → returns AuthContext.

    Reuses the same session/user primitives as get_current_user; the
    failure mode is intentionally a flat 403 because the admin gate
    runs BEFORE the workspace/role checks.
    """
    from auth.dependencies import AuthContext, _load_session, _load_user, _read_cookie

    repo = request.app.state.repo
    session_id = _read_cookie(request)
    if not session_id:
        raise HTTPException(403, "platform admin required")
    sess = _load_session(repo, session_id)
    if not sess:
        raise HTTPException(403, "platform admin required")
    user = _load_user(repo, sess["user_id"])
    if not user:
        raise HTTPException(403, "platform admin required")
    if user.get("status") == "locked":
        # Locked accounts are still gated out at the admin layer.
        raise HTTPException(403, "platform admin required")
    if not is_platform_admin(user["user_id"]):
        raise HTTPException(403, "platform admin required")
    return AuthContext(user_id=user["user_id"], email=user["email"])


async def handle_rotate(request: Request, ctx, repo) -> dict:
    from auth.audit_log import append as audit_append
    from auth.dependencies import require_csrf
    from auth.passwords import verify_password
    from auth.ratelimit import hit as rl_hit
    from control_plane import key_rotation

    # 1. Admin gate (platform-admin allow-list). Runs before CSRF
    #    because admin boundary > auth semantics: anyone without admin
    #    status is rejected with 403, no matter what they send.
    if not is_platform_admin(ctx.user_id):
        raise HTTPException(403, "platform admin required")
    # 2. CSRF + live session.
    require_csrf(request, x_csrf_token=request.headers.get("X-CSRF-Token"))
    # 3. Role.
    if not _has_admin_role(repo, ctx.user_id):
        raise HTTPException(403, "owner or admin role required")
    # Body: JSON object only, never echoed.
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        payload = None
    if not isinstance(payload, dict):
        raise HTTPException(400, "JSON object body required")
    # 3. Re-auth, budgeted per user.
    budget = rl_hit(repo, bucket="keys_reauth", key=ctx.user_id)
    if not budget.allowed:
        raise HTTPException(
            429, "too many re-authentication attempts; try again later",
            headers={"Retry-After": str(budget.window_seconds)},
        )
    current_password = payload.get("current_password")
    user = repo.get_user(ctx.user_id)
    if not isinstance(current_password, str) or not verify_password(
        current_password, user.get("password_hash"),
    ):
        audit_append(
            repo, event="admin.keys_rotate_reauth_failed",
            context={"reason": "current_password_mismatch"},
            user_id=ctx.user_id, actor=ctx.email,
        )
        raise HTTPException(403, "re-authentication failed: current_password required")

    old_key = payload.get("old_key")
    new_key = payload.get("new_key")
    if not isinstance(old_key, str) or not isinstance(new_key, str) \
            or not old_key or not new_key:
        raise HTTPException(400, "old_key and new_key are required")

    fp_old = key_rotation._fingerprint(old_key)  # noqa: SLF001
    fp_new = key_rotation._fingerprint(new_key)  # noqa: SLF001
    _log.info(
        "admin key rotation requested",
        extra={"old_key_fingerprint": fp_old, "new_key_fingerprint": fp_new},
    )

    def _clean(exc: BaseException) -> str:
        return key_rotation.redact_keys(str(exc), old_key, new_key)

    try:
        return key_rotation.rotate(
            repo=repo,
            old_key=old_key,
            new_key=new_key,
            actor_user_id=ctx.user_id,
        )
    except key_rotation.RotationInProgress as e:
        raise HTTPException(503, _clean(e))
    except key_rotation.KeyFingerprintMissing as e:
        raise HTTPException(400, _clean(e))
    except key_rotation.RotationFailed as e:
        _log.error(
            "admin key rotation failed",
            extra={"old_key_fingerprint": fp_old, "new_key_fingerprint": fp_new,
                   "error_class": type(e.__cause__ or e).__name__},
        )
        raise HTTPException(500, _clean(e))
    except Exception as e:  # noqa: BLE001 - never let a raw message escape
        _log.error(
            "admin key rotation crashed",
            extra={"old_key_fingerprint": fp_old, "new_key_fingerprint": fp_new,
                   "error_class": type(e).__name__},
        )
        raise HTTPException(500, "key rotation failed; see server logs "
                                 f"(old={fp_old}, new={fp_new})")
