"""Client IP resolution behind trusted reverse proxies (S1b / A1 F26).

Rate limits key on the client IP. Behind Caddy and the SvelteKit
server every request reaches the API from a proxy address, so the
socket peer alone would put every user into one bucket. The fix is
the usual "rightmost untrusted hop" rule:

- ``X-Forwarded-For`` is honoured ONLY when the socket peer is a
  trusted proxy (``VIDEO_FACTORY_TRUSTED_PROXIES``).
- The header is walked right to left; trusted proxy hops are
  skipped; the first untrusted address is the client. A client can
  prepend anything it likes to the header, but it cannot get past
  the hop our own proxy appended, so spoofed left-hand entries never
  win.
- If every hop is trusted (e.g. a call from the host itself), the
  leftmost address is used.

``VIDEO_FACTORY_TRUSTED_PROXIES`` is a comma-separated list of IPs or
CIDRs. Default: loopback + the RFC 1918 172.16.0.0/12 block that
Docker bridge and compose networks are allocated from. Set it to an
empty string to trust no proxy at all (socket peer only).
"""
from __future__ import annotations

import ipaddress
import os
from functools import lru_cache
from typing import Optional

DEFAULT_TRUSTED_PROXIES = "127.0.0.0/8,::1/128,172.16.0.0/12"


@lru_cache(maxsize=16)
def _parse_networks(raw: str) -> tuple:
    nets = []
    for part in raw.split(","):
        p = part.strip()
        if not p:
            continue
        try:
            nets.append(ipaddress.ip_network(p, strict=False))
        except ValueError:
            # A typo must not silently trust the world; skip it.
            continue
    return tuple(nets)


def trusted_networks() -> tuple:
    raw = os.environ.get("VIDEO_FACTORY_TRUSTED_PROXIES")
    if raw is None:
        raw = DEFAULT_TRUSTED_PROXIES
    return _parse_networks(raw)


def _as_ip(value: str):
    v = (value or "").strip().strip('"')
    if v.startswith("[") and "]" in v:          # "[::1]:443"
        v = v[1:v.index("]")]
    elif v.count(":") == 1 and "." in v:        # "1.2.3.4:5678"
        v = v.split(":", 1)[0]
    try:
        return ipaddress.ip_address(v)
    except ValueError:
        return None


def is_trusted_proxy(value: Optional[str]) -> bool:
    ip = _as_ip(value or "")
    if ip is None:
        return False
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    return any(ip in net for net in trusted_networks())


def _peer(request) -> str:
    client = getattr(request, "client", None)
    return (client.host if client else "") or ""


def client_ip(request) -> str:
    """Return the best-effort real client address for ``request``."""
    peer = _peer(request)
    if not is_trusted_proxy(peer):
        return peer or "unknown"
    raw = ",".join(request.headers.getlist("x-forwarded-for")) \
        if hasattr(request.headers, "getlist") \
        else (request.headers.get("x-forwarded-for") or "")
    hops = [h.strip() for h in raw.split(",") if h.strip()]
    if not hops:
        return peer or "unknown"
    for hop in reversed(hops):
        ip = _as_ip(hop)
        if ip is None:
            # Garbage in the chain: stop trusting it at this point.
            return str(_as_ip(peer) or peer or "unknown")
        if not is_trusted_proxy(str(ip)):
            return str(ip)
    first = _as_ip(hops[0])
    return str(first) if first is not None else (peer or "unknown")


def forwarded_https(request) -> bool:
    """True iff a TRUSTED proxy says the original request was HTTPS."""
    if not is_trusted_proxy(_peer(request)):
        return False
    proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0]
    return proto.strip().lower() == "https"
