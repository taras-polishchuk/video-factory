"""Origin / Referer allow-list for the pre-session auth routes.

S1b / A1 F5 + F7. ``/auth/signup``, ``/auth/login``, ``/auth/forgot``,
``/auth/forgot/consume`` and ``/auth/verify`` run before a session
exists, so there is no session-bound CSRF token to check. We use the
OWASP "verify origin with standard headers" control instead of a
double-submit cookie:

1. ``X-VF-Internal-Token`` (see ``auth.session.derive_internal_token``)
   present and valid -> allowed. This is the SvelteKit server calling
   from its form actions; SvelteKit already ran its own Origin check
   on the browser POST (``kit.csrf.checkOrigin``). Present but wrong
   -> 403 (mis-configured secret or forgery; never silently ignored).
2. ``Origin`` present -> it must be in the allow-list or equal to the
   request's own ``Host`` (same-origin). ``Origin: null`` (sandboxed
   iframe, data: URL, some redirects) is never allowed.
3. No ``Origin`` but ``Referer`` present -> same test on the Referer's
   origin.
4. Neither header -> the caller is not a browser making a cross-site
   request (browsers always send ``Origin`` on POST). Allowed in
   dev/test so curl and the test suite keep working; REFUSED in
   strict mode, which is the default when ``VIDEO_FACTORY_ENV`` is
   production / prod / staging. ``VIDEO_FACTORY_REQUIRE_ORIGIN=1|0``
   overrides the default either way.

Allow-list: the origin of ``VIDEO_FACTORY_PUBLIC_BASE_URL`` plus every
entry of ``VIDEO_FACTORY_CORS_ORIGINS`` (comma-separated).
"""
from __future__ import annotations

import logging
import os
from typing import Optional
from urllib.parse import urlsplit

from fastapi import HTTPException, Request

from auth.session import (
    INTERNAL_TOKEN_HEADER,
    is_deployed_env,
    verify_internal_token,
)

_log = logging.getLogger("video_factory.security")


def normalize_origin(value: Optional[str]) -> Optional[str]:
    """``scheme://host[:port]`` (lower-case, default port dropped) or None."""
    if not value:
        return None
    v = value.strip()
    if not v or v.lower() == "null":
        return None
    try:
        u = urlsplit(v)
        port = u.port
    except ValueError:
        return None
    scheme = (u.scheme or "").lower()
    host = (u.hostname or "").lower()
    if scheme not in ("http", "https") or not host:
        return None
    if ":" in host:  # IPv6 literal
        host = f"[{host}]"
    default = 443 if scheme == "https" else 80
    netloc = host if port in (None, default) else f"{host}:{port}"
    return f"{scheme}://{netloc}"


def allowed_origins() -> set[str]:
    out: set[str] = set()
    base = normalize_origin(os.environ.get("VIDEO_FACTORY_PUBLIC_BASE_URL"))
    if base:
        out.add(base)
    for part in (os.environ.get("VIDEO_FACTORY_CORS_ORIGINS") or "").split(","):
        o = normalize_origin(part)
        if o:
            out.add(o)
    return out


def strict_mode() -> bool:
    raw = (os.environ.get("VIDEO_FACTORY_REQUIRE_ORIGIN") or "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return is_deployed_env()


def _same_host(origin: str, request: Request) -> bool:
    host = (request.headers.get("host") or "").strip().lower()
    if not host:
        return False
    netloc = origin.split("://", 1)[1]
    if netloc == host:
        return True
    # Host may carry the default port explicitly.
    for suffix in (":80", ":443"):
        if host.endswith(suffix) and netloc == host[: -len(suffix)]:
            return True
    return False


def _origin_ok(origin: Optional[str], request: Request) -> bool:
    if not origin:
        return False
    return origin in allowed_origins() or _same_host(origin, request)


def check_origin(request: Request) -> tuple[bool, str]:
    """Return ``(allowed, reason)`` for a pre-session state change."""
    token = request.headers.get(INTERNAL_TOKEN_HEADER)
    if token is not None:
        if verify_internal_token(token):
            return True, "internal"
        return False, "internal_token_invalid"
    raw_origin = request.headers.get("origin")
    if raw_origin is not None:
        if _origin_ok(normalize_origin(raw_origin), request):
            return True, "origin"
        return False, "origin_not_allowed"
    referer = request.headers.get("referer")
    if referer:
        if _origin_ok(normalize_origin(referer), request):
            return True, "referer"
        return False, "referer_not_allowed"
    if strict_mode():
        return False, "origin_missing"
    return True, "no_origin_header"


def require_trusted_origin(request: Request) -> None:
    """Raise 403 unless :func:`check_origin` allows the request."""
    ok, reason = check_origin(request)
    if ok:
        return
    _log.warning(
        "pre-session auth request refused",
        extra={"reason": reason, "path": request.url.path,
               "origin": (request.headers.get("origin") or "")[:200]},
    )
    raise HTTPException(403, f"cross-site request refused ({reason})")
