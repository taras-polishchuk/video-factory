"""Password hashing for Video Factory.

Prefers argon2-cffi; falls back to bcrypt (cost >= 12). The chosen
algorithm is encoded in the resulting hash string so verify() can
detect it without an out-of-band flag. The same plaintext hash
twice produces two different ciphertexts (salt) — that is the
intended behavior.

Password policy enforced at the API layer (see api/auth.py):

- minimum length 12
- at least one lower, one upper, one digit
- (optional) ASCII-only to keep the unicode surface honest

Hash format:

    algo:params:hash

where `algo` ∈ {"argon2", "bcrypt"}, `params` are cost/time
descriptors, `hash` is the base64 or hex digest returned by the
underlying library.
"""
from __future__ import annotations

import os
from typing import Optional


_ARGON2_PREFIX = "argon2"
_BCRYPT_PREFIX = "bcrypt"

# Tuned for offline test runs to be < 500ms; production uses higher
# parameters via env override.
_ARGON2_TIME_COST = int(os.environ.get("VF_ARGON2_TIME_COST", "2"))
_ARGON2_MEMORY_COST = int(os.environ.get("VF_ARGON2_MEMORY_COST", "19456"))  # KiB
_ARGON2_PARALLELISM = int(os.environ.get("VF_ARGON2_PARALLELISM", "1"))


class PasswordHashingError(RuntimeError):
    pass


def _argon2_hash(plaintext: str) -> str:
    try:
        from argon2 import PasswordHasher
    except Exception as e:  # pragma: no cover - environment issue
        raise PasswordHashingError(f"argon2-cffi unavailable: {e}") from e
    ph = PasswordHasher(
        time_cost=_ARGON2_TIME_COST,
        memory_cost=_ARGON2_MEMORY_COST,
        parallelism=_ARGON2_PARALLELISM,
    )
    return f"{_ARGON2_PREFIX}:{ph.hash(plaintext)}"


def _argon2_verify(plaintext: str, encoded: str) -> bool:
    try:
        from argon2 import PasswordHasher
    except Exception:
        return False
    ph = PasswordHasher()
    # Strip the "argon2:" prefix; the underlying hash is everything after.
    body = encoded.split(":", 1)[1] if encoded.startswith(f"{_ARGON2_PREFIX}:") else encoded
    try:
        return ph.verify(body, plaintext)
    except Exception:
        return False


def _bcrypt_hash(plaintext: str) -> str:
    import bcrypt  # type: ignore[import-not-found]
    cost = 12
    salt = bcrypt.gensalt(rounds=cost)
    digest = bcrypt.hashpw(plaintext.encode("utf-8"), salt)
    return f"{_BCRYPT_PREFIX}:cost={cost}:{digest.decode('ascii')}"


def _bcrypt_verify(plaintext: str, encoded: str) -> bool:
    try:
        import bcrypt  # type: ignore[import-not-found]
    except Exception:
        return False
    try:
        # Strip the "bcrypt:cost=N:" prefix
        parts = encoded.split(":", 2)
        if len(parts) != 3 or parts[0] != _BCRYPT_PREFIX:
            return False
        return bcrypt.checkpw(plaintext.encode("utf-8"), parts[2].encode("ascii"))
    except Exception:
        return False


def is_strong_password(plaintext: str) -> bool:
    """Accept a plaintext password iff it satisfies the policy."""
    if not isinstance(plaintext, str):
        return False
    if len(plaintext) < 12:
        return False
    if len(plaintext) > 256:
        return False
    has_lower = any(c.islower() for c in plaintext)
    has_upper = any(c.isupper() for c in plaintext)
    has_digit = any(c.isdigit() for c in plaintext)
    return has_lower and has_upper and has_digit


def hash_password(plaintext: str) -> str:
    """Hash a plaintext password using the preferred algorithm.

    Returns an encoded string. The result is safe to store in a TEXT
    column; never log the plaintext.
    """
    if not plaintext:
        raise ValueError("plaintext required")
    try:
        return _argon2_hash(plaintext)
    except PasswordHashingError:
        return _bcrypt_hash(plaintext)


def verify_password(plaintext: str, encoded: Optional[str]) -> bool:
    """Constant-time-ish comparison.

    Returns False on missing/wrong hash. Never raises for an
    authentication failure — only programmer errors propagate.
    """
    if not plaintext or not encoded:
        return False
    if encoded.startswith(f"{_ARGON2_PREFIX}:"):
        return _argon2_verify(plaintext, encoded)
    if encoded.startswith(f"{_BCRYPT_PREFIX}:"):
        return _bcrypt_verify(plaintext, encoded)
    # Legacy or malformed — refuse rather than guess.
    return False


def algorithm_of(encoded: Optional[str]) -> str:
    """Return the algorithm name embedded in the encoded hash.

    Returns "unknown" if the format is unrecognized (caller decides
    whether to reject or migrate).
    """
    if not encoded:
        return "unknown"
    head = encoded.split(":", 1)[0]
    if head in (_ARGON2_PREFIX, _BCRYPT_PREFIX):
        return head
    return "unknown"
