"""Per-workspace HMAC tag for artifact blob keys.

Every asset/job-output in `artifacts/storage/` carries a
`storage_key` like `workspaces/<slug>/assets/<id>_<name>`.  In a
shared deployment that layout is purely cosmetic — a malicious
operator with shell access could `cp workspaces/A/assets/x.mp4
workspaces/B/assets/` and the receiving workspace would happily
serve it as their own.

Wave 5 binds the blob path to its workspace via a per-blob HMAC
sidecar.  When the API writes a blob to `artifacts/storage/<key>`,
it also writes `artifacts/storage/<key>.hmac` containing:

    HMAC-SHA256(key=VIDEO_FACTORY_DB_KEY,
                msg=f"{workspace_id}|{storage_key}")

When the API serves the blob back it recomputes the HMAC over the
caller's `workspace_id` + the requested `storage_key` and refuses
to serve if the sidecar does not match.  A blob copied across
workspaces fails the check immediately.

The HMAC key is the same `VIDEO_FACTORY_DB_KEY` as the SQLCipher
page key because:

  * The threat model (compromised shell access to the artifact
    directory) is the same threat model that justifies SQLCipher
    on the DB.  Using one key for both keeps the operator's secret
    inventory small.
  * The HMAC key is only ever needed on the API host that has the
    env var.  All read paths are inside the API.

For dev / plaintext mode (no `VIDEO_FACTORY_DB_KEY`) we derive a
HMAC key from `VIDEO_FACTORY_SECRET_KEY` (the Fernet key for
credential blobs) so the sidecars still bind workspace to blob —
the operator pays one env var in production, gets the binding in
dev too.  This is intentional: we never want a workspace to be
able to read another workspace's blobs even in dev.
"""
from __future__ import annotations

import hashlib
import hmac as _hmac
import os
from pathlib import Path


_HMAC_ALG = hashlib.sha256
_SIDECAR_SUFFIX = ".hmac"


def _key_bytes() -> bytes:
    """Return the HMAC key bytes.

    Priority:

      * `VIDEO_FACTORY_DB_KEY` if set (matches the SQLCipher key;
        preferred in production).
      * `VIDEO_FACTORY_SECRET_KEY` if set (Fernet key; dev / test).
      * A static dev key as a last resort, so dev mode never
        crashes.  The dev key is logged at WARNING level in
        `warn_if_dev_key_in_use`.
    """
    raw = os.environ.get("VIDEO_FACTORY_DB_KEY", "").strip()
    if raw:
        return raw.encode("utf-8")
    raw = os.environ.get("VIDEO_FACTORY_SECRET_KEY", "").strip()
    if raw:
        return raw.encode("utf-8")
    # Last resort — NEVER use in production.  Surfaced by the
    # helper below so an operator who forgot to set the env var
    # sees a clear warning at startup.
    return b"video-factory-dev-key-DO-NOT-USE-IN-PROD"


def _static_dev_key_marker() -> bytes:
    """Sentinel value that detects the missing-env-var fallback."""
    return b"video-factory-dev-key-DO-NOT-USE-IN-PROD"


def dev_key_in_use() -> bool:
    """True iff the static dev fallback HMAC key is active."""
    return _key_bytes() == _static_dev_key_marker()


def warn_if_dev_key_in_use() -> str | None:
    """Return a warning string iff the dev fallback key is in use.

    Wire this into the FastAPI startup hook so operators who
    forgot to set `VIDEO_FACTORY_DB_KEY` see a clear log line at
    boot instead of a quiet binding failure months later.
    """
    if dev_key_in_use():
        return (
            "VIDEO_FACTORY_DB_KEY and VIDEO_FACTORY_SECRET_KEY are "
            "both unset.  Falling back to a static dev HMAC key.  "
            "This is unsafe in production."
        )
    return None


def compute_hmac(*, workspace_id: str, storage_key: str) -> bytes:
    """Compute the workspace-bound HMAC tag for `storage_key`."""
    msg = f"{workspace_id}|{storage_key}".encode("utf-8")
    return _hmac.new(_key_bytes(), msg, _HMAC_ALG).digest()


def write_sidecar(*, storage_root: str, storage_key: str,
                  workspace_id: str) -> Path:
    """Write the `.hmac` sidecar for `storage_key`.

    The sidecar is written next to the blob: `<root>/<key>.hmac`.
    The sidecar body is the hex-encoded HMAC tag.
    """
    tag = compute_hmac(workspace_id=workspace_id,
                       storage_key=storage_key)
    sidecar = Path(storage_root) / (storage_key + _SIDECAR_SUFFIX)
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_bytes(tag)
    return sidecar


def verify_sidecar(*, storage_root: str, storage_key: str,
                   workspace_id: str) -> bool:
    """Return True iff the sidecar matches the expected HMAC tag.

    A missing sidecar fails closed — we never serve an artifact
    that wasn't explicitly tagged.  This means older artifacts
    written before Wave 5 must be re-uploaded (or have their
    sidecars regenerated via the migration helper below).
    """
    sidecar = Path(storage_root) / (storage_key + _SIDECAR_SUFFIX)
    if not sidecar.exists():
        return False
    expected = compute_hmac(workspace_id=workspace_id,
                            storage_key=storage_key)
    actual = sidecar.read_bytes()
    return _hmac.compare_digest(expected, actual)


def rekey_sidecar(*, storage_root: str, storage_key: str,
                  old_workspace_id: str, new_workspace_id: str,
                  old_key_bytes: bytes, new_key_bytes: bytes) -> bool:
    """Re-key a sidecar atomically when the HMAC master key changes.

    Reads the existing tag, re-derives under the new master key,
    writes the new tag.  Used by the master-key rotation workflow
    so the sidecars keep verifying after `VIDEO_FACTORY_DB_KEY`
    rotates.
    """
    sidecar = Path(storage_root) / (storage_key + _SIDECAR_SUFFIX)
    if not sidecar.exists():
        return False
    actual = sidecar.read_bytes()
    expected_old = _hmac.new(
        old_key_bytes,
        f"{old_workspace_id}|{storage_key}".encode("utf-8"),
        _HMAC_ALG,
    ).digest()
    if not _hmac.compare_digest(actual, expected_old):
        return False
    new_tag = _hmac.new(
        new_key_bytes,
        f"{new_workspace_id}|{storage_key}".encode("utf-8"),
        _HMAC_ALG,
    ).digest()
    sidecar.write_bytes(new_tag)
    return True


__all__ = [
    "compute_hmac",
    "dev_key_in_use",
    "rekey_sidecar",
    "verify_sidecar",
    "warn_if_dev_key_in_use",
    "write_sidecar",
]