"""Adversarial test for the Wave 5 follow-up cross-thread defect.

The dev's fix (commit 4abbc84) refreshes `repo.db_key` and calls
`repo.close()` after the cipher file `os.replace`.  `Repo.close()`
only clears `self._local.conn` for the **calling thread's** thread-
local storage — every other thread that has previously called
`repo._conn()` keeps its cached OLD-key connection pointing at the
NEW-cipher file.

Consequence: any writer thread that opened its connection BEFORE
rotation and writes AFTER rotation either

    * raises RotationInProgress forever (because its cached conn
      sees the OLD `key_rotation` row with `in_progress=1` from
      before the post-swap UPDATE that used the NEW key), OR
    * if it bypasses `tx()` and uses `repo._conn().execute()`,
      writes are encrypted with the OLD key into the NEW-cipher
      file — silently corrupt, never visible to a NEW-key reader.

Either way: production rotation under concurrent API workers (which
are always on a different thread than the one running `rotate()`)
is broken.  This test pins the defect so a follow-up fix can be
verified.
"""

from __future__ import annotations

import os
import sys
import threading
import time
import uuid as _uuid

# Ensure the repo root is on sys.path regardless of where pytest
# is invoked from.  Falls back to the parent of this file's parent.
_REPO_ROOT = os.environ.get("VIDEO_FACTORY_REPO_ROOT")
if _REPO_ROOT:
    sys.path.insert(0, _REPO_ROOT)
else:
    _HERE = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, os.path.dirname(_HERE))

import pytest
from core import cipher_db
from core.repo import Repo
from control_plane import key_rotation


OLD_KEY = "xthread-old-key-1234567890"
NEW_KEY = "xthread-new-key-0987654321"


def _seed(repo: Repo) -> str:
    user = repo.create_user(
        email=f"xthread-{os.getpid()}@example.com",
        password_hash="x" * 32,
    )
    repo.create_workspace(slug="qa", name="QA",
                          owner_user_id=user["user_id"])
    return user["user_id"]


def test_cross_thread_writer_persists_after_rotation(tmp_path, monkeypatch):
    """A writer thread that warms up its Repo._local.conn before
    rotation MUST be able to write AFTER rotation, and those
    writes MUST be visible to a fresh NEW-key connection.

    This is the production invariant: API workers (e.g. the
    FastAPI sync handler pool) write from many threads, each
    with its own thread-local connection cache.  If the
    rotation function does not refresh those caches, the
    post-rotation state of the API process is broken.
    """
    db = str(tmp_path / "xthread.db")
    # monkeypatch: a bare os.environ write leaked the key into later
    # tests on the same xdist worker (plaintext-DB tests then refused).
    monkeypatch.setenv("VIDEO_FACTORY_DB_KEY", OLD_KEY)
    repo = Repo(db, db_key=OLD_KEY)
    key_rotation.ensure_table(repo)
    user_id = _seed(repo)

    writer_done = threading.Event()
    writer_markers: list[tuple[str, str]] = []  # (aid, status)

    def writer_loop() -> None:
        # Populate thread-local conn under OLD key.
        with repo.tx() as c:
            c.execute(
                "INSERT INTO audit_log(audit_id, event, "
                "context_json, created_at, user_id, workspace_id) "
                "VALUES (?,?,?,?,?,?)",
                (str(_uuid.uuid4()), "xthread.warmup", "{}",
                 "2026-10-07T00:00:00Z", user_id, ""),
            )
        writer_done.wait(timeout=120)
        for i in range(3):
            aid = str(_uuid.uuid4())
            try:
                with repo.tx() as c:
                    c.execute(
                        "INSERT INTO audit_log(audit_id, event, "
                        "context_json, created_at, user_id, "
                        "workspace_id) "
                        "VALUES (?,?,?,?,?,?)",
                        (aid, "xthread.post_rot",
                         f"{{\"i\":{i}}}",
                         "2026-10-07T00:00:01Z", user_id, ""),
                    )
                writer_markers.append((aid, "ok"))
            except key_rotation.RotationInProgress as e:
                writer_markers.append((aid, f"rip:{e!s}"))
            except Exception as e:  # noqa: BLE001
                writer_markers.append((aid, f"err:{e!r}"))

    wt = threading.Thread(target=writer_loop, daemon=True)
    wt.start()
    time.sleep(0.5)

    state = key_rotation.rotate(
        repo=repo, old_key=OLD_KEY, new_key=NEW_KEY,
        actor_user_id="xthread-qa", db_path=db,
    )
    assert state["completed"], f"rotation failed: {state}"

    writer_done.set()
    wt.join(timeout=10)

    fresh = cipher_db.connect(db, key=NEW_KEY)
    try:
        rows = fresh.execute(
            "SELECT audit_id FROM audit_log "
            "WHERE event='xthread.post_rot'"
        ).fetchall()
    finally:
        fresh.close()

    persisted = {r[0] for r in rows}
    succeeded = {aid for aid, st in writer_markers if st == "ok"}
    blocked = [st for _, st in writer_markers if st.startswith("rip:")]

    # The production invariant: every writer-thread write that
    # reported success MUST be visible to a fresh NEW-key reader.
    assert succeeded.issubset(persisted), (
        f"DATA LOSS: writer thread reported {len(succeeded)} "
        f"successful writes but NEW-key reader sees "
        f"{len(persisted)}.  Writer markers: {writer_markers}. "
        f"This is the cross-thread silent-corruption defect — "
        f"the writer's stale OLD-key connection encrypts pages "
        f"into the NEW-cipher file and they cannot be read back."
    )

    # And the writer thread should not have been permanently refused
    # by RotationInProgress.  Rotation is ~1s; after that the
    # gate must be off for all threads, not just the rotating one.
    assert not blocked, (
        f"Writer thread permanently blocked by RotationInProgress "
        f"after rotation completed: {blocked}.  The rotation "
        f"function cleared only the calling thread's "
        f"`_local.conn` cache; the writer thread's cached OLD-key "
        f"conn reads the stale pre-swap key_rotation row."
    )


def test_cross_thread_bypass_does_not_corrupt(tmp_path, monkeypatch):
    """If a writer thread bypasses tx() (using repo._conn() directly)
    and writes post-rotation, those writes MUST NOT corrupt the
    NEW-cipher file.  Acceptable outcomes:
      * the write raises an error, OR
      * the write lands in the NEW-cipher file (visible to a
        fresh NEW-key reader).

    NOT acceptable: silent write (no error raised) that does NOT
    appear in the NEW-cipher file (encrypted with OLD key).
    """
    db = str(tmp_path / "xthread_bypass.db")
    # monkeypatch: a bare os.environ write leaked the key into later
    # tests on the same xdist worker (plaintext-DB tests then refused).
    monkeypatch.setenv("VIDEO_FACTORY_DB_KEY", OLD_KEY)
    repo = Repo(db, db_key=OLD_KEY)
    key_rotation.ensure_table(repo)
    user_id = _seed(repo)

    writer_done = threading.Event()
    results: list[tuple[str, bool, str]] = []  # (aid, ok, err)

    def writer_loop() -> None:
        with repo.tx() as c:
            c.execute(
                "INSERT INTO audit_log(audit_id, event, "
                "context_json, created_at, user_id, workspace_id) "
                "VALUES (?,?,?,?,?,?)",
                (str(_uuid.uuid4()), "bypass.warmup", "{}",
                 "2026-10-07T00:00:00Z", user_id, ""),
            )
        writer_done.wait(timeout=120)
        for i in range(3):
            aid = str(_uuid.uuid4())
            try:
                c = repo._conn()
                c.execute(
                    "INSERT INTO audit_log(audit_id, event, "
                    "context_json, created_at, user_id, "
                    "workspace_id) "
                    "VALUES (?,?,?,?,?,?)",
                    (aid, "bypass.write", f"{{\"i\":{i}}}",
                     "2026-10-07T00:00:01Z", user_id, ""),
                )
                c.commit()
                results.append((aid, True, ""))
            except Exception as e:  # noqa: BLE001
                results.append((aid, False, repr(e)))

    wt = threading.Thread(target=writer_loop, daemon=True)
    wt.start()
    time.sleep(0.5)

    state = key_rotation.rotate(
        repo=repo, old_key=OLD_KEY, new_key=NEW_KEY,
        actor_user_id="bypass-qa", db_path=db,
    )
    assert state["completed"], f"rotation failed: {state}"

    writer_done.set()
    wt.join(timeout=10)

    fresh = cipher_db.connect(db, key=NEW_KEY)
    try:
        rows = fresh.execute(
            "SELECT audit_id FROM audit_log "
            "WHERE event='bypass.write'"
        ).fetchall()
    finally:
        fresh.close()

    persisted = {r[0] for r in rows}
    succeeded = {aid for aid, ok, _ in results if ok}

    assert succeeded.issubset(persisted), (
        f"SILENT CORRUPTION: writer thread's bypass writes "
        f"succeeded ({len(succeeded)} rows) but NEW-key reader "
        f"sees only {len(persisted)}.  Detail: {results}. "
        f"This is exactly the defect the Wave 5 follow-up was "
        f"meant to fix — the writer's thread-local conn is "
        f"stuck on the OLD key while the file on disk is the "
        f"NEW cipher.  Every page encrypted by the writer is "
        f"unreadable to the NEW-key reader."
    )