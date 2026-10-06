"""Credential vault: per-company encrypted provider secrets.

Each row is `(company_id, provider_name, encrypted_value, ...)` and
holds an opaque blob that the render plane decrypts at submit() time.

Why:

  * Operators in a hosted deployment cannot ssh in to write env vars.
  * The web UI must be the operator-facing surface for adding/removing
    provider credentials.
  * The system must NOT log, return, or echo the plaintext value through
    any API surface; only a masked preview is exposed.

Encryption:

  * Fernet (AES-128-CBC + HMAC-SHA256). The symmetric key comes from
    `VIDEO_FACTORY_SECRET_KEY` env var (44-char base64). On dev /
    single-host installs, the operator sets the key once. On multi-host
    installs, all hosts share the key (deploy it as a secret).
  * Without the key, the DB is useless — startup refuses to construct
    a vault and any `get_secret` raises.

Audit:

  * Every write/delete surfaces to runner_audit with a stable event
    name and the actor = "operator".
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from typing import Optional


class SecretKeyMissing(RuntimeError):
    pass


class SecretReadError(RuntimeError):
    pass


@dataclass(frozen=True)
class Secret:
    """Decrypted secret. Never log this object; never return it over HTTP."""
    provider: str
    value: str
    fingerprint: str  # sha256[:16] of the value, safe to display


@dataclass(frozen=True)
class SecretMask:
    """Public surface for an HTTP/UI response."""
    provider: str
    has_value: bool
    fingerprint: str
    updated_at: str
    updated_by: str


def _key() -> bytes:
    """Read the symmetric key from env. Raise if missing."""
    raw = os.environ.get("VIDEO_FACTORY_SECRET_KEY", "").strip()
    if not raw:
        raise SecretKeyMissing(
            "VIDEO_FACTORY_SECRET_KEY is not set. Generate one with "
            "`python -c 'from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())'` and set it in the "
            "API server env. The vault refuses to operate without it."
        )
    return raw.encode("utf-8")


def _fernet():
    # Lazy import so the package is not required for offline-only
    # installs that never write secrets (mock-only mode). When the
    # operator enables the live path, cryptography is mandatory.
    from cryptography.fernet import Fernet
    return Fernet(_key())


def encrypt(provider: str, value: str) -> bytes:
    """Encrypt a value bound to a stable provider label.

    We bind the ciphertext to the provider by AAD so a ciphertext
    minted for `heygen_direct` cannot be replayed as `comfy_cloud`.
    """
    if not value:
        raise ValueError("refusing to encrypt empty value")
    token = _fernet().encrypt(value.encode("utf-8"))
    # Tag with provider name as AAD on a follow-up wrap so future
    # `decrypt` can refuse mismatched provider labels. Fernet natively
    # does not expose AAD in 3.x; we layer it manually with HMAC.
    import hmac as _hmac
    tag = _hmac.new(_key(), provider.encode("utf-8") + b"|" + token, hashlib.sha256).digest()
    return tag + token


def decrypt(provider: str, blob: bytes) -> str:
    if len(blob) <= 32:
        raise SecretReadError("malformed secret blob")
    import hmac as _hmac
    tag = blob[:32]
    token = blob[32:]
    expected = _hmac.new(_key(), provider.encode("utf-8") + b"|" + token, hashlib.sha256).digest()
    if not _hmac.compare_digest(tag, expected):
        raise SecretReadError("provider/cipher mismatch")
    try:
        return _fernet().decrypt(token).decode("utf-8")
    except Exception as e:
        raise SecretReadError(f"decrypt failed: {e}") from e


def fingerprint(value: str) -> str:
    """Short stable hash of the secret. Safe to surface in audit + UI."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def mask_value(value: str) -> str:
    """Mask all but the last 4 chars of the secret for the masked surface."""
    if not value:
        return ""
    if len(value) <= 4:
        return "*" * len(value)
    return ("*" * (len(value) - 4)) + value[-4:]