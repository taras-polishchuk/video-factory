"""Idempotency keys for provider submits.

Keys are content-derived so retries never double-charge. The store
maps idem_key -> ProviderSubmitReceipt. When a duplicate submit arrives
the store returns the prior receipt without invoking the provider.

Two implementations: in-memory (default) and SQLite-backed for durable
restart safety.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
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
    """In-memory idempotency store."""

    def __init__(self) -> None:
        self._by_key: Dict[str, ProviderSubmitReceipt] = {}
        self._lock = threading.Lock()

    def lookup(self, key: str) -> Optional[ProviderSubmitReceipt]:
        with self._lock:
            return self._by_key.get(key)

    def save(self, key: str, receipt: ProviderSubmitReceipt) -> None:
        with self._lock:
            if key in self._by_key:
                return
            self._by_key[key] = receipt

    def submit_once(self, key: str, receipt: ProviderSubmitReceipt) -> ProviderSubmitReceipt:
        with self._lock:
            existing = self._by_key.get(key)
            if existing is not None:
                return existing
            self._by_key[key] = receipt
            return receipt


class DurableIdempotencyStore:
    """SQLite-backed idempotency. Survives process restart.

    Table schema is created on demand; caller owns the connection path.
    """

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS idem_receipts (
        idem_key TEXT PRIMARY KEY,
        provider_job_id TEXT NOT NULL,
        metadata_json TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    """

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        import threading as _t
        self._lock = threading.Lock()
        self._local = _t.local()
        self._bootstrap()

    def _conn(self):
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(self.db_path, check_same_thread=False, isolation_level=None)
            c.execute("PRAGMA journal_mode=WAL")
            c.executescript(self.SCHEMA)
            self._local.conn = c
        return c

    def _bootstrap(self):
        self._conn()

    def lookup(self, key: str) -> Optional[ProviderSubmitReceipt]:
        with self._lock:
            row = self._conn().execute(
                "SELECT provider_job_id, metadata_json FROM idem_receipts WHERE idem_key=?",
                (key,),
            ).fetchone()
        if not row:
            return None
        return ProviderSubmitReceipt(
            provider_job_id=row[0],
            accepted_metadata=json.loads(row[1]) if row[1] else {},
        )

    def save(self, key: str, receipt: ProviderSubmitReceipt) -> None:
        with self._lock:
            try:
                self._conn().execute(
                    "INSERT INTO idem_receipts(idem_key, provider_job_id, metadata_json, created_at) VALUES (?,?,?,?)",
                    (key, receipt.provider_job_id,
                     json.dumps(receipt.accepted_metadata or {}),
                     json.dumps({"ts": "now"})[1:-1]),  # placeholder; real time below
                )
            except sqlite3.IntegrityError:
                return
            import time as _t
            self._conn().execute(
                "UPDATE idem_receipts SET created_at=? WHERE idem_key=?",
                (_t.strftime("%Y-%m-%dT%H:%M:%SZ", _t.gmtime()), key),
            )

    def submit_once(self, key: str, receipt: ProviderSubmitReceipt) -> ProviderSubmitReceipt:
        existing = self.lookup(key)
        if existing is not None:
            return existing
        self.save(key, receipt)
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