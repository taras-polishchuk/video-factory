"""Idempotency keys for provider submits.

Keys are content-derived so retries never double-charge. The store
maps idem_key -> ProviderSubmitReceipt. When a duplicate submit arrives
the store returns the prior receipt without invoking the provider.
"""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass
from typing import Any, Dict, Optional


def make_idem_key(*parts: str) -> str:
    """Stable short key derived from arbitrary string parts."""
    joined = "|".join(str(p) for p in parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class ProviderSubmitReceipt:
    provider_job_id: str
    accepted_metadata: Dict[str, Any]


class IdempotencyStore:
    """In-memory idempotency store. Replace with persistent store for production."""

    def __init__(self) -> None:
        self._by_key: Dict[str, ProviderSubmitReceipt] = {}
        self._lock = threading.Lock()

    def lookup(self, key: str) -> Optional[ProviderSubmitReceipt]:
        with self._lock:
            return self._by_key.get(key)

    def save(self, key: str, receipt: ProviderSubmitReceipt) -> None:
        with self._lock:
            # If something already exists under this key, do not overwrite.
            # Caller MUST handle collision via lookup() first.
            if key in self._by_key:
                return
            self._by_key[key] = receipt

    def submit_once(self, key: str, receipt: ProviderSubmitReceipt) -> ProviderSubmitReceipt:
        """Submit-or-replay. Returns existing receipt if key is known; otherwise stores and returns new."""
        with self._lock:
            existing = self._by_key.get(key)
            if existing is not None:
                return existing
            self._by_key[key] = receipt
            return receipt


def canonicalize_for_hash(obj: Any) -> Any:
    """Stable JSON-friendly form. Used for content_hash."""
    if isinstance(obj, dict):
        return {k: canonicalize_for_hash(obj[k]) for k in sorted(obj.keys())}
    if isinstance(obj, list):
        return [canonicalize_for_hash(x) for x in obj]
    return obj


def sha256_hex(obj: Any) -> str:
    canon = canonicalize_for_hash(obj)
    payload = json.dumps(canon, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()