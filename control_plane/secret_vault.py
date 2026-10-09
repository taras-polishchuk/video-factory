"""Credential vault: per-workspace encrypted provider secrets.

Each row is `(workspace_id, provider_name, encrypted_value, ...)` and
holds an opaque blob that the render plane decrypts at submit() time.

Why:

  * Operators in a hosted deployment cannot ssh in to write env vars.
  * The web UI must be the operator-facing surface for adding/removing
    provider credentials.
  * The system must NOT log, return, or echo the plaintext value
    through any API surface; only a masked preview is exposed.

Encryption:

  * Fernet (AES-128-CBC + HMAC-SHA256). The symmetric key comes from
    `VIDEO_FACTORY_SECRET_KEY` env var (44-char base64). On dev /
    single-host installs, the operator sets the key once. On
    multi-host installs, all hosts share the key (deploy it as a
    secret).
  * Without the key, the DB is useless — startup refuses to
    construct a vault and any `get_secret` raises.
  * Wave 2: the per-blob HMAC tag is bound to the
    `(workspace_id, provider)` pair, so a ciphertext minted for
    workspace A cannot be replayed as workspace B. The provider
    binding is also preserved (so a heygen_direct blob cannot be
    read as comfy_cloud).

Audit:

  * Every write/delete surfaces to runner_audit with a stable event
    name and the actor = "operator".
"""

from __future__ import annotations

import hashlib
import hmac as _hmac
import os
from dataclasses import dataclass
from typing import Optional


class SecretKeyMissing(RuntimeError):
    pass


class SecretKeyInvalid(RuntimeError):
    """``VIDEO_FACTORY_SECRET_KEY`` is set but is not a usable Fernet key."""
    pass


class SecretReadError(RuntimeError):
    pass


class WorkspaceMismatch(SecretReadError):
    """Raised when a ciphertext is read for the wrong workspace."""
    pass


class ProviderMismatch(SecretReadError):
    """Raised when a ciphertext is read for the wrong provider."""
    pass


@dataclass(frozen=True)
class Secret:
    """Decrypted secret. Never log this object; never return it over HTTP."""
    provider: str
    value: str
    fingerprint: str  # sha256[:16] of the value, safe to display
    workspace_id: Optional[str] = None


@dataclass(frozen=True)
class SecretMask:
    """Public surface for an HTTP/UI response."""
    provider: str
    has_value: bool
    fingerprint: str
    updated_at: str
    updated_by: str
    workspace_id: Optional[str] = None


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


def validate_key() -> None:
    """Prove the vault key is usable by building the Fernet.

    Raises ``SecretKeyMissing`` when the env var is empty and
    ``SecretKeyInvalid`` when it is set but is not 32 url-safe
    base64-encoded bytes (Wave 8 QA D5: /readyz used to accept a
    too-short key and the failure only surfaced at first
    ``encrypt()``). The key value never appears in the message.
    """
    try:
        _fernet()
    except SecretKeyMissing:
        raise
    except (ValueError, TypeError) as e:  # binascii.Error is a ValueError
        raise SecretKeyInvalid(
            "VIDEO_FACTORY_SECRET_KEY is not a valid Fernet key "
            "(expected 32 url-safe base64-encoded bytes)."
        ) from e


def _bind_blob(workspace_id: Optional[str], provider: str,
                token: bytes) -> bytes:
    """Compute the per-blob HMAC tag binding (workspace, provider)."""
    wid = (workspace_id or "").encode("utf-8")
    return _hmac.new(
        _key(), wid + b"|" + provider.encode("utf-8") + b"|" + token,
        hashlib.sha256,
    ).digest()


def encrypt(workspace_id: Optional[str], provider: str, value: str) -> bytes:
    """Encrypt a value bound to (workspace, provider).

    Wave 2: the HMAC tag binds BOTH the workspace_id AND the
    provider label, so a ciphertext minted for workspace A's
    `heygen_direct` cannot be replayed as workspace B's
    `heygen_direct` (or as either workspace's `comfy_cloud`).
    """
    if value is None or value == "":
        raise ValueError("refusing to encrypt empty value")
    token = _fernet().encrypt(value.encode("utf-8"))
    tag = _bind_blob(workspace_id, provider, token)
    return tag + token


def decrypt(workspace_id: Optional[str], provider: str, blob: bytes) -> str:
    """Decrypt a value bound to (workspace, provider).

    Raises SecretReadError on HMAC mismatch, fernet failure, or
    truncated blob.
    """
    if len(blob) <= 32:
        raise SecretReadError("malformed secret blob")
    tag = blob[:32]
    token = blob[32:]
    expected = _bind_blob(workspace_id, provider, token)
    if not _hmac.compare_digest(tag, expected):
        # Distinguish provider vs workspace mismatch for diagnostics.
        # We re-derive a tag for the same provider but with
        # workspace_id=None, and check again.
        prov_only = _bind_blob(None, provider, token)
        if _hmac.compare_digest(tag, prov_only):
            raise WorkspaceMismatch(
                f"workspace binding mismatch (got wid={workspace_id!r})"
            )
        # Also try a different provider: if either provider
        # check passes, then this is a provider mismatch.
        for alt in ("comfy_cloud", "heygen_direct",
                     "comfy_gpu_worker", "publer"):
            if alt == provider:
                continue
            if _hmac.compare_digest(
                tag, _bind_blob(workspace_id, alt, token)
            ):
                raise ProviderMismatch(
                    f"provider binding mismatch (got provider={provider!r})"
                )
        # Final fallback: report workspace mismatch (most common
        # in production).
        raise WorkspaceMismatch(
            f"workspace/provider binding mismatch"
        )
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


# ── Wave 1 / pre-Wave 2 backwards compat ──────────────────────
#
# Earlier callers invoked encrypt/decrypt with the positional
# signature (provider, value) or (provider, blob).  We expose
# thin shims so legacy code paths (and tests that pre-date the
# Wave 2 rename) keep working.  The shims do NOT bind to a
# workspace — they treat workspace_id=None — which means
# existing ciphertexts written before Wave 2 are still readable.


def encrypt_legacy(provider: str, value: str) -> bytes:
    """Pre-Wave 2 signature: encrypt(provider, value).

    Equivalent to `encrypt(None, provider, value)`.  New code
    should use the explicit workspace-aware `encrypt`.
    """
    return encrypt(None, provider, value)


def decrypt_legacy(provider: str, blob: bytes) -> str:
    """Pre-Wave 2 signature: decrypt(provider, blob)."""
    return decrypt(None, provider, blob)
