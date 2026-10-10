"""HTTP hardening for the API process itself (S1b / A1 F14, F19).

Caddy already adds hardening headers on the public vhost, but the API
must not depend on it: ops tooling, health probes, a mis-routed proxy
or a direct hit on :8000 reach uvicorn without Caddy in between.

- ``SecurityHeadersMiddleware``: pure ASGI middleware that adds
  ``X-Content-Type-Options: nosniff``, ``X-Frame-Options: DENY``,
  ``Content-Security-Policy: frame-ancestors 'none'`` and
  ``Referrer-Policy: no-referrer`` to every response that does not
  already carry them. The CSP only restricts framing so the bundled
  ``/docs`` page keeps working.
- ``cors_origins_from_env``: CORS stays OFF unless
  ``VIDEO_FACTORY_CORS_ORIGINS`` lists explicit origins. ``*`` and
  anything that is not an http(s) origin are dropped, so a typo can
  never open the API to every site.
"""
from __future__ import annotations

import logging
import os
from typing import Iterable, Optional

SECURITY_HEADERS: tuple[tuple[bytes, bytes], ...] = (
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"content-security-policy", b"frame-ancestors 'none'"),
    (b"referrer-policy", b"no-referrer"),
)

# Same set as a str dict, for responses built outside the middleware
# stack (Starlette renders the catch-all 500 in ServerErrorMiddleware,
# which sits outside every user middleware).
SECURITY_HEADERS_STR: dict[str, str] = {
    k.decode("ascii"): v.decode("ascii") for k, v in SECURITY_HEADERS
}

CORS_ALLOW_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE"]
CORS_ALLOW_HEADERS = ["Content-Type", "X-CSRF-Token", "X-Workspace-Id",
                      "X-Request-ID"]


class SecurityHeadersMiddleware:
    """Add the hardening headers to every HTTP response."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        async def _send(message):
            if message.get("type") == "http.response.start":
                headers = list(message.get("headers") or [])
                present = {k.lower() for k, _ in headers}
                for name, value in SECURITY_HEADERS:
                    if name not in present:
                        headers.append((name, value))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, _send)


def cors_origins_from_env(explicit: Optional[Iterable[str]] = None) -> list[str]:
    """Return the CORS allow-list (may be empty = no CORS middleware)."""
    from auth.origin import normalize_origin

    raw: list[str] = list(explicit) if explicit else [
        p for p in (os.environ.get("VIDEO_FACTORY_CORS_ORIGINS") or "").split(",")
    ]
    out: list[str] = []
    for item in raw:
        item = (item or "").strip()
        if not item:
            continue
        norm = normalize_origin(item)
        if item == "*" or norm is None:
            logging.getLogger("video_factory.startup").warning(
                "ignoring CORS origin %r: only explicit http(s) origins are allowed",
                item[:100],
            )
            continue
        if norm not in out:
            out.append(norm)
    return out
