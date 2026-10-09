"""Server-side session store + signed cookie helpers.

Sessions live in the `sessions` SQLite table; the cookie carries
only the opaque session_id. The CSRF token is HMAC-derived from
the session id + secret, so a CSRF token bound to a forged cookie
cannot be computed without the secret.

Why server-side (not JWT):

- The set of valid sessions is observable, so we can implement
  "log out everywhere" and "rotate the secret invalidates all
  sessions" cheaply.
- 7-day rolling expiry means we re-stamp `last_seen_at` on every
  request; old rows are GC'd on a timer.

Cookie attributes:

- Name: `vf_session`
- HttpOnly
- SameSite=Strict (always; the API does not support cross-site
  form posts)
- Secure in production (`vf_https` env var or `secure_cookies`
  flag passed to `issue_cookie()`)

Secret:

- Read from `VIDEO_FACTORY_SESSION_SECRET`. If absent the auth
  endpoints return 503; the rest of the API works as before
  (no auth) so that dev / single-tenant installs still function.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
from typing import Optional


SESSION_COOKIE_NAME = "vf_session"
DEFAULT_TTL_SECONDS = 7 * 24 * 60 * 60  # 7 days


class SessionSecretMissing(RuntimeError):
    pass


def _session_secret() -> bytes:
    raw = os.environ.get("VIDEO_FACTORY_SESSION_SECRET", "").strip()
    if not raw:
        raise SessionSecretMissing(
            "VIDEO_FACTORY_SESSION_SECRET is not set. "
            "Generate a 32-byte secret with `python -c "
            "'import secrets; print(secrets.token_hex(32))'` and "
            "set it in the API server env. Auth endpoints will return "
            "503 until it is set."
        )
    return raw.encode("utf-8")


def new_session_id() -> str:
    """Return a fresh opaque session id (URL-safe, 32 bytes)."""
    return secrets.token_urlsafe(32)


def derive_csrf_token(session_id: str) -> str:
    """Return the CSRF token bound to `session_id`.

    Uses HMAC-SHA256 over the session id with the session secret.
    The token is hex (64 chars). Anyone who can read the session
    id can read the token — they are equivalent in privilege.
    """
    secret = _session_secret()
    mac = hmac.new(secret, session_id.encode("utf-8"), hashlib.sha256)
    return mac.hexdigest()


def verify_csrf_token(session_id: str, presented: str) -> bool:
    """Constant-time comparison of the presented CSRF token."""
    if not presented or not session_id:
        return False
    expected = derive_csrf_token(session_id)
    return hmac.compare_digest(expected, presented)


def expires_at_iso(ttl_seconds: int = DEFAULT_TTL_SECONDS) -> str:
    return time.strftime(
        "%Y-%m-%dT%H:%M:%SZ",
        time.gmtime(time.time() + ttl_seconds),
    )


def is_expired(expires_at_iso: str) -> bool:
    """True if the ISO timestamp is in the past.

    We parse manually to avoid the timezone library surface; the
    DB always writes Z-suffixed UTC.
    """
    if not expires_at_iso:
        return True
    try:
        epoch = time.mktime(time.strptime(expires_at_iso, "%Y-%m-%dT%H:%M:%SZ"))
    except (ValueError, TypeError):
        return True
    return epoch < time.time()


def session_cookie_attrs(*, secure: bool) -> dict:
    """Return the cookie attribute dict for Set-Cookie."""
    return {
        "httponly": True,
        "secure": bool(secure),
        "samesite": "strict",
        "path": "/",
    }


def build_set_cookie_value(
    *, session_id: str, max_age: int = DEFAULT_TTL_SECONDS, secure: bool = False
) -> str:
    """Build the value attribute for Set-Cookie.

    Returns only the value (no attributes) so the caller can wire
    attributes via the response object. Format: name=value; ...attrs
    """
    parts = [f"{SESSION_COOKIE_NAME}={session_id}"]
    parts.append("HttpOnly")
    parts.append("SameSite=Strict")
    parts.append(f"Max-Age={int(max_age)}")
    parts.append("Path=/")
    if secure:
        parts.append("Secure")
    return "; ".join(parts)


def build_clear_cookie_value(*, secure: bool = False) -> str:
    parts = [f"{SESSION_COOKIE_NAME}="]
    parts.append("HttpOnly")
    parts.append("SameSite=Strict")
    parts.append("Max-Age=0")
    parts.append("Path=/")
    if secure:
        parts.append("Secure")
    return "; ".join(parts)


def is_production() -> bool:
    """True iff ``VIDEO_FACTORY_ENV=production`` (case-insensitive).

    Single source of truth for the production flag; used by the
    cookie policy below and by ``/readyz`` (dev HMAC key refusal).
    """
    return os.environ.get("VIDEO_FACTORY_ENV", "").strip().lower() == "production"


def should_use_secure_cookies() -> bool:
    """Resolve whether to set Secure on the session cookie.

    True if `VF_HTTPS=1` (production signal) or
    `VIDEO_FACTORY_ENV=production`. Otherwise False.
    """
    if os.environ.get("VF_HTTPS", "").strip() == "1":
        return True
    if is_production():
        return True
    return False
