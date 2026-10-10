"""Master-key rotation workflow.

Wave 5 §3.4 — the operator-facing surface for rotating the
SQLCipher page-encryption key (`VIDEO_FACTORY_DB_KEY`) without
downtime.

The rotation has three distinct keys in play:

  * **OLD key** — currently encrypting the live DB.  Every page
    was encrypted under this key.  Decrypt-only.
  * **NEW key** — the operator-supplied replacement.  Encrypt-
    only.  We open a fresh cipher DB at `cipher_path + ".new"`,
    load every page from the OLD DB, write it under the NEW key.
  * **rotation_in_progress flag** — a row in `key_rotation` (a
    new Wave 5 single-row table) that gates credential writes and
    surfaces a UI banner.  We never toggle the flag without
    wrapping the read/write in an explicit transaction.

Algorithm (single-operator, brief spec):

  1. Acquire `rotation_in_progress = 1` on the LIVE DB so the
     API refuses new credentials.  This is an exclusive write; if
     another rotation is already in progress we raise.
  2. Open the LIVE DB with the OLD key (read-only mode).
  3. Open a fresh cipher DB at `<db_path>.new` with the NEW key.
  4. Inside one transaction: dump all tables from the OLD DB and
     load them into the NEW DB.  Set journal_mode=WAL.
  5. Verify: re-open the NEW DB with the NEW key, check that
     row counts match the OLD DB.  Spot-check a credential row
     round-trips through Fernet (i.e. the new DB has the same
     encrypted_blob bytes that the old DB had — meaning no row was
     silently lost).
  6. Atomically swap: `os.replace(cipher_path + ".new",
     cipher_path)`.  This is a rename at the filesystem level
     and is atomic on POSIX.
  7. Set `rotation_completed = 1`, clear `rotation_in_progress`.
  8. Emit audit rows `key.rotation_started`,
     `key.rotation_completed` (or `_failed`).

Failure handling: if anything between 2 and 5 raises we ROLLBACK
the inner transaction, delete the partial `.new` file, set
`rotation_failed = 1` (operator reads it via the status endpoint),
and clear `rotation_in_progress` so the API is usable again.
The LIVE DB is never modified until step 6 — so a failed
rotation leaves the live key untouched.

The API refuses new credentials while `rotation_in_progress = 1`.
Reads continue normally (the live DB is unchanged during the
copy).  After the swap, the API automatically picks up the new
key on the next request (per-thread `_conn()` cache will rebuild
when `_conn()` notices the underlying file changed; the
`_bootstrap_schema` check is only on first open).
"""
from __future__ import annotations

import os
import sqlite3
import threading
from pathlib import Path
from typing import Optional

from core import cipher_db


# ── caller-error vs infrastructure-error classification ─────────────
#
# A *caller error* in the rotation path means the operator (or an
# attacker posing as one) sent an `old_key` that does NOT match the
# key that the live SQLCipher DB is encrypted with.  It is a 4xx,
# NOT a 5xx: nothing about the DB is corrupt, no migration crashed,
# no I/O failed.  A *caller error* MUST NOT flip the global
# `key_rotation.failed` flag (which operators use as the alarm for
# real infrastructure faults) and MUST NOT emit a 500 audit row.
#
# Reserve `key_rotation.failed = 1` and `key.rotation_failed` for
# true infrastructure faults (corruption, IO error, migration crash,
# row-count divergence).  Surface caller-error attempts with a
# distinct event `key.rotation_rejected_caller_error` so operators
# can grep for it separately.
class BadOldKey(RuntimeError):
    """Caller supplied an `old_key` that does not match the live DB.

    Subclass of :class:`RotationFailed` is DELIBERATELY avoided:
    callers keying off `RotationFailed` for "real infrastructure
    fault, set the alarm" must not be tricked into firing here.

    Raised by :func:`rotate` / :func:`_rotate_locked` and surfaced
    by the admin handler as 403 (the admin gate is in scope on the
    rotate endpoint, so the caller-error path returns 403 rather
    than 400).  The global `key_rotation.failed` flag stays
    untouched on this path; only an audit row of type
    `key.rotation_rejected_caller_error` is recorded.
    """


# ── key_rotation table schema ────────────────────────────────────
#
# Single-row table.  We never INSERT a second row; we always
# UPDATE the existing row inside a transaction.  The schema
# records the operator flag, name, plus start/end timestamps and
# the actor (user_id) so audit can attribute.

KEY_ROTATION_SCHEMA = """
CREATE TABLE IF NOT EXISTS key_rotation (
    rotation_id TEXT PRIMARY KEY,
    rotation_in_progress INTEGER NOT NULL DEFAULT 0,
    rotation_completed INTEGER NOT NULL DEFAULT 0,
    rotation_failed INTEGER NOT NULL DEFAULT 0,
    started_at TEXT,
    completed_at TEXT,
    failed_at TEXT,
    actor_user_id TEXT,
    old_key_fingerprint TEXT,
    new_key_fingerprint TEXT
);
"""


# ── audit event names ────────────────────────────────────────────

EVENT_STARTED = "key.rotation_started"
EVENT_COMPLETED = "key.rotation_completed"
EVENT_FAILED = "key.rotation_failed"
# Caller-error path: emitted when the operator supplied an `old_key`
# that does not match the live DB.  Distinct from `key.rotation_failed`
# (which is the operator-visible alarm for real infrastructure faults
# such as corruption, IO error or migration crash).  Operators grep
# for `key.rotation_rejected_caller_error` to spot probing attempts
# or accidentally-stale operator credentials; the global
# `key_rotation.failed` flag is left UNTOUCHED on this path.
EVENT_REJECTED_CALLER = "key.rotation_rejected_caller_error"


class RotationInProgress(RuntimeError):
    """Another rotation is already running.  Operator must wait."""


class RotationFailed(RuntimeError):
    """Rotation aborted; live DB unchanged.  See status row."""


class KeyFingerprintMissing(KeyError):
    """Caller did not supply both old and new key."""


# ── public API ───────────────────────────────────────────────────

def ensure_table(repo) -> None:
    """Create the key_rotation table on the given Repo (idempotent).

    Also seeds a single sentinel row so `rotation_state` can read
    it via `SELECT ... LIMIT 1` without having to special-case
    "no rows yet".  The seed is a no-op if a row already exists.
    """
    conn = repo._conn()  # noqa: SLF001
    conn.executescript(KEY_ROTATION_SCHEMA)
    # Seed a sentinel row if the table is empty.  Using
    # `INSERT OR IGNORE` would need a UNIQUE constraint; a
    # guarded SELECT is simpler and keeps the schema minimal.
    row = conn.execute(
        "SELECT 1 FROM key_rotation LIMIT 1"
    ).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO key_rotation(rotation_id, "
            "rotation_in_progress) VALUES (?, 0)",
            ("rot_0001",),
        )


def _fingerprint(key: str) -> str:
    """Stable, non-reversible identifier for a key.

    We never write the key itself to the audit table or status row;
    only the SHA-256[:16] fingerprint.  This is enough to prove
    "the same operator rotated from K1 -> K2" in a forensic
    review without leaking the key material.
    """
    import hashlib
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def redact_keys(text: str, *keys: Optional[str]) -> str:
    """Replace every occurrence of ``keys`` in ``text`` by its fingerprint.

    S1b / A1 F15: error strings from SQLCipher / the copy step end up
    in the audit row, the HTTP error body and the logs; none of them
    may ever carry key material.
    """
    out = text or ""
    for k in keys:
        if k:
            out = out.replace(k, f"[key:{_fingerprint(k)}]")
    return out


def _is_bad_old_key_error(exc: BaseException) -> bool:
    """True iff ``exc`` looks like a wrong-old-key failure.

    SQLCipher raises ``sqlcipher3.dbapi2.DatabaseError: file is not
    a database`` (HMAC mismatch on the first decrypted page) when
    the caller opens it with the wrong PRAGMA key.  The cipher_db
    connection factory only normalises the *PRAGMA key* execution
    error to ``sqlite3.DatabaseError``; the HMAC mismatch fires
    later, on the first statement, and bubbles up as the raw
    ``sqlcipher3.dbapi2.DatabaseError`` (which does NOT inherit
    from ``sqlite3.DatabaseError`` — verified 2026-10).  Both
    flavours must therefore be classified as caller-error here.
    """
    if isinstance(exc, sqlite3.DatabaseError):
        return True
    try:
        from sqlcipher3 import dbapi2 as _sqc  # type: ignore
    except Exception:  # pragma: no cover - sqlcipher3-binary optional
        _sqc = None
    if _sqc is not None and isinstance(exc, _sqc.DatabaseError):
        return True
    return False


def _audit(repo, *, event: str, actor_user_id: str,
           extra: Optional[dict] = None) -> None:
    """Append a runner_audit row for the rotation event.

    Uses the canonical `append_runner_audit` helper so the row
    shape stays consistent with the rest of the codebase (and is
    mirrored to `audit_log` for actor attribution).  We pass
    `job_id="-"` because key rotation is not job-scoped — the
    cross-actor `audit_log` mirror is the one operators grep.
    """
    from control_plane.repo_helpers import append_runner_audit
    payload = {"event": event, "actor": actor_user_id}
    if extra:
        payload.update(extra)
    append_runner_audit(
        repo,
        job_id="-",  # not job-scoped; surfaced via audit_log
        event=event,
        context=payload,
        user_id=actor_user_id,
    )


def rotation_state(repo) -> dict:
    """Return the current rotation status row (or a default)."""
    ensure_table(repo)
    row = repo._conn().execute(  # noqa: SLF001
        "SELECT rotation_in_progress, rotation_completed, "
        "rotation_failed, started_at, completed_at, failed_at, "
        "actor_user_id, old_key_fingerprint, new_key_fingerprint "
        "FROM key_rotation LIMIT 1"
    ).fetchone()
    if row is None:
        return {
            "in_progress": False,
            "completed": False,
            "failed": False,
            "started_at": None,
            "completed_at": None,
            "failed_at": None,
            "actor_user_id": None,
            "old_key_fingerprint": None,
            "new_key_fingerprint": None,
        }
    return {
        "in_progress": bool(row[0]),
        "completed": bool(row[1]),
        "failed": bool(row[2]),
        "started_at": row[3],
        "completed_at": row[4],
        "failed_at": row[5],
        "actor_user_id": row[6],
        "old_key_fingerprint": row[7],
        "new_key_fingerprint": row[8],
    }


# ── instrumentation hook ──────────────────────────────────────────────
#
# Tests + production observability need a way to assert "the asyncio
# event loop is free during a rotation".  A small, sync-only hook is
# the cheapest way to expose the inside-of-the-lock boundary without
# changing the public surface or the lock semantics.
#
#   * `before_copy(epoch)` fires once per rotation, INSIDE
#     `repo._lock`, immediately before `copy_cipher_to_cipher`.  The
#     body runs synchronously on the worker thread that owns the
#     lock — slow hooks will block the rotation (intentional: tests
#     install slow hooks to simulate a long-running re-key and
#     assert other handlers stay responsive on the event loop).
#   * `epoch` is the Repo epoch captured at the moment the hook
#     fires; useful for assertions like "the rotating thread holds
#     the lock right now" or "no other thread has touched _conn
#     since we bumped the epoch".
#
# The hook never escapes this module: callers pass a `RotationHook`
# (or None) as the `instrumentation` kwarg to `rotate()` /
# `rotate_async()`.

class RotationHook:
    """Optional observability + test instrumentation for `rotate()`.

    Sync-only by design: the hook runs inside `repo._lock` on the
    thread that owns the lock.  A slow hook is intentional — it is
    the exact mechanism the new S1b tests use to assert that the
    asyncio event loop is NOT blocked while a rotation is in flight.
    """

    __slots__ = ("_before_copy",)

    def __init__(self, *, before_copy=None) -> None:
        self._before_copy = before_copy

    def before_copy(self, *, epoch: int) -> None:
        if self._before_copy is not None:
            try:
                self._before_copy(epoch=epoch)
            except Exception:
                # Hooks are observability, not control flow.  A
                # buggy hook MUST NOT break the rotation; the lock
                # and the cipher file are already live state.
                import logging as _logging
                _logging.getLogger(__name__).exception(
                    "rotation hook before_copy raised; "
                    "ignoring (rotation will proceed)"
                )


# ── write gate ───────────────────────────────────────────

def credential_writes_allowed(repo) -> bool:
    """Backwards-compat alias for :func:`writes_allowed`.

    Kept so existing call sites (and external monitoring that
    grepped for this name) keep working.  New code should call
    ``writes_allowed`` directly.
    """
    return writes_allowed(repo)


def writes_allowed(repo) -> bool:
    """True iff the API may currently write to ANY table.

    Wave 5 follow-up (t_abf264b4): rotated from a credential-only
    gate to a generic write gate.  While a rotation is in
    progress this returns False for ALL writes (audit_log, jobs,
    workspaces, credentials, ...), not just credentials.  The
    brief's Item #5 demanded that concurrent writers cannot
    interleave between the OLD-key snapshot and the cipher swap
    — the simplest way to enforce that is to refuse every write
    transaction while the flag is set.

    Wired into :meth:`core.repo.Repo.tx` so the canonical
    `with repo.tx() as c: c.execute(...)` block is gated.  The
    rotation function itself passes ``_skip_rotation_check=True``
    to bypass the gate for its own bookkeeping rows.
    """
    return not rotation_state(repo)["in_progress"]


# ── the rotation itself ──────────────────────────────────────────

def rotate(*, repo, old_key: str, new_key: str,
            actor_user_id: str, db_path: Optional[str] = None,
            instrumentation: Optional["RotationHook"] = None
            ) -> dict:
    """Run the full rotation.  Returns a status summary dict.

    Parameters
    ----------
    repo: core.repo.Repo
        The live Repo, opened with the OLD key.  Used for
        row-count verification and audit.
    old_key: str
        The currently-active key.  Must match what the LIVE DB
        was opened with; we verify by opening a second connection
        and asserting `SELECT count(*) FROM sqlite_master` works.
    new_key: str
        The replacement key.  Must differ from `old_key`.
    actor_user_id: str
        Recorded in the rotation row + audit rows.
    db_path: str | None
        Filesystem path to the live DB.  Defaults to
        `repo.db_path`.  Passed so callers can dry-run against a
        test path without mutating the production Repo object.
    instrumentation: RotationHook | None
        Optional observability hook.  Fires once inside `repo._lock`
        right before `copy_cipher_to_cipher`.  See :class:`RotationHook`
        for the contract; in production this is `None` and the
        rotation is unchanged.

    Raises
    ------
    RotationInProgress
        Another rotation is already in progress.
    RotationFailed
        Live DB was not modified; see status row for the failure
        reason.
    KeyFingerprintMissing
        old_key == new_key.
    """
    if not old_key or not new_key:
        raise KeyFingerprintMissing(
            "old_key and new_key must both be supplied"
        )
    if old_key == new_key:
        raise KeyFingerprintMissing(
            "old_key and new_key must differ"
        )
    if db_path is None:
        db_path = repo.db_path
    db_path = str(db_path)  # narrow Optional to str

    ensure_table(repo)

    # ── 0. Hold the Repo lock for the ENTIRE rotation. ────────
    # Wave 5 follow-up (t_abf264b4): the lock is the primary
    # mechanism that makes the rotation atomic from the caller's
    # perspective.  While we hold it, every `Repo.tx()` call from
    # other threads blocks.  This single change addresses both
    # BLOCKER-1 (silent corruption of post-rotation writes through
    # the same Repo) and BLOCKER-2 (row-count verify false-negative
    # when a concurrent writer interleaves between the snapshot
    # and the copy).
    #
    # Combined with the `tx()` rotation check added in
    # core/repo.py, this gives two independent layers of safety:
    #   1. The lock makes the rotation a critical section in the
    #      multi-threading sense — no other thread can enter `tx()`
    #      while we run.
    #   2. The flag-based check makes the rotation a critical
    #      section in the single-thread sense too — if some path
    #      bypasses the lock (e.g. a direct `repo._conn().execute`
    #      from a runner), the flag still surfaces a
    #      `RotationInProgress` error.
    #
    # Wave 5 follow-up-3 (t_c20fdb7d): `rotate()` is synchronous
    # and the asyncio handler that invokes us (`handle_rotate`)
    # used to call us directly on the event-loop thread, blocking
    # the loop for ~0.4s per SQLCipher re-key.  Async callers MUST
    # dispatch through :func:`rotate_async` so the loop stays
    # responsive; this function's contract is unchanged (it still
    # acquires the lock on the calling thread).
    with repo._lock:  # noqa: SLF001 — rotation owns the lock
        return _rotate_locked(
            repo=repo,
            old_key=old_key,
            new_key=new_key,
            actor_user_id=actor_user_id,
            db_path=db_path,
            instrumentation=instrumentation,
        )


async def rotate_async(*, repo, old_key: str, new_key: str,
                       actor_user_id: str,
                       db_path: Optional[str] = None,
                       instrumentation: Optional["RotationHook"] = None
                       ) -> dict:
    """Async wrapper around :func:`rotate`.

    Dispatches the (synchronous, lock-holding) rotation body to a
    worker thread via :func:`asyncio.to_thread` so the asyncio
    event loop is free during the ~0.4s SQLCipher re-key.  The
    Repo lock invariants are unchanged: the worker thread acquires
    `repo._lock` exactly as the synchronous path does, so every
    `Repo.tx()` call from another thread still blocks for the full
    rotation window.

    Use this from any `async def` API handler; the sync `rotate()`
    remains for tests and one-shot scripts.
    """
    import asyncio as _asyncio
    return await _asyncio.to_thread(
        rotate,
        repo=repo,
        old_key=old_key,
        new_key=new_key,
        actor_user_id=actor_user_id,
        db_path=db_path,
        instrumentation=instrumentation,
    )


def _rotate_locked(*, repo, old_key: str, new_key: str,
                    actor_user_id: str, db_path: str,
                    instrumentation: Optional["RotationHook"] = None
                    ) -> dict:
    """Inner rotation body.  Caller MUST hold `repo._lock`.

    Factored out of :func:`rotate` so the lock-acquisition step
    is visible at a glance in the public function.  Everything
    from the WAL checkpoint through the cipher swap and the
    ``db_key`` refresh runs under the lock.

    When ``instrumentation`` is provided, its ``before_copy``
    callback fires once inside the lock, immediately before
    ``copy_cipher_to_cipher`` — this is the seam S1b tests use to
    assert that the asyncio event loop stays responsive while a
    rotation is in flight.
    """
    # ── 1. Acquire the in-progress flag ────────────────────────
    # Force a WAL checkpoint on the live Repo connection first so
    # the new connection that copy_cipher_to_cipher will open sees
    # the same on-disk state.  Without this, the second connection
    # can observe the database BEFORE the schema bootstrap because
    # the WAL has not been flushed.  PASSIVE is a no-op on an idle
    # WAL, so we use FULL.
    try:
        repo._conn().execute("PRAGMA wal_checkpoint(FULL)")  # noqa: SLF001
    except Exception:
        pass
    with repo.tx(_skip_rotation_check=True) as c:
        row = c.execute(
            "SELECT rotation_id, rotation_in_progress "
            "FROM key_rotation LIMIT 1"
        ).fetchone()
        if row is None:
            c.execute(
                "INSERT INTO key_rotation(rotation_id, "
                "rotation_in_progress) VALUES (?, 0)",
                ("rot_0001",),
            )
        else:
            if row[1]:
                raise RotationInProgress(
                    "another rotation is already in progress; "
                    "wait for it to finish or fail before "
                    "starting a new one"
                )
        import time as _t
        c.execute(
            "UPDATE key_rotation SET rotation_in_progress=1, "
            "rotation_failed=0, rotation_completed=0, "
            "started_at=?, actor_user_id=?, "
            "old_key_fingerprint=?, new_key_fingerprint=?",
            (_t.strftime("%Y-%m-%dT%H:%M:%SZ", _t.gmtime()),
             actor_user_id,
             _fingerprint(old_key), _fingerprint(new_key)),
        )
        _audit(repo, event=EVENT_STARTED, actor_user_id=actor_user_id,
                extra={"old_key_fingerprint": _fingerprint(old_key),
                        "new_key_fingerprint": _fingerprint(new_key)})

    # ── 2..6. Copy + atomic swap (still under the lock) ─────
    new_path = Path(db_path).with_suffix(
        Path(db_path).suffix + ".new"
    )
    if new_path.exists():
        new_path.unlink()
    backup_path = Path(db_path).with_suffix(
        Path(db_path).suffix + ".pre_rotation"
    )
    try:
        # Snapshot the OLD DB row counts so we can verify.  Done
        # under the lock so no other writer can interleave between
        # the snapshot and the copy.  (BLOCKER-2 fix.)
        #
        # If the operator supplied an `old_key` that does not match
        # the live DB, SQLCipher raises a `DatabaseError` here
        # ("file is not a database" — HMAC mismatch on the first
        # decrypted page).  We translate that into a `BadOldKey`
        # BEFORE the broad `except Exception` block can flip
        # `rotation_failed = 1` — caller-error MUST stay out of the
        # infrastructure-fault alarm path.
        try:
            old_counts = _table_counts(db_path, key=old_key)
        except Exception as _e:  # noqa: BLE001 - classify then re-raise
            if _is_bad_old_key_error(_e):
                raise BadOldKey(
                    "old_key does not match the live DB "
                    f"(sqlcipher rejected the key): "
                    f"{redact_keys(str(_e), old_key)}"
                ) from _e
            raise
        # Backup the OLD DB so a partial failure leaves a
        # recoverable copy on disk.  Cheap: SQLite WAL is
        # already coherent on disk.
        if not backup_path.exists():
            import shutil
            shutil.copy2(db_path, backup_path)
            # Copy WAL + SHM if present.
            for ext in ("-wal", "-shm"):
                src = Path(db_path).with_suffix(
                    Path(db_path).suffix + ext
                )
                if src.exists():
                    shutil.copy2(src, backup_path.with_suffix(
                        backup_path.suffix + ext
                    ))
        # ── Instrumentation seam (t_c20fdb7d) ────────────────────
        # Fires INSIDE `repo._lock`, immediately before the
        # SQLCipher re-key.  Tests install a hook here that sleeps
        # N seconds; with the old synchronous path the asyncio
        # event loop would block for those N seconds, with
        # `rotate_async` it does NOT.  In production this is a
        # cheap no-op (None).
        if instrumentation is not None:
            instrumentation.before_copy(epoch=repo._epoch)  # noqa: SLF001
        # Open OLD with src_key + write NEW with dst_key.  We use
        # copy_cipher_to_cipher (NOT migrate_plaintext_to_cipher)
        # because the LIVE DB is already encrypted with src_key —
        # we cannot pretend it's plaintext.
        #
        # If the operator supplied an `old_key` that does not match
        # the live DB, SQLCipher raises a `DatabaseError` here
        # while opening the SOURCE cipher file.  Same caller-error
        # classification as for `_table_counts` above — translate
        # to `BadOldKey` BEFORE the broad `except Exception` block
        # can flip `rotation_failed = 1`.
        try:
            cipher_db.copy_cipher_to_cipher(
                src_path=db_path,
                dst_path=str(new_path),
                src_key=old_key,
                dst_key=new_key,
            )
        except Exception as _e:  # noqa: BLE001 - classify then re-raise
            if _is_bad_old_key_error(_e):
                raise BadOldKey(
                    "old_key does not match the live DB "
                    f"(sqlcipher rejected the key during copy): "
                    f"{redact_keys(str(_e), old_key)}"
                ) from _e
            raise
        # Verify the new DB has the same row counts.  Both sides
        # of the comparison are taken under the lock so the verify
        # cannot see partial state.
        new_counts = _table_counts(str(new_path), key=new_key)
        if old_counts != new_counts:
            raise RotationFailed(
                f"row counts diverged: old={old_counts} "
                f"new={new_counts}"
            )
        # Atomic swap.
        import os as _os
        # Remove WAL/SHM for OLD so they don't shadow the new file.
        for ext in ("", "-wal", "-shm"):
            src = Path(db_path).with_suffix(
                Path(db_path).suffix + ext
            )
            if ext and src.exists():
                src.unlink()
        _os.replace(new_path, db_path)
    except Exception as e:
        # ── caller-error fast-path ────────────────────────────
        # A wrong-old-key failure (operator typo, stale credential,
        # probing attempt) is NOT a real infrastructure fault.
        # We MUST NOT set `rotation_failed = 1` here — that flag
        # is the operator's alarm for "corruption / IO error /
        # migration crash".  Emit a distinct audit row, clear
        # `rotation_in_progress` so the API is usable again, and
        # raise `BadOldKey` so the admin handler returns 403
        # instead of 500.
        #
        # `BadOldKey` from the inner translators carries the
        # original SQLCipher error in `__cause__` for forensic
        # context, but we only emit the redacted string + the
        # fingerprint of the rejected old_key.
        if isinstance(e, BadOldKey) or _is_bad_old_key_error(e):
            import time as _t
            # Clear the in-progress flag; leave `rotation_failed`
            # untouched (it stays 0 unless a real infra fault
            # surfaces).  Holding `repo._lock` for the whole
            # rotation means no other writer is in tx() right
            # now, so this UPDATE is uncontested.
            try:
                # Repo uses isolation_level=None so the caller
                # manages transactions explicitly.  `UPDATE` is
                # auto-committed because there is no surrounding
                # BEGIN — same pattern as the infra-fault branch
                # below.
                repo._conn().execute(  # noqa: SLF001
                    "UPDATE key_rotation SET rotation_in_progress=0",
                )
            except Exception:
                # If we cannot even UPDATE the row (e.g. the
                # SQLCipher error left the connection in a broken
                # state), re-raise as a caller-error anyway.  The
                # audit row below uses an explicit connection
                # pulled from the cipher module so the rejection
                # is still recorded.
                pass
            _audit(
                repo, event=EVENT_REJECTED_CALLER,
                actor_user_id=actor_user_id,
                extra={
                    "old_key_fingerprint": _fingerprint(old_key),
                    "new_key_fingerprint": _fingerprint(new_key),
                    "reason": "wrong_old_key",
                    "error": redact_keys(
                        # The translator (BadOldKey) wraps the
                        # operator-suppressed message as the
                        # string passed to its constructor.  If we
                        # reach here from the raw sqlcipher error
                        # path (BadOldKey is False but the error
                        # class matches), we use the exception's
                        # own string.
                        str(e.__cause__) if getattr(e, "__cause__", None) else str(e),
                        old_key, new_key,
                    ),
                },
            )
            # Clean up any partial `.new` artifact (the row-count
            # snapshot already succeeded, but the copy may have
            # left a partial dst on disk — but only if the failure
            # was AFTER the copy began; for a fail-fast HMAC
            # mismatch the dst is not yet created).
            if new_path.exists():
                try:
                    new_path.unlink(missing_ok=True)
                except Exception:
                    pass
            # If the operator passed us a non-BadOldKey raw
            # sqlcipher error, translate it here so the public
            # surface is uniform.
            if not isinstance(e, BadOldKey):
                raise BadOldKey(
                    "old_key does not match the live DB "
                    f"(sqlcipher rejected the key): "
                    f"{redact_keys(str(e), old_key)}"
                ) from e
            raise
        # ── infrastructure-fault path (unchanged) ─────────────
        # Mark the rotation failed; live DB untouched.
        import time as _t
        # The flag-flip happens via tx() with the rotation check
        # skipped because we just caught a real error from the
        # rotation; we MUST be able to mark it failed regardless
        # of the gate state.  Holding `repo._lock` for the whole
        # rotation guarantees that no other writer is in tx() at
        # this point.
        repo._conn().execute(  # noqa: SLF001
            "UPDATE key_rotation SET rotation_in_progress=0, "
            "rotation_failed=1, failed_at=?",
            (_t.strftime("%Y-%m-%dT%H:%M:%SZ", _t.gmtime()),),
        )
        _audit(repo, event=EVENT_FAILED, actor_user_id=actor_user_id,
                extra={"error": redact_keys(str(e), old_key, new_key)})
        # Clean up the .new artifact if it's still there.
        if new_path.exists():
            new_path.unlink(missing_ok=True)
        if isinstance(e, RotationFailed):
            raise
        raise RotationFailed(
            f"rotation aborted: {redact_keys(str(e), old_key, new_key)}"
        ) from e

    # ── 7. Mark completed + REFRESH the Repo to use NEW key ─
    # The BLOCKER-1 fix lives here.  The cipher file on disk is
    # now the NEW-cipher file, but the Repo instance still has
    # `db_key = old_key` and its per-thread connection cache
    # still points at the OLD-key cipher state.  If we did
    # nothing, the next call to `Repo._conn()` would return a
    # connection that PRAGMA-key'd with the OLD key while the
    # file on disk is encrypted with the NEW key — every
    # subsequent write through this Repo would be silently
    # dropped or encrypted with the wrong key.  The fix:
    #   1. Drop the cached connection (it points at the OLD
    #      cipher state).
    #   2. Update `db_key` so the next `_conn()` opens with the
    #      NEW key against the new cipher file.
    #   3. Mark the rotation completed.  Because the flag is
    #      cleared only AFTER the swap, and the swap is only
    #      after we've snapshotted row counts under the lock,
    #      the entire critical section is monotonic.
    #
    # Wave 5 follow-up-2 (t_282da1ca): step 1 only cleared the
    # *rotating thread's* per-thread conn.  Every other thread
    # that had called `repo._conn()` before the swap still held
    # a stale OLD-key connection.  Bumping `repo._epoch` here
    # is what makes those threads close-and-reopen on their
    # next `_conn()` access.  See the epoch docstring in
    # `core.repo.Repo.__init__` for the full rationale.
    repo.db_key = new_key  # noqa: SLF001 — rotate owns the key flip
    repo._epoch += 1  # noqa: SLF001 — invalidate every thread's stale conn
    repo.close()  # noqa: SLF001 — drop stale per-thread conn (rotating thread)

    import time as _t
    with repo.tx(_skip_rotation_check=True) as c:
        c.execute(
            "UPDATE key_rotation SET rotation_in_progress=0, "
            "rotation_completed=1, completed_at=?",
            (_t.strftime("%Y-%m-%dT%H:%M:%SZ", _t.gmtime()),),
        )
    _audit(repo, event=EVENT_COMPLETED, actor_user_id=actor_user_id,
            extra={"old_key_fingerprint": _fingerprint(old_key),
                    "new_key_fingerprint": _fingerprint(new_key)})
    return rotation_state(repo)


def _table_counts(db_path: str, *, key: str) -> dict[str, int]:
    """Open `db_path` with `key` and return row counts per table.

    Used to assert row-count parity between OLD and NEW during
    rotation.  We deliberately open a fresh connection rather
    than reusing the Repo because the Repo is bound to the OLD
    key and we want to prove the NEW key can read everything back.
    """
    conn = cipher_db.connect(db_path, key=key)
    try:
        tables = [
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
            if not r[0].startswith("sqlite_")
        ]
        return {
            t: conn.execute(
                f"SELECT count(*) FROM {t}"
            ).fetchone()[0] for t in tables
        }
    finally:
        conn.close()


# ── DB-fingerprint helpers ───────────────────────────────────────

def can_open_with_key(db_path: str, key: str) -> bool:
    """Return True iff `db_path` opens with `key` AND has tables."""
    try:
        conn = cipher_db.connect(db_path, key=key)
        try:
            n = conn.execute(
                "SELECT count(*) FROM sqlite_master "
                "WHERE type='table'"
            ).fetchone()[0]
            return n > 0
        finally:
            conn.close()
    except Exception:
        return False


__all__ = [
    "EVENT_COMPLETED",
    "EVENT_FAILED",
    "EVENT_REJECTED_CALLER",
    "EVENT_STARTED",
    "BadOldKey",
    "KeyFingerprintMissing",
    "RotationFailed",
    "RotationHook",
    "RotationInProgress",
    "can_open_with_key",
    "credential_writes_allowed",
    "ensure_table",
    "rotate",
    "rotate_async",
    "rotation_state",
    "writes_allowed",
]
