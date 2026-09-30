"""Job queue + worker lease.

In-memory FIFO with explicit lease semantics. Workers claim a job with
a TTL; if the worker dies, the lease expires and the job becomes
re-claimable. Single-machine only. Multi-worker scale-out is V2.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class Lease:
    lease_id: str
    job_id: str
    worker_id: str
    acquired_at: float
    expires_at: float


@dataclass
class Job:
    job_id: str
    payload: dict
    enqueued_at: float = field(default_factory=time.time)
    attempts: int = 0

    def to_dict(self) -> dict:
        return {"job_id": self.job_id, "attempts": self.attempts, "payload": self.payload}


class JobQueue:
    def __init__(self) -> None:
        self._jobs: Dict[str, Job] = {}
        self._pending: List[str] = []
        self._leases: Dict[str, Lease] = {}
        self._lock = threading.Lock()

    def enqueue(self, *, job_id: Optional[str] = None, payload: dict) -> str:
        jid = job_id or f"job_{uuid.uuid4().hex[:12]}"
        with self._lock:
            self._jobs[jid] = Job(job_id=jid, payload=payload)
            self._pending.append(jid)
        return jid

    def claim(self, *, worker_id: str, lease_seconds: int = 60) -> Optional[Lease]:
        """Claim the next ready job, expiring stale leases first."""
        now = time.time()
        with self._lock:
            # Expire any stale leases and re-enqueue those jobs.
            for lid in list(self._leases.keys()):
                l = self._leases[lid]
                if l.expires_at <= now:
                    self._leases.pop(lid, None)
                    if l.job_id not in self._pending:
                        self._pending.append(l.job_id)
            while self._pending:
                jid = self._pending.pop(0)
                job = self._jobs.get(jid)
                if job is None:
                    continue
                job.attempts += 1
                lease = Lease(
                    lease_id=f"lease_{uuid.uuid4().hex[:12]}",
                    job_id=jid,
                    worker_id=worker_id,
                    acquired_at=now,
                    expires_at=now + lease_seconds,
                )
                self._leases[lease.lease_id] = lease
                return lease
            return None

    def complete(self, lease_id: str) -> None:
        with self._lock:
            l = self._leases.pop(lease_id, None)
            if l is None:
                return
            self._jobs.pop(l.job_id, None)

    def fail(self, lease_id: str, *, retry: bool = True) -> None:
        with self._lock:
            l = self._leases.pop(lease_id, None)
            if l is None:
                return
            if retry:
                self._pending.append(l.job_id)
            else:
                self._jobs.pop(l.job_id, None)

    def renew(self, lease_id: str, *, seconds: int = 60) -> bool:
        with self._lock:
            l = self._leases.get(lease_id)
            if l is None:
                return False
            l.expires_at = time.time() + seconds
            return True

    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending)

    def active_count(self) -> int:
        with self._lock:
            return len(self._leases)

    def get_job(self, job_id: str) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(job_id)

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "pending": list(self._pending),
                "leases": [
                    {
                        "lease_id": l.lease_id,
                        "job_id": l.job_id,
                        "worker_id": l.worker_id,
                        "expires_at": l.expires_at,
                    }
                    for l in self._leases.values()
                ],
            }