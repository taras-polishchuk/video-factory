"""Retention sweeps for job artifacts and audit rows (F-07, F-42).

Two knobs, both default OFF (``0``):

* ``ARTIFACTS_RETENTION_DAYS``  delete rendered job artifacts
  (everything under ``<VIDEO_FACTORY_STORAGE>/jobs/``) whose mtime is
  older than N days. Uploaded workspace/company assets
  (``workspaces/*/assets``, ``companies/*/assets``) are user inputs,
  not artifacts, and are never touched. Neither are files at the
  storage root (``idem.db`` + WAL files of the durable idempotency
  store). DB rows (``job_outputs``) are kept: the download routes
  already answer 404 "output bytes missing" for an evicted blob.
* ``AUDIT_LOG_RETENTION_DAYS``  delete ``audit_log`` and
  ``runner_audit`` rows created more than N days ago.

Runs:

* in-process: ``RetentionSweeper`` (started by ``api.main`` on app
  startup) sweeps once at startup and then every 24 h on a daemon
  thread; it is a no-op thread when both knobs are 0;
* out-of-process: ``python -m cli.maintenance retention`` (one sweep,
  JSON summary on stdout) for a systemd timer / compose cron. See
  docs/operations-prod.md "Retention".
"""
from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from typing import Optional

_log = logging.getLogger("video_factory.retention")

ARTIFACTS_RETENTION_ENV = "ARTIFACTS_RETENTION_DAYS"
AUDIT_RETENTION_ENV = "AUDIT_LOG_RETENTION_DAYS"
SWEEP_INTERVAL_SECONDS = 24 * 60 * 60

# Only this subtree of the storage root holds job artifacts.
ARTIFACTS_SUBDIR = "jobs"


def retention_days(env_name: str, *, environ=None) -> int:
    """Read a retention knob. Empty, ``0``, negative or invalid = off."""
    env = os.environ if environ is None else environ
    raw = (env.get(env_name) or "").strip()
    if not raw:
        return 0
    try:
        days = int(float(raw))
    except ValueError:
        _log.warning("ignoring invalid %s=%r (retention off)", env_name, raw)
        return 0
    return max(0, days)


def _cutoff_epoch(days: int, now: Optional[float]) -> float:
    return (time.time() if now is None else float(now)) - days * 86400.0


def sweep_artifacts(storage_root: str, days: int, *,
                    now: Optional[float] = None,
                    dry_run: bool = False) -> dict:
    """Delete files under ``<storage_root>/jobs`` older than ``days``.

    "Older" is by mtime, strictly before ``now - days``. Empty
    directories left behind are removed (never the ``jobs`` dir itself).
    Symlinks are not followed and are never deleted. Returns counts.
    """
    out = {"enabled": days > 0, "days": days, "files_deleted": 0,
           "bytes_deleted": 0, "dirs_removed": 0, "errors": 0,
           "dry_run": bool(dry_run)}
    if days <= 0:
        return out
    base = Path(storage_root).resolve() / ARTIFACTS_SUBDIR
    if not base.is_dir():
        return out
    cutoff = _cutoff_epoch(days, now)
    # bottom-up, so emptied dirs can be pruned in the same pass
    for dirpath, dirnames, filenames in os.walk(base, topdown=False,
                                                followlinks=False):
        d = Path(dirpath)
        for name in filenames:
            p = d / name
            try:
                st = p.lstat()
                if p.is_symlink() or st.st_mtime >= cutoff:
                    continue
                if not dry_run:
                    p.unlink()
                out["files_deleted"] += 1
                out["bytes_deleted"] += int(st.st_size)
            except FileNotFoundError:
                continue
            except OSError as e:
                out["errors"] += 1
                _log.warning("retention: cannot delete %s: %s", p, e)
        if d != base and not dry_run:
            try:
                d.rmdir()  # only succeeds when empty
                out["dirs_removed"] += 1
            except OSError:
                pass
    return out


def sweep_audit(repo, days: int, *, now: Optional[float] = None,
                dry_run: bool = False) -> dict:
    """Delete audit rows older than ``days`` (``audit_log`` +
    ``runner_audit``). ``created_at`` is ISO-8601 UTC with a ``Z``
    suffix everywhere, so a string compare is a time compare."""
    out = {"enabled": days > 0, "days": days, "rows_deleted": {},
           "dry_run": bool(dry_run)}
    if days <= 0:
        return out
    cutoff_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                               time.gmtime(_cutoff_epoch(days, now)))
    out["cutoff"] = cutoff_iso
    if dry_run:
        conn = repo._conn()  # noqa: SLF001
        for table in ("audit_log", "runner_audit"):
            try:
                n = conn.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE created_at < ?",
                    (cutoff_iso,),
                ).fetchone()[0]
            except Exception:  # noqa: BLE001  (table absent)
                n = 0
            out["rows_deleted"][table] = int(n)
        return out
    out["rows_deleted"] = repo.delete_audit_older_than(cutoff_iso)
    return out


def run_retention(*, repo, storage_root: str, environ=None,
                  now: Optional[float] = None,
                  dry_run: bool = False) -> dict:
    """One sweep of both knobs. Never raises: a failing half is
    reported under ``error`` and the other half still runs."""
    result: dict = {}
    a_days = retention_days(ARTIFACTS_RETENTION_ENV, environ=environ)
    u_days = retention_days(AUDIT_RETENTION_ENV, environ=environ)
    try:
        result["artifacts"] = sweep_artifacts(storage_root, a_days, now=now,
                                              dry_run=dry_run)
    except Exception as e:  # noqa: BLE001
        _log.exception("artifact retention sweep failed")
        result["artifacts"] = {"error": f"{type(e).__name__}: {e}"}
    try:
        result["audit"] = sweep_audit(repo, u_days, now=now, dry_run=dry_run)
    except Exception as e:  # noqa: BLE001
        _log.exception("audit retention sweep failed")
        result["audit"] = {"error": f"{type(e).__name__}: {e}"}
    if a_days or u_days:
        _log.info("retention sweep: %s", result)
    return result


class RetentionSweeper:
    """Startup + daily sweep on a daemon thread.

    ``start()`` returns immediately; the first sweep runs on the
    thread right away, then every ``interval`` seconds until
    ``stop()``. When both knobs are 0 at start time no thread is
    started at all.
    """

    def __init__(self, *, repo, storage_root: str,
                 interval: float = SWEEP_INTERVAL_SECONDS,
                 environ=None) -> None:
        self.repo = repo
        self.storage_root = storage_root
        self.interval = float(interval)
        self.environ = environ
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.last_result: Optional[dict] = None

    def enabled(self) -> bool:
        return bool(
            retention_days(ARTIFACTS_RETENTION_ENV, environ=self.environ)
            or retention_days(AUDIT_RETENTION_ENV, environ=self.environ)
        )

    def start(self) -> bool:
        if not self.enabled() or self._thread is not None:
            return False
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="retention-sweeper")
        self._thread.start()
        return True

    def stop(self, timeout: Optional[float] = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.last_result = run_retention(
                repo=self.repo, storage_root=self.storage_root,
                environ=self.environ,
            )
            if self._stop.wait(self.interval):
                return
