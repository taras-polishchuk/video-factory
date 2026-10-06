"""SecretProvider: resolve per-company secrets at submit() time.

Order of resolution:
  1. Repo-vault (encrypted, per-company). Decrypted only at the moment
     of submit() and only inside the adapter. Never logged.
  2. Environment variable (legacy single-tenant path). Kept for backward
     compatibility — operators running the offline single-tenant demo
     do not need a vault.
  3. None — adapter raises ProviderError / ProviderDisabled as
     appropriate.

The provider is constructed once per submit() call. Adapters accept an
optional `secret_provider` argument so tests can pass mocks.

Why a separate module:

  * Adapters stay decoupled from Repo (no SQLite handle in render plane).
  * The lookup logic is shared across HeyGen / Comfy Cloud / Comfy GPU.
  * Tests can mock the provider in one place.
"""

from __future__ import annotations

import os
from typing import Optional, Protocol


class SecretProvider(Protocol):
    """Resolve a secret for (company_id, provider, env_var_name).

    Returns the plaintext string, or None if not configured.
    """

    def resolve(self, *, company_id: Optional[str], provider: str,
                env_var: str) -> Optional[str]: ...


class EnvOnlySecretProvider:
    """Default for offline single-tenant: only env vars, no vault."""

    def resolve(self, *, company_id: Optional[str], provider: str,
                env_var: str) -> Optional[str]:
        return os.environ.get(env_var)


class VaultSecretProvider:
    """Repo-backed vault with env fallback.

    Constructed with a `repo` (any object exposing
    `decrypt_credential(company_id=..., provider=...)` and `NotFound`).
    """

    def __init__(self, repo) -> None:
        self.repo = repo

    def resolve(self, *, company_id: Optional[str], provider: str,
                env_var: str) -> Optional[str]:
        # 1. Try vault first (per-company)
        if company_id is not None:
            try:
                value = self.repo.decrypt_credential(
                    company_id=company_id, provider=provider,
                )
                if value:
                    return value
            except Exception:
                # NotFound, SecretReadError, SecretKeyMissing — fall through to env.
                pass
        # 2. Env fallback for backward compat
        return os.environ.get(env_var)


def provider_name_to_env(provider: str) -> str:
    """Map provider name → env var name used in single-tenant mode."""
    return {
        "heygen_direct": "HEYGEN_API_KEY",
        "comfy_cloud": "COMFY_CLOUD_API_KEY",
        "comfy_gpu_worker": "COMFY_GPU_API_BASE",  # base URL, not key
        "comfy_heygen_partner": "COMFY_GPU_API_BASE",  # uses Comfy proxy
        "publer": "PUBLER_API_KEY",
    }.get(provider, "")