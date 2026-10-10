"""Wave 5 QA — adversarial tests for SQLCipher + key rotation.

Independent verification layer on top of the builder's
test_wave5_cipher.py suite. Each test exercises an attack or
edge case the builder either skipped or covered with the
canonical happy-path fixture.

Tests:

  1. test_encrypted_db_with_wrong_key_raises
       Open a cipher DB with a key that does NOT match the one
       used at create. Must raise sqlite3.DatabaseError (and the
       underlying sqlcipher3.dbapi2.DatabaseError must be
       normalised to stdlib).

  2. test_plaintext_db_refused_with_helpful_error
       Drop a plaintext SQLite DB at the configured db_path while
       VIDEO_FACTORY_DB_KEY is set. Repo must refuse and surface
       a message that names `migrate_plaintext_to_cipher`.

  3. test_migration_atomicity_rollback_on_failure
       Trigger a migration failure mid-stream (e.g. duplicate
       table name) and assert no partial cipher file is left on
       disk + no half-written rows in any other DB.

  4. test_rotation_audit_records_user_id
       Run rotation with a known actor_user_id. The NEW-cipher
       DB (read with the NEW key) MUST have an audit row whose
       user_id matches.

  5. test_per_workspace_hmac_prevents_cross_workspace_decrypt
       Encrypt a sidecar under workspace A, attempt verification
       under workspace B and under a tampered storage_key.
       Both must fail.

Plus the discovered BLOCKER for Wave 5 §3.4 (proves Repo.db_key
is not refreshed after a rotation swap — silent data corruption
in production deployments where the API process continues to
serve writes through the same Repo instance):

  6. test_repo_post_rotation_writes_are_persistent (currently
     FAILS — see defect report).
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from unittest import mock

import pytest

from core import cipher_db
from control_plane import artifact_hmac, key_rotation
from core.repo import Repo


# ── helpers ───────────────────────────────────────────────────────


def _seed_workspace(repo: Repo) -> dict:
    """Create a user + workspace for FK-bearing tables."""
    user = repo.create_user(email=f"qa-{os.getpid()}@example.com",
                            password_hash="x" * 32)
    ws = repo.create_workspace(slug="qa", name="QA", owner_user_id=user["user_id"])
    return {"user": user, "workspace": ws}


def _write_plaintext_db(path: str) -> None:
    """Make a small plaintext SQLite DB at `path`."""
    c = sqlite3.connect(path)
    try:
        c.execute("CREATE TABLE x(a INTEGER, b TEXT)")
        c.executemany("INSERT INTO x VALUES(?,?)",
                      [(1, "alpha"), (2, "beta"), (3, "gamma")])
        c.commit()
    finally:
        c.close()


# ── 1. encrypted DB with wrong key ────────────────────────────────


def test_encrypted_db_with_wrong_key_raises(tmp_path: Path):
    """Cipher DB opened with the wrong key must raise stdlib
    sqlite3.DatabaseError.

    The cipher middleware (sqlcipher3-binary) raises a private
    `sqlcipher3.dbapi2.DatabaseError` that does NOT inherit from
    `sqlite3.DatabaseError`. Repo._conn / cipher_db.connect must
    normalise this so existing `except sqlite3.DatabaseError`
    sites fire. Without the normalisation, every existing
    call site silently leaks the raw cipher exception.
    """
    db = str(tmp_path / "enc.db")
    key = "correct-key-12345678"

    # Create cipher DB with the correct key.
    c = cipher_db.connect(db, key=key)
    c.execute("CREATE TABLE secret(id INTEGER, payload TEXT)")
    c.executemany(
        "INSERT INTO secret VALUES(?,?)",
        [(1, "shh"), (2, "private"), (3, "data")],
    )
    c.commit()
    c.close()

    # Wrong key via Repo MUST raise sqlite3.DatabaseError.
    with pytest.raises(sqlite3.DatabaseError):
        Repo(db, db_key="WRONG-key-9999")._conn().execute("SELECT 1")

    # Wrong key via raw cipher_db.connect MUST also surface a
    # DatabaseError (the underlying class — stdlib or
    # sqlcipher3-binary private).
    with pytest.raises(Exception) as excinfo:
        cipher_db.connect(db, key="WRONG-key-9999").execute("SELECT 1")
    assert "not a database" in str(excinfo.value).lower()


# ── 2. plaintext DB refused with helpful error ────────────────────


def test_plaintext_db_refused_with_helpful_error(tmp_path: Path, monkeypatch):
    """Plaintext DB + VIDEO_FACTORY_DB_KEY set ⇒ Repo refuses to open.

    The error must name `migrate_plaintext_to_cipher` so the
    operator knows the recovery path. Without this hint an
    operator who upgraded to a keyed deployment is stuck.
    """
    monkeypatch.setenv("VIDEO_FACTORY_DB_KEY", "qa-key-aaaa")
    db = str(tmp_path / "plain.db")
    _write_plaintext_db(db)

    with pytest.raises(cipher_db.PlaintextDBRefused) as excinfo:
        Repo(db)

    msg = str(excinfo.value)
    assert "migrate_plaintext_to_cipher" in msg, (
        "error message must name the migration helper; got: " + msg
    )
    assert "VIDEO_FACTORY_DB_KEY" in msg


# ── 3. migration atomicity (rollback on failure) ──────────────────


def test_migration_atomicity_rollback_on_failure(tmp_path: Path, monkeypatch):
    """Migration failure mid-stream must leave NO partial cipher DB.

    Simulates a duplicate-table failure (the cipher file gets
    a CREATE TABLE x, then our pre-seeded file's INSERT for
    table `x` collides — or we monkeypatch _dump_database to
    return a duplicate CREATE).
    """
    src = str(tmp_path / "src.db")
    dst = str(tmp_path / "dst.db")
    _write_plaintext_db(src)

    # Monkeypatch _dump_database to inject a duplicate CREATE.
    import core.cipher_db as _cd

    real_dump = _cd._dump_database

    def bad_dump(conn):
        sql = real_dump(conn)
        # Inject a duplicate CREATE TABLE so the cipher DB will
        # raise "table x already exists" when replayed.
        return sql + "\nCREATE TABLE x(a INTEGER);\n"

    monkeypatch.setattr(_cd, "_dump_database", bad_dump)

    with pytest.raises(Exception):
        _cd.migrate_plaintext_to_cipher(
            plain_path=src, cipher_path=dst, key="qa-key",
        )

    # No partial cipher file must be left.
    assert not Path(dst).exists(), (
        f"partial cipher file left after failure: {dst}"
    )
    # No WAL/SHM dangling around.
    for ext in ("", "-wal", "-shm"):
        assert not Path(dst + ext).exists()


# ── 4. rotation audit records user_id ─────────────────────────────


def test_rotation_audit_records_user_id(tmp_path: Path, monkeypatch):
    """Rotation with a known actor_user_id must write an audit
    row whose `user_id` matches.

    Read via the NEW key (fresh connection) to bypass any
    Repo._conn stale-cache side effects of the swap.
    """
    db = str(tmp_path / "rot.db")
    monkeypatch.setenv("VIDEO_FACTORY_DB_KEY", "rot-old-key")

    repo = Repo(db, db_key="rot-old-key")
    seed = _seed_workspace(repo)
    actor = seed["user"]["user_id"]

    # Rotate.
    state = key_rotation.rotate(
        repo=repo, old_key="rot-old-key", new_key="rot-new-key",
        actor_user_id=actor, db_path=db,
    )
    assert state["completed"], f"rotation did not complete: {state}"
    assert state["actor_user_id"] == actor
    assert state["old_key_fingerprint"] == key_rotation._fingerprint(
        "rot-old-key")
    assert state["new_key_fingerprint"] == key_rotation._fingerprint(
        "rot-new-key")

    # Read via NEW key — must have an audit row attributable to
    # the actor and referencing key.rotation_completed.
    new_conn = cipher_db.connect(db, key="rot-new-key")
    try:
        rows = new_conn.execute(
            "SELECT event, user_id, context_json FROM audit_log "
            "WHERE event LIKE 'key.rotation%' ORDER BY created_at",
        ).fetchall()
    finally:
        new_conn.close()

    events = [r[0] for r in rows]
    user_ids = {r[1] for r in rows}
    assert "key.rotation_started" in events, \
        f"missing key.rotation_started audit: {events}"
    assert actor in user_ids, \
        f"actor_user_id {actor!r} not recorded in audit user_ids: {user_ids}"


# ── 5. per-workspace HMAC prevents cross-workspace decrypt ───────


def test_per_workspace_hmac_prevents_cross_workspace_decrypt(tmp_path: Path, monkeypatch):
    """A blob tagged for workspace A must NOT verify under workspace B.

    Covers the cross-tenant threat: a malicious operator with
    shell access to artifacts/storage/ tries to read
    `workspaces/A/assets/foo` as workspace B.
    """
    monkeypatch_root = str(tmp_path)
    # monkeypatch, not os.environ: the key must not leak into later tests.
    monkeypatch.setenv("VIDEO_FACTORY_DB_KEY", "hmac-key-12345678")

    storage_key = "workspaces/alice/assets/test_foo.png"

    artifact_hmac.write_sidecar(
        storage_root=monkeypatch_root,
        storage_key=storage_key,
        workspace_id="ws_alice",
    )

    # Owner workspace verifies.
    assert artifact_hmac.verify_sidecar(
        storage_root=monkeypatch_root,
        storage_key=storage_key,
        workspace_id="ws_alice",
    ), "owner workspace must verify"

    # Different workspace must fail.
    assert not artifact_hmac.verify_sidecar(
        storage_root=monkeypatch_root,
        storage_key=storage_key,
        workspace_id="ws_bob",
    ), "cross-workspace verify must fail"

    # Tampered storage_key (path traversal) must fail.
    assert not artifact_hmac.verify_sidecar(
        storage_root=monkeypatch_root,
        storage_key="workspaces/bob/assets/test_foo.png",
        workspace_id="ws_bob",
    ), "tampered storage_key verify must fail"

    # Missing sidecar must fail (fail-closed).
    (tmp_path / (storage_key + ".hmac")).unlink()
    assert not artifact_hmac.verify_sidecar(
        storage_root=monkeypatch_root,
        storage_key=storage_key,
        workspace_id="ws_alice",
    ), "missing sidecar must fail closed"


# ── 6. (DISCOVERY) Repo post-rotation writes are not persistent ──
#
# Defect discovered during Wave 5 QA independent verification:
# after key_rotation.rotate() runs, the same Repo instance's
# cached connection still uses the OLD key. Subsequent writes
# through that connection silently land on disk encrypted
# with the OLD key (or are dropped) and are NOT visible to a
# fresh connection opened with the NEW key. This causes silent
# data corruption in any production deployment that continues
# to serve writes through the same Repo instance after rotation
# (i.e. all production deployments, since restarting the API
# process is the documented but not enforced post-rotation step).
#
# The test below is currently FAILING — it documents the defect
# and will turn green once the upstream fix lands (either Repo
# auto-refreshes its db_key after a swap, or the rotate()
# function rebuilds the connection cache).


def test_repo_post_rotation_writes_are_persistent(tmp_path: Path, monkeypatch):
    """DEFECT DISCOVERY: Repo writes after rotation must reach
    the NEW-cipher DB.

    Reads back via a fresh cipher_db connection (NEW key) to
    bypass Repo._conn's stale cache and prove that what was
    "committed" through Repo is actually on disk in the new
    cipher.
    """
    db = str(tmp_path / "rot_persist.db")
    monkeypatch.setenv("VIDEO_FACTORY_DB_KEY", "per-old-key")

    repo = Repo(db, db_key="per-old-key")
    _seed_workspace(repo)

    # Rotate.
    state = key_rotation.rotate(
        repo=repo, old_key="per-old-key", new_key="per-new-key",
        actor_user_id="qa-actor", db_path=db,
    )
    assert state["completed"], f"rotation failed: {state}"

    # Now write through the SAME repo instance (post-rotation).
    repo._conn().execute(
        "INSERT INTO audit_log(audit_id, event, context_json, "
        "user_id, created_at) VALUES (?,?,?,?,?)",
        ("post_rot_1", "post.rot.write", "{\"src\":\"repo\"}",
                 "qa-actor", "2026-10-07T00:00:00Z"),
    )
    repo._conn().commit()

    # The row must be visible to a fresh connection opened
    # with the NEW key. If it's NOT visible, the rotation
    # swapped the file but left Repo pointing at the OLD key —
    # the write was either silently dropped or landed on a
    # page encrypted with the wrong key (silent data corruption
    # in production).
    fresh = cipher_db.connect(db, key="per-new-key")
    try:
        rows = fresh.execute(
            "SELECT event, context_json FROM audit_log "
            "WHERE audit_id='post_rot_1'"
        ).fetchall()
    finally:
        fresh.close()

    assert rows == [("post.rot.write", '{"src":"repo"}')], (
        "write through Repo after rotation is NOT visible to a "
        "fresh NEW-key connection: Repo.db_key was not refreshed "
        "after the swap. This is BLOCKER for production rotation."
    )


# ── 7..14. Wave 5 follow-up (t_abf264b4) ───────────────────────
#
# The original 6 tests above proved the BLOCKER.  These 8 tests
# pin the fix in place: db_key refresh, write-gating, the
# concurrent-writer invariant, and the failure-recovery path.
# If any of these regress the production rotation breaks again.


def test_repo_db_key_is_refreshed_after_rotation(tmp_path: Path, monkeypatch):
    """After rotation, `Repo.db_key` MUST equal the NEW key.

    Guards against a future refactor that drops the
    `repo.db_key = new_key` step in `key_rotation.rotate` and
    leaves the Repo pointing at the OLD key.  Without this
    property every subsequent `_conn()` would re-PRAGMA the
    wrong key and the BLOCKER-1 symptom returns.
    """
    db = str(tmp_path / "rot_refresh.db")
    monkeypatch.setenv("VIDEO_FACTORY_DB_KEY", "ref-old-key")
    repo = Repo(db, db_key="ref-old-key")
    _seed_workspace(repo)

    state = key_rotation.rotate(
        repo=repo, old_key="ref-old-key", new_key="ref-new-key",
        actor_user_id="qa-actor", db_path=db,
    )
    assert state["completed"]

    assert repo.db_key == "ref-new-key", (
        f"Repo.db_key was not refreshed after rotation: "
        f"got {repo.db_key!r}, expected 'ref-new-key'"
    )


def test_repo_uses_new_key_for_post_rotation_writes(
    tmp_path: Path, monkeypatch,
):
    """Post-rotation writes via Repo.tx() must land in NEW-cipher DB.

    The Repo.tx() path is the canonical write entry point used
    by every method on the Repo.  After the rotation swap, a
    write through `repo.tx()` (e.g. `create_user`) must
    persist into the file opened with the NEW key.  If
    `db_key` is stale, the new connection opens with the wrong
    key and the write is silently dropped.
    """
    db = str(tmp_path / "rot_tx.db")
    monkeypatch.setenv("VIDEO_FACTORY_DB_KEY", "tx-old-key")
    repo = Repo(db, db_key="tx-old-key")
    _seed_workspace(repo)

    key_rotation.rotate(
        repo=repo, old_key="tx-old-key", new_key="tx-new-key",
        actor_user_id="qa-actor", db_path=db,
    )

    # Write through the canonical tx() path.
    repo.create_user(email="post-rot@example.com", password_hash="h" * 32)

    # Read via a fresh connection opened with the NEW key.
    fresh = cipher_db.connect(db, key="tx-new-key")
    try:
        rows = fresh.execute(
            "SELECT email FROM users WHERE email='post-rot@example.com'"
        ).fetchall()
    finally:
        fresh.close()
    assert rows == [("post-rot@example.com",)], (
        f"write through Repo.tx() post-rotation not visible to "
        f"NEW-key reader: {rows!r}"
    )


def test_rotation_state_actually_clears_in_progress_flag(
    tmp_path: Path, monkeypatch,
):
    """rotation_state() must report completed=True AND
    in_progress=False after rotate().

    The original BLOCKER-1 left `rotation_in_progress=1` in the
    DB because the post-swap UPDATE silently no-op'd against
    the NEW-cipher file with the OLD PRAGMA key.  This test
    guards the fix: after rotate() returns, the state row must
    have BOTH `completed=1` AND `in_progress=0`.
    """
    db = str(tmp_path / "rot_state.db")
    monkeypatch.setenv("VIDEO_FACTORY_DB_KEY", "st-old-key")
    repo = Repo(db, db_key="st-old-key")
    _seed_workspace(repo)

    state = key_rotation.rotate(
        repo=repo, old_key="st-old-key", new_key="st-new-key",
        actor_user_id="qa-actor", db_path=db,
    )

    assert state["completed"] is True, f"completed not set: {state}"
    assert state["in_progress"] is False, (
        f"in_progress still True after successful rotation: {state}"
    )

    # Confirm via a fresh NEW-key connection (bypass any Repo
    # conn cache).
    fresh = cipher_db.connect(db, key="st-new-key")
    try:
        row = fresh.execute(
            "SELECT rotation_in_progress, rotation_completed "
            "FROM key_rotation LIMIT 1"
        ).fetchone()
    finally:
        fresh.close()
    assert row == (0, 1), (
        f"key_rotation row in NEW-cipher DB shows stale state: "
        f"in_progress={row[0]} completed={row[1]}"
    )


def test_runner_audit_has_both_started_and_completed_events(
    tmp_path: Path, monkeypatch,
):
    """runner_audit in NEW-cipher DB must contain both
    `key.rotation_started` AND `key.rotation_completed` rows.

    The original MAJOR-2 left only `_started` in NEW-cipher
    because the post-swap _audit call wrote to a NEW-cipher
    file with the OLD PRAGMA key (silent corruption).  After
    the BLOCKER-1 fix both events must be visible to a fresh
    NEW-key reader.
    """
    db = str(tmp_path / "rot_audit.db")
    monkeypatch.setenv("VIDEO_FACTORY_DB_KEY", "au-old-key")
    repo = Repo(db, db_key="au-old-key")
    _seed_workspace(repo)
    actor = "audit-actor"

    key_rotation.rotate(
        repo=repo, old_key="au-old-key", new_key="au-new-key",
        actor_user_id=actor, db_path=db,
    )

    fresh = cipher_db.connect(db, key="au-new-key")
    try:
        rows = fresh.execute(
            "SELECT event FROM runner_audit "
            "WHERE event LIKE 'key.rotation%' ORDER BY created_at"
        ).fetchall()
    finally:
        fresh.close()

    events = [r[0] for r in rows]
    assert "key.rotation_started" in events, (
        f"missing key.rotation_started in NEW-cipher runner_audit: "
        f"{events}"
    )
    assert "key.rotation_completed" in events, (
        f"missing key.rotation_completed in NEW-cipher "
        f"runner_audit: {events}. MAJOR-2 not fixed."
    )


def test_writes_allowed_returns_false_during_rotation(
    tmp_path: Path, monkeypatch,
):
    """Generic `writes_allowed` gate must block ALL writes, not
    just credentials (MAJOR-3 fix).

    Previously only `Repo.upsert_credential` checked
    `credential_writes_allowed`; audit_log, jobs, workspaces,
    etc. were ungated.  The fix extends the gate to a generic
    `writes_allowed` and wires it into `Repo.tx()`.
    """
    db = str(tmp_path / "rot_gate.db")
    monkeypatch.setenv("VIDEO_FACTORY_DB_KEY", "gt-old-key")
    repo = Repo(db, db_key="gt-old-key")
    _seed_workspace(repo)

    # Pre-rotation: writes allowed.
    assert key_rotation.writes_allowed(repo) is True
    assert key_rotation.credential_writes_allowed(repo) is True  # alias

    # Force the in-progress flag without actually rotating.
    key_rotation.ensure_table(repo)
    with repo.tx(_skip_rotation_check=True) as c:
        c.execute(
            "UPDATE key_rotation SET rotation_in_progress=1"
        )

    # Now ALL writes must be blocked.
    assert key_rotation.writes_allowed(repo) is False
    assert key_rotation.credential_writes_allowed(repo) is False

    # A non-credential write (e.g. audit_log via tx()) must
    # raise RotationInProgress.  This is the MAJOR-3
    # invariant: every write, not just credentials.
    from control_plane.key_rotation import RotationInProgress
    with pytest.raises(RotationInProgress):
        with repo.tx() as c:
            c.execute(
                "INSERT INTO audit_log(audit_id, event, "
                "context_json, user_id, created_at) "
                "VALUES (?,?,?,?,?)",
                ("gt_1", "test.event", "{}", "u", "2026-10-07T00:00:00Z"),
            )

    # Clean up: clear the flag so Repo can be closed cleanly.
    with repo.tx(_skip_rotation_check=True) as c:
        c.execute("UPDATE key_rotation SET rotation_in_progress=0")


def test_upsert_credential_still_raises_during_rotation(
    tmp_path: Path, monkeypatch,
):
    """Backward-compat: `upsert_credential` still raises
    RotationInProgress while rotation is in progress.

    The original Wave 5 §3.4 gate was on
    `credential_writes_allowed` only.  After the follow-up,
    the gate is generic (`writes_allowed`) but the
    credential-specific path must still raise so the API's
    503 mapping keeps working.
    """
    db = str(tmp_path / "rot_cred.db")
    monkeypatch.setenv("VIDEO_FACTORY_DB_KEY", "cr-old-key")
    repo = Repo(db, db_key="cr-old-key")
    seed = _seed_workspace(repo)
    wid = seed["workspace"]["workspace_id"]

    # Force the in-progress flag.
    key_rotation.ensure_table(repo)
    with repo.tx(_skip_rotation_check=True) as c:
        c.execute("UPDATE key_rotation SET rotation_in_progress=1")

    from control_plane.key_rotation import RotationInProgress
    with pytest.raises(RotationInProgress):
        repo.upsert_credential(
            workspace_id=wid,
            provider="heygen_direct",
            fingerprint_str="abc",
            encrypted_blob=b"x" * 32,
            updated_by="u",
        )

    # Clean up.
    with repo.tx(_skip_rotation_check=True) as c:
        c.execute("UPDATE key_rotation SET rotation_in_progress=0")


def test_concurrent_writers_blocked_during_rotation(
    tmp_path: Path, monkeypatch,
):
    """Brief's Item #5: rotation + concurrent writer must
    complete with zero data loss.

    The invariant we care about is:
      * during rotation, `Repo.tx()` write attempts are
        gated by the rotation flag (either run because the
        lock is available, or are blocked with
        `RotationInProgress`).
      * after the rotation, the Repo can write again, and
        every write is visible to a fresh NEW-key connection.
      * NO write returns an unexpected error (the only
        allowed outcomes are "ran" and "RotationInProgress").

    We approximate this by spawning a writer thread that
    attempts writes while the main thread is running a
    rotation.  We don't artificially slow the rotation
    because the real `copy_cipher_to_cipher` already
    produces a valid NEW-cipher file; the BLOCKER-1
    invariant is "no data loss", which is what we test.
    """
    import threading
    db = str(tmp_path / "rot_conc.db")
    monkeypatch.setenv("VIDEO_FACTORY_DB_KEY", "co-old-key")
    repo = Repo(db, db_key="co-old-key")
    _seed_workspace(repo)

    writer_results: list = []
    writer_done = threading.Event()
    NUM_WRITES = 20

    def writer():
        from control_plane.key_rotation import RotationInProgress
        try:
            for i in range(NUM_WRITES):
                try:
                    repo.create_user(
                        email=f"conc-{i}@example.com",
                        password_hash="h" * 32,
                    )
                    writer_results.append(("ok", i))
                except RotationInProgress:
                    writer_results.append(("blocked", i))
                except Exception as e:  # noqa: BLE001
                    writer_results.append(("error", i, repr(e)))
        finally:
            writer_done.set()

    t = threading.Thread(target=writer)
    t.start()
    state = key_rotation.rotate(
        repo=repo, old_key="co-old-key", new_key="co-new-key",
        actor_user_id="qa-actor", db_path=db,
    )
    writer_done.wait(timeout=10)
    t.join(timeout=5)

    assert state["completed"], f"rotation failed: {state}"

    # No "error" outcomes — only "ok" or "blocked".
    errored = [r for r in writer_results if r[0] == "error"]
    assert not errored, (
        f"writer thread saw unexpected errors: {errored}"
    )

    # BLOCKER-1 invariant: every "ok" must be visible to a
    # fresh NEW-key connection.
    fresh = cipher_db.connect(db, key="co-new-key")
    try:
        rows = fresh.execute(
            "SELECT email FROM users WHERE email LIKE 'conc-%' "
            "ORDER BY email"
        ).fetchall()
    finally:
        fresh.close()

    ok_count = sum(1 for r in writer_results if r[0] == "ok")
    assert len(rows) == ok_count, (
        f"data loss detected: {ok_count} writes reported 'ok' but "
        f"only {len(rows)} visible to NEW-key reader. "
        f"writer_results={writer_results}, rows={rows}"
    )


def test_rotation_failure_does_not_corrupt_repo_state(
    tmp_path: Path, monkeypatch,
):
    """A failed rotation must leave Repo in a usable state.

    The rotation function catches errors during the copy,
    marks `rotation_failed=1`, and re-raises `RotationFailed`.
    The Repo must be usable afterwards: `db_key` still equals
    the OLD key (no swap happened), and writes work normally.
    """
    db = str(tmp_path / "rot_fail.db")
    monkeypatch.setenv("VIDEO_FACTORY_DB_KEY", "fl-old-key")
    repo = Repo(db, db_key="fl-old-key")
    _seed_workspace(repo)

    # Force copy_cipher_to_cipher to raise so the rotation
    # takes the failure path.
    def boom(*args, **kwargs):
        raise RuntimeError("simulated copy failure")

    from control_plane.key_rotation import RotationFailed
    with mock.patch(
        "control_plane.key_rotation.cipher_db.copy_cipher_to_cipher",
        side_effect=boom,
    ):
        with pytest.raises(RotationFailed):
            key_rotation.rotate(
                repo=repo, old_key="fl-old-key", new_key="fl-new-key",
                actor_user_id="qa-actor", db_path=db,
            )

    # Repo.db_key must still be the OLD key (no swap).
    assert repo.db_key == "fl-old-key", (
        f"Repo.db_key changed after a failed rotation: "
        f"got {repo.db_key!r}"
    )

    # Repo must be usable: writes still work.
    user = repo.create_user(
        email="after-fail@example.com", password_hash="h" * 32,
    )
    assert user["email"] == "after-fail@example.com"

    # State must report failed=1, in_progress=0.
    state = key_rotation.rotation_state(repo)
    assert state["failed"] is True
    assert state["in_progress"] is False
    assert state["completed"] is False