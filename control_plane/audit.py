"""Audit log. In-memory implementation. Never logs secret values.

Every state-changing call MUST be recorded. AuditLog is append-only.
Records are JSON-serializable. The log deliberately does NOT include
secret values, full request/response bodies, or signed URLs.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


# Fields that MUST NEVER appear in an audit record, even if the caller
# passes them in the context dict. Defense in depth.
_SECRET_LIKE_KEYS = {
    "api_key", "apikey", "authorization", "auth", "password",
    "secret", "token", "bearer", "cookie", "session",
    "heygen_api_key", "comfy_cloud_api_key", "comfy_gpu_password",
    "comfy_gpu_user", "webhook_secret", "signed_url",
}


def _scrub(ctx: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if ctx is None:
        return {}
    out: Dict[str, Any] = {}
    for k, v in ctx.items():
        if k.lower() in _SECRET_LIKE_KEYS:
            out[k] = "[REDACTED]"
        else:
            out[k] = v
    return out


@dataclass(frozen=True)
class AuditRecord:
    timestamp: str
    event: str
    actor: str
    campaign_id: Optional[str] = None
    video_id: Optional[str] = None
    scene_id: Optional[str] = None
    provider_job_id: Optional[str] = None
    context: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["context"] = _scrub(self.context)
        return d


class AuditLog:
    """Abstract audit log."""

    def record(self, record: AuditRecord) -> None:  # pragma: no cover
        raise NotImplementedError


class InMemoryAuditLog(AuditLog):
    """Thread-safe in-memory audit log. Use for tests + offline demo."""

    def __init__(self) -> None:
        self._records: List[AuditRecord] = []
        self._lock = threading.Lock()

    def record(self, record: AuditRecord) -> None:
        with self._lock:
            self._records.append(record)

    def records(self) -> List[AuditRecord]:
        with self._lock:
            return list(self._records)

    def filter(self, **kwargs: Any) -> List[AuditRecord]:
        with self._lock:
            out = []
            for r in self._records:
                if all(getattr(r, k, None) == v for k, v in kwargs.items()):
                    out.append(r)
            return out

    def to_json(self) -> str:
        with self._lock:
            return json.dumps([r.to_dict() for r in self._records], indent=2)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()