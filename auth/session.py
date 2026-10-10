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
- Rotation (S1b / A1 F29): `VIDEO_FACTORY_SESSION_SECRET_PREVIOUS`
  (one value or a comma-separated list) is accepted for
  VERIFICATION ONLY. New CSRF tokens are always derived from the
  current secret. See docs/operations-prod.md "Session secret
  rotation".
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


def _previous_session_secrets() -> list[bytes]:
    """Secrets that still VERIFY (never sign) during a rotation.

    ``VIDEO_FACTORY_SESSION_SECRET_PREVIOUS`` may hold one value or a
    comma-separated list. Empty entries and an entry equal to the
    current secret are ignored.
    """
    raw = os.environ.get("VIDEO_FACTORY_SESSION_SECRET_PREVIOUS", "")
    current = os.environ.get("VIDEO_FACTORY_SESSION_SECRET", "").strip()
    out: list[bytes] = []
    for part in raw.split(","):
        p = part.strip()
        if p and p != current:
            out.append(p.encode("utf-8"))
    return out


def _verification_secrets() -> list[bytes]:
    """Current secret first, then the verify-only previous ones."""
    return [_session_secret(), *_previous_session_secrets()]


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
    """Constant-time comparison of the presented CSRF token.

    Accepts a token derived from the current secret or from any
    ``VIDEO_FACTORY_SESSION_SECRET_PREVIOUS`` entry (rotation window).
    """
    if not presented or not session_id:
        return False
    ok = False
    pres = presented.encode("utf-8", "surrogatepass")
    for secret in _verification_secrets():
        expected = hmac.new(secret, session_id.encode("utf-8"),
                            hashlib.sha256).hexdigest().encode("ascii")
        # No early exit: every candidate is compared.
        ok = hmac.compare_digest(expected, pres) or ok
    return ok


# Server-to-server marker for the SvelteKit server (S1b / A1 F5, F7).
# The web server holds VIDEO_FACTORY_SESSION_SECRET already (it derives
# CSRF tokens for logout / workspace switch); it sends this value in
# ``X-VF-Internal-Token`` on its calls to the pre-session /auth/*
# routes so the API's Origin check can tell them apart from a
# cross-site browser POST. A browser cannot compute it without the
# secret, and a cross-origin request carrying a custom header needs a
# CORS preflight the API never grants.
INTERNAL_TOKEN_HEADER = "X-VF-Internal-Token"
_INTERNAL_TOKEN_LABEL = b"vf-internal-caller:v1"


def derive_internal_token() -> str:
    """Return the internal-caller token for the CURRENT secret."""
    return hmac.new(_session_secret(), _INTERNAL_TOKEN_LABEL,
                    hashlib.sha256).hexdigest()


def verify_internal_token(presented: Optional[str]) -> bool:
    """True iff ``presented`` matches the current or a previous secret."""
    if not presented:
        return False
    try:
        secrets_ = _verification_secrets()
    except SessionSecretMissing:
        return False
    ok = False
    pres = presented.strip().encode("utf-8", "surrogatepass")
    for secret in secrets_:
        expected = hmac.new(secret, _INTERNAL_TOKEN_LABEL,
                            hashlib.sha256).hexdigest().encode("ascii")
        ok = hmac.compare_digest(expected, pres) or ok
    return ok


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


def is_deployed_env() -> bool:
    """True for ``VIDEO_FACTORY_ENV`` = production / prod / staging.

    Deployed environments get the strict defaults (Secure cookies,
    Origin required on pre-session auth routes).
    """
    env = os.environ.get("VIDEO_FACTORY_ENV", "").strip().lower()
    return env in ("production", "prod", "staging")


def should_use_secure_cookies(request=None) -> bool:
    """Resolve whether to set Secure on the session cookie.

    A2 F-05: Secure by default whenever

    - ``VF_HTTPS=1``, or
    - ``VIDEO_FACTORY_ENV`` is production / prod / staging, or
    - the request itself arrived over HTTPS (``request.url.scheme``,
      which uvicorn ``--proxy-headers`` derives from a trusted
      ``X-Forwarded-Proto``).

    Only plain-HTTP local dev / tests get a non-Secure cookie.
    """
    if os.environ.get("VF_HTTPS", "").strip() == "1":
        return True
    if is_deployed_env():
        return True
    if request is not None:
        try:
            if request.url.scheme == "https":
                return True
            from auth.client_ip import forwarded_https
            if forwarded_https(request):
                return True
        except Exception:  # noqa: BLE001 - malformed scope: fall through
            pass
    return False


def read_session_cookie(request) -> Optional[str]:
    """Return the raw session id from the vf_session cookie, or None.

    Used by F8 (session fixation: destroy any presented session before
    issuing a new one on signup/login).
    """
    return request.cookies.get(SESSION_COOKIE_NAME)
